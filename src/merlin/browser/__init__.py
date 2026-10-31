"""Browser schemas; Playwright itself runs only inside the remote sandbox."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OpenArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=4000)


class ClickArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: str = Field(min_length=1, max_length=2000)


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


def validate_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    schemas: dict[str, type[BaseModel]] = {
        "browser_open": OpenArguments,
        "browser_click": ClickArguments,
        "browser_read": EmptyArguments,
        "browser_screenshot": EmptyArguments,
        "browser_console": EmptyArguments,
    }
    schema = schemas.get(name)
    if schema is None:
        raise ValueError("Unknown browser tool")
    return schema.model_validate(arguments).model_dump()


def extra_tool_definitions() -> list[dict[str, Any]]:
    definitions = []
    for name, description, properties, required in [
        (
            "browser_open",
            "Read an approved public page or open an application on sandbox localhost.",
            {"url": {"type": "string"}},
            ["url"],
        ),
        ("browser_read", "Read the current page title, URL, and visible text.", {}, []),
        (
            "browser_click",
            "Click a CSS selector on a sandbox-local application. External clicks are disabled.",
            {"selector": {"type": "string"}},
            ["selector"],
        ),
        (
            "browser_screenshot",
            "Capture a screenshot of the current browser page as an artifact.",
            {},
            [],
        ),
        ("browser_console", "Read recent browser console and network errors.", {}, []),
    ]:
        definitions.append(
            {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )
    return definitions


browser_tool_definitions = extra_tool_definitions
