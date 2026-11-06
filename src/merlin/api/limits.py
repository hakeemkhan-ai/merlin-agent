"""Small ASGI admission controls, including bodies without Content-Length."""

from __future__ import annotations

import asyncio
import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestLimits:
    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int = 1048576,
        max_streams: int = 32,
        *,
        owner_token: str | None = None,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_streams = max_streams
        self.streams = 0
        self.authorization = f"Bearer {owner_token}".encode() if owner_token is not None else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        public_callback = scope.get("method") == "GET" and path == "/v1/github/callback"
        if self.authorization is not None and path.startswith("/v1/") and not public_callback:
            # FastAPI resolves body parameters before route dependencies. Reject
            # unauthorized clients before buffering uploads or parsing JSON.
            supplied = [
                value for name, value in scope.get("headers", []) if name == b"authorization"
            ]
            if len(supplied) != 1 or not hmac.compare_digest(supplied[0], self.authorization):
                await JSONResponse(
                    {"detail": "Owner authentication required"},
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
        if scope.get("method") in {"POST", "PUT", "PATCH"}:
            headers = dict(scope.get("headers", []))
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                length = -1
            if length < 0 or length > self.max_body_bytes:
                await JSONResponse({"detail": "Request body exceeds the supported size"}, 413)(
                    scope, receive, send
                )
                return
            body = bytearray()
            try:
                async with asyncio.timeout(30):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > self.max_body_bytes:
                            await JSONResponse(
                                {"detail": "Request body exceeds the supported size"}, 413
                            )(scope, receive, send)
                            return
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await JSONResponse({"detail": "Request body deadline exceeded"}, 408)(
                    scope, receive, send
                )
                return
            upstream = receive
            buffered = bytes(body)
            delivered = False

            async def buffered_receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": buffered, "more_body": False}
                return await upstream()

            receive = buffered_receive
        streaming = (
            scope.get("method") == "GET"
            and scope.get("path", "").startswith("/v1/tasks/")
            and scope.get("path", "").endswith("/events")
        )
        if streaming and self.streams >= self.max_streams:
            await JSONResponse(
                {"detail": "Too many event streams; retry later"}, 429, headers={"Retry-After": "2"}
            )(scope, receive, send)
            return
        if streaming:
            self.streams += 1
        try:
            await self.app(scope, receive, send)
        finally:
            if streaming:
                self.streams -= 1
