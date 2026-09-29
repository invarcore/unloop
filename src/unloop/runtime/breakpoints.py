"""Execution breakpoint management and conditional triggers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from unloop.protocol.models import TurnSnapshot


@dataclass
class Breakpoint:
    """Represents an execution breakpoint trigger rule."""

    id: str
    on_error: bool = False
    tool_name: str | None = None
    turn_index: int | None = None
    condition: Callable[[dict[str, Any]], bool] | None = None
    condition_repr: str | None = None
    on_watchdog: bool = True
    enabled: bool = True


class BreakpointManager:
    """Evaluates whether an active turn satisfies any registered breakpoint."""

    def __init__(self) -> None:
        self._breakpoints: dict[str, Breakpoint] = {}
        self._counter: int = 0

    def add(
        self,
        on_error: bool = False,
        tool_name: str | None = None,
        turn_index: int | None = None,
        condition: Callable[[dict[str, Any]], bool] | None = None,
        condition_repr: str | None = None,
        on_watchdog: bool = True,
    ) -> str:
        """Register a new breakpoint. Returns the breakpoint ID."""
        self._counter += 1
        bp_id = f"bp_{self._counter}"
        self._breakpoints[bp_id] = Breakpoint(
            id=bp_id,
            on_error=on_error,
            tool_name=tool_name,
            turn_index=turn_index,
            condition=condition,
            condition_repr=condition_repr or ("<custom lambda>" if condition else None),
            on_watchdog=on_watchdog,
        )
        return bp_id

    def remove(self, bp_id: str) -> bool:
        """Remove a breakpoint by ID."""
        return self._breakpoints.pop(bp_id, None) is not None

    def list_breakpoints(self) -> list[Breakpoint]:
        """List all active breakpoints."""
        return list(self._breakpoints.values())

    def check(self, turn: TurnSnapshot, watchdog_tripped: bool = False) -> tuple[bool, str | None]:
        """Evaluate if any enabled breakpoint is triggered by this turn."""
        for bp in self._breakpoints.values():
            if not bp.enabled:
                continue

            # 1. Watchdog alert trigger
            if bp.on_watchdog and watchdog_tripped:
                return True, f"[{bp.id}] Watchdog loop/oscillation alert triggered"

            # 2. Turn index trigger
            if bp.turn_index is not None and turn.turn_index == bp.turn_index:
                return True, f"[{bp.id}] Reached target turn index {bp.turn_index}"

            # 3. Tool name trigger
            if bp.tool_name:
                for tool in turn.tool_invocations:
                    if tool.tool_name == bp.tool_name:
                        return True, f"[{bp.id}] Tool breakpoint hit for '{bp.tool_name}'"

            # 4. Error trigger
            if bp.on_error:
                for tool in turn.tool_invocations:
                    if tool.error is not None:
                        return True, f"[{bp.id}] Tool error encountered in '{tool.tool_name}': {tool.error}"

            # 5. Custom state condition trigger
            if bp.condition:
                try:
                    if bp.condition(turn.state):
                        return True, f"[{bp.id}] Condition satisfied: {bp.condition_repr}"
                except Exception:
                    # Condition evaluation failed safely
                    pass

        return False, None
