"""Execution interceptor and tracing context manager for unloop."""

from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from unloop.protocol.models import (
    SessionMetadata,
    TokenTelemetry,
    ToolInvocationRecord,
    TurnSnapshot,
)
from unloop.runtime.breakpoints import BreakpointManager
from unloop.runtime.watchdog import OscillationAlert, OscillationWatchdog
from unloop.storage.db import UnloopStore


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
        prompt: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
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
        arguments: Dict[str, Any],
        result: Optional[Any] = None,
        error: Optional[str] = None,
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

    def __enter__(self) -> "TurnContext":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type and not self.snapshot.tool_invocations:
            # An unhandled exception occurred in the turn
            self.snapshot.metadata["error"] = str(exc_val)

        self.session._finalize_turn(self.snapshot)


class UnloopSession:
    """Manages an active agent debugging session with recording and breakpoint controls."""

    def __init__(
        self,
        name: str = "agent_run",
        db_path: str | Path = "session.unloop",
        framework: str = "custom",
        model_name: Optional[str] = None,
        interactive: bool = False,
        raise_on_breakpoint: bool = False,
    ):
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

        self._current_turn: Optional[TurnSnapshot] = None
        self._turn_counter: int = 0
        self._last_state: Dict[str, Any] = {}
        self._interactive_handler: Optional[Callable[[TurnSnapshot, str], None]] = None

    @property
    def session_id(self) -> str:
        return self.metadata.session_id

    @property
    def active_branch(self) -> str:
        return self.metadata.active_branch

    @property
    def current_turn_id(self) -> Optional[str]:
        return self._current_turn.turn_id if self._current_turn else None

    def next_turn_index(self) -> int:
        idx = self._turn_counter
        self._turn_counter += 1
        return idx

    def add_breakpoint(self, **kwargs) -> str:
        """Add an execution breakpoint."""
        return self.breakpoints.add(**kwargs)

    def set_interactive_handler(self, handler: Callable[[TurnSnapshot, str], None]) -> None:
        """Register custom callback for interactive breakpoint handling (e.g., TUI or REPL)."""
        self._interactive_handler = handler

    def step(
        self,
        prompt: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> TurnContext:
        """Context manager to trace a single cognitive turn."""
        return TurnContext(self, prompt=prompt, state=state, messages=messages)

    def _finalize_turn(self, turn: TurnSnapshot) -> None:
        """Compute delta, run watchdog and breakpoints, and persist to SQLite."""
        # 1. Compute delta against previous turn's state
        turn.compute_state_delta(self._last_state)
        self._last_state = copy.deepcopy(turn.state)

        # 2. Evaluate watchdog
        alert: Optional[OscillationAlert] = self.watchdog.record_turn(turn)
        watchdog_tripped = alert is not None

        # 3. Evaluate breakpoints
        should_break, reason = self.breakpoints.check(turn, watchdog_tripped=watchdog_tripped)
        if should_break:
            turn.is_breakpoint = True
            turn.breakpoint_reason = reason
        elif watchdog_tripped and alert:
            turn.is_breakpoint = True
            turn.breakpoint_reason = alert.message

        # 4. Persist to SQLite WAL
        self.store.save_turn(turn)
        self._current_turn = turn

        # 5. Handle breakpoint trigger
        if turn.is_breakpoint:
            if self.interactive and self._interactive_handler:
                self._interactive_handler(turn, turn.breakpoint_reason or "Breakpoint hit")
            elif self.raise_on_breakpoint:
                raise BreakpointHalt(turn.breakpoint_reason or "Unknown breakpoint", turn)

    def rewind_to(self, turn_id: str, new_branch_name: Optional[str] = None) -> TurnSnapshot:
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

    def __enter__(self) -> "UnloopSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# Backwards compatibility alias
AgdbSession = UnloopSession
