"""Interactive Textual TUI for unloop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Label, ListItem, ListView, Static

from unloop.protocol.models import TurnSnapshot
from unloop.storage.db import UnloopStore


class TurnItem(ListItem):
    """Widget representing a single turn in the timeline list."""

    def __init__(self, turn: TurnSnapshot):
        super().__init__()
        self.turn = turn

    def render(self) -> Text:
        bp = "?? " if self.turn.is_breakpoint else "   "
        tools = f" ({len(self.turn.tool_invocations)} tools)" if self.turn.tool_invocations else ""
        return Text(f"{bp}Turn #{self.turn.turn_index} [{self.turn.turn_id[:6]}]{tools}")


class UnloopTuiApp(App):
    """3-Pane Textual Application for time-travel agent inspection and rewind."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #main-container {
        layout: horizontal;
        height: 1fr;
    }
    #timeline-pane {
        width: 35%;
        border: solid cyan;
    }
    #inspector-pane {
        width: 65%;
        border: solid green;
        padding: 1;
        overflow-y: auto;
    }
    #controls-pane {
        height: 3;
        dock: bottom;
        background: $surface;
        align: center middle;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("s", "step_next", "Next Turn"),
        ("u", "step_prev", "Prev Turn"),
    ]

    def __init__(self, db_path: str | Path, **kwargs):
        super().__init__(**kwargs)
        self.db_path = Path(db_path)
        self.store = UnloopStore(self.db_path)
        self.turns: list[TurnSnapshot] = []
        self.selected_turn: Optional[TurnSnapshot] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main-container"):
            with Vertical(id="timeline-pane"):
                yield Label("[bold cyan]Execution Timeline DAG[/bold cyan]")
                yield ListView(id="turns-list")
            with Vertical(id="inspector-pane"):
                yield Label("[bold green]Turn Inspector & State Delta[/bold green]")
                yield Static(id="inspector-content")
        with Horizontal(id="controls-pane"):
            yield Label("[bold white]Keys: [s] Next | [u] Prev | [q] Quit[/bold white]")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"unloop: {self.db_path.name}"
        cursor = self.store.conn.cursor()
        row = cursor.execute("SELECT session_id, active_branch FROM sessions LIMIT 1").fetchone()
        if row:
            session_id, active_branch = row[0], row[1]
            self.turns = self.store.get_history(session_id, branch_id=active_branch)
            turns_list = self.query_one("#turns-list", ListView)
            for t in self.turns:
                turns_list.append(TurnItem(t))
            if self.turns:
                self._update_inspector(self.turns[0])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, TurnItem):
            self._update_inspector(event.item.turn)

    def _update_inspector(self, turn: TurnSnapshot) -> None:
        self.selected_turn = turn
        content = [
            f"[bold cyan]Turn ID:[/bold cyan] {turn.turn_id}",
            f"[bold cyan]Branch:[/bold cyan] {turn.branch_id} | [bold cyan]Index:[/bold cyan] #{turn.turn_index}",
        ]

        if turn.is_breakpoint:
            content.append(f"\n[bold red]?? BREAKPOINT HIT:[/bold red] {turn.breakpoint_reason}")

        if turn.prompt:
            content.append(f"\n[bold blue]Prompt:[/bold blue]\n{turn.prompt}")

        if turn.response:
            content.append(f"\n[bold green]Response / Reasoning:[/bold green]\n{turn.response}")

        if turn.tool_invocations:
            content.append("\n[bold yellow]Tool Invocations:[/bold yellow]")
            for tool in turn.tool_invocations:
                content.append(f"  ï¿½ {tool.tool_name} (args: {json.dumps(tool.arguments)})")
                if tool.result is not None:
                    content.append(f"    -> Result: {json.dumps(tool.result)}")
                if tool.error:
                    content.append(f"    [red]-> Error: {tool.error}[/red]")

        if turn.state_delta:
            content.append(f"\n[bold magenta]State Delta:[/bold magenta]\n{json.dumps(turn.state_delta, indent=2)}")

        content.append(f"\n[bold white]Current Memory State:[/bold white]\n{json.dumps(turn.state, indent=2)}")

        self.query_one("#inspector-content", Static).update("\n".join(content))


def run_tui(db_path: str | Path):
    """Launch the interactive TUI."""
    app = UnloopTuiApp(db_path)
    app.run()


AgdbTuiApp = UnloopTuiApp
