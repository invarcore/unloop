# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""Unit and integration tests for deterministic 6-slot compaction verification."""

import tempfile
from pathlib import Path

from click.testing import CliRunner

import unloop
from unloop.cli.main import cli
from unloop.compaction_slots import (
    canonicalize_constraints_rfc8785,
    extract_hard_constraints,
    parse_compaction_slots,
    verify_compaction_checkpoint,
)

PRE_PROMPT = """
System instructions:
- Never modify files under `db/migrations/001_init.sql` or `src/auth/middleware.py`.
- Do not pass `--force` or `--no-verify` to git commands.
- Read-only access to `/etc/hosts`.
"""

VALID_6_SLOT_SUMMARY = """
## Goal
Refactor the user profile endpoint latency.

## Constraints and Preferences
- Never modify files under `db/migrations/001_init.sql` or `src/auth/middleware.py`.
- Do not pass `--force` or `--no-verify` to git commands.

## Progress
Profiled SQL queries in `src/users/repo.py`.

## Key Decisions
Use composite index on `(tenant_id, created_at)`.

## Next Steps
Run `pytest tests/test_users.py`.

## Critical Context
Tenant RLS role `kf_app` must remain active. Read-only access to `/etc/hosts` preserved.
"""

DRIFTED_6_SLOT_SUMMARY = """
## Goal
Refactor the user profile endpoint latency.

## Constraints and Preferences
- Follow standard git best practices and avoid touching database files if possible.

## Progress
Profiled SQL queries in `src/users/repo.py`.

## Key Decisions
Use composite index.

## Next Steps
Run tests.

## Critical Context
Tenant RLS role `kf_app` must remain active.
"""


def test_parse_compaction_slots():
    slots = parse_compaction_slots(VALID_6_SLOT_SUMMARY)
    assert len(slots) == 6
    assert "Goal" in slots
    assert "Constraints and Preferences" in slots
    assert "Refactor the user profile endpoint latency." in slots["Goal"]

    # Test alternative header formatting: [Slot], **Slot**, ### Slot
    alt_text = """
### Goal:
Speed up DB.
**Constraints and Preferences**
Never delete tables.
[Progress]:
Done step 1.
"""
    alt_slots = parse_compaction_slots(alt_text)
    assert alt_slots["Goal"] == "Speed up DB."
    assert "Never delete tables." in alt_slots["Constraints and Preferences"]
    assert alt_slots["Progress"] == "Done step 1."


def test_passes_when_all_6_slots_and_constraints_survive():
    report = verify_compaction_checkpoint(PRE_PROMPT, VALID_6_SLOT_SUMMARY)
    assert report.passed is True
    assert report.missing_slots == ()
    assert report.dropped_anchors == ()
    assert report.dropped_constraints == ()
    assert len(report.canonical_constraints_digest) == 64


def test_fails_closed_when_speccompact_drops_negative_constraints():
    report = verify_compaction_checkpoint(PRE_PROMPT, DRIFTED_6_SLOT_SUMMARY)
    assert report.passed is False
    assert "db/migrations/001_init.sql" in report.dropped_anchors
    assert "--no-verify" in report.dropped_anchors
    assert len(report.dropped_constraints) >= 2


def test_detects_missing_openclaw_schema_slots():
    incomplete = "## Goal\nFix bug\n## Constraints and Preferences\n- Never use `--force`."
    report = verify_compaction_checkpoint("- Never use `--force`.", incomplete)
    assert report.passed is False
    assert "Progress" in report.missing_slots
    assert "Key Decisions" in report.missing_slots
    assert "Next Steps" in report.missing_slots
    assert "Critical Context" in report.missing_slots


def test_require_all_slots_false():
    partial = """## Constraints and Preferences
- Never use `--force`.
"""
    report = verify_compaction_checkpoint(
        "- Never use `--force`.", partial, require_all_slots=False
    )
    assert report.passed is True
    assert report.missing_slots == ()


def test_extract_hard_constraints_various_negative_tokens():
    text = """
    1. Don't touch `config.json`.
    2. Must not disable auth.
    3. Cannot access secret/token.
    4. Can't overwrite `main.py`.
    5. Forbidden: modifying `--dry-run`.
    6. Prohibited from changing schema.
    7. Strictly avoid running `--all`.
    8. Immutable: `base_image`.
    9. Regular positive remark: build the UI cleanly.
    """
    clauses, anchors = extract_hard_constraints(text)
    assert len(clauses) == 8
    assert "config.json" in anchors
    assert "--dry-run" in anchors
    assert "--all" in anchors
    assert "base_image" in anchors


def test_canonicalize_constraints_rfc8785():
    clauses1 = ["never delete files", "do not skip checks"]
    clauses2 = ["do not skip checks", "never delete files"]
    anchors1 = {"--no-verify", "path/to/file"}
    anchors2 = {"path/to/file", "--no-verify"}

    d1 = canonicalize_constraints_rfc8785(clauses1, anchors1)
    d2 = canonicalize_constraints_rfc8785(clauses2, anchors2)
    assert d1 == d2
    assert len(d1) == 64


def test_cli_diff_verify_slots_passed():
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_verify_slots.unloop"

        with unloop.session("compaction_pass_session", db_path=db_path) as dbg:
            with dbg.step(prompt=PRE_PROMPT, state={}) as step:
                step.record_tool("init", arguments={})
                step.set_response("Init done")

            with dbg.step(prompt=VALID_6_SLOT_SUMMARY, state={}) as step:
                step.record_tool("compact", arguments={})
                step.set_response("Compacted summary generated")

        res = runner.invoke(
            cli,
            ["diff", str(db_path), "--from", "0", "--to", "1", "--verify-slots"],
        )
        assert res.exit_code == 0
        assert "[UNLOOP COMPACTION GATE PASSED]" in res.output
        assert "RFC8785 Digest:" in res.output


def test_cli_diff_verify_slots_failed():
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_verify_slots_fail.unloop"

        with unloop.session("compaction_fail_session", db_path=db_path) as dbg:
            with dbg.step(prompt=PRE_PROMPT, state={}) as step:
                step.record_tool("init", arguments={})
                step.set_response("Init done")

            with dbg.step(prompt=DRIFTED_6_SLOT_SUMMARY, state={}) as step:
                step.record_tool("compact", arguments={})
                step.set_response("Drifted summary generated")

        res = runner.invoke(
            cli,
            ["diff", str(db_path), "--from", "0", "--to", "1", "--verify-slots"],
        )
        assert res.exit_code == 2
        assert "[UNLOOP COMPACTION GATE FAILED]" in res.output
        assert "Missing Slots:" in res.output or "Dropped Anchors:" in res.output or "Dropped Invariant:" in res.output
