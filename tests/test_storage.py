"""Tests for unloop SQLite WAL storage engine."""

import tempfile
from pathlib import Path

from unloop.protocol.models import SessionMetadata, ToolInvocationRecord, TurnSnapshot
from unloop.storage.db import UnloopStore


def test_storage_crud_and_branching() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_session.unloop"
        store = UnloopStore(db_path)
        try:
            meta = SessionMetadata(name="test_run", framework="langgraph")
            store.create_session(meta)

            retrieved_meta = store.get_session(meta.session_id)
            assert retrieved_meta is not None
            assert retrieved_meta.name == "test_run"

            # Turn 0
            turn0 = TurnSnapshot(
                session_id=meta.session_id,
                turn_index=0,
                prompt="Initial query",
                response="I will search",
                state={"step": 0},
                tool_invocations=[ToolInvocationRecord(tool_name="search", arguments={"q": "test"})],
            )
            store.save_turn(turn0)

            # Turn 1
            turn1 = TurnSnapshot(
                session_id=meta.session_id,
                parent_id=turn0.turn_id,
                turn_index=1,
                prompt="Tool returned",
                response="Final answer",
                state={"step": 1, "done": True},
            )
            store.save_turn(turn1)

            history = store.get_history(meta.session_id, "main")
            assert len(history) == 2
            assert history[0].turn_id == turn0.turn_id
            assert history[1].turn_id == turn1.turn_id

            # Branch forking
            forked_branch = store.fork_branch(
                session_id=meta.session_id,
                origin_turn_id=turn0.turn_id,
                new_branch_id="alt_branch",
                description="Exploratory branch",
            )
            assert forked_branch == "alt_branch"

            branches = store.get_branches(meta.session_id)
            assert len(branches) == 2
            branch_ids = [b["branch_id"] for b in branches]
            assert "main" in branch_ids
            assert "alt_branch" in branch_ids

            # Turn diffing
            diff = store.diff_turns(turn0, turn1)
            assert diff["turn_a_index"] == 0
            assert diff["turn_b_index"] == 1
            assert "search" in diff["tools_a"]
            assert diff["state_diff"]["added"]["done"] is True
            assert diff["state_diff"]["modified"]["step"]["turn_b"] == 1
        finally:
            store.close()


def test_storage_append_only_enforcement() -> None:
    import pytest

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "append_only.unloop"
        with UnloopStore(db_path) as store:
            meta = SessionMetadata(name="append_test")
            store.create_session(meta)

            turn = TurnSnapshot(session_id=meta.session_id, turn_index=0, prompt="Original")
            store.save_turn(turn)

            # Trying to overwrite without allow_update must raise ValueError
            turn_modified = TurnSnapshot(
                turn_id=turn.turn_id,
                session_id=meta.session_id,
                turn_index=0,
                prompt="Attempted overwrite",
            )
            with pytest.raises(ValueError, match="strictly append-only"):
                store.save_turn(turn_modified)


def test_storage_encryption_and_redaction() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "secure.unloop"
        key = "unloop_secret_vault_key"

        with UnloopStore(db_path, encryption_key=key, redact_secrets=True) as store:
            meta = SessionMetadata(name="secure_test")
            store.create_session(meta)

            turn = TurnSnapshot(
                session_id=meta.session_id,
                turn_index=0,
                prompt="Use Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.sensitive secret",
                response="Normal response",
                state={"password": "MySecretPassword123", "step": 1},
            )
            store.save_turn(turn)

            # Check raw SQLite row is encrypted and redacted
            cursor = store.conn.cursor()
            raw_row = cursor.execute("SELECT prompt, state_json FROM turns WHERE turn_id = ?", (turn.turn_id,)).fetchone()
            raw_prompt = raw_row[0]
            raw_state = raw_row[1]

            # Should be encrypted with enc: prefix
            assert raw_prompt.startswith("enc:")
            assert raw_state.startswith("enc:")

            # When reading through the store with the key, it decrypts and shows redacted secret
            retrieved = store.get_turn(turn.turn_id)
            assert retrieved is not None
            assert "[REDACTED]" in retrieved.prompt
            assert "eyJhbGci" not in retrieved.prompt
            assert "[REDACTED]" in str(retrieved.state["password"])
