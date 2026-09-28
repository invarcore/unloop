"""Demonstration of unloop with a native Python agent loop.

Simulates an agent that gets stuck in a repetitive critique loop,
triggers the unloop anti-oscillation watchdog, rewinds to a previous turn,
mutates state, and completes execution successfully on a recovery branch.
"""

import sys
import unloop

if sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def mock_tool_call(tool_name: str, query: str):
    """Simulates a tool that repeatedly returns incomplete data."""
    if query == "broken_query":
        return {"status": "retry_needed", "data": None}
    return {"status": "success", "data": f"Resolved answers for '{query}'"}


def run_agent_demonstration():
    print("=== Starting unloop Native Loop Demonstration ===")
    db_file = "demo_agent.unloop"

    with unloop.session("research_assistant", db_path=db_file) as dbg:
        # Breakpoint on error or watchdog alerts
        dbg.add_breakpoint(on_error=True)

        memory = {
            "query": "broken_query",
            "iterations": 0,
            "status": "in_progress",
        }

        # Step 1: Initial planning turn
        with dbg.step(prompt="Research user question", state=memory) as step:
            step.set_response("Planning search queries.")
            step.set_telemetry(prompt_tokens=150, completion_tokens=40)
            memory["iterations"] += 1

        checkpoint_turn_id = dbg.current_turn_id
        print(f"[*] Checkpoint saved at Turn #{step.turn_index} (ID: {checkpoint_turn_id[:8]})")

        # Step 2-4: Agent enters a repetitive critique death loop
        loop_tripped_at = None
        for i in range(3):
            with dbg.step(prompt=f"Search attempt {i+1}", state=memory) as step:
                # Tool invocation with identical argument
                res = mock_tool_call("web_search", memory["query"])
                step.record_tool("web_search", arguments={"query": memory["query"]}, result=res)
                step.set_response(f"Attempt {i+1} failed; retrying with same query.")
                memory["iterations"] += 1

                if step.snapshot.is_breakpoint:
                    print(f"\n[BREAKPOINT HIT] {step.snapshot.breakpoint_reason}")
                    loop_tripped_at = step.snapshot.turn_id
                    break

        # Time-Travel Rewind & In-Place State Mutation
        print("\n--- Performing Time-Travel Rewind ---")
        assert checkpoint_turn_id is not None
        rewound = dbg.rewind_to(checkpoint_turn_id, new_branch_name="recovery_fix")
        print(f"[REWIND] Back to Turn #{rewound.turn_index} on branch '{dbg.active_branch}'")

        # Mutate the flawed memory query
        memory["query"] = "corrected_query"
        print(f"[MUTATE] memory['query'] changed to '{memory['query']}'")

        # Step on recovery branch: successful execution
        with dbg.step(prompt="Resume with fixed query", state=memory) as step:
            res = mock_tool_call("web_search", memory["query"])
            step.record_tool("web_search", arguments={"query": memory["query"]}, result=res)
            step.set_response("Query succeeded! Synthesizing final answer.")
            memory["status"] = "completed"
            memory["result"] = res["data"]
            step.set_telemetry(prompt_tokens=180, completion_tokens=95)

        print(f"\n[SUCCESS] Agent successfully completed on branch '{dbg.active_branch}'")
        print(f"Final state: {memory}")

    print(f"\n[TRACE SAVED] Inspect session with: unloop info {db_file}")
    print(f"              Inspect history with: unloop history {db_file}")


if __name__ == "__main__":
    run_agent_demonstration()
