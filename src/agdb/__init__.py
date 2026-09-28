"""agdb: Agent GNU Debugger.

Terminal time-travel stepper, anti-oscillation watchdog, and state mutation engine for AI agents.
"""

from agdb.protocol.models import (
    SessionMetadata,
    TokenTelemetry,
    ToolInvocationRecord,
    TurnSnapshot,
)
from agdb.runtime.breakpoints import Breakpoint, BreakpointManager
from agdb.runtime.interceptor import AgdbSession, BreakpointHalt, TurnContext
from agdb.runtime.watchdog import OscillationAlert, OscillationWatchdog
from agdb.storage.db import AgdbStore

__version__ = "0.1.0"


def session(
    name: str = "agent_run",
    db_path: str = "session.agdb",
    framework: str = "custom",
    interactive: bool = False,
    raise_on_breakpoint: bool = False,
) -> AgdbSession:
    """Convenience constructor for an agdb debugging session."""
    return AgdbSession(
        name=name,
        db_path=db_path,
        framework=framework,
        interactive=interactive,
        raise_on_breakpoint=raise_on_breakpoint,
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
    "__version__",
    "session",
]
