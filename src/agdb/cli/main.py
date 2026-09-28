"""Command line interface for agdb."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agdb.storage.db import AgdbStore

console = Console()


@click.group()
@click.version_option(package_name="agdb")
def cli():
    """agdb: Agent GNU Debugger - Terminal time-travel stepper and post-mortem inspector."""
    pass


@cli.command("info")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
def info(db_path: str):
    """Display metadata, branches, and status summary of an agdb session file."""
    with AgdbStore(db_path) as store:
        cursor = store.conn.cursor()
        sessions = cursor.execute(
            "SELECT session_id, name, created_at, framework, active_branch FROM sessions ORDER BY created_at DESC"
        ).fetchall()

        if not sessions:
            console.print("[yellow]No recorded sessions found in file.[/yellow]")
            return

        for s in sessions:
            session_id = s["session_id"]
            meta = store.get_session(session_id)
            if not meta:
                continue

            turns = store.get_history(session_id, meta.active_branch)
            total_prompt_tokens = sum(t.telemetry.prompt_tokens for t in turns)
            total_comp_tokens = sum(t.telemetry.completion_tokens for t in turns)
            breakpoints_hit = sum(1 for t in turns if t.is_breakpoint)

            table = Table(title=f"Session: {meta.name} ({session_id[:8]}...)", border_style="cyan")
            table.add_column("Property", style="bold green")
            table.add_column("Value", style="white")

            table.add_row("Session ID", session_id)
            table.add_row("Framework", meta.framework)
            table.add_row("Model", meta.model_name or "N/A")
            table.add_row("Active Branch", meta.active_branch)
            table.add_row("Total Turns", str(meta.total_turns))
            table.add_row("Breakpoints Triggered", str(breakpoints_hit))
            table.add_row("Total Tokens", f"{total_prompt_tokens + total_comp_tokens} (P: {total_prompt_tokens}, C: {total_comp_tokens})")

            console.print(table)


@cli.command("history")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--session-id", default=None, help="Specific session ID to inspect.")
@click.option("--branch", default=None, help="Branch name to inspect (defaults to session active branch).")
def history(db_path: str, session_id: Optional[str], branch: Optional[str]):
    """List execution turns with tool calls and state deltas."""
    with AgdbStore(db_path) as store:
        cursor = store.conn.cursor()
        if session_id:
            row = cursor.execute("SELECT session_id, active_branch FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        else:
            row = cursor.execute("SELECT session_id, active_branch FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()

        if not row:
            console.print("[red]No sessions found.[/red]")
            return

        active_sess_id = row[0]
        target_branch = branch or row[1] or "main"
        turns = store.get_history(active_sess_id, branch_id=target_branch)

        if not turns:
            console.print(f"[yellow]No turns found on branch '{target_branch}'.[/yellow]")
            return

        console.print(f"[bold cyan]Execution Timeline (Session: {active_sess_id[:8]}..., Branch: {target_branch}, Turns: {len(turns)})[/bold cyan]\n")

        for t in turns:
            bp_tag = f" [bold red]🛑 {t.breakpoint_reason}[/bold red]" if t.is_breakpoint else ""
            panel_title = f"Turn #{t.turn_index} (ID: {t.turn_id[:8]}){bp_tag}"

            content = []
            if t.prompt:
                content.append(f"[bold blue]Prompt:[/bold blue] {t.prompt}")
            if t.response:
                content.append(f"[bold green]Response:[/bold green] {t.response}")

            if t.tool_invocations:
                content.append("\n[bold yellow]Tool Invocations:[/bold yellow]")
                for tool in t.tool_invocations:
                    status = "[red]FAILED[/red]" if tool.error else "[green]OK[/green]"
                    content.append(f"  • {tool.tool_name} {status} (args: {json.dumps(tool.arguments)})")
                    if tool.error:
                        content.append(f"    [red]Error: {tool.error}[/red]")

            if t.state_delta and any(t.state_delta.values()):
                content.append(f"\n[bold magenta]State Delta:[/bold magenta] {json.dumps(t.state_delta)}")

            border = "red" if t.is_breakpoint else "blue"
            console.print(Panel("\n".join(content), title=panel_title, border_style=border))


@cli.command("export")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--session-id", default=None, help="Specific session ID to export.")
@click.option("--output", "-o", type=click.Path(), default="incident_report.md", help="Output file path.")
def export_report(db_path: str, session_id: Optional[str], output: str):
    """Export an agdb session into a comprehensive Markdown post-mortem incident report."""
    with AgdbStore(db_path) as store:
        cursor = store.conn.cursor()
        if session_id:
            row = cursor.execute("SELECT session_id, name, framework, active_branch FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        else:
            row = cursor.execute("SELECT session_id, name, framework, active_branch FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()

        if not row:
            console.print("[red]No sessions found.[/red]")
            return

        active_sess_id, name, framework, active_branch = row[0], row[1], row[2], row[3]
        turns = store.get_history(active_sess_id, branch_id=active_branch or "main")

        md = [
            f"# agdb Post-Mortem Incident Report: {name}",
            f"- **Session ID**: `{active_sess_id}`",
            f"- **Framework**: `{framework}`",
            f"- **Branch**: `{active_branch}`",
            f"- **Total Turns**: {len(turns)}",
            f"- **Breakpoints Triggered**: {sum(1 for t in turns if t.is_breakpoint)}",
            "",
            "## Timeline of Turns",
            "",
        ]

        for t in turns:
            md.append(f"### Turn #{t.turn_index} (`{t.turn_id[:8]}`)")
            if t.is_breakpoint:
                md.append(f"> ⚠️ **Breakpoint Tripped**: {t.breakpoint_reason}")
                md.append("")
            if t.prompt:
                md.append(f"**Prompt**: {t.prompt}")
            if t.response:
                md.append(f"**Response**: {t.response}")
            if t.tool_invocations:
                md.append("**Tools Executed**:")
                for tool in t.tool_invocations:
                    md.append(f"- `{tool.tool_name}`: `{json.dumps(tool.arguments)}` -> `{tool.result or tool.error}`")
            if t.state:
                md.append(f"**State Snapshot**: `{json.dumps(t.state)}`")
            md.append("")

    Path(output).write_text("\n".join(md), encoding="utf-8")
    console.print(f"[green]Successfully exported incident report to {output}[/green]")


@cli.command("replay")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
def replay(db_path: str):
    """Launch the interactive terminal TUI to inspect and step through an agdb session."""
    from agdb.tui.app import run_tui
    run_tui(db_path)
