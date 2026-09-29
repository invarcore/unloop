"""Tests for unloop CLI commands."""

import sys
import tempfile
from pathlib import Path

from click.testing import CliRunner

import unloop
from unloop.cli.main import cli


def test_cli_info_history_and_export() -> None:
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.unloop"
        report_path = Path(tmpdir) / "report.md"

        with unloop.session("cli_session", db_path=db_path) as dbg:
            with dbg.step(prompt="CLI Test prompt", state={"score": 100}) as step:
                step.record_tool("check_status", arguments={"env": "prod"}, result="healthy")
                step.set_response("System all clear")

        # Test info
        res_info = runner.invoke(cli, ["info", str(db_path)])
        assert res_info.exit_code == 0
        assert "cli_session" in res_info.output
        assert "Total Turns" in res_info.output

        # Test history
        res_hist = runner.invoke(cli, ["history", str(db_path)])
        assert res_hist.exit_code == 0
        assert "check_status" in res_hist.output
        assert "System all clear" in res_hist.output

        # Test export
        res_export = runner.invoke(cli, ["export", str(db_path), "-o", str(report_path)])
        assert res_export.exit_code == 0
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "unloop Post-Mortem Incident Report: cli_session" in content


def test_cli_diff() -> None:
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "diff_test.unloop"

        with unloop.session("diff_session", db_path=db_path) as dbg:
            with dbg.step(prompt="Turn 0", state={"counter": 1, "status": "pending"}) as step:
                step.record_tool("init_system", arguments={})
                step.set_response("Initialized")

            with dbg.step(prompt="Turn 1", state={"counter": 2, "status": "completed", "extra": "yes"}) as step:
                step.record_tool("execute_task", arguments={"id": 42})
                step.set_response("Completed")

        res = runner.invoke(cli, ["diff", str(db_path), "--from", "0", "--to", "1"])
        assert res.exit_code == 0
        assert "Turn Diff" in res.output
        assert "init_system" in res.output
        assert "execute_task" in res.output


def test_cli_check_passed() -> None:
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "check_pass.unloop"

        with unloop.session("clean_session", db_path=db_path) as dbg:
            with dbg.step(prompt="Clean step", state={"val": 1}) as step:
                step.record_tool("ok_tool", arguments={}, result="OK")
                step.set_response("Clean response")

        res = runner.invoke(cli, ["check", str(db_path), "--max-turns", "5", "--no-errors", "--no-oscillations"])
        assert res.exit_code == 0
        assert "unloop check PASSED" in res.output


def test_cli_check_failed_on_max_turns() -> None:
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "check_fail.unloop"

        with unloop.session("long_session", db_path=db_path) as dbg:
            for i in range(5):
                with dbg.step(prompt=f"Step {i}", state={"i": i}):
                    pass

        # Exceeds threshold of 3 turns
        res = runner.invoke(cli, ["check", str(db_path), "--max-turns", "3"])
        assert res.exit_code == 1
        assert "unloop check FAILED" in res.output
        assert "Turn count limit exceeded" in res.output


def test_cli_check_failed_on_tool_error() -> None:
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "check_err.unloop"

        with unloop.session("err_session", db_path=db_path) as dbg:
            with dbg.step(prompt="Err step", state={}) as step:
                step.record_tool("failing_tool", arguments={}, error="Network timeout")

        res = runner.invoke(cli, ["check", str(db_path), "--no-errors"])
        assert res.exit_code == 1
        assert "failed with error: Network timeout" in res.output


def test_cli_record_subprocess() -> None:
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        session_out = Path(tmpdir) / "record_session.unloop"

        # Run a simple python script via unloop record
        script_code = (
            "import os, unloop\n"
            "with unloop.session('subprocess_agent') as s:\n"
            "    with s.step(prompt='Subprocess run', state={'ok': True}) as st:\n"
            "        st.set_response('Subprocess complete')\n"
        )
        script_path = Path(tmpdir) / "agent_script.py"
        script_path.write_text(script_code, encoding="utf-8")

        res = runner.invoke(cli, ["record", "-o", str(session_out), "--", sys.executable, str(script_path)])
        assert res.exit_code == 0
        assert session_out.exists()
        assert "unloop record finished" in res.output

        with unloop.UnloopStore(session_out) as store:
            sess = store.get_session("subprocess_agent") or store.get_history(store.conn.execute("SELECT session_id FROM sessions").fetchone()[0])
            assert len(sess) > 0
