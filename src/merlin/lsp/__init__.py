"""Typed language navigation declarations for sandbox-local language servers."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from merlin.sandbox._remote import excluded, relative_name


class LocationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    line: int = Field(default=1, ge=1)
    column: int = Field(default=1, ge=1)


class FileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class SymbolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    language: Literal["python", "typescript"] = "typescript"


def validate_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    schemas: dict[str, type[BaseModel]] = {
        "lsp_definition": LocationArguments,
        "lsp_references": LocationArguments,
        "lsp_hover": LocationArguments,
        "lsp_symbols": SymbolArguments,
        "lsp_diagnostics": FileArguments,
    }
    schema = schemas.get(name)
    if schema is None:
        raise ValueError("Unknown language-server tool")
    values = schema.model_validate(arguments).model_dump()
    if "path" in values:
        path = relative_name(values["path"])
        if excluded(path, caches=False):
            raise ValueError("Secret paths cannot be accessed by language-server tools")
        values["path"] = path
    return values


def extra_tool_definitions() -> list[dict[str, Any]]:
    definitions = []
    for name, description in [
        ("lsp_definition", "Find the definition at a 1-based line and column."),
        ("lsp_references", "Find symbol references at a 1-based line and column."),
        ("lsp_hover", "Read language-server information at a 1-based line and column."),
        ("lsp_diagnostics", "Read current diagnostics for a Python or TypeScript/JavaScript file."),
    ]:
        properties: dict[str, dict[str, Any]] = {"path": {"type": "string"}}
        if name != "lsp_diagnostics":
            properties.update(
                {
                    "line": {"type": "integer", "minimum": 1},
                    "column": {"type": "integer", "minimum": 1},
                }
            )
        definitions.append(
            {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": ["path"],
                    "additionalProperties": False,
                },
            }
        )
    definitions.append(
        {
            "name": "lsp_symbols",
            "description": "Find workspace symbols in Python or TypeScript repositories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "language": {"type": "string", "enum": ["python", "typescript"]},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }
    )
    return definitions


lsp_tool_definitions = extra_tool_definitions
