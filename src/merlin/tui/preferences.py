"""Atomic, private UI preferences; conversation drafts and tokens are never stored here."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from merlin.client import config_path


def preference_path() -> Path:
    override = os.environ.get("MERLIN_UI_CONFIG")
    return Path(override) if override else config_path().with_name("ui.json")


def load_preferences() -> dict[str, Any]:
    try:
        value = json.loads(preference_path().read_text())
        if not isinstance(value, dict):
            return {}
        result = {
            key: value[key]
            for key in ("theme", "repository", "session_id")
            if isinstance(value.get(key), str)
        }
        if isinstance(value.get("models"), list):
            result["models"] = [
                model for model in value["models"] if isinstance(model, str) and len(model) <= 256
            ][:10]
        return result
    except (OSError, ValueError):
        return {}


def save_preferences(value: dict[str, Any]) -> None:
    path = preference_path()
    allowed = {
        key: value[key] for key in ("theme", "repository", "session_id", "models") if key in value
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".ui-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(allowed, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
