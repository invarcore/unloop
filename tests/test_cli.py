"""Tests for unloop CLI commands."""

import tempfile
from pathlib import Path
from click.testing import CliRunner
import unloop
from unloop.cli.main import cli


def test_cli_info_history_and_export():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "cli_test.unloop"
        report_path = Path(tmpdir) / "report.md"

        with unloop.session("cli_session", db_path=db_path) as dbg:
            with dbg.step(prompt="CLI Test prompt", state={"score": 100}) as step:
                step.record_tool("check_status", arguments={"env": "prod"}, result="healthy")
                step.set_response("System all clear")

        runner = CliRunner()

        # 1. Test info
        res_info = runner.invoke(cli, ["info", str(db_path)])
        assert res_info.exit_code == 0
        assert "cli_session" in res_info.output

        # 2. Test history
        res_hist = runner.invoke(cli, ["history", str(db_path)])
        assert res_hist.exit_code == 0
        assert "CLI Test prompt" in res_hist.output
        assert "check_status" in res_hist.output

        # 3. Test export
        res_exp = runner.invoke(cli, ["export", str(db_path), "-o", str(report_path)])
        assert res_exp.exit_code == 0
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "Post-Mortem Incident Report" in content
        assert "check_status" in content
