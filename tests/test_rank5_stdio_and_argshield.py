# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""Test suite for InvarCore Rank 5 Sprint (UN-01/UN-02 + ArgShield).

Verifies:
1. ArgShield path jailing via os.path.commonpath and prefix-sibling escape defense.
2. Targeted CLI flag injection defense against CVE-2026-85626 (no global dash ban).
3. Explicit path key restriction (Guardrail 1: non-path text never jailed).
4. Volatile noise scrubbing (UN-02) and RFC 8785 canonical hashing.
5. First-class STALE_REF TOCTOU drift detection (Guardrail 4).
6. Repetition (A->A) and 2-cycle alternating loop (A->B->A->B) detection.
7. Unicode NFC normalization across all inputs.
8. StdioSessionRecorder with SQLite WAL mode and JSON-RPC 2.0 error synthesis.
9. Bidirectional run_stdio_proxy_session pump with flush=True.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import unicodedata
from pathlib import Path

import pytest

from unloop.argshield import (
    BANNED_FLAG_PREFIXES,
    POSITIONAL_FLAG_PARAMS,
    STRICT_PATH_KEYS,
    ArgShieldPolicy,
    ArgShieldViolationError,
    sanitize_tool_arguments,
    verify_no_flag_injection,
    verify_path_in_jail,
)
from unloop.semantic_watchdog import (
    _STALE_REF_RE,
    _VOLATILE_NOISE_RE,
    OscillationDetectedError,
    SemanticOscillationWatchdog,
    compute_semantic_turn_digest,
)
from unloop.stdio_recorder import (
    StdioSessionRecorder,
    run_stdio_proxy_session,
)

# ============================================================================
# MODULE 1: ARGSHIELD TESTS
# ============================================================================


def test_argshield_policy_dataclass(tmp_path: Path) -> None:
    jail = tmp_path / "custom_jail"
    policy = ArgShieldPolicy(jail_dir=jail)
    assert policy.jail_dir == jail
    assert "ref" in POSITIONAL_FLAG_PARAMS
    assert "--output=" in policy.banned_flag_prefixes


def test_verify_path_in_jail_valid_nested(tmp_path: Path) -> None:
    jail = tmp_path / "sandbox"
    jail.mkdir()
    subfile = jail / "src" / "index.ts"
    subfile.parent.mkdir()
    subfile.write_text("console.log('hello');", encoding="utf-8")

    # Relative path inside jail
    resolved = verify_path_in_jail("src/index.ts", jail)
    assert resolved == subfile.resolve()

    # Path object inside jail
    resolved_path_obj = verify_path_in_jail(Path("src/index.ts"), jail)
    assert resolved_path_obj == subfile.resolve()


def test_verify_path_in_jail_traversal_blocked(tmp_path: Path) -> None:
    jail = tmp_path / "sandbox"
    jail.mkdir()

    with pytest.raises(ArgShieldViolationError) as exc_info:
        verify_path_in_jail("../secret.txt", jail)
    assert exc_info.value.violation_type == "path_traversal"
    assert exc_info.value.field == "path"

    with pytest.raises(ArgShieldViolationError) as exc_info2:
        verify_path_in_jail("src/../../evil.sh", jail)
    assert exc_info2.value.violation_type == "path_traversal"


def test_verify_path_in_jail_prefix_sibling_escape_blocked(tmp_path: Path) -> None:
    """Prefix-sibling escape: /tmp/sandbox_sibling must not match /tmp/sandbox."""
    jail = tmp_path / "sandbox"
    jail.mkdir()
    sibling = tmp_path / "sandbox_sibling"
    sibling.mkdir()

    # Pass sibling path directly
    with pytest.raises(ArgShieldViolationError) as exc_info:
        verify_path_in_jail(str(sibling), jail)
    assert exc_info.value.violation_type == "path_traversal"


def test_verify_path_in_jail_null_byte_blocked(tmp_path: Path) -> None:
    jail = tmp_path / "sandbox"
    jail.mkdir()

    with pytest.raises(ArgShieldViolationError) as exc_info:
        verify_path_in_jail("src/main.py\x00.evil", jail)
    assert exc_info.value.violation_type == "null_byte"


def test_verify_no_flag_injection_banned_prefixes() -> None:
    for prefix in BANNED_FLAG_PREFIXES:
        payload = f"{prefix}malicious_value"
        with pytest.raises(ArgShieldViolationError) as exc_info:
            verify_no_flag_injection("any_arg", payload)
        assert exc_info.value.violation_type == "flag_injection"
        assert prefix in exc_info.value.message


def test_verify_no_flag_injection_positional_params_cve_2026_85626() -> None:
    """CVE-2026-85626: git ref/object parameter option injection."""
    for pos_key in ("ref", "object", "revision", "branch", "tag", "commit"):
        with pytest.raises(ArgShieldViolationError) as exc_info:
            verify_no_flag_injection(pos_key, "--upload-pack=id")
        assert exc_info.value.violation_type == "flag_injection"

        with pytest.raises(ArgShieldViolationError) as exc_info2:
            verify_no_flag_injection(pos_key, "-f")
        assert exc_info2.value.violation_type == "flag_injection"

    # Legitimate non-dash positional arguments must pass
    verify_no_flag_injection("ref", "main")
    verify_no_flag_injection("revision", "v1.2.3")
    verify_no_flag_injection("commit", "abc1234")


def test_no_global_dash_ban_on_non_positional_strings() -> None:
    """Guardrail 2: Never apply a global leading-dash ban on all strings."""
    # Commit messages, text bodies, or notes starting with dashes are valid
    verify_no_flag_injection("commit_message", "- Fixed regression in auth token parser")
    verify_no_flag_injection("body", "-- Documentation note about CLI flags")
    verify_no_flag_injection("query", "-foo +bar")


def test_sanitize_tool_arguments_restricts_path_jailing_strictly(tmp_path: Path) -> None:
    """Guardrail 1: Non-path keys (e.g. content) must never be passed to path jail."""
    jail = tmp_path / "jail"
    jail.mkdir()
    (jail / "valid.txt").write_text("data", encoding="utf-8")

    args = {
        "path": "valid.txt",
        "content": "Line with ../../../etc/passwd traversal text inside body",
        "description": "Text mentioning /var/log/syslog",
        "ref": "main",
    }

    sanitized = sanitize_tool_arguments("write_file", args, jail_dir=jail)
    assert sanitized["path"] == str((jail / "valid.txt").resolve())
    # Content must NOT be jailed or rejected because 'content' is not in STRICT_PATH_KEYS
    assert "../../../etc/passwd" in sanitized["content"]
    assert sanitized["description"] == "Text mentioning /var/log/syslog"


def test_sanitize_tool_arguments_catches_invalid_path_in_strict_keys(tmp_path: Path) -> None:
    jail = tmp_path / "jail"
    jail.mkdir()

    for key in STRICT_PATH_KEYS:
        args = {key: "../escape.txt"}
        with pytest.raises(ArgShieldViolationError) as exc_info:
            sanitize_tool_arguments("tool", args, jail_dir=jail)
        assert exc_info.value.violation_type == "path_traversal"


def test_sanitize_tool_arguments_nested_and_lists(tmp_path: Path) -> None:
    jail = tmp_path / "jail"
    jail.mkdir()
    (jail / "file.txt").write_text("ok", encoding="utf-8")

    args = {
        "meta": {"path": "file.txt", "notes": "- Note"},
        "targets": ["main", "feature"],
    }
    sanitized = sanitize_tool_arguments("batch", args, jail_dir=jail)
    assert sanitized["meta"]["path"] == str((jail / "file.txt").resolve())
    assert sanitized["meta"]["notes"] == "- Note"
    assert sanitized["targets"] == ["main", "feature"]

    # Nested list with banned flag
    bad_args = {
        "targets": ["main", "--output=/tmp/pwn"],
    }
    with pytest.raises(ArgShieldViolationError) as exc_info:
        sanitize_tool_arguments("batch", bad_args, jail_dir=jail)
    assert exc_info.value.violation_type == "flag_injection"


# ============================================================================
# MODULE 2: SEMANTIC WATCHDOG TESTS
# ============================================================================


def test_volatile_noise_regex() -> None:
    sample = (
        "Run 12345678-abcd-1234-abcd-123456789abc at 2026-11-07T12:34:56.789Z "
        "ptr 0x7ffd98a1 duration: 15.2ms latency='45ms' time=0.03s timestamp: 1729000000"
    )
    scrubbed = _VOLATILE_NOISE_RE.sub("<SCRUBBED>", sample)
    assert "12345678-abcd" not in scrubbed
    assert "2026-11-07" not in scrubbed
    assert "0x7ffd98a1" not in scrubbed
    assert "15.2ms" not in scrubbed
    assert "45ms" not in scrubbed


def test_compute_semantic_turn_digest_invariance_to_noise() -> None:
    """Guardrail 3: State transitions differing only by volatile noise have identical digests."""
    state1 = {
        "status": "processing",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "timestamp": "2026-11-07T10:00:00Z",
        "pointer": "0xdeadbeef",
        "step": 3,
    }
    state2 = {
        "step": 3,
        "pointer": "0xcafe1234",
        "timestamp": "2026-11-07T10:05:32.123Z",
        "session_id": "99999999-8888-7777-6666-555555555555",
        "status": "processing",
    }

    digest1 = compute_semantic_turn_digest(state1)
    digest2 = compute_semantic_turn_digest(state2)
    assert digest1 == digest2


def test_unicode_nfc_normalization() -> None:
    """Guardrail 6: Decomposed and composed unicode normalize to identical digest."""
    composed = "café résumé"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed

    digest1 = compute_semantic_turn_digest(composed)
    digest2 = compute_semantic_turn_digest(decomposed)
    assert digest1 == digest2


def test_stale_ref_regex() -> None:
    assert bool(_STALE_REF_RE.search("Conflict: STALE_REF on node"))
    assert bool(_STALE_REF_RE.search("Revision @sha256:e3b0c44298fc1c14 rejected"))
    assert bool(_STALE_REF_RE.search("etag_mismatch detected during optimistic lock"))
    assert bool(_STALE_REF_RE.search("conflict_detected: version is older than head"))
    assert not bool(_STALE_REF_RE.search("Clean commit successfully merged"))


def test_stale_ref_consecutive_strikes_trip_watchdog() -> None:
    """Guardrail 4: Consecutive STALE_REF strikes trip OscillationDetectedError."""
    watchdog = SemanticOscillationWatchdog(consecutive_stale_ref_limit=2)

    # First strike
    watchdog.record_turn_content("Write failed with STALE_REF on block 1")
    assert watchdog.stale_ref_count == 1

    # Second consecutive strike -> Trips immediately!
    with pytest.raises(OscillationDetectedError) as exc_info:
        watchdog.record_turn_content("Retry write failed: @sha256:12345678 mismatch")
    assert exc_info.value.pattern == "STALE_REF_DRIFT"


def test_stale_ref_resets_on_clean_turn() -> None:
    watchdog = SemanticOscillationWatchdog(consecutive_stale_ref_limit=2)

    watchdog.record_turn_content("Write failed with STALE_REF")
    assert watchdog.stale_ref_count == 1

    # Clean intermediate turn resets consecutive counter
    watchdog.record_turn_content("Querying latest commit ref")
    assert watchdog.stale_ref_count == 0

    # Next strike is only count 1, does not trip
    watchdog.record_turn_content("New write conflict_detected")
    assert watchdog.stale_ref_count == 1


def test_consecutive_identical_turns_trip_watchdog() -> None:
    """Test A -> A consecutive identical turn repeat."""
    watchdog = SemanticOscillationWatchdog(max_consecutive_identical=2)

    turn = {"tool": "read_file", "path": "app.py", "timestamp": "2026-11-07T12:00:00Z"}
    watchdog.record_turn_content(turn)

    # Same content with different timestamp -> produces same semantic digest -> trips!
    turn_repetition = {"tool": "read_file", "path": "app.py", "timestamp": "2026-11-07T12:00:05Z"}
    with pytest.raises(OscillationDetectedError) as exc_info:
        watchdog.record_turn_content(turn_repetition)
    assert exc_info.value.pattern == "CONSECUTIVE_REPEAT"


def test_two_cycle_alternating_loop_trips_watchdog() -> None:
    """Test A -> B -> A -> B alternating oscillation."""
    watchdog = SemanticOscillationWatchdog()

    turn_a = {"action": "query_index", "term": "security"}
    turn_b = {"action": "check_filter", "term": "security"}

    watchdog.record_turn_content(turn_a)  # A
    watchdog.record_turn_content(turn_b)  # B
    watchdog.record_turn_content(turn_a)  # A

    # B completes the A -> B -> A -> B loop!
    with pytest.raises(OscillationDetectedError) as exc_info:
        watchdog.record_turn_content(turn_b)
    assert exc_info.value.pattern == "ALTERNATING_CYCLE"


# ============================================================================
# MODULE 3: STDIO RECORDER & PROXY PUMP TESTS
# ============================================================================


def test_stdio_recorder_wal_mode_pragmas(tmp_path: Path) -> None:
    db_file = tmp_path / "test_session.sqlite"
    recorder = StdioSessionRecorder(db_path=db_file, session_id="test_wal")

    cursor = recorder._conn.cursor()
    cursor.execute("PRAGMA journal_mode;")
    mode = cursor.fetchone()[0]
    assert mode.lower() == "wal"

    cursor.execute("PRAGMA synchronous;")
    sync = cursor.fetchone()[0]
    # In SQLite, NORMAL is 1
    assert sync in (1, "NORMAL", "normal")

    cursor.execute("PRAGMA busy_timeout;")
    timeout = cursor.fetchone()[0]
    assert timeout == 5000

    recorder.close()


def test_stdio_recorder_intercept_client_valid_tool_call(tmp_path: Path) -> None:
    jail = tmp_path / "jail"
    jail.mkdir()
    (jail / "main.py").write_text("print('ok')", encoding="utf-8")

    db_file = tmp_path / "session.sqlite"
    recorder = StdioSessionRecorder(db_path=db_file, session_id="sess_valid", jail_dir=jail)

    client_req = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {
            "name": "read_file",
            "arguments": {
                "path": "main.py",
            },
        },
    }
    line = json.dumps(client_req)
    forward_line, err = recorder.intercept_client_line(line)
    assert err is None
    parsed = json.loads(forward_line)
    assert parsed["params"]["arguments"]["path"] == str((jail / "main.py").resolve())

    frames = recorder.get_frames()
    assert len(frames) == 1
    assert frames[0]["direction"] == "CLIENT_TO_SERVER"
    recorder.close()


def test_stdio_recorder_synthesizes_jsonrpc_error_on_argshield_violation(tmp_path: Path) -> None:
    jail = tmp_path / "jail"
    jail.mkdir()

    db_file = tmp_path / "session.sqlite"
    recorder = StdioSessionRecorder(db_path=db_file, session_id="sess_block", jail_dir=jail)

    # Malicious tool call attempting CVE-2026-85626 option injection in git ref
    client_req = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tools/call",
        "params": {
            "name": "git_checkout",
            "arguments": {
                "ref": "--upload-pack=id",
            },
        },
    }
    line = json.dumps(client_req)
    _forward_line, err_str = recorder.intercept_client_line(line)

    assert err_str is not None
    err_json = json.loads(err_str)
    assert err_json["jsonrpc"] == "2.0"
    assert err_json["id"] == 99
    assert err_json["error"]["code"] == -32602
    assert "ArgShield violation" in err_json["error"]["message"]
    assert err_json["error"]["data"]["field"] == "ref"
    assert err_json["error"]["data"]["violation"] == "flag_injection"

    # Both client line and synthesized server line must be recorded
    frames = recorder.get_frames()
    assert len(frames) == 2
    assert frames[0]["direction"] == "CLIENT_TO_SERVER"
    assert frames[1]["direction"] == "SERVER_TO_CLIENT"
    recorder.close()


def test_run_stdio_proxy_session_bidirectional_and_blocking(tmp_path: Path) -> None:
    """Guardrail 5: run_stdio_proxy_session spawns child, proxies lines, synthesizes errors."""
    db_file = tmp_path / "proxy_session.sqlite"
    jail = tmp_path / "jail"
    jail.mkdir()

    # Child python script: simple echo server that reads line and writes {"status": "ok", "echo": line}
    child_code = (
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line: continue\n"
        "    sys.stdout.write(json.dumps({'status': 'ok', 'echo': line}) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )

    # 1. First line: valid tool call
    valid_call = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "test", "arguments": {"notes": "valid"}},
    })

    # 2. Second line: malicious flag injection
    malicious_call = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "test", "arguments": {"ref": "--upload-pack=cat /etc/passwd"}},
    })

    client_in = io.StringIO(f"{valid_call}\n{malicious_call}\n")
    client_out = io.StringIO()

    cmd = [sys.executable, "-c", child_code]
    rc = run_stdio_proxy_session(
        cmd=cmd,
        session_id="proxy_test",
        db_path=db_file,
        jail_dir=jail,
        client_in=client_in,
        client_out=client_out,
        timeout=10.0,
    )

    assert rc == 0
    output_lines = [ln.strip() for ln in client_out.getvalue().splitlines() if ln.strip()]
    assert len(output_lines) >= 2

    parsed_responses = [json.loads(ln) for ln in output_lines]
    echo_resp = next(r for r in parsed_responses if r.get("status") == "ok")
    error_resp = next(r for r in parsed_responses if "error" in r)

    assert echo_resp["status"] == "ok"
    assert "valid" in echo_resp["echo"]

    # SYNTHESIZED ArgShield error response for malicious call
    assert error_resp["jsonrpc"] == "2.0"
    assert error_resp["id"] == 2
    assert error_resp["error"]["code"] == -32602
    assert "ArgShield violation" in error_resp["error"]["message"]
    assert error_resp["error"]["data"]["violation"] == "flag_injection"

    # Verify SQLite DB has all recorded frames
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("SELECT direction, line FROM stdio_frames ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()

    # We should have client frame 1, server frame 1, client frame 2, server error frame 2
    assert len(rows) == 4
    directions = [r[0] for r in rows]
    assert directions.count("CLIENT_TO_SERVER") == 2
    assert directions.count("SERVER_TO_CLIENT") == 2


def test_sanitize_tool_arguments_edge_cases(tmp_path: Path) -> None:
    jail = tmp_path / "jail"
    jail.mkdir()
    (jail / "nested.txt").write_text("data", encoding="utf-8")

    assert sanitize_tool_arguments("tool", None) == {}
    assert sanitize_tool_arguments("tool", "raw_string") == "raw_string"  # type: ignore

    # List of nested dictionaries containing path
    args = {
        "items": [
            {"path": "nested.txt"},
            {"count": 42},
        ]
    }
    sanitized = sanitize_tool_arguments("tool", args, jail_dir=jail)
    assert sanitized["items"][0]["path"] == str((jail / "nested.txt").resolve())
    assert sanitized["items"][1]["count"] == 42


def test_verify_path_in_jail_cross_drive_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jail = tmp_path / "jail"
    jail.mkdir()

    import os
    def fake_commonpath(paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(os.path, "commonpath", fake_commonpath)
    with pytest.raises(ArgShieldViolationError) as exc_info:
        verify_path_in_jail("some/path.txt", jail)
    assert exc_info.value.violation_type == "path_traversal"
    assert "outside jail volume" in exc_info.value.message


def test_stdio_recorder_context_manager_and_edge_lines(tmp_path: Path) -> None:
    db_file = tmp_path / "recorder_edge.sqlite"

    with StdioSessionRecorder(db_path=db_file, session_id="edge_sess") as recorder:
        # 1. Non-JSON line
        f1, err1 = recorder.intercept_client_line("NOT_JSON")
        assert f1 == "NOT_JSON"
        assert err1 is None

        # 2. JSON list (not dict)
        f2, err2 = recorder.intercept_client_line("[1, 2, 3]")
        assert f2 == "[1, 2, 3]"
        assert err2 is None

        # 3. Non-tool-call JSON-RPC
        f3, err3 = recorder.intercept_client_line('{"jsonrpc": "2.0", "method": "ping"}')
        assert "ping" in f3
        assert err3 is None

        # 4. Server line interception
        srv_line = recorder.intercept_server_line("pong")
        assert srv_line == "pong"

        # 5. Query frames for specific session
        frames = recorder.get_frames("edge_sess")
        assert len(frames) == 4
        empty_frames = recorder.get_frames("nonexistent_session")
        assert len(empty_frames) == 0


def test_run_stdio_proxy_session_timeout(tmp_path: Path) -> None:
    db_file = tmp_path / "timeout.sqlite"
    cin = io.StringIO("line\n")
    cout = io.StringIO()

    with pytest.raises(TimeoutError) as exc_info:
        run_stdio_proxy_session(
            cmd=[sys.executable, "-c", "import time; time.sleep(5)"],
            session_id="timeout_sess",
            db_path=db_file,
            client_in=cin,
            client_out=cout,
            timeout=0.3,
        )
    assert "timed out" in str(exc_info.value)


def test_verify_no_flag_injection_space_delimited() -> None:
    """Enhanced P0: Flag injection defense blocks space-delimited and standalone flags."""
    with pytest.raises(ArgShieldViolationError) as exc1:
        verify_no_flag_injection("any_arg", "--output /tmp/evil")
    assert exc1.value.violation_type == "flag_injection"

    with pytest.raises(ArgShieldViolationError) as exc2:
        verify_no_flag_injection("any_arg", "--output")
    assert exc2.value.violation_type == "flag_injection"

    with pytest.raises(ArgShieldViolationError) as exc3:
        verify_no_flag_injection("any_arg", "--upload-pack cat /etc/passwd")
    assert exc3.value.violation_type == "flag_injection"


def test_verify_path_in_jail_windows_device_names(tmp_path: Path) -> None:
    """Enhanced P1: Defense against Windows DOS reserved device names (CON, NUL, AUX, etc.)."""
    jail = tmp_path / "jail"
    jail.mkdir()

    for dev_name in ("CON", "con.txt", "NUL", "nul.dat", "AUX", "COM1", "lpt1.log"):
        with pytest.raises(ArgShieldViolationError) as exc_info:
            verify_path_in_jail(dev_name, jail)
        assert exc_info.value.violation_type == "device_name"
        assert "reserved device name" in exc_info.value.message


def test_argshield_policy_from_tool_schema(tmp_path: Path) -> None:
    """Enhanced P2: Dynamic schema inference from MCP tools/list definitions."""
    jail = tmp_path / "jail"
    jail.mkdir()

    tools_schema = [
        {
            "name": "custom_importer",
            "description": "Imports data",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source_archive": {
                        "type": "string",
                        "description": "The file path of the archive to extract",
                    },
                    "target_dest": {
                        "type": "string",
                        "format": "path",
                    },
                    "description": {
                        "type": "string",
                        "description": "User description of import",
                    },
                },
            },
        }
    ]

    policy = ArgShieldPolicy.from_tool_schema(tools_schema, jail_dir=jail)
    assert "source_archive" in policy.strict_path_keys
    assert "target_dest" in policy.strict_path_keys
    assert "description" not in policy.strict_path_keys

    # Custom inferred keys are now properly jailed
    bad_args = {"source_archive": "../outside.zip"}
    with pytest.raises(ArgShieldViolationError) as exc_info:
        sanitize_tool_arguments("custom_importer", bad_args, policy=policy)
    assert exc_info.value.violation_type == "path_traversal"


def test_stdio_recorder_dynamically_learns_tool_schema(tmp_path: Path) -> None:
    """Verify StdioSessionRecorder wire-level learning of tool schemas from tools/list."""
    jail = tmp_path / "jail"
    jail.mkdir()
    db_file = tmp_path / "schema_learn.sqlite"

    recorder = StdioSessionRecorder(db_path=db_file, session_id="learn_sess", jail_dir=jail)

    # 1. Server responds to tools/list with custom tool schema
    tools_list_response = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [
                {
                    "name": "backup_repo",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "backup_dest": {
                                "type": "string",
                                "description": "The filesystem path for backup output",
                            }
                        },
                    },
                }
            ]
        },
    })
    recorder.intercept_server_line(tools_list_response)

    # Verify that the policy on recorder now contains backup_dest!
    assert "backup_dest" in recorder.argshield_policy.strict_path_keys

    # 2. Next client request invokes backup_repo with path traversal in backup_dest
    client_call = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "backup_repo",
            "arguments": {
                "backup_dest": "../../../etc/shadow",
            },
        },
    })
    _forward, err_str = recorder.intercept_client_line(client_call)
    assert err_str is not None
    err = json.loads(err_str)
    assert err["error"]["code"] == -32602
    assert err["error"]["data"]["field"] == "backup_dest"
    assert err["error"]["data"]["violation"] == "path_traversal"

    recorder.close()


