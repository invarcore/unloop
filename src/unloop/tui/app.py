"""Interactive Textual TUI for unloop.

Features a 3-pane layout for inspecting agent cognitive trajectories:
- Timeline DAG (left)
- Turn Inspector (middle)
- State Delta & Memory Scratchpad (right)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from unloop.protocol.models import TurnSnapshot
from unloop.storage.db import UnloopStore


class TurnItem(ListItem):
    """Widget representing a single turn in the timeline list."""

    def __init__(self, turn: TurnSnapshot) -> None:
        super().__init__()
        self.turn = turn

    def render(self) -> Text:
        bp = "[!] " if self.turn.is_breakpoint else "    "
        tools = f" ({len(self.turn.tool_invocations)} tools)" if self.turn.tool_invocations else ""
        return Text(f"{bp}Turn #{self.turn.turn_index} [{self.turn.turn_id[:6]}]{tools}")


class UnloopTuiApp(App):
    """3-Pane Textual Application for time-travel agent inspection, state mutation, and branching."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #main-container {
        layout: horizontal;
        height: 1fr;
    }
    #timeline-pane {
        width: 28%;
        border: solid cyan;
        padding: 0 1;
    }
    #inspector-pane {
        width: 44%;
        border: solid green;
        padding: 0 1;
        overflow-y: auto;
    }
    #diff-pane {
        width: 28%;
        border: solid magenta;
        padding: 0 1;
        overflow-y: auto;
    }
    #controls-pane {
        height: 3;
        dock: bottom;
        background: $surface;
        align: center middle;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("s", "step_next", "Next Turn"),
        ("u", "step_prev", "Prev Turn"),
        ("f", "fork_branch", "Fork Branch"),
        ("m", "mutate_state", "Mutate State"),
    ]

    def __init__(self, db_path: str | Path, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.db_path = Path(db_path)
        self.store = UnloopStore(self.db_path)
        self.turns: list[TurnSnapshot] = []
        self.selected_turn: TurnSnapshot | None = None
        self.active_session_id: str | None = None
        self.active_branch: str = "main"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main-container"):
            with Vertical(id="timeline-pane"):
                yield Label("[bold cyan]Execution Timeline DAG[/bold cyan]")
                yield ListView(id="turns-list")
            with Vertical(id="inspector-pane"):
                yield Label("[bold green]Turn Inspector[/bold green]")
                yield Static(id="inspector-content")
            with Vertical(id="diff-pane"):
                yield Label("[bold magenta]State Delta & Memory[/bold magenta]")
                yield Static(id="diff-content")
        with Horizontal(id="controls-pane"):
            yield Label(
                "[bold white]Keys: [s] Next | [u] Prev | [f] Fork Branch | [m] Mutate State | [q] Quit[/bold white]",
                id="controls-label",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"unloop: {self.db_path.name}"
        cursor = self.store.conn.cursor()
        row = cursor.execute("SELECT session_id, active_branch FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()
        if row:
            self.active_session_id = row[0]
            self.active_branch = row[1] or "main"
            self._load_turns()

    def _load_turns(self) -> None:
        if not self.active_session_id:
            return
        self.turns = self.store.get_history(self.active_session_id, branch_id=self.active_branch)
        turns_list = self.query_one("#turns-list", ListView)
        turns_list.clear()
        for t in self.turns:
            turns_list.append(TurnItem(t))
        if self.turns:
            turns_list.index = 0
            self._update_views(self.turns[0])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, TurnItem):
            self._update_views(event.item.turn)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, TurnItem):
            self._update_views(event.item.turn)

    def action_step_next(self) -> None:
        """Step to next turn in timeline."""
        turns_list = self.query_one("#turns-list", ListView)
        if turns_list.index is not None and turns_list.index < len(self.turns) - 1:
            turns_list.index += 1

    def action_step_prev(self) -> None:
        """Step to previous turn in timeline."""
        turns_list = self.query_one("#turns-list", ListView)
        if turns_list.index is not None and turns_list.index > 0:
            turns_list.index -= 1

    def action_fork_branch(self) -> None:
        """Fork a new branch from the currently selected turn."""
        if not self.selected_turn or not self.active_session_id:
            self.notify("No turn selected to fork from.", severity="warning")
            return

        new_branch_name = f"fork_turn_{self.selected_turn.turn_index}_{self.selected_turn.turn_id[:4]}"
        try:
            self.store.fork_branch(
                session_id=self.active_session_id,
                origin_turn_id=self.selected_turn.turn_id,
                new_branch_id=new_branch_name,
                description=f"Forked in TUI from turn #{self.selected_turn.turn_index}",
            )
            self.active_branch = new_branch_name
            self.notify(f"Forked new branch: '{new_branch_name}'", severity="information")
            self._load_turns()
        except Exception as e:
            self.notify(f"Fork failed: {e}", severity="error")

    def action_mutate_state(self) -> None:
        """Simulate state mutation on the current turn."""
        if not self.selected_turn or not self.active_session_id:
            self.notify("No turn selected for state mutation.", severity="warning")
            return

        mutated_state = dict(self.selected_turn.state)
        mutated_state["_mutated_in_tui"] = True
        self.selected_turn.state = mutated_state
        self.store.save_turn(self.selected_turn)
        self.notify(f"Mutated state on turn #{self.selected_turn.turn_index}.", severity="information")
        self._update_views(self.selected_turn)

    def _update_views(self, turn: TurnSnapshot) -> None:
        self.selected_turn = turn

        # 1. Update Middle Pane (Inspector)
        insp = [
            f"[bold cyan]Turn ID:[/bold cyan] {turn.turn_id}",
            f"[bold cyan]Branch:[/bold cyan] {turn.branch_id} | [bold cyan]Index:[/bold cyan] #{turn.turn_index}",
        ]

        if turn.is_breakpoint:
            insp.append(f"\n[bold red][BREAKPOINT HIT][/bold red] {turn.breakpoint_reason}")

        if turn.prompt:
            insp.append(f"\n[bold blue]Prompt:[/bold blue]\n{turn.prompt}")

        if turn.response:
            insp.append(f"\n[bold green]Response / Reasoning:[/bold green]\n{turn.response}")

        if turn.tool_invocations:
            insp.append("\n[bold yellow]Tool Invocations:[/bold yellow]")
            for tool in turn.tool_invocations:
                status = "[red]FAILED[/red]" if tool.error else "[green]OK[/green]"
                insp.append(f"  * {tool.tool_name} {status} (args: {json.dumps(tool.arguments)})")
                if tool.result is not None:
                    insp.append(f"    -> Result: {json.dumps(tool.result)}")
                if tool.error:
                    insp.append(f"    [red]-> Error: {tool.error}[/red]")

        self.query_one("#inspector-content", Static).update("\n".join(insp))

        # 2. Update Right Pane (State Delta & Scratchpad)
        diff_text = []
        if turn.state_delta and any(turn.state_delta.values()):
            diff_text.append("[bold magenta]State Delta (vs Parent):[/bold magenta]")
            diff_text.append(json.dumps(turn.state_delta, indent=2))
        else:
            diff_text.append("[dim]No state delta from parent turn.[/dim]")

        diff_text.append("\n[bold white]Current Memory State:[/bold white]")
        diff_text.append(json.dumps(turn.state, indent=2))

        if turn.telemetry and (turn.telemetry.total_tokens > 0 or turn.telemetry.estimated_cost_usd > 0):
            diff_text.append("\n[bold cyan]Telemetry:[/bold cyan]")
            diff_text.append(f"Tokens: {turn.telemetry.total_tokens} (P: {turn.telemetry.prompt_tokens}, C: {turn.telemetry.completion_tokens})")
            if turn.telemetry.estimated_cost_usd > 0:
                diff_text.append(f"Cost: ${turn.telemetry.estimated_cost_usd:.4f}")

        self.query_one("#diff-content", Static).update("\n".join(diff_text))


def run_tui(db_path: str | Path) -> None:
    """Launch the interactive 3-pane TUI."""
    app = UnloopTuiApp(db_path)
    app.run()


AgdbTuiApp = UnloopTuiApp
