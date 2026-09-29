"""Tests for unloop Textual 3-pane TUI application."""

import asyncio
import tempfile
from pathlib import Path

from textual.widgets import ListView

import unloop
from unloop.tui.app import UnloopTuiApp


def test_tui_navigation_and_interactions() -> None:
    async def _run_tui_test() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tui_test.unloop"

            with unloop.session("tui_session", db_path=db_path) as dbg:
                with dbg.step(prompt="First Step", state={"status": "initial", "counter": 0}) as step:
                    step.record_tool("init", arguments={}, result="OK")
                    step.set_response("Initialized")

                with dbg.step(prompt="Second Step", state={"status": "active", "counter": 1}) as step:
                    step.record_tool("compute", arguments={"x": 10}, result=20)
                    step.set_response("Computed result")

            app = UnloopTuiApp(db_path)
            try:
                async with app.run_test() as pilot:
                    assert len(app.turns) == 2
                    assert app.selected_turn is not None
                    assert app.selected_turn.turn_index == 0

                    # Press 's' to step next
                    await pilot.press("s")
                    list_view = app.query_one("#turns-list", ListView)
                    assert list_view.index == 1

                    # Press 'u' to step prev
                    await pilot.press("u")
                    assert list_view.index == 0

                    # Press 'm' to mutate state
                    await pilot.press("m")
                    assert app.selected_turn.state.get("_mutated_in_tui") is True

                    # Press 'f' to fork branch
                    await pilot.press("f")
                    assert "fork_turn_" in app.active_branch
            finally:
                app.store.close()

    asyncio.run(_run_tui_test())
