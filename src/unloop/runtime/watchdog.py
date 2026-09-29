"""Anti-oscillation and loop detection watchdog for AI agents.

Monitors sliding windows of tool invocations and state checksums to detect
repetition death loops and state ping-pongs before token budgets are exhausted.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from unloop.protocol.models import ToolInvocationRecord, TurnSnapshot


@dataclass
class OscillationAlert:
    """Alert raised when an oscillation or runaway loop pattern is detected."""

    alert_type: str  # "REPETITIVE_TOOL", "PING_PONG_OSCILLATION", "STATE_CYCLE"
    message: str
    tool_name: str | None = None
    argument_hash: str | None = None
    frequency: int = 0


class OscillationWatchdog:
    """Monitors agent turns in real time and detects cognitive loops and tool oscillations."""

    def __init__(
        self,
        max_consecutive_tools: int = 3,
        window_size: int = 8,
        detect_state_cycles: bool = True,
    ) -> None:
        self.max_consecutive_tools = max_consecutive_tools
        self.window_size = window_size
        self.detect_state_cycles = detect_state_cycles

        self._tool_history: deque[tuple[str, str]] = deque(maxlen=window_size)  # (tool_name, arg_hash)
        self._state_hashes: deque[str] = deque(maxlen=window_size)

    def record_turn(self, turn: TurnSnapshot) -> OscillationAlert | None:
        """Record a turn and check for loop patterns. Returns OscillationAlert if triggered."""
        # 1. Check state cycle
        if self.detect_state_cycles and turn.state:
            curr_hash = turn.state_hash
            if curr_hash in self._state_hashes:
                # Same state returned within window!
                return OscillationAlert(
                    alert_type="STATE_CYCLE",
                    message=f"State oscillation detected: Agent reverted to previous state hash {curr_hash}",
                    frequency=self._state_hashes.count(curr_hash) + 1,
                )
            self._state_hashes.append(curr_hash)

        # 2. Check tool invocations
        for tool in turn.tool_invocations:
            alert = self.record_tool_call(tool)
            if alert:
                return alert

        return None

    def record_tool_call(self, tool: ToolInvocationRecord) -> OscillationAlert | None:
        """Check a single tool call for repetitive execution or ping-pong patterns."""
        entry = (tool.tool_name, tool.argument_hash)
        self._tool_history.append(entry)

        # A. Check identical consecutive tool calls
        if len(self._tool_history) >= self.max_consecutive_tools:
            tail = list(self._tool_history)[-self.max_consecutive_tools:]
            if all(item == entry for item in tail):
                return OscillationAlert(
                    alert_type="REPETITIVE_TOOL",
                    message=(
                        f"Critique death loop: Tool '{tool.tool_name}' invoked "
                        f"{self.max_consecutive_tools} times with identical arguments"
                    ),
                    tool_name=tool.tool_name,
                    argument_hash=tool.argument_hash,
                    frequency=self.max_consecutive_tools,
                )

        # B. Check ping-pong oscillation (A -> B -> A -> B)
        if len(self._tool_history) >= 4:
            h = list(self._tool_history)
            if h[-1] == h[-3] and h[-2] == h[-4] and h[-1] != h[-2]:
                return OscillationAlert(
                    alert_type="PING_PONG_OSCILLATION",
                    message=(
                        f"Ping-pong oscillation: Agent alternating between '{h[-2][0]}' "
                        f"and '{h[-1][0]}'"
                    ),
                    tool_name=tool.tool_name,
                    argument_hash=tool.argument_hash,
                    frequency=2,
                )

        return None

    def reset(self) -> None:
        """Clear watchdog history."""
        self._tool_history.clear()
        self._state_hashes.clear()
