# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""unloop: The Time-Travel Debugger for AI Agents.

Break out of critique loops, rewind cognitive turns, and mutate state in-flight.
"""

from pathlib import Path

from unloop.protocol.models import (
    SessionMetadata,
    TokenTelemetry,
    ToolInvocationRecord,
    TurnSnapshot,
    is_side_effect_tool_name,
)
from unloop.runtime.breakpoints import Breakpoint, BreakpointManager
from unloop.runtime.interceptor import (
    AgdbSession,
    BreakpointHalt,
    TurnContext,
    UnloopSession,
    agdb_trace,
    get_current_session,
    set_current_session,
    trace,
)
from unloop.runtime.watchdog import OscillationAlert, OscillationWatchdog
from unloop.storage.db import AgdbStore, UnloopStore

__version__ = "0.2.0"


def session(
    name: str = "agent_run",
    db_path: str | Path | None = None,
    framework: str = "custom",
    interactive: bool = False,
    raise_on_breakpoint: bool = False,
    side_effect_policy: str = "warn",
) -> UnloopSession:
    """Convenience constructor for an unloop debugging session."""
    return UnloopSession(
        name=name,
        db_path=db_path,
        framework=framework,
        interactive=interactive,
        raise_on_breakpoint=raise_on_breakpoint,
        side_effect_policy=side_effect_policy,
    )


__all__ = [
    "AgdbSession",
    "AgdbStore",
    "Breakpoint",
    "BreakpointHalt",
    "BreakpointManager",
    "OscillationAlert",
    "OscillationWatchdog",
    "SessionMetadata",
    "TokenTelemetry",
    "ToolInvocationRecord",
    "TurnContext",
    "TurnSnapshot",
    "UnloopSession",
    "UnloopStore",
    "__version__",
    "agdb_trace",
    "get_current_session",
    "is_side_effect_tool_name",
    "session",
    "set_current_session",
    "trace",
]
