"""High-performance SQLite WAL storage engine for unloop."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from unloop.protocol.models import (
    SessionMetadata,
    TokenTelemetry,
    ToolInvocationRecord,
    TurnSnapshot,
)


class UnloopStore:
    """Manages append-only SQLite WAL persistence for agent turns, sessions, and branches."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Initialize WAL mode, pragmas, and schema."""
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA foreign_keys = ON;")

        schema_file = Path(__file__).parent / "schema.sql"
        if schema_file.exists():
            schema_sql = schema_file.read_text(encoding="utf-8")
            cursor.executescript(schema_sql)
        self.conn.commit()

    def create_session(self, session: SessionMetadata) -> SessionMetadata:
        """Register a new agent debugging session."""
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

    def get_session(self, session_id: str) -> Optional[SessionMetadata]:
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

    def save_turn(self, turn: TurnSnapshot) -> None:
        """Persist a turn snapshot and update branch HEAD."""
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO turns (
                    turn_id, session_id, parent_id, branch_id, turn_index, timestamp,
                    prompt, response, messages_json, state_json, state_delta_json,
                    state_hash, tools_json, telemetry_json, metadata_json,
                    is_breakpoint, breakpoint_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.turn_id,
                    turn.session_id,
                    turn.parent_id,
                    turn.branch_id,
                    turn.turn_index,
                    turn.timestamp,
                    turn.prompt,
                    turn.response,
                    json.dumps(turn.messages),
                    json.dumps(turn.state),
                    json.dumps(turn.state_delta) if turn.state_delta else None,
                    turn.state_hash,
                    json.dumps([t.model_dump() for t in turn.tool_invocations]),
                    json.dumps(turn.telemetry.model_dump()),
                    json.dumps(turn.metadata),
                    1 if turn.is_breakpoint else 0,
                    turn.breakpoint_reason,
                ),
            )
            # Update branch HEAD
            self.conn.execute(
                """
                UPDATE branches SET head_turn_id = ? 
                WHERE session_id = ? AND branch_id = ?
                """,
                (turn.turn_id, turn.session_id, turn.branch_id),
            )

    def get_turn(self, turn_id: str) -> Optional[TurnSnapshot]:
        """Fetch a specific turn snapshot by turn_id."""
        row = self.conn.execute(
            "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_turn(row)

    def get_history(self, session_id: str, branch_id: str = "main") -> List[TurnSnapshot]:
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

    def get_head(self, session_id: str, branch_id: str = "main") -> Optional[TurnSnapshot]:
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
        description: Optional[str] = None,
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

    def get_branches(self, session_id: str) -> List[Dict[str, Any]]:
        """List all branches for a given session."""
        rows = self.conn.execute(
            "SELECT * FROM branches WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _row_to_turn(self, row: sqlite3.Row) -> TurnSnapshot:
        tools_data = json.loads(row["tools_json"] or "[]")
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
            prompt=row["prompt"],
            response=row["response"],
            messages=json.loads(row["messages_json"] or "[]"),
            state=json.loads(row["state_json"] or "{}"),
            state_delta=json.loads(row["state_delta_json"]) if row["state_delta_json"] else None,
            tool_invocations=tools,
            telemetry=telemetry,
            metadata=json.loads(row["metadata_json"] or "{}"),
            is_breakpoint=bool(row["is_breakpoint"]),
            breakpoint_reason=row["breakpoint_reason"],
        )

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()

    def __enter__(self) -> "UnloopStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# Backwards compatibility alias
AgdbStore = UnloopStore
