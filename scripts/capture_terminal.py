#!/usr/bin/env python3
"""Capture synthetic terminal screens without connecting to any external service."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from merlin.tui.app import MerlinApp


async def capture(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    def tick() -> None:
        # Keep executor completion callbacks responsive on restricted headless hosts.
        if not loop.is_closed():
            loop.call_later(0.05, tick)

    tick()
    app = MerlinApp(demo=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.8)
        await app.action_new_session().wait()
        await pilot.pause(0.2)
        (output / "welcome.svg").write_text(app.export_screenshot(title="Merlin · Offline demo"))
        await app.open_session("demo-session").wait()
        await pilot.pause(0.8)
        (output / "conversation.svg").write_text(
            app.export_screenshot(title="Merlin · Offline demo")
        )
        app.open_approval("demo-approval")
        await pilot.pause(0.3)
        (output / "approval.svg").write_text(
            app.export_screenshot(title="Merlin · Synthetic proposal")
        )
        await pilot.press("escape")
        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.2)
        (output / "narrow.svg").write_text(app.export_screenshot(title="Merlin · 80×24"))
        await pilot.press("ctrl+q")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".merlin/reports/tui"))
    options = parser.parse_args()
    asyncio.run(capture(options.output))
    print(f"Synthetic terminal captures: {options.output}")


if __name__ == "__main__":
    main()
