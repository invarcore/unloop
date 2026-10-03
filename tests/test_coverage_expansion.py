"""Comprehensive coverage expansion for unloop.

Tests CLI edge cases, advanced breakpoints, interceptor wrappers,
storage branches, and TUI interaction paths.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import unloop
from unloop.cli.main import cli
from unloop.protocol.models import SessionMetadata, ToolInvocationRecord, TurnSnapshot
from unloop.runtime.breakpoints import BreakpointManager
from unloop.storage.db import UnloopStore
from unloop.tui.app import UnloopTuiApp


# =========================================================================
# 1. Breakpoints Deep Coverage
# =========================================================================
def test_breakpoints_exhaustive() -> None:
    mgr = BreakpointManager()

    # 1. Add and list
    bp_idx = mgr.add(turn_index=3)
    bp_tool = mgr.add(tool_name="special_tool")
    bp_err = mgr.add(on_error=True)
    bp_cond = mgr.add(condition=lambda s: s.get("danger") is True, condition_repr="danger == True")
    bp_fail_cond = mgr.add(condition=lambda s: 1 / 0)  # Exception raising condition

    all_bps = mgr.list_breakpoints()
    assert len(all_bps) == 5

    # 2. Disabled breakpoint check
    all_bps[0].enabled = False
    turn_at_3 = TurnSnapshot(session_id="s1", turn_index=3)
    hit, _ = mgr.check(turn_at_3)
    assert hit is False  # disabled, so skipped
    all_bps[0].enabled = True

    # 3. Turn index hit
    hit, reason = mgr.check(turn_at_3)
    assert hit is True
    assert "Reached target turn index 3" in str(reason)

    # 4. Tool name hit
    turn_tool = TurnSnapshot(
        session_id="s1",
        turn_index=0,
        tool_invocations=[ToolInvocationRecord(tool_name="special_tool", arguments={})],
    )
    hit, reason = mgr.check(turn_tool)
    assert hit is True
    assert "Tool breakpoint hit for 'special_tool'" in str(reason)

    # 5. Error in metadata
    turn_meta_err = TurnSnapshot(session_id="s1", turn_index=0, metadata={"error": "Disk failure"})
    hit, reason = mgr.check(turn_meta_err)
    assert hit is True
    assert "Turn execution error encountered" in str(reason)

    # 6. Error in tool invocation
    turn_tool_err = TurnSnapshot(
        session_id="s1",
        turn_index=0,
        tool_invocations=[ToolInvocationRecord(tool_name="calc", arguments={}, error="ZeroDivisionError")],
    )
    hit, reason = mgr.check(turn_tool_err)
    assert hit is True
    assert "Tool error encountered in 'calc'" in str(reason)

    # 7. Custom condition hit
    turn_cond = TurnSnapshot(session_id="s1", turn_index=0, state={"danger": True})
    hit, reason = mgr.check(turn_cond)
    assert hit is True
    assert "Condition satisfied: danger == True" in str(reason)

    # 8. Exception raising condition handled safely
    turn_any = TurnSnapshot(session_id="s1", turn_index=0, state={"danger": False})
    # Remove all except fail_cond to test isolation
    mgr.remove(bp_idx)
    mgr.remove(bp_tool)
    mgr.remove(bp_err)
    mgr.remove(bp_cond)
    hit, _ = mgr.check(turn_any)
    assert hit is False

    # 9. Remove non-existent ID
    assert mgr.remove("bp_nonexistent") is False
    assert mgr.remove(bp_fail_cond) is True


# =========================================================================
# 2. CLI Subcommands Edge Cases
# =========================================================================
def test_cli_bare_invocation() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, [])
    assert res.exit_code == 0
    assert "The Time-Travel Debugger for AI Agents" in res.output


def test_cli_info_empty_file(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "empty.unloop"
    with UnloopStore(db_path):
        pass  # Just create empty tables

    res = runner.invoke(cli, ["info", str(db_path)])
    assert res.exit_code == 0
    assert "No recorded sessions found in file" in res.output


def test_cli_history_edge_cases(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "hist_empty.unloop"
    with UnloopStore(db_path):
        pass

    # No sessions
    res = runner.invoke(cli, ["history", str(db_path)])
    assert res.exit_code == 0
    assert "No sessions found" in res.output

    # Create session with no turns
    with UnloopStore(db_path) as store:
        meta = SessionMetadata(session_id="s_empty", name="Empty Session")
        store.create_session(meta)

    res_empty_branch = runner.invoke(cli, ["history", str(db_path), "--session-id", "s_empty"])
    assert res_empty_branch.exit_code == 0
    assert "No turns found on branch 'main'" in res_empty_branch.output


def test_cli_diff_edge_cases(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "diff_edge.unloop"
    with UnloopStore(db_path) as store:
        meta = SessionMetadata(session_id="s1", name="Diff Session")
        store.create_session(meta)
        # Turn 0
        store.save_turn(TurnSnapshot(session_id="s1", turn_index=0, state={"val": 10}))
        # Turn 1 with identical state
        store.save_turn(TurnSnapshot(session_id="s1", turn_index=1, state={"val": 10}))

    # Turn not found
    res_not_found = runner.invoke(cli, ["diff", str(db_path), "--from", "0", "--to", "99"])
    assert res_not_found.exit_code == 0
    assert "Turn #99 not found" in res_not_found.output

    res_a_not_found = runner.invoke(cli, ["diff", str(db_path), "--from", "42", "--to", "1"])
    assert res_a_not_found.exit_code == 0
    assert "Turn #42 not found" in res_a_not_found.output

    # Identical state
    res_identical = runner.invoke(cli, ["diff", str(db_path), "--from", "0", "--to", "1"])
    assert res_identical.exit_code == 0
    assert "<identical state>" in res_identical.output


def test_cli_record_no_command() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["record"])
    assert res.exit_code != 0


def test_cli_check_violations(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "violations.unloop"
    with unloop.session("violation_session", db_path=db_path) as dbg:
        with dbg.step(prompt="Oscillating prompt", state={}) as step:
            step.snapshot.is_breakpoint = True
            step.snapshot.breakpoint_reason = "Watchdog loop detected"
            step.record_tool("broken_tool", arguments={}, error="Network timeout")

    # Check with no-oscillations and no-errors and fail-on-breakpoint
    res = runner.invoke(
        cli,
        ["check", str(db_path), "--no-oscillations", "--no-errors", "--fail-on-breakpoint"],
    )
    assert res.exit_code == 1
    assert "unloop check FAILED" in res.output
    assert "Oscillation detected" in res.output
    assert "broken_tool" in res.output
    assert "Breakpoint triggered" in res.output


def test_cli_export_empty_file(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "export_empty.unloop"
    with UnloopStore(db_path):
        pass

    res = runner.invoke(cli, ["export", str(db_path)])
    assert res.exit_code == 0
    assert "No sessions found" in res.output


def test_cli_replay_mocked(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "replay.unloop"
    with UnloopStore(db_path):
        pass

    with patch("unloop.tui.app.run_tui") as mock_tui:
        res = runner.invoke(cli, ["replay", str(db_path)])
        assert res.exit_code == 0
        assert mock_tui.called


# =========================================================================
# 3. Interceptor & Trace Decorator Edge Cases
# =========================================================================
def test_trace_decorator_auto_session_sync(tmp_path: Path) -> None:
    # Function returning a dict (non-string return value)
    @unloop.trace(name="auto_calc")
    def calculate_metrics(query: str, state: dict) -> dict:
        return {"result": 42, "query": query}

    res = calculate_metrics("What is 6x7?", {"status": "start"})
    assert res["result"] == 42


def test_trace_decorator_auto_session_raising(tmp_path: Path) -> None:
    @unloop.trace(name="failing_func")
    def fail_hard(param: str) -> None:
        raise RuntimeError("Planned explosion")

    with pytest.raises(RuntimeError, match="Planned explosion"):
        fail_hard("boom")


def test_trace_decorator_async_auto_session() -> None:
    async def _async_test() -> None:
        @unloop.trace(name="async_auto")
        async def fetch_data(url: str, params: dict) -> int:
            await asyncio.sleep(0.01)
            return 200

        status = await fetch_data("https://example.com", {"q": "test"})
        assert status == 200

        @unloop.trace(name="async_fail")
        async def async_failure() -> None:
            raise ValueError("Async error")

        with pytest.raises(ValueError, match="Async error"):
            await async_failure()

    asyncio.run(_async_test())


# =========================================================================
# 4. Storage Engine Edge Cases
# =========================================================================
def test_storage_get_head_and_fork_nonexistent(tmp_path: Path) -> None:
    db_path = tmp_path / "head_test.unloop"
    with UnloopStore(db_path) as store:
        meta = SessionMetadata(session_id="s_head", name="Head Test")
        store.create_session(meta)

        # Empty branch head is None
        assert store.get_head("s_head", "main") is None

        # Forking from non-existent turn raises ValueError
        with pytest.raises(ValueError, match="Origin turn turn_999 not found"):
            store.fork_branch("s_head", "turn_999", "new_branch")

        # Save turn and verify head
        t0 = TurnSnapshot(session_id="s_head", turn_index=0)
        store.save_turn(t0)
        head = store.get_head("s_head", "main")
        assert head is not None
        assert head.turn_id == t0.turn_id

        # diff_turns with removed keys
        t1 = TurnSnapshot(session_id="s_head", turn_index=1, state={"keep": 1})
        t2 = TurnSnapshot(session_id="s_head", turn_index=2, state={})  # 'keep' removed
        diff = store.diff_turns(t1, t2)
        assert "keep" in diff["state_diff"]["removed"]


# =========================================================================
# 5. TUI App Additional Key Actions
# =========================================================================
def test_tui_app_empty_and_actions(tmp_path: Path) -> None:
    async def _run() -> None:
        db_path = tmp_path / "tui_empty.unloop"
        with UnloopStore(db_path):
            pass

        app_empty = UnloopTuiApp(db_path)
        try:
            async with app_empty.run_test():
                assert len(app_empty.turns) == 0
                assert app_empty.selected_turn is None
        finally:
            app_empty.store.close()

        # App with breakpoint turn to test 'b' toggle and 'r' rewind
        db_populated = tmp_path / "tui_pop.unloop"
        with unloop.session("pop_session", db_path=db_populated) as dbg:
            with dbg.step(prompt="Step 0", state={"step": 0}):
                pass
            with dbg.step(prompt="Step 1", state={"step": 1}) as step:
                step.snapshot.is_breakpoint = True
                step.snapshot.breakpoint_reason = "Test BP"

        app_pop = UnloopTuiApp(db_populated)
        try:
            async with app_pop.run_test() as pilot:
                assert len(app_pop.turns) == 2
                # Toggle breakpoint with 'b'
                await pilot.press("b")
                assert app_pop.selected_turn is not None
                # Press 'r' for rewind
                await pilot.press("r")
        finally:
            app_pop.store.close()

    asyncio.run(_run())


def test_additional_coverage_boosters(tmp_path: Path) -> None:
    # 1. Breakpoint watchdog alert check (hits line 72 in breakpoints.py)
    mgr = BreakpointManager()
    mgr.add(on_watchdog=True)
    turn = TurnSnapshot(session_id="s1", turn_index=0)
    hit, reason = mgr.check(turn, watchdog_tripped=True)
    assert hit is True
    assert "Watchdog loop/oscillation alert triggered" in str(reason)

    # 2. CLI record with leading '--' (hits lines 220-221 in main.py)
    runner = CliRunner()
    rec_out = tmp_path / "rec_sub.unloop"
    res = runner.invoke(cli, ["record", "-o", str(rec_out), "--", "python", "-c", "print('ok')"])
    assert res.exit_code == 0

    # 3. CLI history with tool error (hits line 127 in main.py)
    db_err = tmp_path / "hist_err.unloop"
    with unloop.session("err_session", db_path=db_err) as dbg:
        with dbg.step(prompt="Run with tool error", state={}) as step:
            step.record_tool("fail_op", arguments={"flag": True}, error="Connection reset by peer")
    res_hist = runner.invoke(cli, ["history", str(db_err)])
    assert res_hist.exit_code == 0
    assert "Connection reset by peer" in res_hist.output

    # 4. Context step mutate_state and set_telemetry (hits lines 103-112 in interceptor.py)
    db_ctx = tmp_path / "ctx.unloop"
    with unloop.session("ctx_session", db_path=db_ctx) as dbg:
        with dbg.step(prompt="Context mutation", state={"orig": 1}) as step:
            step.mutate_state("orig", 2)
            step.set_telemetry(prompt_tokens=150, completion_tokens=75, cost_usd=0.003)
            step.snapshot.state_delta = {"orig": 2}
    with UnloopStore(db_ctx) as store:
        history = store.get_history(dbg.session_id)
        assert len(history) == 1
        assert history[0].telemetry.estimated_cost_usd == 0.003

    # 5. Encrypted store with state_delta (hits line 474 in db.py)
    db_enc = tmp_path / "enc.unloop"
    enc_key = "secret_encryption_key_12345"
    with UnloopStore(db_enc, encryption_key=enc_key) as store:
        meta = SessionMetadata(session_id="enc_s", name="Encrypted")
        store.create_session(meta)
        t_enc = TurnSnapshot(
            session_id="enc_s",
            turn_index=0,
            prompt="Encrypted prompt",
            state={"secret": "value"},
            state_delta={"secret": "value"},
        )
        store.save_turn(t_enc)
        loaded = store.get_history("enc_s")
        assert len(loaded) == 1
        assert loaded[0].state_delta == {"secret": "value"}

