"""Tests for agdb SQLite WAL storage engine."""

import tempfile
from pathlib import Path
from agdb.protocol.models import SessionMetadata, TurnSnapshot, ToolInvocationRecord
from agdb.storage.db import AgdbStore


def test_storage_crud_and_branching():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_session.agdb"
        store = AgdbStore(db_path)

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

        store.close()
