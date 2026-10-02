"""Runtime execution interceptor, turn context manager, and tracing decorator."""

from __future__ import annotations

import copy
import functools
import inspect
import os
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from unloop.protocol.models import (
    SessionMetadata,
    TokenTelemetry,
    ToolInvocationRecord,
    TurnSnapshot,
)
from unloop.runtime.breakpoints import BreakpointManager
from unloop.runtime.watchdog import OscillationAlert, OscillationWatchdog
from unloop.storage.db import UnloopStore

_CURRENT_SESSION: ContextVar[UnloopSession | None] = ContextVar("_CURRENT_SESSION", default=None)


def get_current_session() -> UnloopSession | None:
    """Retrieve the currently active unloop session in the active context."""
    return _CURRENT_SESSION.get()


def set_current_session(sess: UnloopSession | None) -> None:
    """Set the active unloop session in the current context."""
    _CURRENT_SESSION.set(sess)


class BreakpointHalt(Exception):
    """Raised when an execution breakpoint halts execution in headless CI mode."""

    def __init__(self, reason: str, turn: TurnSnapshot):
        super().__init__(f"Execution halted by unloop breakpoint: {reason}")
        self.reason = reason
        self.turn = turn


class TurnContext:
    """Context manager for an individual cognitive turn."""

    def __init__(
        self,
        session: UnloopSession,
        prompt: str | None = None,
        state: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ):
        self.session = session
        self.turn_index = session.next_turn_index()
        self.parent_id = session.current_turn_id
        self.branch_id = session.active_branch

        self.snapshot = TurnSnapshot(
            session_id=session.session_id,
            parent_id=self.parent_id,
            branch_id=self.branch_id,
            turn_index=self.turn_index,
            prompt=prompt,
            messages=copy.deepcopy(messages or []),
            state=copy.deepcopy(state or {}),
        )

    def record_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any | None = None,
        error: str | None = None,
        duration_ms: float = 0.0,
    ) -> ToolInvocationRecord:
        """Record a tool execution within this turn."""
        rec = ToolInvocationRecord(
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            error=error,
            duration_ms=duration_ms,
        )
        self.snapshot.tool_invocations.append(rec)
        return rec

    def set_response(self, response: str) -> None:
        """Set the assistant reasoning or completion content."""
        self.snapshot.response = response

    def set_telemetry(self, prompt_tokens: int, completion_tokens: int, cost_usd: float = 0.0) -> None:
        """Record token usage metrics."""
        self.snapshot.telemetry = TokenTelemetry(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            estimated_cost_usd=cost_usd,
        )

    def mutate_state(self, key: str, value: Any) -> None:
        """Mutate a state variable within this turn."""
        self.snapshot.state[key] = value

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type and not self.snapshot.tool_invocations:
            # An unhandled exception occurred in the turn
            self.snapshot.metadata["error"] = str(exc_val)

        self.session._finalize_turn(self.snapshot)


class UnloopSession:
    """Manages an active agent debugging session with recording and breakpoint controls."""

    def __init__(
        self,
        name: str = "agent_run",
        db_path: str | Path | None = None,
        framework: str = "custom",
        model_name: str | None = None,
        interactive: bool = False,
        raise_on_breakpoint: bool = False,
    ):
        if db_path is None:
            db_path = os.environ.get("UNLOOP_SESSION_FILE", "session.unloop")

        self.db_path = Path(db_path)
        self.store = UnloopStore(self.db_path)
        self.metadata = SessionMetadata(
            name=name,
            framework=framework,
            model_name=model_name,
            active_branch="main",
        )
        self.store.create_session(self.metadata)

        self.watchdog = OscillationWatchdog()
        self.breakpoints = BreakpointManager()
        self.interactive = interactive
        self.raise_on_breakpoint = raise_on_breakpoint

        self._current_turn: TurnSnapshot | None = None
        self._turn_counter: int = 0
        self._last_state: dict[str, Any] = {}
        self._interactive_handler: Callable[[TurnSnapshot, str], None] | None = None
        self._context_token: Any = None

    @property
    def session_id(self) -> str:
        return self.metadata.session_id

    @property
    def active_branch(self) -> str:
        return self.metadata.active_branch

    @property
    def current_turn_id(self) -> str | None:
        return self._current_turn.turn_id if self._current_turn else None

    def next_turn_index(self) -> int:
        idx = self._turn_counter
        self._turn_counter += 1
        return idx

    def add_breakpoint(self, **kwargs: Any) -> str:
        """Add an execution breakpoint."""
        return self.breakpoints.add(**kwargs)

    def set_interactive_handler(self, handler: Callable[[TurnSnapshot, str], None]) -> None:
        """Register custom callback for interactive breakpoint handling (e.g., TUI or REPL)."""
        self._interactive_handler = handler

    def step(
        self,
        prompt: str | None = None,
        state: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> TurnContext:
        """Context manager to trace a single cognitive turn."""
        return TurnContext(self, prompt=prompt, state=state, messages=messages)

    def _finalize_turn(self, turn: TurnSnapshot) -> None:
        """Compute delta, run watchdog and breakpoints, and persist to SQLite."""
        # 1. Compute delta against previous turn's state
        turn.compute_state_delta(self._last_state)
        self._last_state = copy.deepcopy(turn.state)

        # 2. Evaluate watchdog
        alert: OscillationAlert | None = self.watchdog.record_turn(turn)
        watchdog_tripped = alert is not None

        # 3. Evaluate breakpoints
        should_break, reason = self.breakpoints.check(turn, watchdog_tripped=watchdog_tripped)
        if should_break:
            turn.is_breakpoint = True
            turn.breakpoint_reason = reason
        elif watchdog_tripped and alert:
            turn.is_breakpoint = True
            turn.breakpoint_reason = f"[Watchdog: {alert.alert_type}] {alert.message}"

        # 4. Persist to SQLite WAL
        self.store.save_turn(turn)
        self._current_turn = turn

        # 5. Handle breakpoint trigger
        if turn.is_breakpoint:
            if self.interactive and self._interactive_handler:
                self._interactive_handler(turn, turn.breakpoint_reason or "Breakpoint hit")
            elif self.raise_on_breakpoint:
                raise BreakpointHalt(turn.breakpoint_reason or "Unknown breakpoint", turn)

    def rewind_to(self, turn_id: str, new_branch_name: str | None = None) -> TurnSnapshot:
        """Rewind the session to a prior turn snapshot, restoring its state for forked execution."""
        target_turn = self.store.get_turn(turn_id)
        if not target_turn:
            raise ValueError(f"Turn {turn_id} not found in store.")

        # Fork new branch if branching from past turn
        if not new_branch_name:
            new_branch_name = f"fork_{turn_id[:6]}"

        self.store.fork_branch(
            session_id=self.session_id,
            origin_turn_id=turn_id,
            new_branch_id=new_branch_name,
            description=f"Rewound to turn #{target_turn.turn_index}",
        )

        self.metadata.active_branch = new_branch_name
        self._current_turn = target_turn
        self._turn_counter = target_turn.turn_index + 1
        self._last_state = copy.deepcopy(target_turn.state)
        self.watchdog.reset()
        return target_turn

    def close(self) -> None:
        """Close SQLite session."""
        self.store.close()

    def __enter__(self) -> Self:
        self._context_token = _CURRENT_SESSION.set(self)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._context_token is not None:
            _CURRENT_SESSION.reset(self._context_token)
            self._context_token = None
        self.close()


def trace(
    name: str | None = None,
    session: UnloopSession | None = None,
    prompt_arg: str = "prompt",
    state_arg: str = "state",
) -> Callable:
    """Decorator to trace an agent cognitive function or turn with unloop.

    Automatically records inputs (prompt, state), execution duration, return values,
    and any raised errors into an unloop session.
    """

    def decorator(func: Callable) -> Callable:
        sig = inspect.signature(func)

        def _extract_prompt_state(args: tuple, kwargs: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
            try:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                p = bound.arguments.get(prompt_arg)
                if p is None and len(args) > 0 and isinstance(args[0], str):
                    p = args[0]
                s = bound.arguments.get(state_arg)
                if s is None and len(args) > 1 and isinstance(args[1], dict):
                    s = args[1]
                elif s is None:
                    for k, v in bound.arguments.items():
                        if k != prompt_arg and isinstance(v, dict):
                            s = v
                            break
                return (str(p) if p is not None else None, s if isinstance(s, dict) else {})
            except Exception:
                return (None, {})

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                sess = session or get_current_session()
                owns_session = False
                if sess is None:
                    sess = UnloopSession(name=name or func.__name__)
                    owns_session = True

                p, s = _extract_prompt_state(args, kwargs)
                try:
                    with sess.step(prompt=p, state=s) as ctx:
                        start_t = time.perf_counter()
                        try:
                            res = await func(*args, **kwargs)
                            dur = (time.perf_counter() - start_t) * 1000.0
                            if isinstance(res, str):
                                ctx.set_response(res)
                            elif res is not None:
                                ctx.set_response(str(res))
                                ctx.snapshot.metadata["return_value"] = (
                                    res if isinstance(res, (dict, list, int, float, bool)) else str(res)
                                )
                            ctx.snapshot.metadata["duration_ms"] = dur
                            ctx.snapshot.metadata["traced_func"] = func.__name__
                            return res
                        except Exception as e:
                            dur = (time.perf_counter() - start_t) * 1000.0
                            ctx.snapshot.metadata["duration_ms"] = dur
                            ctx.snapshot.metadata["traced_func"] = func.__name__
                            ctx.snapshot.metadata["error"] = str(e)
                            raise
                finally:
                    if owns_session:
                        sess.close()

            return async_wrapper
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                sess = session or get_current_session()
                owns_session = False
                if sess is None:
                    sess = UnloopSession(name=name or func.__name__)
                    owns_session = True

                p, s = _extract_prompt_state(args, kwargs)
                try:
                    with sess.step(prompt=p, state=s) as ctx:
                        start_t = time.perf_counter()
                        try:
                            res = func(*args, **kwargs)
                            dur = (time.perf_counter() - start_t) * 1000.0
                            if isinstance(res, str):
                                ctx.set_response(res)
                            elif res is not None:
                                ctx.set_response(str(res))
                                ctx.snapshot.metadata["return_value"] = (
                                    res if isinstance(res, (dict, list, int, float, bool)) else str(res)
                                )
                            ctx.snapshot.metadata["duration_ms"] = dur
                            ctx.snapshot.metadata["traced_func"] = func.__name__
                            return res
                        except Exception as e:
                            dur = (time.perf_counter() - start_t) * 1000.0
                            ctx.snapshot.metadata["duration_ms"] = dur
                            ctx.snapshot.metadata["traced_func"] = func.__name__
                            ctx.snapshot.metadata["error"] = str(e)
                            raise
                finally:
                    if owns_session:
                        sess.close()

            return sync_wrapper

    return decorator


# Backwards compatibility alias
AgdbSession = UnloopSession
agdb_trace = trace
