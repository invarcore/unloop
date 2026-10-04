<p align="center">
  <img src="./assets/logo.png" alt="unloop logo" width="180px" style="border-radius: 24px; box-shadow: 0 8px 32px rgba(0, 240, 255, 0.25);" />
</p>

# unloop

<p align="center">
  <strong>The Time-Travel Debugger for AI Agents.</strong><br>
  Break out of critique death loops &bull; Rewind cognitive turns like a video player &bull; Mutate corrupted state in-flight &bull; Fork alternative branches &bull; Terminal TUI & CI Replay Cassettes
</p>

<p align="center">
  <a href="https://github.com/invarcore/unloop/actions"><img src="https://github.com/invarcore/unloop/actions/workflows/ci.yml/badge.svg" alt="CI Status"></a>
  <a href="https://invarcore.com"><img src="https://img.shields.io/badge/Website-invarcore.com-0284C7?logo=googlechrome&logoColor=white" alt="Invarcore Website"></a>
  <a href="https://pypi.org/project/unloop/"><img src="https://img.shields.io/badge/PyPI-v0.1.0-blue.svg" alt="PyPI Version"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-3776AB.svg?logo=python&logoColor=white" alt="Python Versions"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License"></a>
  <a href="https://github.com/invarcore/unloop"><img src="https://img.shields.io/badge/Storage-SQLite%20WAL%20(%3C2ms)-orange.svg" alt="Storage Overhead"></a>
</p>

---

## ⚡ The Problem: The Agent "Restart-From-Scratch" Tax

Debugging multi-turn AI agents today feels like programming with 1970s punch-cards:
1. Your agent takes 8 sequential cognitive turns toward a complex goal ($1.50 in tokens, 3 minutes elapsed).
2. On turn 9, a tool returns unexpected JSON or a subtle schema shift; the agent enters a circular critique loop or hallucinates.
3. You hit `Ctrl+C`, patch a prompt, and **rerun from step 1**—burning more money and waiting all over again.

Existing observability platforms (LangSmith, Arize Phoenix, AgentOps) are **post-facto, read-only web dashboards**. They tell you *why* your agent burned $50 after the money is already gone.

**`unloop` is the interactive, terminal-native time-travel debugger**:

| Scenario | Without `unloop` | With `unloop` |
| :--- | :--- | :--- |
| **Agent enters a circular retry loop** | Kill with `Ctrl+C`, lose entire session, rerun from step 1 ($$$ & time wasted). | Hit pause, step back 2 turns, patch the prompt/state, resume seamlessly. |
| **Agent made a bad edit 3 steps ago** | Manually clean up git commits, wipe agent history, re-prompt from scratch. | Select Turn #3 in TUI, hit **[Fork]**, try an alternate prompt path. |
| **Watchdog over runaway token costs** | Discover the loop only after hitting an API quota or budget alert. | Automatic anti-oscillation watchdog freezes execution on turn 3 of a loop. |
| **Debugging team agent failures in CI** | Sifting through raw terminal logs and 30,000-line JSON dumps. | Interactive single-file replay cassette (`.unloop`) with timeline TUI. |

---

## 🛠️ Real-World Usage Scenarios

`unloop` supports multiple ways to debug and trace agents in production and daily developer workflows:

### 1. Terminal Agent Harness (`unloop wrap`)
Wrap existing terminal agents (such as **Claude Code**, **GitHub Copilot CLI**, or **Aider**) without changing a single line of their code:

```bash
# Wrap any CLI agent with unloop flight-recording
unloop wrap -- claude

# Or with Copilot / Aider
unloop wrap -- aider --model sonnet
```

- **Silent Flight Recording**: Every prompt, tool invocation (`git diff`, `npm test`, file writes), and return value is streamed into a local `.unloop` cassette.
- **Break Loop Trigger**: When an agent gets stuck in a loop, press `Alt+U` (or let the automatic watchdog trigger) to drop into the interactive TUI, rewind turns, edit state, and resume.

---

### 2. MCP Proxy Middleware (Model Context Protocol)
Use `unloop` as a transparent Model Context Protocol (MCP) gateway for tools like **Claude Code** and **Copilot Workspace**:

```bash
# Add unloop MCP proxy to your agent configuration
claude mcp add unloop-proxy -- "unloop-mcp"
```

- **Policy Enforcement**: Intercept destructive commands (e.g., `rm -rf` or unapproved schema migrations) before execution.
- **Automatic Checkpointing**: Micro-snapshots taken before every tool execution allow instantaneous rollback of both agent memory and file system state.

---

### 3. Python Agent Loop SDK
Instrument your custom agent loops, **LangGraph**, or **CrewAI** pipelines with minimal Python code:

#### Option A: Zero-Boilerplate `@unloop.trace` Decorator
Wrap agent turn functions directly (supports both sync and async):

```python
import unloop

# Automatically records prompt, memory deltas, durations, and returned reasoning
@unloop.trace()
def run_agent_turn(prompt: str, state: dict) -> str:
    response = call_llm(prompt, state)
    state["last_thought"] = response
    return response

# Or asynchronously:
@unloop.trace()
async def async_agent_turn(prompt: str, state: dict) -> dict:
    return await execute_agent_pipeline(prompt, state)
```

#### Option B: Context Manager with Breakpoints & Rewind
Use fine-grained control for complex multi-tool loops:

```python
import unloop

with unloop.session("research_assistant", db_path="run.unloop") as dbg:
    # Set breakpoints on tool errors or specific dangerous tools
    dbg.add_breakpoint(on_error=True)
    dbg.add_breakpoint(tool_name="delete_database")

    for turn_idx in range(10):
        with dbg.step(prompt="Analyze research report", state=agent.memory) as step:
            action, args = agent.decide()
            step.set_response(f"Decided to invoke {action}")

            result = run_tool(action, args)
            step.record_tool(action, arguments=args, result=result)
            agent.memory["last_action"] = action

        # Time-travel rewind to prior turn if stuck
        if dbg.current_turn_id and agent.is_stuck():
            dbg.rewind_to(checkpoint_turn_id, new_branch_name="recovery")
```

---

### 4. CI/CD Flight Recorder & Post-Mortem Cassettes
Run headless agent benchmarks, automated PR bots, and evaluations with zero overhead:

```yaml
# In your GitHub Actions workflow
- name: Run Autonomous Fixer Agent (Headless Flight Recording)
  run: |
    unloop record -o ./failure.unloop -- python run_agent.py "fix vuln-1049"

- name: Enforce Agent Quality Gate (Fail on loops, errors, or token blowups)
  run: |
    unloop check ./failure.unloop --max-turns 15 --no-oscillations --no-errors
```

When a CI agent fails:
1. Download the single-file `failure.unloop` artifact.
2. Run `unloop replay failure.unloop` locally.
3. Step through the exact mental state, prompt deltas, and tool calls the agent experienced in CI.

---

## 🖥️ Terminal TUI Interface

```text
┌── Execution Timeline DAG ──┐┌── Turn Inspector ───────────────┐┌── State Delta & Memory ─────┐
│                            ││                                 ││                             │
│ [main]                     ││ Turn ID:  a1a7ee52              ││ State Delta (vs Parent):    │
│ ├── Turn #0 [a1a7ee]       ││ Branch:   recovery_fix          ││  query: "broken" -> "fixed" │
│ ├── Turn #1 [f2c901] [!]   ││ Turn Index: #1                  ││  status: "init" -> "active" │
│ └── [recovery_fix]         ││                                 ││                             │
│    └── Turn #1 [e1fd91] OK ││ Prompt: Resume with fixed query ││ Current Memory State:       │
│                            ││ Response: Query resolved.       ││  {                          │
│                            ││                                 ││    "query": "fixed",        │
│                            ││ Tool Invocations:               ││    "status": "completed"    │
│                            ││   * web_search OK               ││  }                          │
└────────────────────────────┘└─────────────────────────────────┘└─────────────────────────────┘
Keys: [s] Next Turn | [u] Prev Turn | [f] Fork Branch | [m] Mutate State | [q] Quit
```

---

## 🚀 Quickstart & Commands

### Installation

```bash
pip install unloop
# or with uv
uv tool install unloop
```

### CLI Commands

```bash
# Headless flight recording runner (CI or local scripts)
unloop record -o run.unloop -- python agent.py

# CI Quality Gate assertion (assert loop-freedom, max turns, and zero tool errors)
unloop check run.unloop --max-turns 12 --no-oscillations --no-errors

# Side-by-side state and tool execution diff between two turns
unloop diff run.unloop --from 0 --to 1

# Display summary of recorded session metadata, tokens, and branches
unloop info run.unloop

# Inspect full turn execution timeline with colorized state diffs
unloop history run.unloop

# Export a clean, shareable Markdown post-mortem incident report
unloop export run.unloop -o post_mortem.md

# Launch 3-pane interactive terminal time-travel TUI
unloop replay run.unloop
```

---

## 🏛️ Architecture & Storage Engine

```text
┌─────────────────────────────────────────────────────────────┐
│                 unloop System Architecture                  │
└─────────────────────────────────────────────────────────────┘

  [ AGENT APPLICATION ] (Claude CLI / LangGraph / Native Python)
            │
            ▼  (@unloop.trace / unloop wrap / with dbg.step())
  ┌─────────────────────────────────────────────────────────┐
  │ 1. Interception & Watchdog Engine                       │
  │    • Anti-Oscillation Watchdog (Loop detector)          │
  │    • Breakpoint Evaluator (Errors & Conditions)         │
  └───────────────────────────┬─────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────┐
  │ 2. Canonical Protocol & Models                          │
  │    • TurnSnapshot (turn_id, parent_id, branch_id)       │
  │    • Cognitive & Memory Deltas                          │
  └───────────────────────────┬─────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────┐
  │ 3. Storage & DAG Branching Engine                       │
  │    • Append-only SQLite WAL (.unloop file)              │
  │    • Microsecond commit latency (<2ms overhead)         │
  │    • Git-like branch forking & rewind                   │
  └───────────────────────────┬─────────────────────────────┘
                              │
             ┌────────────────┴───────────────┐
             ▼                                ▼
  ┌───────────────────────┐        ┌────────────────────────┐
  │ 4. Interactive TUI    │        │ 5. Headless CI Harness │
  │    (Textual 3-Pane)   │        │    (unloop CLI)        │
  └───────────────────────┘        └────────────────────────┘
```

---

## 🧩 Framework Compatibility

| Framework / Tool | Integration Pattern | Status |
| :--- | :--- | :---: |
| **Claude Code (`claude`)** | Terminal wrapper & MCP Proxy | 🟢 Supported |
| **GitHub Copilot CLI** | Terminal wrapper & MCP Proxy | 🟢 Supported |
| **Native Python Loops** | Zero dependencies, `with dbg.step()` | 🟢 Supported |
| **LangGraph / LangChain** | Node checkpoint hook and state graph listener | 🟡 In Progress |
| **FastMCP / MCP Servers** | Wire-level tool interception middleware | 🟡 In Progress |
| **CrewAI / AutoGen** | Task execution listener and state serializer | 🔵 Planned |

---

## 🧪 Development & Testing

Unloop maintains strict test coverage (**>=90% enforced in CI**) and a zero-live-HTTP CI architecture:

- **Tier 1 (Golden Trajectory Fixtures)**: Real-world multi-turn developer debugging trajectories derived from SWE-bench (`encode__httpx-1422`) checked into `tests/fixtures/corpora/` for deterministic, sub-second oscillation and time-travel validation.
- **Tier 2 (Opt-in Live Harness)**: Live cloud agent tracing verification against OpenRouter (`benchmarks/live_debugger_smoke_test.py --openrouter`).

```bash
# Clone the repository
git clone https://github.com/invarcore/unloop.git
cd unloop

# Install with development dependencies using uv
uv sync --all-extras

# Run linter
uv run ruff check .

# Run test suite with strict coverage enforcement (>=90%)
uv run pytest --cov=unloop --cov-report=term-missing --cov-fail-under=90

# Run local hermetic end-to-end time-travel smoke test (<200ms)
uv run python benchmarks/live_debugger_smoke_test.py

# Optional: Run live tracing verification against OpenRouter Free Tier
uv run python benchmarks/live_debugger_smoke_test.py --openrouter --model openrouter/free

# Run hermetic containerized test suite via Docker Compose
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit

# Run interactive loop recovery demonstration
uv run python examples/01_native_loop_agent.py
```

---

---

## 🏛️ Invarcore Verification Fabric

This engine is part of the **[Invarcore](https://invarcore.com)** enterprise verification fabric. Invarcore develops mathematical invariants, cryptographic policy contracts, and execution runtimes for autonomous AI systems.

* **Official Website & Architecture**: [https://invarcore.com](https://invarcore.com)
* **Technical Whitepapers & Invariant Specs**: [https://invarcore.com/#whitepapers](https://invarcore.com/#whitepapers)
* **GitHub Organization**: [https://github.com/invarcore](https://github.com/invarcore)
* **Security & Vulnerability Disclosure**: [security@invarcore.com](mailto:security@invarcore.com)

## 📄 License

MIT © [Vinay Kumar Ksheera Sagar (sagarv48)](https://github.com/sagarv48)
