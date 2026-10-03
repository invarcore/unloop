"""Integration tests for real-world SWE-bench developer trajectory playback and watchdog.

Tests oscillation detection, state cycle recognition, and time-travel rewind
on realistic multi-turn agent debugging sessions derived from real SWE-bench
developer trajectories.
"""

from __future__ import annotations

import json
from pathlib import Path

from unloop.protocol.models import ToolInvocationRecord, TurnSnapshot
from unloop.runtime.interceptor import UnloopSession
from unloop.runtime.watchdog import OscillationWatchdog

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "corpora"


def test_swebench_trajectory_fixture_integrity() -> None:
    """Validate SWE-bench real-world trajectory fixture format and turns."""
    traj_file = FIXTURES_DIR / "swebench_agent_trajectory.json"
    assert traj_file.exists(), f"Missing trajectory fixture: {traj_file}"

    with open(traj_file, encoding="utf-8") as f:
        data = json.load(f)

    assert data["instance_id"] == "encode__httpx-1422"
    assert len(data["turns"]) == 7
    for turn in data["turns"]:
        assert "thought" in turn
        assert "tool_name" in turn
        assert "state" in turn


def test_watchdog_detects_real_world_state_oscillation() -> None:
    """Verify OscillationWatchdog detects state cycle loop at Turn 7.

    The SWE-bench trajectory has the agent cycling back to a previously-seen
    state at turn 7 (reverting to patch_option_a after trying option_b),
    which the watchdog should catch as STATE_CYCLE.
    """
    traj_file = FIXTURES_DIR / "swebench_agent_trajectory.json"
    with open(traj_file, encoding="utf-8") as f:
        data = json.load(f)

    watchdog = OscillationWatchdog(
        max_consecutive_tools=3,
        window_size=8,
        detect_state_cycles=True,
    )

    alerts: list[tuple[int, object]] = []
    for turn_idx, turn_data in enumerate(data["turns"], start=1):
        tool_rec = ToolInvocationRecord(
            tool_name=turn_data["tool_name"],
            arguments=turn_data["arguments"],
            result=turn_data["observation"],
        )
        snapshot = TurnSnapshot(
            session_id="test_swebench_oscillation",
            turn_index=turn_idx,
            prompt=turn_data["thought"],
            response=turn_data["observation"],
            tool_invocations=[tool_rec],
            state=turn_data["state"],
        )

        alert = watchdog.record_turn(snapshot)
        if alert:
            alerts.append((turn_idx, alert))

    # Turns 1-6 should pass cleanly without triggering alerts
    assert len(alerts) == 1
    trigger_turn, triggered_alert = alerts[0]

    # Turn 7 should trigger STATE_CYCLE because state reverted to patch_option_a
    assert trigger_turn == 7
    assert triggered_alert.alert_type == "STATE_CYCLE"
    assert "State oscillation detected" in triggered_alert.message


def test_watchdog_ping_pong_detection() -> None:
    """Test ping-pong alternation pattern (A -> B -> A -> B) detection."""
    watchdog = OscillationWatchdog(window_size=8)

    t1 = ToolInvocationRecord(tool_name="run_test", arguments={"cmd": "pytest"})
    t2 = ToolInvocationRecord(tool_name="edit_code", arguments={"file": "app.py"})

    assert watchdog.record_tool_call(t1) is None
    assert watchdog.record_tool_call(t2) is None
    assert watchdog.record_tool_call(t1) is None
    alert = watchdog.record_tool_call(t2)

    assert alert is not None
    assert alert.alert_type == "PING_PONG_OSCILLATION"
    assert "alternating" in alert.message


def test_unloop_session_time_travel_rewind_on_trajectory(tmp_path: Path) -> None:
    """Test recording SWE-bench trajectory in UnloopStore and rewinding to break out of loop.

    Records turns 1-6 into a real SQLite WAL store, then verifies time-travel
    by fetching Turn 3's state and forking a new branch with an alternate fix.
    """
    traj_file = FIXTURES_DIR / "swebench_agent_trajectory.json"
    with open(traj_file, encoding="utf-8") as f:
        data = json.load(f)

    db_path = tmp_path / "test_session.unloop"

    session = UnloopSession(
        name="swebench_debugger",
        db_path=db_path,
        framework="swebench_react",
    )

    # Record turns 1 through 6 using session.step() context manager
    for _idx, turn_data in enumerate(data["turns"][:6]):
        with session.step(
            prompt=turn_data["thought"],
            state=turn_data["state"],
        ) as ctx:
            ctx.record_tool(
                tool_name=turn_data["tool_name"],
                arguments=turn_data["arguments"],
                result=turn_data["observation"],
            )
            ctx.set_response(turn_data["observation"])

    # Verify 6 turns were stored on the main branch
    stored_turns = session.store.get_history(session.session_id, branch_id="main")
    assert len(stored_turns) == 6

    # Verify time-travel: fetch Turn 3 (index=2) state before the fruitless loop began
    turn_3 = stored_turns[2]
    assert turn_3.state["patch_id"] == "patch_option_a"

    # Rewind to Turn 3 and fork a new branch for alternate fix
    rewound = session.rewind_to(turn_3.turn_id, new_branch_name="alt_fix_c")
    assert rewound.turn_index == turn_3.turn_index

    # Record a corrective turn on the forked branch
    with session.step(
        prompt="Watchdog intervened: Rewinding to Turn 3 and taking approach C.",
        state={"file_hash": "c3d4e5f6a1b20004", "patch_id": "patch_option_c_resolved"},
    ) as ctx:
        ctx.record_tool(
            tool_name="edit_file",
            arguments={"file": "httpx/_config.py", "action": "apply_patch_c"},
            result="Patch C applied successfully with proper exception hierarchy.",
        )
        ctx.set_response("Successfully resolved with proper exception hierarchy.")

    # Verify the forked branch has the corrective turn
    forked_turns = session.store.get_history(session.session_id, branch_id="alt_fix_c")
    assert len(forked_turns) == 1
    assert forked_turns[0].state["patch_id"] == "patch_option_c_resolved"

    # Original branch should still have exactly 6 turns (immutable)
    original_turns = session.store.get_history(session.session_id, branch_id="main")
    assert len(original_turns) == 6

    session.close()


def test_watchdog_repetitive_tool_detection() -> None:
    """Verify detection of identical consecutive tool calls (death loop)."""
    watchdog = OscillationWatchdog(max_consecutive_tools=3, window_size=8)

    tool = ToolInvocationRecord(
        tool_name="run_test",
        arguments={"cmd": "pytest tests/test_auth.py"},
    )

    assert watchdog.record_tool_call(tool) is None
    assert watchdog.record_tool_call(tool) is None
    alert = watchdog.record_tool_call(tool)

    assert alert is not None
    assert alert.alert_type == "REPETITIVE_TOOL"
    assert "3 times" in alert.message
    assert alert.tool_name == "run_test"


def test_watchdog_no_false_positive_on_varied_tools() -> None:
    """Ensure varied tool sequences don't trigger false oscillation alerts."""
    watchdog = OscillationWatchdog(max_consecutive_tools=3, window_size=8)

    tools = [
        ToolInvocationRecord(tool_name="read_file", arguments={"file": "a.py"}),
        ToolInvocationRecord(tool_name="edit_file", arguments={"file": "a.py"}),
        ToolInvocationRecord(tool_name="run_test", arguments={"cmd": "pytest"}),
        ToolInvocationRecord(tool_name="read_file", arguments={"file": "b.py"}),
        ToolInvocationRecord(tool_name="edit_file", arguments={"file": "b.py"}),
        ToolInvocationRecord(tool_name="run_test", arguments={"cmd": "pytest -x"}),
    ]

    for tool in tools:
        alert = watchdog.record_tool_call(tool)
        assert alert is None, f"Unexpected alert on varied tool sequence: {alert}"
