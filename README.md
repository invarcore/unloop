# agdb (Agent GNU Debugger) ⏱️🔍

[![CI](https://github.com/sagarv48/agdb/actions/workflows/ci.yml/badge.svg)](https://github.com/sagarv48/agdb/actions)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> **Terminal time-travel stepper, anti-oscillation watchdog, and state mutation engine for AI agents.**

Stop restarting from scratch when your AI agent derails on turn 8. **Pause mid-execution, inspect and mutate state, and rewind cognitive turns like a video player.**

---

## ⚡ The Problem: The Agent "Restart-From-Scratch" Tax

Debugging multi-turn AI agents today feels like batch programming in 1975:
1. Your agent takes 8 sequential turns towards a goal ($1.50 in API cost, 3 minutes elapsed).
2. On turn 9, a tool call returns unexpected JSON; the agent enters a circular critique loop or hallucinates.
3. You hit `Ctrl+C`, tweak the prompt or code, and **rerun from step 1**—burning more money and waiting all over again.

Existing observability platforms (LangSmith, Arize, Phoenix) are **post-facto, read-only web viewers**. They tell you *why* an agent failed after you already spent money.

**`agdb` is the interactive, terminal-native debugger**:
- 🛑 **Semantic Breakpoints**: Pause on tool errors, watchdog alerts, or custom state conditions.
- 🔄 **Time-Travel Rewind**: Step backward $N$ turns without re-running expensive LLM calls.
- ✏️ **In-Place State Mutation**: Edit corrupted memory or bad tool returns in-flight.
- 🌿 **DAG Branch Forking**: Experiment with alternative directions from any past turn.
- 🐕 **Anti-Oscillation Watchdog**: Automatically catches repetitive tool loops and state ping-pongs before token limits burn out.
- 📦 **Single-File Cassettes**: Portable SQLite WAL (`.agdb`) storage for zero-cost deterministic replay in CI.

---

## 🚀 60-Second Quickstart

### 1. Installation

```bash
pip install agdb
# or with uv
uv tool install agdb
```

### 2. Instrument Any Python Agent Loop

```python
import agdb

# Wrap your agent loop with agdb session tracking
with agdb.session("researcher_agent", db_path="run.agdb") as dbg:
    # Set breakpoints on tool errors or specific tools
    dbg.add_breakpoint(on_error=True)
    dbg.add_breakpoint(tool_name="delete_database")

    for turn_idx in range(10):
        with dbg.step(prompt="Analyze report", state=agent.memory) as step:
            # 1. Run LLM reasoning
            action, args = agent.decide()
            step.set_response(f"Decided to call {action}")

            # 2. Execute & trace tool call
            result = run_tool(action, args)
            step.record_tool(action, arguments=args, result=result)

            # 3. Update agent state
            agent.memory["last_tool"] = action
```

### 3. Inspect & Post-Mortem in Terminal

```bash
# Print summary of the session
agdb info run.agdb

# Inspect execution timeline with colorized state diffs
agdb history run.agdb

# Export post-mortem incident report for team review
agdb export run.agdb -o post_mortem.md

# Launch 3-pane interactive terminal TUI
agdb replay run.agdb
```

---

## 🏗️ Architecture & Core Components

```
┌────────────────────────────────────────────────────────┐
│               agdb System Architecture                 │
└────────────────────────────────────────────────────────┘

  [ AGENT APPLICATION ] (Custom Loops / LangGraph / MCP)
            │
            ▼  (@agdb.trace / with dbg.step())
  ┌────────────────────────────────────────────────────┐
  │ 1. Interception & Watchdog Engine                  │
  │    • Anti-Oscillation Watchdog (Loop detector)     │
  │    • Breakpoint Evaluator (Errors & Conditions)    │
  └─────────────────────────┬──────────────────────────┘
                            │
                            ▼
  ┌────────────────────────────────────────────────────┐
  │ 2. Canonical Protocol & Models                     │
  │    • TurnSnapshot (turn_id, parent_id, branch_id)  │
  │    • Cognitive & Memory Deltas                     │
  └─────────────────────────┬──────────────────────────┘
                            │
                            ▼
  ┌────────────────────────────────────────────────────┐
  │ 3. Storage & DAG Branching Engine                  │
  │    • Append-only SQLite WAL (.agdb file)           │
  │    • Microsecond commit latency (<2ms overhead)    │
  │    • Git-like branch forking & rewind              │
  └─────────────────────────┬──────────────────────────┘
                            │
             ┌──────────────┴──────────────┐
             ▼                             ▼
  ┌───────────────────────┐ ┌──────────────────────────┐
  │ 4. Interactive TUI    │ │ 5. Headless CI Harness   │
  │    (Textual 3-Pane)   │ │    (agdb CLI)            │
  └───────────────────────┘ └──────────────────────────┘
```

---

## 🛠️ Testing & Development

```bash
# Install with dev dependencies
uv pip install -e ".[dev]"

# Run comprehensive test suite
pytest -v
```

---

## 📄 License

MIT © [Vinay Kumar Ksheera Sagar (sagarv48)](https://github.com/sagarv48)
