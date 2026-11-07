# **InvarCore Rank 5 Sprint Master Spec (UN-01 / UN-02 \+ ArgShield)**

Architecture & Verification Blueprint — Flash-Hardened Edition for Antigravity IDE

## **0\. SYSTEM INSTRUCTIONS FOR ANTIGRAVITY IDE (GOOGLE FLASH GUARDRAILS)**

1. **1\. Explicit Path Parameter Validation:** Never pass arbitrary text or file contents into verify\_path\_in\_jail(). Restrict path jailing strictly to explicit path keys (path, filepath, filename, dir, directory, cwd, worktree, repo\_path).  
2. **2\. Targeted CLI Flag Injection DFA (CVE-2026-85626):** Never apply a global leading-dash ban on all strings. Block leading dashes strictly on high-risk banned flag prefixes (--output=, \--upload-pack=, \--exec=, \--config=, \--git-dir=) and positional parameters.  
3. **3\. Volatile Noise Normalization (UN-02):** Always scrub runtime timestamps, UUIDs, and hex pointers via \_VOLATILE\_NOISE\_RE and serialize via RFC 8785 before hashing state transitions.  
4. **4\. First-Class STALE\_REF Interception:** Trip OscillationDetectedError immediately upon consecutive STALE\_REF or @sha256\_prefix strikes.  
5. **5\. Bidirectional Subprocess Pipe Pump (UN-01):** Provide run\_stdio\_proxy\_session() to spawn child MCP servers, streaming stdin/stdout with flush=True and synthesizing JSON-RPC 2.0 error responses on ArgShieldViolationError.  
6. **6\. Unicode NFC Normalization:** Normalize all strings via unicodedata.normalize("NFC", val) before hashing or policy evaluation.

## **MODULE 1: ARGSHIELD (src/unloop/argshield.py)**

Target Repository: invarcore/unloop  
Addresses CVE-2026-85626, CVE-2026-77262, and OWASP ASI04.  
\[Implementation: ArgShieldPolicy, verify\_path\_in\_jail with commonpath containment, verify\_no\_flag\_injection, and sanitize\_tool\_arguments restricting path validation strictly to strict path keys\].

## **MODULE 2: SEMANTIC ANTI-OSCILLATION WATCHDOG (src/unloop/semantic\_watchdog.py)**

Target Repository: invarcore/unloop  
Addresses UN-02 and STALE\_REF TOCTOU drift.  
\[Implementation: \_VOLATILE\_NOISE\_RE scrubbing, \_STALE\_REF\_RE detection, compute\_semantic\_turn\_digest using RFC 8785, and SemanticOscillationWatchdog tracking identical repeats (A-\>A) and 2-cycle alternating loops (A-\>B-\>A-\>B)\].

## **MODULE 3: TRANSPARENT STDIO RECORDER & PUMP (src/unloop/stdio\_recorder.py)**

Target Repository: invarcore/unloop  
Addresses UN-01.  
\[Implementation: StdioSessionRecorder with SQLite WAL mode, intercept\_client\_line enforcing ArgShield and returning JSON-RPC error responses on security violations, intercept\_server\_line feeding the SemanticOscillationWatchdog, and run\_stdio\_proxy\_session for line-buffered stdio pumping\].

## **MODULE 4: TEST SUITE & VERIFICATION (tests/test\_rank5\_stdio\_and\_argshield.py)**

\[Implementation: pytest suite covering CVE-2026-85626 git option injection, PathJail traversal and prefix-sibling escape defense, volatile noise scrubbing in semantic loops, STALE\_REF strikes, and StdioSessionRecorder WAL frame recording with JSON-RPC error interception\].