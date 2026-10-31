"""Read trusted worker-presence metadata; never load or execute task repositories."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any


def healthy(
    heartbeat: Path,
    *,
    max_age_seconds: int,
    readiness: Callable[[int], Mapping[str, Any]],
    timestamp: float | None = None,
) -> bool:
    try:
        info = heartbeat.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return False
        age = (time.time() if timestamp is None else timestamp) - info.st_mtime
        if not 0 <= age <= max_age_seconds:
            return False
        status = readiness(max_age_seconds)
        return status.get("ready") is True
    except Exception:
        return False


def main() -> int:
    from merlin.config import Settings
    from merlin.persistence import SQLTaskStore

    store = None
    try:
        settings = Settings(_env_file=None)
        store = SQLTaskStore(settings.database_url)
        return (
            0
            if healthy(
                Path("/tmp/merlin-worker-heartbeat"),
                max_age_seconds=settings.readiness_worker_max_age_seconds,
                readiness=store.readiness,
            )
            else 1
        )
    except Exception:
        # Docker records healthcheck output; configuration/connection errors may
        # embed database URLs, so expose the exit status without a traceback.
        return 1
    finally:
        if store is not None:
            with suppress(Exception):
                store.close()


if __name__ == "__main__":
    raise SystemExit(main())
