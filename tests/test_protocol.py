"""Tests for unloop protocol models."""

from unloop.protocol.models import SessionMetadata, ToolInvocationRecord, TurnSnapshot


def test_tool_invocation_argument_hash() -> None:
    tool1 = ToolInvocationRecord(tool_name="search", arguments={"q": "rag", "limit": 5})
    tool2 = ToolInvocationRecord(tool_name="search", arguments={"limit": 5, "q": "rag"})
    tool3 = ToolInvocationRecord(tool_name="search", arguments={"q": "different"})

    assert tool1.argument_hash == tool2.argument_hash
    assert tool1.argument_hash != tool3.argument_hash


def test_turn_snapshot_state_delta() -> None:
    snap = TurnSnapshot(
        session_id="sess_1",
        turn_index=1,
        state={"counter": 2, "target": "active", "temp": "val"},
    )

    prev_state = {"counter": 1, "temp": "val", "old_key": "remove_me"}
    delta = snap.compute_state_delta(prev_state)

    assert delta["added"] == {"target": "active"}
    assert delta["modified"] == {"counter": {"old": 1, "new": 2}}
    assert delta["removed"] == ["old_key"]
    assert snap.state_hash is not None


def test_session_metadata() -> None:
    meta = SessionMetadata(name="eval_run", framework="langgraph", model_name="claude-3-7-sonnet")
    assert meta.name == "eval_run"
    assert meta.framework == "langgraph"
    assert meta.model_name == "claude-3-7-sonnet"
    assert meta.active_branch == "main"
