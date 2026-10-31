"""UI-independent connection settings, safe display text, and async task transport."""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx


def terminal_text(value: str) -> str:
    """Keep printable Unicode, including Indic joiners, without terminal controls."""
    return "".join(
        char
        for char in value
        if char in "\n\t"
        or (
            unicodedata.category(char) != "Cc"
            and char not in "\u202a\u202b\u202d\u202e\u202c\u2066\u2067\u2068\u2069"
        )
    )


def config_path() -> Path:
    override = os.environ.get("MERLIN_CLIENT_CONFIG")
    return Path(override) if override else Path.home() / ".config" / "merlin" / "client.json"


def load_config() -> dict[str, str]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError("Cannot read Merlin client configuration") from error
    if not isinstance(value, dict):
        raise ValueError("Invalid Merlin client configuration")
    return {key: str(value[key]) for key in ("api_url", "owner_token") if key in value}


def save_config(api_url: str, owner_token: str) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".client-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"api_url": api_url.rstrip("/"), "owner_token": owner_token}, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_connection(url: str) -> str:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as error:
        raise ValueError("Enter a valid Merlin API URL") from error
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The server URL must not contain credentials, queries, or fragments")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.host in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("Remote Merlin connections require HTTPS")
    if not parsed.host:
        raise ValueError("Enter a valid Merlin API URL")
    return url.rstrip("/")


def connection() -> tuple[str, str]:
    configuration = load_config()
    token = os.environ.get("MERLIN_OWNER_TOKEN", configuration.get("owner_token", ""))
    url = os.environ.get("MERLIN_API_URL", configuration.get("api_url", "http://127.0.0.1:8000"))
    if not token:
        raise ValueError("Run 'merlin login' or connect in the terminal interface")
    return validate_connection(url), token


class ClientError(Exception):
    """A static public error; never expose URLs, credentials, or response bodies."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def check_response(response: httpx.Response) -> None:
    if not response.is_error:
        return
    status = response.status_code
    message = {
        401: "Owner authentication failed. Check your connection credential.",
        403: "This operation is not authorized.",
        404: "This resource or server feature is unavailable.",
        409: "The task or proposal changed. Refresh before trying again.",
        413: "This preview exceeds the supported size.",
        422: "Check the supplied repository, model, or message.",
        429: "The service is busy. Retry in a moment.",
    }.get(status, "The service could not complete this request.")
    raise ClientError(f"{message} (HTTP {status})", status)


class EventDecoder:
    """Incremental SSE framing shared by synchronous and asynchronous clients."""

    def __init__(self) -> None:
        self.data: list[str] = []
        self.size = 0

    def feed(self, line: str) -> dict[str, Any] | None:
        if not line:
            data, self.data = self.data, []
            self.size = 0
            if data:
                try:
                    event = json.loads("\n".join(data))
                    if (
                        not isinstance(event, dict)
                        or type(event.get("sequence")) is not int
                        or event["sequence"] < 1
                        or not isinstance(event.get("kind"), str)
                        or not isinstance(event.get("data", {}), dict)
                    ):
                        raise ValueError
                except ValueError as error:
                    raise ClientError(
                        "Received an invalid task event. Reconnect to replay it."
                    ) from error
                return event
        elif line.startswith("data:"):
            self.data.append(line[5:].removeprefix(" "))
            self.size += len(line)
            if self.size > 24 * 1024 * 1024:
                raise ClientError("Task event exceeds the supported size.")
        return None


async def sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Parse complete SSE frames. Advance the cursor only after applying an event."""
    check_response(response)
    decoder = EventDecoder()
    async for line in response.aiter_lines():
        if event := decoder.feed(line):
            yield event
    # An incomplete frame is deliberately not consumed: reconnect replays it.


class MerlinClient:
    def __init__(self, url: str, token: str, *, transport: Any = None) -> None:
        self.url = validate_connection(url)
        self.http = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30, read=60),
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.http.request(method, path, **kwargs)
            check_response(response)
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ClientError("Cannot reach Merlin. Check the server and connection.") from error

    async def events(self, task_id: str, after: int) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self.http.stream(
                "GET", f"/v1/tasks/{task_id}/events", headers={"Last-Event-ID": str(after)}
            ) as response:
                async for event in sse_events(response):
                    yield event
        except httpx.HTTPError as error:
            raise ClientError(
                "Connection interrupted. Reconnect to continue following this task."
            ) from error
