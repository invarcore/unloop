"""Tests for agdb runtime interceptor and breakpoints."""

import tempfile
from pathlib import Path
import pytest
import agdb
from agdb.runtime.interceptor import BreakpointHalt


def test_session_tracing_and_breakpoints():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.agdb"

        with agdb.session("test_agent", db_path=db_path, raise_on_breakpoint=True) as dbg:
            # Add breakpoint on error
            dbg.add_breakpoint(on_error=True)

            # Turn 1: normal execution
            with dbg.step(prompt="Step 1", state={"val": 10}) as step:
                step.record_tool("calc", arguments={"op": "add"}, result=15)
                step.set_response("Done addition")

            # Turn 2: triggers breakpoint on error
            with pytest.raises(BreakpointHalt) as exc_info:
                with dbg.step(prompt="Step 2", state={"val": 15}) as step:
                    step.record_tool("remote_api", arguments={"url": "http://bad"}, error="404 Not Found")

            assert "Tool error encountered" in str(exc_info.value)

        # Reopen store and verify persistence
        store = agdb.AgdbStore(db_path)
        history = store.get_history(dbg.session_id, "main")
        assert len(history) == 2
        assert history[0].is_breakpoint is False
        assert history[1].is_breakpoint is True
        assert "404 Not Found" in history[1].breakpoint_reason
        store.close()


def test_session_rewind_and_branch_forking():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "rewind.agdb"

        with agdb.session("branch_agent", db_path=db_path) as dbg:
            # Turn 0
            with dbg.step(prompt="Init", state={"turn": 0}) as step:
                step.set_response("Started")
            turn0_id = dbg.current_turn_id

            # Turn 1 (bad path)
            with dbg.step(prompt="Erroneous direction", state={"turn": 1, "path": "wrong"}) as step:
                step.set_response("Going wrong way")

            # Rewind to Turn 0!
            assert turn0_id is not None
            rewound_turn = dbg.rewind_to(turn0_id, new_branch_name="recovery_branch")
            assert rewound_turn.turn_index == 0
            assert dbg.active_branch == "recovery_branch"

            # Execute Turn 1 on recovery branch
            with dbg.step(prompt="Correct direction", state={"turn": 1, "path": "fixed"}) as step:
                step.set_response("Fixed way")

        store = agdb.AgdbStore(db_path)
        branches = store.get_branches(dbg.session_id)
        assert len(branches) == 2
        recov_history = store.get_history(dbg.session_id, "recovery_branch")
        assert len(recov_history) == 1
        assert recov_history[0].state["path"] == "fixed"
        store.close()
