"""Comprehensive tests for Prompt Compaction Diffing in unloop."""

import tempfile
from pathlib import Path

from click.testing import CliRunner

import unloop
from unloop.cli.main import cli
from unloop.protocol.models import ToolInvocationRecord, TurnSnapshot
from unloop.storage.db import UnloopStore


def test_turn_snapshot_compute_prompt_diff_empty() -> None:
    snap = TurnSnapshot(session_id="s1", turn_index=0, prompt=None)
    diff = snap.compute_prompt_diff(None)
    assert diff["has_diff"] is False
    assert diff["prev_length"] == 0
    assert diff["curr_length"] == 0
    assert diff["is_compacted"] is False
    assert diff["added_lines"] == []
    assert diff["removed_lines"] == []


def test_turn_snapshot_compute_prompt_diff_identical() -> None:
    prompt = "System: You are an agent.\nUser: Fix the bug.\nConstraint: Never delete files."
    snap = TurnSnapshot(session_id="s1", turn_index=1, prompt=prompt)
    diff = snap.compute_prompt_diff(prompt)
    assert diff["has_diff"] is False
    assert diff["prev_length"] == len(prompt)
    assert diff["curr_length"] == len(prompt)
    assert diff["compression_ratio"] == 1.0
    assert diff["is_compacted"] is False
    assert diff["removed_lines"] == []
    assert diff["added_lines"] == []


def test_turn_snapshot_compaction_drop_detection() -> None:
    original_prompt = (
        "System: You are an agent.\n"
        "Constraint 1: Never drop database tables.\n"
        "Constraint 2: Output only in RFC-8259 JSON.\n"
        "Document: Very long context line 1\n"
        "Document: Very long context line 2\n"
        "User: Run migration."
    )
    # Compaction occurred: documents and Constraint 1 were dropped by summarizer
    compacted_prompt = (
        "System: You are an agent.\n"
        "Constraint 2: Output only in RFC-8259 JSON.\n"
        "User: Run migration."
    )

    snap = TurnSnapshot(session_id="s1", turn_index=2, prompt=compacted_prompt)
    diff = snap.compute_prompt_diff(original_prompt)

    assert diff["has_diff"] is True
    assert diff["is_compacted"] is True
    assert diff["prev_length"] > diff["curr_length"]
    assert diff["compression_ratio"] < 1.0
    assert "Constraint 1: Never drop database tables." in diff["removed_lines"]
    assert "Document: Very long context line 1" in diff["removed_lines"]
    assert diff["added_lines"] == []


def test_turn_snapshot_prompt_expansion() -> None:
    prev_prompt = "User: Initial prompt."
    curr_prompt = (
        "User: Initial prompt.\n"
        "Observation: Tool output returned error code 404.\n"
        "System: Attempt fallback service."
    )
    snap = TurnSnapshot(session_id="s1", turn_index=1, prompt=curr_prompt)
    diff = snap.compute_prompt_diff(prev_prompt)

    assert diff["has_diff"] is True
    assert diff["is_compacted"] is False
    assert diff["prev_length"] < diff["curr_length"]
    assert "Observation: Tool output returned error code 404." in diff["added_lines"]


def test_storage_diff_turns_includes_prompt_and_side_effects() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_diff_storage.unloop"

        with UnloopStore(db_path) as store:
            turn_a = TurnSnapshot(
                session_id="s1",
                turn_index=0,
                prompt="System prompt.\nUser instruction 1.\nNegative constraint: Do not email external users.",
                tool_invocations=[
                    ToolInvocationRecord(
                        tool_name="send_email",
                        arguments={"to": "dev@internal.org"},
                        is_side_effect=True,
                    )
                ],
            )
            turn_b = TurnSnapshot(
                session_id="s1",
                turn_index=1,
                prompt="System prompt.\nUser instruction 1.",
                tool_invocations=[
                    ToolInvocationRecord(tool_name="read_file", arguments={"path": "report.txt"})
                ],
            )

            res = store.diff_turns(turn_a, turn_b)

            assert "prompt_diff" in res
            p_diff = res["prompt_diff"]
            assert p_diff["is_compacted"] is True
            assert "Negative constraint: Do not email external users." in p_diff["removed_lines"]

            assert len(res["side_effects_a"]) == 1
            assert res["side_effects_a"][0]["tool_name"] == "send_email"
            assert len(res["side_effects_b"]) == 0


def test_cli_diff_prompt_flag() -> None:
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "cli_diff_prompt.unloop"

        with unloop.session("prompt_diff_session", db_path=db_path) as dbg:
            with dbg.step(
                prompt="System: Critical Rule: Do not run rm -rf.\nUser: Clean project."
            ) as s0:
                s0.set_response("Checking files")

            with dbg.step(prompt="User: Clean project.") as s1:
                s1.record_tool("write_file", arguments={"file": "clean.sh"})
                s1.set_response("Cleaned")

        # 1. Normal diff without --prompt flag should show compaction summary
        res = runner.invoke(cli, ["diff", str(db_path), "--from", "0", "--to", "1"])
        assert res.exit_code == 0
        assert "Compaction Drops" in res.output
        assert "Egress Side-Effects" in res.output

        # 2. Diff with --prompt flag should display detailed unified diff panel
        res_p = runner.invoke(cli, ["diff", str(db_path), "--from", "0", "--to", "1", "--prompt"])
        assert res_p.exit_code == 0
        assert "Prompt Compaction Diff (#0 -> #1)" in res_p.output
        assert "Critical Rule: Do not run rm -rf" in res_p.output

        # 3. Diff identical prompts with --prompt flag
        res_same = runner.invoke(cli, ["diff", str(db_path), "--from", "0", "--to", "0", "-p"])
        assert res_same.exit_code == 0
        assert "Prompts are identical between these turns" in res_same.output
