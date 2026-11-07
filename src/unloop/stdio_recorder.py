# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""Transparent Stdio Session Recorder and Subprocess Proxy Pump.

Addresses UN-01 (Bare subprocesses exit unrecorded unless explicitly instrumented).
Enforces SQLite WAL mode pragmas, synthesizes JSON-RPC 2.0 error responses on ArgShield
violations, feeds incoming lines to SemanticOscillationWatchdog, and provides
bidirectional subprocess pipe pumping.
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from unloop.argshield import ArgShieldPolicy, ArgShieldViolationError, sanitize_tool_arguments
from unloop.semantic_watchdog import (
    OscillationDetectedError,
    SemanticOscillationWatchdog,
    compute_semantic_turn_digest,
)


class StdioSessionRecorder:
    """Records bidirectional stdio frames into an append-only SQLite WAL database."""

    def __init__(
        self,
        db_path: Path | str,
        session_id: str,
        jail_dir: Path | str | None = None,
        argshield_policy: ArgShieldPolicy | None = None,
        watchdog: SemanticOscillationWatchdog | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.session_id = session_id
        self.jail_dir = Path(jail_dir).resolve() if jail_dir else None
        self.argshield_policy = argshield_policy or ArgShieldPolicy(jail_dir=self.jail_dir)
        self.watchdog = watchdog or SemanticOscillationWatchdog()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite database with strict WAL mode pragmas."""
        cursor = self._conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA busy_timeout = 5000;")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stdio_frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                direction TEXT NOT NULL,
                line TEXT NOT NULL,
                digest TEXT NOT NULL
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_frames_session ON stdio_frames(session_id);
        """)
        self._conn.commit()

    def record_frame(self, direction: str, line: str, digest: str | None = None) -> str:
        """Record a single stdio frame with timestamp and RFC 8785 digest."""
        if digest is None:
            digest = compute_semantic_turn_digest(line)
        now = time.time()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO stdio_frames (session_id, timestamp, direction, line, digest)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.session_id, now, direction, line, digest),
            )
        return digest

    def intercept_client_line(self, line: str) -> tuple[str, str | None]:
        """Intercept a client line sent to child server.

        Validates tool calls with ArgShield.
        On ArgShieldViolationError, synthesizes a JSON-RPC 2.0 error response,
        records both client frame and synthesized server frame, and returns
        (original_line, synthesized_error_json_string).
        """
        clean_line = line.strip("\r\n")
        digest = compute_semantic_turn_digest(clean_line)

        try:
            data = json.loads(clean_line)
        except Exception:
            # Non-JSON line: record and pass through directly
            self.record_frame("CLIENT_TO_SERVER", clean_line, digest)
            return (clean_line, None)

        if not isinstance(data, dict):
            self.record_frame("CLIENT_TO_SERVER", clean_line, digest)
            return (clean_line, None)

        # Check for MCP JSON-RPC tool call pattern
        is_tool_call = False
        tool_name = ""
        arguments: dict[str, Any] | None = None
        req_id = data.get("id")

        params = data.get("params")
        if isinstance(params, dict):
            if "arguments" in params:
                is_tool_call = True
                tool_name = params.get("name", "")
                arguments = params.get("arguments")
        elif data.get("method") == "tools/call":
            is_tool_call = True
            arguments = {}

        if is_tool_call and isinstance(arguments, dict):
            try:
                sanitized_args = sanitize_tool_arguments(
                    tool_name=tool_name,
                    arguments=arguments,
                    jail_dir=self.jail_dir,
                    policy=self.argshield_policy,
                )
                data["params"]["arguments"] = sanitized_args
                forward_line = json.dumps(data, ensure_ascii=False)
                forward_digest = compute_semantic_turn_digest(forward_line)
                self.record_frame("CLIENT_TO_SERVER", forward_line, forward_digest)
                return (forward_line, None)
            except ArgShieldViolationError as err:
                # Record offending client frame
                self.record_frame("CLIENT_TO_SERVER", clean_line, digest)

                # Synthesize standard JSON-RPC 2.0 error response (-32602: Invalid params)
                error_response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32602,
                        "message": f"ArgShield violation: {err.message}",
                        "data": {
                            "field": err.field,
                            "violation": err.violation_type,
                        },
                    },
                }
                error_str = json.dumps(error_response, ensure_ascii=False)
                error_digest = compute_semantic_turn_digest(error_str)
                self.record_frame("SERVER_TO_CLIENT", error_str, error_digest)
                return (clean_line, error_str)

        # Other JSON-RPC methods (initialize, ping, list_tools, etc.)
        self.record_frame("CLIENT_TO_SERVER", clean_line, digest)
        return (clean_line, None)

    def intercept_server_line(self, line: str) -> str:
        """Intercept a server line output by child server.

        Records the frame, feeds content into SemanticOscillationWatchdog,
        and dynamically learns tool parameter schemas from tools/list responses.
        Raises OscillationDetectedError if cognitive oscillation is detected.
        """
        clean_line = line.strip("\r\n")

        # Opportunistically learn tool schemas from tools/list responses
        try:
            data = json.loads(clean_line)
            if isinstance(data, dict):
                result = data.get("result")
                if isinstance(result, dict) and "tools" in result and isinstance(result["tools"], list):
                    self.argshield_policy = ArgShieldPolicy.from_tool_schema(
                        tools=result["tools"],
                        jail_dir=self.jail_dir,
                        base_policy=self.argshield_policy,
                    )
        except Exception:
            pass

        digest = self.watchdog.record_turn_content(clean_line)
        self.record_frame("SERVER_TO_CLIENT", clean_line, digest)
        return clean_line

    def get_frames(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Retrieve recorded frames in chronological order."""
        target_session = session_id or self.session_id
        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT id, session_id, timestamp, direction, line, digest
            FROM stdio_frames
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (target_session,),
        )
        return [
            {
                "id": row[0],
                "session_id": row[1],
                "timestamp": row[2],
                "direction": row[3],
                "line": row[4],
                "digest": row[5],
            }
            for row in cursor.fetchall()
        ]

    def close(self) -> None:
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self) -> StdioSessionRecorder:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def run_stdio_proxy_session(
    cmd: list[str],
    session_id: str,
    db_path: Path | str,
    jail_dir: Path | str | None = None,
    client_in: io.TextIOBase | None = None,
    client_out: io.TextIOBase | None = None,
    timeout: float | None = None,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Spawn child MCP server, streaming line-buffered stdio with ArgShield enforcement.

    Implements Guardrail 5:
    - Streams stdin/stdout with flush=True
    - Synthesizes JSON-RPC 2.0 error responses on ArgShieldViolationError
    - Monitors child output with SemanticOscillationWatchdog
    - Persists every frame to SQLite WAL database
    """
    cin = client_in or sys.stdin
    cout = client_out or sys.stdout

    recorder = StdioSessionRecorder(
        db_path=db_path,
        session_id=session_id,
        jail_dir=jail_dir,
    )

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(cwd) if cwd else None,
        env=env,
    )

    pump_error: list[Exception] = []

    def client_to_server_pump() -> None:
        try:
            for raw_line in cin:
                line = raw_line.strip("\r\n")
                forward_line, synthesized_error = recorder.intercept_client_line(line)
                if synthesized_error is not None:
                    cout.write(synthesized_error + "\n")
                    cout.flush()
                else:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.write(forward_line + "\n")
                        proc.stdin.flush()
        except Exception as exc:
            pump_error.append(exc)
        finally:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

    def server_to_client_pump() -> None:
        try:
            if proc.stdout:
                for raw_line in proc.stdout:
                    line = raw_line.strip("\r\n")
                    out_line = recorder.intercept_server_line(line)
                    cout.write(out_line + "\n")
                    cout.flush()
        except Exception as exc:
            pump_error.append(exc)

    t_in = threading.Thread(target=client_to_server_pump, daemon=True)
    t_out = threading.Thread(target=server_to_client_pump, daemon=True)

    t_in.start()
    t_out.start()

    try:
        proc.wait(timeout=timeout)
        t_in.join(timeout=2.0)
        t_out.join(timeout=2.0)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait(timeout=2.0)
        recorder.close()
        raise TimeoutError(f"Subprocess proxy timed out after {timeout} seconds") from exc
    finally:
        recorder.close()

    if pump_error:
        # Re-raise watchdog oscillation errors or critical errors
        for err in pump_error:
            if isinstance(err, OscillationDetectedError):
                raise err

    return proc.returncode
