-- Schema for agdb SQLite WAL Storage Engine

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    framework TEXT,
    model_name TEXT,
    agent_goal TEXT,
    active_branch TEXT NOT NULL DEFAULT 'main',
    metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    parent_id TEXT,
    branch_id TEXT NOT NULL DEFAULT 'main',
    turn_index INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    prompt TEXT,
    response TEXT,
    messages_json TEXT DEFAULT '[]',
    state_json TEXT NOT NULL DEFAULT '{}',
    state_delta_json TEXT,
    state_hash TEXT NOT NULL,
    tools_json TEXT DEFAULT '[]',
    telemetry_json TEXT DEFAULT '{}',
    metadata_json TEXT DEFAULT '{}',
    is_breakpoint INTEGER NOT NULL DEFAULT 0,
    breakpoint_reason TEXT
);

CREATE TABLE IF NOT EXISTS branches (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    branch_id TEXT NOT NULL,
    origin_turn_id TEXT,
    head_turn_id TEXT,
    created_at REAL NOT NULL,
    description TEXT,
    PRIMARY KEY (session_id, branch_id)
);

CREATE INDEX IF NOT EXISTS idx_turns_session_branch ON turns(session_id, branch_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_turns_parent ON turns(parent_id);
