"""unloop CLI: The Agent GNU Debugger command-line interface.

Terminal time-travel stepper, headless CI runner, and post-mortem inspector.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from unloop.storage.db import UnloopStore

console = Console()


@click.group(invoke_without_command=True)
@click.version_option(package_name="unloop")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """unloop: Time-travel debugger for AI agents - step, rewind, inspect, and branch."""
    if ctx.invoked_subcommand is None:
        console.print("[bold cyan]unloop[/bold cyan] - The Time-Travel Debugger for AI Agents.")
        console.print("Run [bold green]unloop --help[/bold green] for available commands.")


@cli.command("info")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
def info(db_path: str) -> None:
    """Display metadata, branches, and status summary of an unloop session file."""
    with UnloopStore(db_path) as store:
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
            table.add_row(
                "Total Tokens",
                f"{total_prompt_tokens + total_comp_tokens} (P: {total_prompt_tokens}, C: {total_comp_tokens})",
            )

            console.print(table)


@cli.command("history")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--session-id", default=None, help="Specific session ID to inspect.")
@click.option("--branch", default=None, help="Branch name to inspect (defaults to session active branch).")
def history(db_path: str, session_id: str | None, branch: str | None) -> None:
    """List execution turns with tool calls and state deltas."""
    with UnloopStore(db_path) as store:
        cursor = store.conn.cursor()
        if session_id:
            row = cursor.execute(
                "SELECT session_id, active_branch FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = cursor.execute(
                "SELECT session_id, active_branch FROM sessions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

        if not row:
            console.print("[red]No sessions found.[/red]")
            return

        active_sess_id = row[0]
        target_branch = branch or row[1] or "main"
        turns = store.get_history(active_sess_id, branch_id=target_branch)

        if not turns:
            console.print(f"[yellow]No turns found on branch '{target_branch}'.[/yellow]")
            return

        console.print(
            f"[bold cyan]Execution Timeline (Session: {active_sess_id[:8]}..., Branch: {target_branch}, Turns: {len(turns)})[/bold cyan]\n"
        )

        for t in turns:
            bp_tag = f" [bold red][BREAKPOINT] {t.breakpoint_reason}[/bold red]" if t.is_breakpoint else ""
            panel_title = f"Turn #{t.turn_index} (ID: {t.turn_id[:8]}){bp_tag}"

            content: list[str] = []
            if t.prompt:
                content.append(f"[bold blue]Prompt:[/bold blue] {t.prompt}")
            if t.response:
                content.append(f"[bold green]Response:[/bold green] {t.response}")

            if t.tool_invocations:
                content.append("\n[bold yellow]Tool Invocations:[/bold yellow]")
                for tool in t.tool_invocations:
                    status = "[red]FAILED[/red]" if tool.error else "[green]OK[/green]"
                    content.append(f"  * {tool.tool_name} {status} (args: {json.dumps(tool.arguments)})")
                    if tool.error:
                        content.append(f"    [red]Error: {tool.error}[/red]")

            if t.state_delta and any(t.state_delta.values()):
                content.append(f"\n[bold magenta]State Delta:[/bold magenta] {json.dumps(t.state_delta)}")

            border = "red" if t.is_breakpoint else "blue"
            console.print(Panel("\n".join(content), title=panel_title, border_style=border))


@cli.command("diff")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--from", "turn_a_idx", type=int, default=0, help="First turn index to compare.")
@click.option("--to", "turn_b_idx", type=int, default=1, help="Second turn index to compare.")
@click.option("--branch", default=None, help="Branch name (defaults to active branch).")
def diff_turns(db_path: str, turn_a_idx: int, turn_b_idx: int, branch: str | None) -> None:
    """Compare state and tool execution differences between two turns."""
    with UnloopStore(db_path) as store:
        cursor = store.conn.cursor()
        row = cursor.execute(
            "SELECT session_id, active_branch FROM sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            console.print("[red]No sessions found in file.[/red]")
            return

        session_id = row[0]
        target_branch = branch or row[1] or "main"
        turns = store.get_history(session_id, branch_id=target_branch)

        turn_a = next((t for t in turns if t.turn_index == turn_a_idx), None)
        turn_b = next((t for t in turns if t.turn_index == turn_b_idx), None)

        if not turn_a:
            console.print(f"[red]Turn #{turn_a_idx} not found in branch '{target_branch}'.[/red]")
            return
        if not turn_b:
            console.print(f"[red]Turn #{turn_b_idx} not found in branch '{target_branch}'.[/red]")
            return

        diff_res = store.diff_turns(turn_a, turn_b)
        table = Table(
            title=f"Turn Diff: #{turn_a_idx} ({turn_a.turn_id[:8]}) vs #{turn_b_idx} ({turn_b.turn_id[:8]})",
            border_style="magenta",
        )
        table.add_column("Category", style="bold cyan")
        table.add_column(f"Turn #{turn_a_idx}", style="yellow")
        table.add_column(f"Turn #{turn_b_idx}", style="green")

        # Compare tools
        table.add_row(
            "Tools Executed",
            ", ".join(diff_res["tools_a"]) or "<none>",
            ", ".join(diff_res["tools_b"]) or "<none>",
        )

        # State modifications
        s_diff = diff_res["state_diff"]
        added = s_diff["added"]
        modified = s_diff["modified"]
        removed = s_diff["removed"]

        if added:
            table.add_row("State Added", "-", json.dumps(added, indent=1))
        if removed:
            table.add_row("State Removed", ", ".join(removed), "-")
        if modified:
            for k, change in modified.items():
                table.add_row(
                    f"State Modified: {k}",
                    json.dumps(change["turn_a"]),
                    json.dumps(change["turn_b"]),
                )

        if not added and not removed and not modified:
            table.add_row("State Changes", "<identical state>", "<identical state>")

        console.print(table)


@cli.command("record", context_settings=dict(ignore_unknown_options=True))
@click.option("--output", "-o", default="session.unloop", help="Target session file path.")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def record(output: str, command: tuple[str, ...]) -> None:
    """Run an agent command under headless unloop recording for CI and post-mortems.

    Example: unloop record -o session.unloop -- python agent.py
    """
    if not command:
        console.print("[red]Error: No command specified to record.[/red]")
        sys.exit(1)

    cmd_list = list(command)
    # Strip leading '--' if present
    if cmd_list and cmd_list[0] == "--":
        cmd_list = cmd_list[1:]

    env = os.environ.copy()
    env["UNLOOP_SESSION_FILE"] = str(Path(output).resolve())
    env["UNLOOP_AUTO_RECORD"] = "1"

    console.print(f"[bold cyan]unloop record:[/bold cyan] Running command: [bold green]{' '.join(cmd_list)}[/bold green]")
    console.print(f"[dim]Output session file: {output}[/dim]\n")

    result = subprocess.run(cmd_list, env=env)

    if Path(output).exists():
        with UnloopStore(output) as store:
            cursor = store.conn.cursor()
            turn_count = cursor.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            bp_count = cursor.execute("SELECT COUNT(*) FROM turns WHERE is_breakpoint = 1").fetchone()[0]
            console.print(
                f"\n[bold green]unloop record finished:[/bold green] Recorded {turn_count} turns ({bp_count} breakpoints tripped) in '{output}'."
            )

    sys.exit(result.returncode)


@cli.command("check")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--max-turns", type=int, default=None, help="Maximum allowable turns before failure.")
@click.option("--no-oscillations", is_flag=True, default=False, help="Fail if any loop or oscillation occurred.")
@click.option("--no-errors", is_flag=True, default=False, help="Fail if any tool execution resulted in an error.")
@click.option(
    "--fail-on-breakpoint", is_flag=True, default=False, help="Fail if any execution breakpoint was triggered."
)
def check_ci(
    db_path: str,
    max_turns: int | None,
    no_oscillations: bool,
    no_errors: bool,
    fail_on_breakpoint: bool,
) -> None:
    """CI quality gate: evaluate unloop session against regression and loop thresholds."""
    violations: list[str] = []

    with UnloopStore(db_path) as store:
        cursor = store.conn.cursor()
        row = cursor.execute(
            "SELECT session_id, name, active_branch FROM sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            console.print("[red]Error: Session file contains no valid sessions.[/red]")
            sys.exit(1)

        session_id, name, active_branch = row[0], row[1], row[2]
        turns = store.get_history(session_id, branch_id=active_branch)

        # 1. Check turn count threshold
        if max_turns is not None and len(turns) > max_turns:
            violations.append(
                f"Turn count limit exceeded: Agent took {len(turns)} turns (threshold is {max_turns})."
            )

        # 2. Check oscillations / watchdog alerts
        if no_oscillations:
            oscillation_turns = [
                t for t in turns if t.is_breakpoint and "Watchdog" in (t.breakpoint_reason or "")
            ]
            if oscillation_turns:
                for ot in oscillation_turns:
                    violations.append(f"Turn #{ot.turn_index}: Oscillation detected ({ot.breakpoint_reason})")

        # 3. Check tool errors
        if no_errors:
            for t in turns:
                for tool in t.tool_invocations:
                    if tool.error:
                        violations.append(
                            f"Turn #{t.turn_index}: Tool '{tool.tool_name}' failed with error: {tool.error}"
                        )

        # 4. Check breakpoints
        if fail_on_breakpoint:
            bp_turns = [t for t in turns if t.is_breakpoint]
            if bp_turns:
                for bpt in bp_turns:
                    violations.append(
                        f"Turn #{bpt.turn_index}: Breakpoint triggered: {bpt.breakpoint_reason}"
                    )

    if violations:
        console.print(f"[bold red]unloop check FAILED ({len(violations)} violations in '{name}'):[/bold red]")
        for v in violations:
            console.print(f"  [red]x[/red] {v}")
        sys.exit(1)
    else:
        console.print(
            f"[bold green]unloop check PASSED:[/bold green] All quality assertions satisfied for '{name}' ({len(turns)} turns)."
        )
        sys.exit(0)


@cli.command("export")
@click.argument("db_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--session-id", default=None, help="Specific session ID to export.")
@click.option("--output", "-o", type=click.Path(), default="incident_report.md", help="Output file path.")
def export_report(db_path: str, session_id: str | None, output: str) -> None:
    """Export an unloop session into a comprehensive Markdown post-mortem incident report."""
    with UnloopStore(db_path) as store:
        cursor = store.conn.cursor()
        if session_id:
            row = cursor.execute(
                "SELECT session_id, name, framework, active_branch FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = cursor.execute(
                "SELECT session_id, name, framework, active_branch FROM sessions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

        if not row:
            console.print("[red]No sessions found.[/red]")
            return

        active_sess_id, name, framework, active_branch = row[0], row[1], row[2], row[3]
        turns = store.get_history(active_sess_id, branch_id=active_branch or "main")

        md = [
            f"# unloop Post-Mortem Incident Report: {name}",
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
                md.append(f"> [WARNING] **Breakpoint Tripped**: {t.breakpoint_reason}")
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
def replay(db_path: str) -> None:
    """Launch the interactive 3-pane terminal TUI to inspect and step through an unloop session."""
    from unloop.tui.app import run_tui

    run_tui(db_path)
