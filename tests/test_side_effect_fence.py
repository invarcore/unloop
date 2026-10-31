"""Comprehensive tests for Egress Side-Effect Ledger and Replay Fence in unloop."""

import tempfile
from pathlib import Path

import pytest

import unloop
from unloop.protocol.models import (
    ToolInvocationRecord,
    TurnSnapshot,
    is_side_effect_tool_name,
)
from unloop.runtime.interceptor import BreakpointHalt


def test_is_side_effect_tool_name_heuristic() -> None:
    # Mutating / egress tools
    assert is_side_effect_tool_name("send_email") is True
    assert is_side_effect_tool_name("write_file") is True
    assert is_side_effect_tool_name("post_webhook") is True
    assert is_side_effect_tool_name("delete_account") is True
    assert is_side_effect_tool_name("execute_sql") is True
    assert is_side_effect_tool_name("create_user") is True
    assert is_side_effect_tool_name("publish_message") is True
    assert is_side_effect_tool_name("notify_slack") is True
    assert is_side_effect_tool_name("custom-post-handler") is True

    # Read-only tools
    assert is_side_effect_tool_name("read_file") is False
    assert is_side_effect_tool_name("search_google") is False
    assert is_side_effect_tool_name("get_user") is False
    assert is_side_effect_tool_name("list_buckets") is False
    assert is_side_effect_tool_name("query_metrics") is False


def test_tool_invocation_side_effect_key() -> None:
    # Auto-generated side effect key using tool_name + argument_hash
    tool1 = ToolInvocationRecord(
        tool_name="send_email",
        arguments={"to": "dev@invarcore.com", "subject": "Test"},
        is_side_effect=True,
    )
    tool2 = ToolInvocationRecord(
        tool_name="send_email",
        arguments={"subject": "Test", "to": "dev@invarcore.com"},
        is_side_effect=True,
    )
    tool3 = ToolInvocationRecord(
        tool_name="send_email",
        arguments={"to": "other@invarcore.com"},
        is_side_effect=True,
    )

    assert tool1.side_effect_key == tool2.side_effect_key
    assert tool1.side_effect_key != tool3.side_effect_key

    # Explicit idempotency key
    tool_custom = ToolInvocationRecord(
        tool_name="post_transaction",
        arguments={"amount": 100},
        is_side_effect=True,
        idempotency_key="tx_order_9981",
    )
    assert tool_custom.side_effect_key == "tx_order_9981"


def test_turn_snapshot_side_effect_invocations_property() -> None:
    snap = TurnSnapshot(
        session_id="s1",
        turn_index=0,
        tool_invocations=[
            ToolInvocationRecord(tool_name="read_file", arguments={}, is_side_effect=False),
            ToolInvocationRecord(tool_name="write_file", arguments={"path": "a.txt"}, is_side_effect=True),
            ToolInvocationRecord(tool_name="search", arguments={}, is_side_effect=False),
            ToolInvocationRecord(tool_name="send_email", arguments={"to": "a@b.com"}, is_side_effect=True),
        ],
    )
    side_effects = snap.side_effect_invocations
    assert len(side_effects) == 2
    assert [t.tool_name for t in side_effects] == ["write_file", "send_email"]


def test_replay_fence_policy_warn() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fence_warn.unloop"

        with unloop.session("fence_agent_warn", db_path=db_path, side_effect_policy="warn") as dbg:
            # Turn 0: plan
            with dbg.step(prompt="Plan deployment") as s0:
                s0.set_response("Ready to deploy")

            t0_id = dbg.current_turn_id
            assert t0_id is not None

            # Turn 1: execute side-effect in real world
            with dbg.step(prompt="Dispatch webhook") as s1:
                rec1 = s1.record_tool("post_webhook", arguments={"url": "https://api.com/deploy"}, result={"ok": True})
                assert rec1.is_side_effect is True
                assert "fenced_replay" not in rec1.metadata

            # Turn 2: agent enters loop or fails
            with dbg.step(prompt="Verify deployment") as s2:
                s2.set_response("Verification crashed")

            # Rewind session to Turn 0 and fork new branch
            dbg.rewind_to(t0_id, new_branch_name="recovery_fork")
            assert dbg.active_branch == "recovery_fork"
            assert len(dbg.abandoned_side_effects) == 1

            # Check is_fenced helper before execution
            with dbg.step(prompt="Forked turn") as s_fork:
                is_fenced, cached = s_fork.is_fenced("post_webhook", arguments={"url": "https://api.com/deploy"})
                assert is_fenced is True
                assert cached is not None

                # Replay side-effect tool under "warn" policy
                rec_replayed = s_fork.record_tool("post_webhook", arguments={"url": "https://api.com/deploy"}, result={"ok": True})
                assert rec_replayed.metadata.get("fenced_replay") is True
                assert "Duplicate side effect" in rec_replayed.metadata.get("replay_fence_warning", "")


def test_replay_fence_policy_stub() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fence_stub.unloop"

        with unloop.session("fence_agent_stub", db_path=db_path, side_effect_policy="stub") as dbg:
            # Turn 0
            with dbg.step(prompt="Start transaction") as s0:
                s0.set_response("Starting")

            t0_id = dbg.current_turn_id
            assert t0_id is not None

            # Turn 1: mutating call
            with dbg.step(prompt="Charge card") as s1:
                s1.record_tool(
                    "charge_customer",
                    arguments={"user_id": 42, "cents": 5000},
                    is_side_effect=True,
                    result={"transaction_id": "ch_12345", "status": "succeeded"},
                )

            # Rewind to Turn 0
            dbg.rewind_to(t0_id, new_branch_name="safe_fork")

            # Replaying on forked branch: tool result should be auto-stubbed from cache
            with dbg.step(prompt="Replay charge attempt") as s_replay:
                rec_stubbed = s_replay.record_tool(
                    "charge_customer",
                    arguments={"user_id": 42, "cents": 5000},
                    is_side_effect=True,
                    # Result not provided (runtime intercepted call before hitting Stripe)
                    result=None,
                )
                assert rec_stubbed.result == {"transaction_id": "ch_12345", "status": "succeeded"}
                assert rec_stubbed.metadata.get("fenced_replay_stubbed") is True


def test_replay_fence_policy_halt() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fence_halt.unloop"

        with unloop.session("fence_agent_halt", db_path=db_path, side_effect_policy="halt") as dbg:
            # Turn 0
            with dbg.step(prompt="Initialize") as s0:
                s0.set_response("Init")

            t0_id = dbg.current_turn_id
            assert t0_id is not None

            # Turn 1: side effect
            with dbg.step(prompt="Send email notification") as s1:
                s1.record_tool("send_email", arguments={"to": "admin@example.com", "msg": "ALERT"})

            # Rewind to Turn 0
            dbg.rewind_to(t0_id, new_branch_name="halt_fork")

            # Replaying side effect on forked branch under "halt" policy should raise BreakpointHalt
            with pytest.raises(BreakpointHalt) as exc_info:
                with dbg.step(prompt="Attempt re-sending email") as s_halt:
                    s_halt.record_tool("send_email", arguments={"to": "admin@example.com", "msg": "ALERT"})

            assert "Replay Fence tripped" in str(exc_info.value)
            assert "send_email" in str(exc_info.value)


def test_replay_fence_policy_switch_on_rewind() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fence_switch.unloop"

        # Session starts with default "warn" policy
        with unloop.session("fence_agent_switch", db_path=db_path) as dbg:
            with dbg.step(prompt="Turn 0") as s0:
                s0.set_response("OK")

            t0_id = dbg.current_turn_id
            assert t0_id is not None

            with dbg.step(prompt="Turn 1: write") as s1:
                s1.record_tool("write_file", arguments={"file": "output.txt", "data": "123"})

            # Dynamically switch policy to "halt" during rewind
            dbg.rewind_to(t0_id, new_branch_name="strict_fork", side_effect_policy="halt")
            assert dbg.side_effect_policy == "halt"

            # Replay should now raise BreakpointHalt
            with pytest.raises(BreakpointHalt):
                with dbg.step(prompt="Replay write") as s_strict:
                    s_strict.record_tool("write_file", arguments={"file": "output.txt", "data": "123"})
