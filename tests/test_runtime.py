"""Tests for unloop runtime interceptor, trace decorator, and breakpoints."""

import asyncio
import tempfile
from pathlib import Path

import pytest

import unloop
from unloop.runtime.interceptor import BreakpointHalt, get_current_session


def test_session_tracing_and_breakpoints() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_session.unloop"

        with unloop.session("test_agent", db_path=db_path, raise_on_breakpoint=True) as dbg:
            dbg.add_breakpoint(on_error=True)

            # Turn 1: normal execution
            with dbg.step(prompt="Analyze system logs", state={"status": "init", "count": 10}) as step:
                step.record_tool("fetch_logs", arguments={"service": "auth"}, result="OK", duration_ms=12.5)
                step.set_response("Logs retrieved cleanly.")
                step.set_telemetry(prompt_tokens=100, completion_tokens=50, cost_usd=0.001)

            # Turn 2: triggers breakpoint on error
            with pytest.raises(BreakpointHalt) as exc_info:
                with dbg.step(prompt="Step 2", state={"status": "querying", "count": 15}) as step:
                    step.record_tool("remote_api", arguments={"url": "http://bad"}, error="404 Not Found")
                    step.set_response("Failed to query.")

            assert "404 Not Found" in str(exc_info.value)
            assert exc_info.value.turn.is_breakpoint is True


def test_session_rewind_and_branch_forking() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "rewind_test.unloop"

        with unloop.session("rewind_agent", db_path=db_path) as dbg:
            # Turn 0
            with dbg.step(prompt="Turn 0", state={"step": 0}) as step:
                step.set_response("Done 0")

            t0_id = dbg.current_turn_id
            assert t0_id is not None

            # Turn 1
            with dbg.step(prompt="Turn 1", state={"step": 1}) as step:
                step.set_response("Done 1")

            # Rewind to Turn 0 and branch
            forked_turn = dbg.rewind_to(t0_id, new_branch_name="alt_branch")
            assert forked_turn.turn_index == 0
            assert dbg.active_branch == "alt_branch"

            # Execute Turn 1 on alt_branch
            with dbg.step(prompt="Alt Turn 1", state={"step": 100}) as step:
                step.set_response("Done Alt 1")

            alt_turns = dbg.store.get_history(dbg.session_id, branch_id="alt_branch")
            assert len(alt_turns) == 1
            assert alt_turns[0].state["step"] == 100


def test_trace_decorator_sync() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "trace_sync.unloop"

        with unloop.session("traced_agent", db_path=db_path) as dbg:
            assert get_current_session() is dbg

            @unloop.trace()
            def agent_action(prompt: str, state: dict) -> str:
                return f"Processed: {prompt} with {state['key']}"

            res = agent_action(prompt="Summarize findings", state={"key": "val123"})
            assert res == "Processed: Summarize findings with val123"

            history = dbg.store.get_history(dbg.session_id)
            assert len(history) == 1
            turn = history[0]
            assert turn.prompt == "Summarize findings"
            assert turn.response == "Processed: Summarize findings with val123"
            assert turn.state["key"] == "val123"
            assert turn.metadata["traced_func"] == "agent_action"


def test_trace_decorator_async() -> None:
    async def _async_test() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trace_async.unloop"

            with unloop.session("async_agent", db_path=db_path) as dbg:

                @unloop.trace()
                async def async_agent_turn(prompt: str, state: dict) -> dict:
                    return {"result": f"Async response to {prompt}", "status": "ok"}

                out = await async_agent_turn(prompt="Async query", state={"env": "test"})
                assert out["status"] == "ok"

                history = dbg.store.get_history(dbg.session_id)
                assert len(history) == 1
                turn = history[0]
                assert turn.prompt == "Async query"
                assert "Async response to Async query" in (turn.response or "")
                assert turn.metadata["traced_func"] == "async_agent_turn"

    asyncio.run(_async_test())


def test_trace_decorator_catches_exception() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "trace_err.unloop"

        with unloop.session("err_agent", db_path=db_path) as dbg:

            @unloop.trace()
            def failing_step(prompt: str, state: dict) -> str:
                raise RuntimeError("LLM rate limit reached")

            with pytest.raises(RuntimeError) as exc:
                failing_step(prompt="Trigger error", state={"attempt": 1})

            assert "rate limit reached" in str(exc.value)

            history = dbg.store.get_history(dbg.session_id)
            assert len(history) == 1
            assert "rate limit reached" in history[0].metadata.get("error", "")
