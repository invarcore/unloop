"""Tests for agdb protocol models."""

from agdb.protocol.models import SessionMetadata, ToolInvocationRecord, TurnSnapshot


def test_tool_invocation_argument_hash():
    tool1 = ToolInvocationRecord(tool_name="search", arguments={"q": "rag", "limit": 5})
    tool2 = ToolInvocationRecord(tool_name="search", arguments={"limit": 5, "q": "rag"})
    tool3 = ToolInvocationRecord(tool_name="search", arguments={"q": "different"})

    assert tool1.argument_hash == tool2.argument_hash
    assert tool1.argument_hash != tool3.argument_hash


def test_turn_snapshot_state_delta():
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
