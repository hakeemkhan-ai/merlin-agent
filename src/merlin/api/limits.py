"""Small ASGI admission controls, including bodies without Content-Length."""

from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestLimits:
    def __init__(self, app: ASGIApp, max_body_bytes: int = 1048576, max_streams: int = 32) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_streams = max_streams
        self.streams = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
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
