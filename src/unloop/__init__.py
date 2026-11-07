# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""unloop: The Time-Travel Debugger for AI Agents.

Break out of critique loops, rewind cognitive turns, and mutate state in-flight.
"""

from pathlib import Path

from unloop.argshield import (
    BANNED_FLAG_PREFIXES,
    POSITIONAL_FLAG_PARAMS,
    STRICT_PATH_KEYS,
    WIN_DEVICE_NAMES,
    ArgShieldPolicy,
    ArgShieldViolationError,
    sanitize_tool_arguments,
    verify_no_flag_injection,
    verify_path_in_jail,
)
from unloop.compaction_slots import (
    CANONICAL_SLOTS,
    SlotVerificationReport,
    canonicalize_constraints_rfc8785,
    extract_hard_constraints,
    parse_compaction_slots,
    verify_compaction_checkpoint,
)
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
from unloop.semantic_watchdog import (
    OscillationDetectedError,
    SemanticOscillationWatchdog,
    compute_semantic_turn_digest,
)
from unloop.stdio_recorder import (
    StdioSessionRecorder,
    run_stdio_proxy_session,
)
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
    "BANNED_FLAG_PREFIXES",
    "CANONICAL_SLOTS",
    "POSITIONAL_FLAG_PARAMS",
    "STRICT_PATH_KEYS",
    "WIN_DEVICE_NAMES",
    "AgdbSession",
    "AgdbStore",
    "ArgShieldPolicy",
    "ArgShieldViolationError",
    "Breakpoint",
    "BreakpointHalt",
    "BreakpointManager",
    "OscillationAlert",
    "OscillationDetectedError",
    "OscillationWatchdog",
    "SemanticOscillationWatchdog",
    "SessionMetadata",
    "SlotVerificationReport",
    "StdioSessionRecorder",
    "TokenTelemetry",
    "ToolInvocationRecord",
    "TurnContext",
    "TurnSnapshot",
    "UnloopSession",
    "UnloopStore",
    "__version__",
    "agdb_trace",
    "canonicalize_constraints_rfc8785",
    "compute_semantic_turn_digest",
    "extract_hard_constraints",
    "get_current_session",
    "is_side_effect_tool_name",
    "parse_compaction_slots",
    "run_stdio_proxy_session",
    "sanitize_tool_arguments",
    "session",
    "set_current_session",
    "trace",
    "verify_compaction_checkpoint",
    "verify_no_flag_injection",
    "verify_path_in_jail",
]
