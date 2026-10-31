"""Lossless native history projection with exact rendered-result deduplication."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


def provider_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Never edit stored history or opaque signed model parts."""
    projected = deepcopy({key: value for key, value in entry.items() if not key.startswith("_")})
    for part in projected.get("parts", []):
        if not isinstance(part, dict):
            continue
        function = part.get("function_response", part.get("functionResponse", {}))
        if not isinstance(function, dict):
            continue
        response = function.get("response", {})
        if not isinstance(response, dict) or not isinstance(response.get("output"), str):
            continue
        data = response.get("data")
        if not isinstance(data, dict):
            continue
        output = response["output"]
        for key in ("output", "result", "resource", "entries", "diagnostics"):
            if key not in data:
                continue
            value = data[key]
            duplicate = value == output if isinstance(value, str) else False
            if key != "output" and not duplicate:
                try:
                    duplicate = json.dumps(value) == output
                except (TypeError, ValueError):
                    pass
            if duplicate:
                del data[key]
        if not data:
            del response["data"]
    return projected


def provider_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [provider_entry(entry) for entry in history]


def serialized_size(history: list[dict[str, Any]]) -> int:
    """Match the provider JSON representation, including default separators."""
    if not history:
        return 2
    return (
        2
        + sum(len(json.dumps(provider_entry(entry), default=str)) for entry in history)
        + (2 * (len(history) - 1))
    )


def entry_sizes(history: list[dict[str, Any]]) -> list[int]:
    return [len(json.dumps(provider_entry(entry), default=str)) for entry in history]


def sizes_total(sizes: list[int]) -> int:
    return 2 + sum(sizes) + max(0, 2 * (len(sizes) - 1))
