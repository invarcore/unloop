"""Canonical protocol models for unloop (Agent GNU Debugger).

These models define the framework-agnostic TurnSnapshot, StateDelta,
and ToolInvocation contracts used for time-travel debugging.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def _canonical_json(data: Any) -> str:
    """Serialize any data to canonical JSON with deterministic sorting."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


class ToolInvocationRecord(BaseModel):
    """Record of a tool call executed during an agent turn."""

    call_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: float = 0.0

    @property
    def argument_hash(self) -> str:
        """Deterministic hash of tool arguments to detect loops."""
        return hashlib.sha256(_canonical_json(self.arguments).encode()).hexdigest()[:12]


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
    parent_id: Optional[str] = None
    branch_id: str = "main"
    turn_index: int = 0
    timestamp: float = Field(default_factory=time.time)

    # Cognitive content
    prompt: Optional[str] = None
    response: Optional[str] = None
    messages: List[Dict[str, Any]] = Field(default_factory=list)

    # State & Memory
    state: Dict[str, Any] = Field(default_factory=dict)
    state_delta: Optional[Dict[str, Any]] = None

    # Actions & Telemetry
    tool_invocations: List[ToolInvocationRecord] = Field(default_factory=list)
    telemetry: TokenTelemetry = Field(default_factory=TokenTelemetry)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # Debugging control
    is_breakpoint: bool = False
    breakpoint_reason: Optional[str] = None

    @property
    def state_hash(self) -> str:
        """Cryptographic checksum of current agent state."""
        return hashlib.sha256(_canonical_json(self.state).encode()).hexdigest()[:16]

    def compute_state_delta(self, previous_state: Dict[str, Any]) -> Dict[str, Any]:
        """Compute key-level diff between parent state and current state."""
        delta: Dict[str, Any] = {"added": {}, "modified": {}, "removed": []}
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


class SessionMetadata(BaseModel):
    """Metadata describing an unloop recording session."""

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "agent_run"
    created_at: float = Field(default_factory=time.time)
    framework: str = "custom"
    model_name: Optional[str] = None
    agent_goal: Optional[str] = None
    total_turns: int = 0
    active_branch: str = "main"
    metadata: Dict[str, Any] = Field(default_factory=dict)
