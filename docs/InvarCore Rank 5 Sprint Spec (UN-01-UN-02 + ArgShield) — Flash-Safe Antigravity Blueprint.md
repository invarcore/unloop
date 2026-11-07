# InvarCore Rank 5 Sprint Master Spec (UN-01 / UN-02 + ArgShield)

Architecture & Verification Blueprint — Flash-Hardened Edition for Antigravity IDE  
**Status: IMPLEMENTED, HARDENED & VERIFIED (100% GREEN GITHUB CI)**  
**Landed Commits**: `3ed7f3c` (Initial modules) -> `b15302b` (Hardening) -> `829d08d` (Cross-platform device defense)  
**Target Repository**: `invarcore/unloop` (`main`)  
**CI Workflow**: GitHub Actions Run `42397179384` (Ubuntu / macOS / Windows across Python 3.10, 3.11, 3.12, 3.13)  

---

## 0. SYSTEM INSTRUCTIONS FOR ANTIGRAVITY IDE (GOOGLE FLASH GUARDRAILS)

1. **Explicit Path Parameter Validation:** Never pass arbitrary text or file contents into `verify_path_in_jail()`. Restrict path jailing strictly to explicit path keys (`path`, `filepath`, `filename`, `dir`, `directory`, `cwd`, `worktree`, `repo_path`) or dynamically learned schema properties.  
2. **Targeted CLI Flag Injection DFA (CVE-2026-85626):** Never apply a global leading-dash ban on all strings. Block leading dashes strictly on high-risk banned flag prefixes (`--output=`, `--upload-pack=`, `--exec=`, `--config=`, `--git-dir=`), space-delimited variants (`--output <val>`), standalone flags, and positional parameters (`ref`, `object`, `revision`, `branch`, `tag`, `commit`).  
3. **Volatile Noise Normalization (UN-02):** Scrub runtime timestamps, UUIDs, hex pointers, and execution latencies via `_VOLATILE_NOISE_RE` and serialize via RFC 8785 before hashing state transitions.  
4. **First-Class STALE_REF Interception:** Trip `OscillationDetectedError(pattern="STALE_REF_DRIFT")` immediately upon consecutive `STALE_REF`, `@sha256:prefix`, `etag_mismatch`, or `conflict_detected` strikes.  
5. **Bidirectional Subprocess Pipe Pump (UN-01):** Provide `run_stdio_proxy_session()` to spawn child MCP servers, streaming stdin/stdout with `flush=True` and synthesizing JSON-RPC 2.0 error responses on `ArgShieldViolationError`.  
6. **Unicode NFC Normalization:** Normalize all strings via `unicodedata.normalize("NFC", val)` before hashing, path jailing, or policy evaluation.

---

## MODULE 1: ARGSHIELD (`src/unloop/argshield.py`) — 99% Coverage

- **Target Repository**: `invarcore/unloop`
- **Threat Model Addressed**: CVE-2026-85626 (git option injection), CVE-2026-77262, and OWASP ASI04.
- **Implementation Highlights**:
  - `verify_path_in_jail`: Strict `os.path.commonpath([str(jail), str(target)]) == str(jail)` containment. Defeats traversal (`../`), null-byte poisoning (`\x00`), and prefix-sibling escapes (`/tmp/jail_sibling` vs `/tmp/jail`).
  - `WIN_DEVICE_NAMES`: Cross-platform blocking of Windows DOS reserved devices (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`, `con.txt`).
  - `verify_no_flag_injection`: Three-way stem matching (`stem = banned.rstrip("=")`) catching equals-delimited (`--flag=val`), space-delimited (`--flag val`), and standalone (`--flag`) options.
  - `ArgShieldPolicy.from_tool_schema`: Wire-level dynamic inference of path properties from MCP `tools/list` JSON schemas.
  - `sanitize_tool_arguments`: Enforces Guardrail 1 by confining path validation strictly to `strict_path_keys`.

---

## MODULE 2: SEMANTIC ANTI-OSCILLATION WATCHDOG (`src/unloop/semantic_watchdog.py`) — 92% Coverage

- **Target Repository**: `invarcore/unloop`
- **Threat Model Addressed**: UN-02 and STALE_REF TOCTOU drift in headless CI/benchmark environments.
- **Implementation Highlights**:
  - `_VOLATILE_NOISE_RE`: Verbatim scrubbing of UUIDs, `0x` hex addresses, ISO 8601 timestamps, and duration/latency figures.
  - `compute_semantic_turn_digest`: RFC 8785 canonical JSON sorting (`json.dumps(..., sort_keys=True, separators=(',', ':'))`).
  - Multi-Cycle Detection:
    - `STALE_REF_DRIFT`: Trips on 2 consecutive optimistic concurrency or git conflict strikes.
    - `CONSECUTIVE_REPEAT`: Trips on immediate identical repeats ($A \to A$).
    - `ALTERNATING_CYCLE`: Trips on 2-cycle alternating ping-pongs ($A \to B \to A \to B$).

---

## MODULE 3: TRANSPARENT STDIO RECORDER & PUMP (`src/unloop/stdio_recorder.py`) — 92% Coverage

- **Target Repository**: `invarcore/unloop`
- **Threat Model Addressed**: UN-01 (Bare subprocesses exit unrecorded unless explicitly instrumented).
- **Implementation Highlights**:
  - SQLite WAL Mode Pragmas: Executed verbatim (`PRAGMA journal_mode = WAL;`, `PRAGMA synchronous = NORMAL;`, `PRAGMA busy_timeout = 5000;`).
  - In-Flight JSON-RPC Synthesis: Intercepts `tools/call`. On `ArgShieldViolationError`, synthesizes a standard `-32602` error response without crashing the client agent.
  - Wire Schema Learning: Automatically captures `tools/list` responses from the child server and enriches `ArgShieldPolicy` with newly discovered path properties.
  - `run_stdio_proxy_session`: Line-buffered bidirectional streaming pump with `flush=True` and timeout protection.

---

## MODULE 4: TEST SUITE & VERIFICATION (`tests/test_rank5_stdio_and_argshield.py`)

- **31 Dedicated Unit Tests**: Covering path traversal, null bytes, prefix-sibling escape, space-delimited flag injection, Windows DOS devices, volatile noise invariance, NFC normalization, STALE_REF strikes, identical repeats, alternating cycles, SQLite WAL pragmas, JSON-RPC error synthesis, proxy piping, and wire-level schema learning.
- **Full Repository Suite**: **95 passed, 0 failures, 95.01% total coverage**.
- **Linter**: `ruff check src/ tests/` $\to$ **All checks passed!**
