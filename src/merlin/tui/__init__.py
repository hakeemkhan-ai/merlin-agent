"""Merlin's interactive terminal client. Providers and workspaces stay on the service."""

from __future__ import annotations

from typing import Any


def launch(**options: Any) -> None:
    from merlin.tui.app import MerlinApp

    MerlinApp(**options).run()
