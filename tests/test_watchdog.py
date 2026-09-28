"""Tests for anti-oscillation watchdog."""

from agdb.protocol.models import ToolInvocationRecord, TurnSnapshot
from agdb.runtime.watchdog import OscillationWatchdog


def test_watchdog_detects_repetitive_tool():
    watchdog = OscillationWatchdog(max_consecutive_tools=3)

    t1 = ToolInvocationRecord(tool_name="web_search", arguments={"q": "fix bug"})
    t2 = ToolInvocationRecord(tool_name="web_search", arguments={"q": "fix bug"})
    t3 = ToolInvocationRecord(tool_name="web_search", arguments={"q": "fix bug"})

    assert watchdog.record_tool_call(t1) is None
    assert watchdog.record_tool_call(t2) is None
    alert = watchdog.record_tool_call(t3)

    assert alert is not None
    assert alert.alert_type == "REPETITIVE_TOOL"
    assert alert.tool_name == "web_search"


def test_watchdog_detects_ping_pong():
    watchdog = OscillationWatchdog(window_size=8)

    t_read = ToolInvocationRecord(tool_name="read_file", arguments={"path": "a.txt"})
    t_edit = ToolInvocationRecord(tool_name="edit_file", arguments={"path": "a.txt"})

    # Sequence: Read -> Edit -> Read -> Edit
    assert watchdog.record_tool_call(t_read) is None
    assert watchdog.record_tool_call(t_edit) is None
    assert watchdog.record_tool_call(t_read) is None
    alert = watchdog.record_tool_call(t_edit)

    assert alert is not None
    assert alert.alert_type == "PING_PONG_OSCILLATION"


def test_watchdog_detects_state_cycle():
    watchdog = OscillationWatchdog(window_size=6)

    turn1 = TurnSnapshot(session_id="s1", state={"status": "init", "retry": 0})
    turn2 = TurnSnapshot(session_id="s1", state={"status": "processing", "retry": 1})
    turn3 = TurnSnapshot(session_id="s1", state={"status": "init", "retry": 0})  # cycled back!

    assert watchdog.record_turn(turn1) is None
    assert watchdog.record_turn(turn2) is None
    alert = watchdog.record_turn(turn3)

    assert alert is not None
    assert alert.alert_type == "STATE_CYCLE"
