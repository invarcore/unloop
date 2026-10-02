"""SQLite WAL storage engine for unloop.

Provides atomic, append-only persistence of TurnSnapshots, branching trees,
and session metadata with microsecond latency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from unloop.protocol.models import (
    SessionMetadata,
    TokenTelemetry,
    ToolInvocationRecord,
    TurnSnapshot,
)

_REDACTION_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key[\"'\s:=]+)[A-Za-z0-9_\-]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(secret[\"'\s:=]+)[A-Za-z0-9_\-]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password[\"'\s:=]+)[^\s,\"'}]{6,}"), r"\1[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED]"),
]


_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(password|secret|api[_-]?key|token|auth|credential|private[_-]?key)"
)


def redact_sensitive_text(text: str | None) -> str | None:
    """Scrub sensitive credentials, bearer tokens, and API keys from text."""
    if not text:
        return text
    redacted = text
    for pat, repl in _REDACTION_PATTERNS:
        redacted = pat.sub(repl, redacted)
    return redacted


def redact_sensitive_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub sensitive keys and credential patterns from dictionary structures."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            if _SENSITIVE_KEY_PATTERN.search(str(k)):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_sensitive_text(v)
        elif isinstance(v, dict):
            out[k] = redact_sensitive_dict(v)
        elif isinstance(v, list):
            out[k] = [
                redact_sensitive_dict(item) if isinstance(item, dict)
                else (redact_sensitive_text(item) if isinstance(item, str) else item)
                for item in v
            ]
        else:
            out[k] = v
    return out


def _cipher_transform(data: bytes, key: str) -> bytes:
    """Deterministic HMAC-SHA256 keystream stream cipher for application-level encryption."""
    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    out = bytearray(len(data))
    block_index = 0
    while block_index * 32 < len(data):
        counter = block_index.to_bytes(8, "big")
        keystream = hmac.new(key_bytes, counter, hashlib.sha256).digest()
        for i in range(min(32, len(data) - block_index * 32)):
            out[block_index * 32 + i] = data[block_index * 32 + i] ^ keystream[i]
        block_index += 1
    return bytes(out)


def encrypt_field(text: str | None, key: str | None) -> str | None:
    """Encrypt field value using application key if configured."""
    if not text or not key:
        return text
    raw_bytes = text.encode("utf-8")
    enc_bytes = _cipher_transform(raw_bytes, key)
    return "enc:" + base64.b64encode(enc_bytes).decode("ascii")


def decrypt_field(text: str | None, key: str | None) -> str | None:
    """Decrypt field value using application key if it was encrypted."""
    if not text or not key:
        return text
    if not text.startswith("enc:"):
        return text
    try:
        enc_bytes = base64.b64decode(text[4:])
        dec_bytes = _cipher_transform(enc_bytes, key)
        return dec_bytes.decode("utf-8")
    except Exception:
        return text


class UnloopStore:
    """SQLite WAL storage engine managing sessions, turns, and branch DAGs."""

    def __init__(
        self,
        db_path: str | Path,
        encryption_key: str | None = None,
        redact_secrets: bool | None = None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.encryption_key = encryption_key or os.environ.get("UNLOOP_ENCRYPTION_KEY")
        self.redact_secrets = (
            redact_secrets
            if redact_secrets is not None
            else (os.environ.get("UNLOOP_REDACT_SECRETS", "1").lower() not in ("0", "false", "off"))
        )
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database tables with WAL journal mode for concurrent read/write."""
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            schema_sql = schema_path.read_text(encoding="utf-8")
        else:
            schema_sql = """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at REAL NOT NULL,
                framework TEXT,
                model_name TEXT,
                agent_goal TEXT,
                active_branch TEXT DEFAULT 'main',
                metadata_json TEXT
            );

            CREATE TABLE IF NOT EXISTS branches (
                session_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                origin_turn_id TEXT,
                head_turn_id TEXT,
                created_at REAL NOT NULL,
                description TEXT,
                PRIMARY KEY (session_id, branch_id),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                parent_id TEXT,
                branch_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                prompt TEXT,
                response TEXT,
                messages_json TEXT,
                state_json TEXT,
                state_delta_json TEXT,
                state_hash TEXT,
                tools_json TEXT,
                telemetry_json TEXT,
                metadata_json TEXT,
                is_breakpoint INTEGER DEFAULT 0,
                breakpoint_reason TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_turns_session_branch ON turns(session_id, branch_id, turn_index);
            CREATE INDEX IF NOT EXISTS idx_turns_parent ON turns(parent_id);
            """

        with self.conn:
            self.conn.executescript(schema_sql)

    def create_session(self, session: SessionMetadata) -> SessionMetadata:
        """Create a new debugging session record with default 'main' branch."""
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO sessions
                (session_id, name, created_at, framework, model_name, agent_goal, active_branch, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.name,
                    session.created_at,
                    session.framework,
                    session.model_name,
                    session.agent_goal,
                    session.active_branch,
                    json.dumps(session.metadata),
                ),
            )
            # Create default main branch
            self.conn.execute(
                """
                INSERT OR IGNORE INTO branches
                (session_id, branch_id, origin_turn_id, head_turn_id, created_at, description)
                VALUES (?, 'main', NULL, NULL, ?, 'Primary execution branch')
                """,
                (session.session_id, session.created_at),
            )
        return session

    def get_session(self, session_id: str) -> SessionMetadata | None:
        """Retrieve session metadata by ID."""
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None

        # Count turns
        turn_count = cursor.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,)
        ).fetchone()[0]

        return SessionMetadata(
            session_id=row["session_id"],
            name=row["name"],
            created_at=row["created_at"],
            framework=row["framework"] or "custom",
            model_name=row["model_name"],
            agent_goal=row["agent_goal"],
            total_turns=turn_count,
            active_branch=row["active_branch"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def save_turn(self, turn: TurnSnapshot, allow_update: bool = False) -> None:
        """Persist a turn snapshot append-only and update branch HEAD."""
        prompt = turn.prompt
        response = turn.response
        messages = turn.messages
        state = turn.state
        state_delta = turn.state_delta
        tools_list = [t.model_dump() for t in turn.tool_invocations]
        telemetry_dict = turn.telemetry.model_dump()
        metadata_dict = turn.metadata

        if self.redact_secrets:
            prompt = redact_sensitive_text(prompt)
            response = redact_sensitive_text(response)
            if messages:
                messages = [
                    redact_sensitive_dict(m) if isinstance(m, dict) else m
                    for m in messages
                ]
            if state:
                state = redact_sensitive_dict(state)

        messages_json = json.dumps(messages)
        state_json = json.dumps(state)
        state_delta_json = json.dumps(state_delta) if state_delta else None
        tools_json = json.dumps(tools_list)
        telemetry_json = json.dumps(telemetry_dict)
        metadata_json = json.dumps(metadata_dict)

        if self.encryption_key:
            prompt = encrypt_field(prompt, self.encryption_key)
            response = encrypt_field(response, self.encryption_key)
            messages_json = encrypt_field(messages_json, self.encryption_key)
            state_json = encrypt_field(state_json, self.encryption_key)
            if state_delta_json:
                state_delta_json = encrypt_field(state_delta_json, self.encryption_key)
            tools_json = encrypt_field(tools_json, self.encryption_key)

        params = (
            turn.turn_id,
            turn.session_id,
            turn.parent_id,
            turn.branch_id,
            turn.turn_index,
            turn.timestamp,
            prompt,
            response,
            messages_json,
            state_json,
            state_delta_json,
            turn.state_hash,
            tools_json,
            telemetry_json,
            metadata_json,
            1 if turn.is_breakpoint else 0,
            turn.breakpoint_reason,
        )

        with self.conn:
            if allow_update:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO turns (
                        turn_id, session_id, parent_id, branch_id, turn_index, timestamp,
                        prompt, response, messages_json, state_json, state_delta_json,
                        state_hash, tools_json, telemetry_json, metadata_json,
                        is_breakpoint, breakpoint_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
            else:
                try:
                    self.conn.execute(
                        """
                        INSERT INTO turns (
                            turn_id, session_id, parent_id, branch_id, turn_index, timestamp,
                            prompt, response, messages_json, state_json, state_delta_json,
                            state_hash, tools_json, telemetry_json, metadata_json,
                            is_breakpoint, breakpoint_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        params,
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(
                        f"Turn '{turn.turn_id}' already exists in session '{turn.session_id}'. Unloop storage is strictly append-only."
                    ) from exc

            # Update branch HEAD
            self.conn.execute(
                """
                UPDATE branches SET head_turn_id = ?
                WHERE session_id = ? AND branch_id = ?
                """,
                (turn.turn_id, turn.session_id, turn.branch_id),
            )

    def get_turn(self, turn_id: str) -> TurnSnapshot | None:
        """Fetch a specific turn snapshot by turn_id."""
        row = self.conn.execute(
            "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_turn(row)

    def get_history(self, session_id: str, branch_id: str = "main") -> list[TurnSnapshot]:
        """Retrieve all turns on a branch ordered by turn_index ascending."""
        rows = self.conn.execute(
            """
            SELECT * FROM turns
            WHERE session_id = ? AND branch_id = ?
            ORDER BY turn_index ASC
            """,
            (session_id, branch_id),
        ).fetchall()
        return [self._row_to_turn(r) for r in rows]

    def get_head(self, session_id: str, branch_id: str = "main") -> TurnSnapshot | None:
        """Get the latest turn on a branch."""
        row = self.conn.execute(
            """
            SELECT * FROM turns
            WHERE session_id = ? AND branch_id = ?
            ORDER BY turn_index DESC LIMIT 1
            """,
            (session_id, branch_id),
        ).fetchone()
        if not row:
            return None
        return self._row_to_turn(row)

    def fork_branch(
        self,
        session_id: str,
        origin_turn_id: str,
        new_branch_id: str,
        description: str | None = None,
    ) -> str:
        """Fork a new branch from origin_turn_id to enable exploratory time-travel execution."""
        origin_turn = self.get_turn(origin_turn_id)
        if not origin_turn:
            raise ValueError(f"Origin turn {origin_turn_id} not found.")

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO branches (session_id, branch_id, origin_turn_id, head_turn_id, created_at, description)
                VALUES (?, ?, ?, ?, strftime('%s', 'now'), ?)
                """,
                (
                    session_id,
                    new_branch_id,
                    origin_turn_id,
                    origin_turn_id,
                    description or f"Forked from {origin_turn_id[:8]}",
                ),
            )
            # Update active branch in session
            self.conn.execute(
                "UPDATE sessions SET active_branch = ? WHERE session_id = ?",
                (new_branch_id, session_id),
            )
        return new_branch_id

    def get_branches(self, session_id: str) -> list[dict[str, Any]]:
        """List all branches for a given session."""
        rows = self.conn.execute(
            "SELECT * FROM branches WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def diff_turns(self, turn_a: TurnSnapshot, turn_b: TurnSnapshot) -> dict[str, Any]:
        """Compute comprehensive diff between two turns (state, tools, telemetry)."""
        state_diff: dict[str, Any] = {"added": {}, "modified": {}, "removed": []}
        all_keys = set(turn_a.state.keys()) | set(turn_b.state.keys())
        for k in all_keys:
            if k not in turn_a.state:
                state_diff["added"][k] = turn_b.state[k]
            elif k not in turn_b.state:
                state_diff["removed"].append(k)
            elif turn_a.state[k] != turn_b.state[k]:
                state_diff["modified"][k] = {
                    "turn_a": turn_a.state[k],
                    "turn_b": turn_b.state[k],
                }

        tools_a = [t.tool_name for t in turn_a.tool_invocations]
        tools_b = [t.tool_name for t in turn_b.tool_invocations]

        return {
            "turn_a_index": turn_a.turn_index,
            "turn_b_index": turn_b.turn_index,
            "turn_a_id": turn_a.turn_id,
            "turn_b_id": turn_b.turn_id,
            "state_diff": state_diff,
            "tools_a": tools_a,
            "tools_b": tools_b,
            "prompt_a": turn_a.prompt,
            "prompt_b": turn_b.prompt,
            "response_a": turn_a.response,
            "response_b": turn_b.response,
        }

    def _row_to_turn(self, row: sqlite3.Row) -> TurnSnapshot:
        prompt = row["prompt"]
        response = row["response"]
        messages_str = row["messages_json"]
        state_str = row["state_json"]
        state_delta_str = row["state_delta_json"]
        tools_str = row["tools_json"]

        if self.encryption_key:
            prompt = decrypt_field(prompt, self.encryption_key)
            response = decrypt_field(response, self.encryption_key)
            messages_str = decrypt_field(messages_str, self.encryption_key)
            state_str = decrypt_field(state_str, self.encryption_key)
            if state_delta_str:
                state_delta_str = decrypt_field(state_delta_str, self.encryption_key)
            tools_str = decrypt_field(tools_str, self.encryption_key)

        tools_data = json.loads(tools_str or "[]")
        tools = [ToolInvocationRecord(**t) for t in tools_data]
        telem_data = json.loads(row["telemetry_json"] or "{}")
        telemetry = TokenTelemetry(**telem_data)

        return TurnSnapshot(
            turn_id=row["turn_id"],
            session_id=row["session_id"],
            parent_id=row["parent_id"],
            branch_id=row["branch_id"],
            turn_index=row["turn_index"],
            timestamp=row["timestamp"],
            prompt=prompt,
            response=response,
            messages=json.loads(messages_str or "[]"),
            state=json.loads(state_str or "{}"),
            state_delta=json.loads(state_delta_str) if state_delta_str else None,
            tool_invocations=tools,
            telemetry=telemetry,
            metadata=json.loads(row["metadata_json"] or "{}"),
            is_breakpoint=bool(row["is_breakpoint"]),
            breakpoint_reason=row["breakpoint_reason"],
        )

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# Backwards compatibility alias
AgdbStore = UnloopStore
