# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""Canonical protocol models for unloop (Agent GNU Debugger).

These models define the framework-agnostic TurnSnapshot, StateDelta,
and ToolInvocation contracts used for time-travel debugging.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

_MUTATING_VERBS = (
    "send",
    "write",
    "post",
    "put",
    "patch",
    "delete",
    "create",
    "insert",
    "update",
    "drop",
    "publish",
    "notify",
    "dispatch",
    "email",
    "tweet",
    "execute_sql",
)


def is_side_effect_tool_name(tool_name: str) -> bool:
    """Heuristic detector for mutating or egress side-effect tools."""
    name_lower = tool_name.lower().replace("-", "_")
    parts = name_lower.split("_")
    for verb in _MUTATING_VERBS:
        if verb in parts or name_lower.startswith(verb):
            return True
    return False


def _canonical_json(data: Any) -> str:
    """Serialize any data to canonical JSON with deterministic sorting."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


class ToolInvocationRecord(BaseModel):
    """Record of a tool call executed during an agent turn."""

    call_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    duration_ms: float = 0.0
    is_side_effect: bool = False
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def argument_hash(self) -> str:
        """Deterministic hash of tool arguments to detect loops."""
        return hashlib.sha256(_canonical_json(self.arguments).encode()).hexdigest()[:12]

    @property
    def side_effect_key(self) -> str:
        """Deterministic fingerprint for idempotency and replay fence tracking."""
        if self.idempotency_key:
            return self.idempotency_key
        return f"{self.tool_name}:{self.argument_hash}"


class TokenTelemetry(BaseModel):
    """Token consumption and estimated cost telemetry for a turn."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class TurnSnapshot(BaseModel):
    """Immutable snapshot of an agent cognitive turn.

    Forms a node in the Directed Acyclic Graph (DAG) of the agent execution.
    """

    turn_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    parent_id: str | None = None
    branch_id: str = "main"
    turn_index: int = 0
    timestamp: float = Field(default_factory=time.time)

    # Cognitive content
    prompt: str | None = None
    response: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)

    # State & Memory
    state: dict[str, Any] = Field(default_factory=dict)
    state_delta: dict[str, Any] | None = None

    # Actions & Telemetry
    tool_invocations: list[ToolInvocationRecord] = Field(default_factory=list)
    telemetry: TokenTelemetry = Field(default_factory=TokenTelemetry)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Debugging control
    is_breakpoint: bool = False
    breakpoint_reason: str | None = None

    @property
    def state_hash(self) -> str:
        """Cryptographic checksum of current agent state."""
        return hashlib.sha256(_canonical_json(self.state).encode()).hexdigest()[:16]

    @property
    def side_effect_invocations(self) -> list[ToolInvocationRecord]:
        """List all mutating or egress side-effect tool calls in this turn."""
        return [t for t in self.tool_invocations if t.is_side_effect]

    def compute_state_delta(self, previous_state: dict[str, Any]) -> dict[str, Any]:
        """Compute key-level diff between parent state and current state."""
        delta: dict[str, Any] = {"added": {}, "modified": {}, "removed": []}
        all_keys = set(previous_state.keys()) | set(self.state.keys())

        for k in all_keys:
            if k not in previous_state:
                delta["added"][k] = self.state[k]
            elif k not in self.state:
                delta["removed"].append(k)
            elif previous_state[k] != self.state[k]:
                delta["modified"][k] = {
                    "old": previous_state[k],
                    "new": self.state[k],
                }

        self.state_delta = delta
        return delta

    def compute_prompt_diff(self, previous_prompt: str | None) -> dict[str, Any]:
        """Compute line-level diff, token/character deltas, and compaction loss metrics."""
        prev = previous_prompt or ""
        curr = self.prompt or ""

        if not prev and not curr:
            return {
                "has_diff": False,
                "prev_length": 0,
                "curr_length": 0,
                "length_delta": 0,
                "compression_ratio": 1.0,
                "is_compacted": False,
                "added_lines": [],
                "removed_lines": [],
                "diff_unified": [],
            }

        prev_lines = prev.splitlines()
        curr_lines = curr.splitlines()

        unified = list(
            difflib.unified_diff(
                prev_lines,
                curr_lines,
                fromfile="turn_previous",
                tofile="turn_current",
                lineterm="",
            )
        )

        added_lines = [
            line[1:] for line in unified if line.startswith("+") and not line.startswith("+++")
        ]
        removed_lines = [
            line[1:] for line in unified if line.startswith("-") and not line.startswith("---")
        ]

        prev_len = len(prev)
        curr_len = len(curr)
        ratio = round(curr_len / prev_len, 4) if prev_len > 0 else 1.0
        is_compacted = (curr_len < prev_len) and (len(removed_lines) > 0)

        return {
            "has_diff": prev != curr,
            "prev_length": prev_len,
            "curr_length": curr_len,
            "length_delta": curr_len - prev_len,
            "compression_ratio": ratio,
            "is_compacted": is_compacted,
            "added_lines": added_lines,
            "removed_lines": removed_lines,
            "diff_unified": unified,
        }


class SessionMetadata(BaseModel):
    """Metadata describing an unloop recording session."""

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "agent_run"
    created_at: float = Field(default_factory=time.time)
    framework: str = "custom"
    model_name: str | None = None
    agent_goal: str | None = None
    total_turns: int = 0
    active_branch: str = "main"
    metadata: dict[str, Any] = Field(default_factory=dict)
