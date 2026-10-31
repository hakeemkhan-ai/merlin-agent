"""Sandbox-local browser daemon. The hosted control plane never imports Playwright."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

PORT = 8765
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TEMPLATE_BROWSER_DIR = "/opt/merlin-browsers"


def allowed_url(url: str, domains: list[str], method: str = "GET") -> bool:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in LOCAL_HOSTS:
            return True
        return host in domains and method.upper() in {"GET", "HEAD"}
    except ValueError:
        return False


def local_url(url: str) -> bool:
    return (
        allowed_url(url, []) and (urlsplit(url).hostname or "").lower().rstrip(".") in LOCAL_HOSTS
    )


class BrowserSession:
    def __init__(self, playwright: Any, configuration: dict[str, Any]) -> None:
        self.domains = configuration["allowed_domains"]
        self.browser = playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(service_workers="block")
        self.context.route("**/*", self.route)
        # HTTP routing does not cover WebSocket messages. Without frame-origin
        # attribution, even a localhost socket could be opened by an external
        # iframe. Fail closed for all sockets before creating any page.
        self.context.route_web_socket("**/*", self.block_websocket)
        self.entries: list[dict[str, str]] = []
        self.navigation_target: str | None = None
        self.page = self.context.new_page()
        self.page.on("console", lambda message: self.record("console", message.text))
        self.page.on("pageerror", lambda error: self.record("pageerror", str(error)))
        self.page.on(
            "requestfailed",
            lambda request: self.record("network", request.url + ": " + str(request.failure)),
        )
        self.page.set_default_timeout(15000)

    def record(self, kind: str, text: str) -> None:
        self.entries.append({"kind": kind, "text": text[:2000]})
        self.entries = self.entries[-100:]

    def block_websocket(self, socket: Any) -> None:
        self.record("blocked_websocket", socket.url)
        # Routed sockets stay local unless connect_to_server() is called. Consume
        # messages without forwarding them. Closing synchronously during route
        # setup deadlocks the pinned Playwright transport before setup returns.
        socket.on_message(lambda message: None)

    def route(self, route: Any) -> None:
        request = route.request
        permitted = allowed_url(request.url, self.domains, request.method)
        if permitted and local_url(request.url):
            # Public pages cannot use the browser as a proxy into local apps.
            # A direct, tool-requested local navigation is the only exception
            # to requiring an already-local top-level page and ancestor frames.
            target = getattr(self, "navigation_target", None)
            direct = bool(
                target
                and local_url(target)
                and request.is_navigation_request()
                and request.frame == self.page.main_frame
            )
            predecessor = request
            local_chain = True
            while predecessor is not None:
                if not local_url(predecessor.url):
                    local_chain = False
                    break
                predecessor = predecessor.redirected_from
            permitted = local_chain and (direct or local_url(self.page.url))
            permitted = permitted and request.frame.page == self.page
            if permitted and not direct:
                frame = request.frame
                while frame is not None:
                    if frame.url not in {"about:blank", ""} and not local_url(frame.url):
                        permitted = False
                        break
                    frame = frame.parent_frame
        if permitted:
            route.continue_()
        else:
            self.record("blocked_request", route.request.method + " " + route.request.url)
            route.abort()

    def execute(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation == "browser_open":
            url = arguments["url"]
            if not allowed_url(url, self.domains):
                raise ValueError(
                    "URL is outside approved public domains or sandbox-local applications"
                )
            self.navigation_target = url
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            finally:
                self.navigation_target = None
        elif operation == "browser_click":
            if not local_url(self.page.url):
                raise ValueError(
                    "External website interactions are disabled; "
                    "clicks require a sandbox-local application"
                )
            self.page.locator(arguments["selector"]).click()
        elif operation == "browser_screenshot":
            target = Path("/tmp") / ("merlin-screenshot-" + uuid4().hex + ".png")
            self.page.screenshot(path=str(target), full_page=True, timeout=15000)
            if target.stat().st_size > 16 * 1024 * 1024:
                target.unlink()
                raise ValueError("Screenshot exceeds artifact size limit")
            return {"output": "Browser screenshot captured", "screenshot_path": str(target)}
        elif operation == "browser_console":
            return {"output": json.dumps(self.entries), "entries": self.entries}
        elif operation != "browser_read":
            raise ValueError("Unknown browser operation")
        if self.page.url != "about:blank" and not allowed_url(self.page.url, self.domains):
            raise ValueError("Navigation left approved destinations")
        text = (
            self.page.locator("body").inner_text(timeout=5000)
            if self.page.url != "about:blank"
            else ""
        )
        return {
            "output": text[:20000],
            "url": self.page.url,
            "title": self.page.title(),
            "truncated": len(text) > 20000,
        }


def serve(configuration_file: str) -> None:
    # E2B's command environment may omit the Docker image's cache setting.
    # Configure it before Playwright starts its driver process, while respecting
    # explicit settings and custom templates that use Playwright's default cache.
    if Path(TEMPLATE_BROWSER_DIR).is_dir() and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = TEMPLATE_BROWSER_DIR
    from playwright.sync_api import sync_playwright

    configuration = json.loads(Path(configuration_file).read_text())
    with sync_playwright() as playwright:
        session = BrowserSession(playwright, configuration)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.headers.get("X-Merlin-Token") != configuration["token"]:
                    self.send_error(403)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 16000:
                    self.send_error(413)
                    return
                try:
                    request = json.loads(self.rfile.read(length))
                    if request["operation"] == "ping":
                        result = {"root": configuration["root"]}
                    else:
                        result = session.execute(request["operation"], request.get("arguments", {}))
                    response = {"ok": True, "data": result}
                except Exception as error:
                    response = {"ok": False, "error": str(error)}
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *arguments: Any) -> None:
                pass

        HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def _request(
    configuration: dict[str, Any], operation: str, arguments: dict[str, Any], timeout: int = 35
) -> dict[str, Any]:
    request = Request(
        f"http://127.0.0.1:{PORT}/",
        data=json.dumps({"operation": operation, "arguments": arguments}).encode(),
        headers={"X-Merlin-Token": configuration["token"], "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read(1024 * 1024))
    if not result.get("ok"):
        raise ValueError(result.get("error", "Browser operation failed"))
    return dict(result["data"])


def operate(request: dict[str, Any]) -> dict[str, Any]:
    root = request["root"]
    arguments = request.get("arguments", {})
    domains = arguments.pop("allowed_domains", [])
    configuration_file = Path("/tmp/merlin-browser.json")
    configuration = None
    if configuration_file.exists():
        candidate = json.loads(configuration_file.read_text())
        if candidate["root"] != root or candidate["allowed_domains"] != domains:
            raise ValueError("Browser policy differs from the workspace policy")
        try:
            _request(candidate, "ping", {}, timeout=1)
            configuration = candidate
        except (OSError, URLError):
            pass
    if configuration is None:
        configuration = {"root": root, "allowed_domains": domains, "token": uuid4().hex}
        configuration_file.write_text(json.dumps(configuration))
        configuration_file.chmod(0o600)
        with Path("/tmp/merlin-browser.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-I", __file__, "--serve", str(configuration_file)],
                stdout=log,
                stderr=log,
                start_new_session=True,
                cwd=root,
            )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                _request(configuration, "ping", {}, timeout=1)
                break
            except (OSError, URLError):
                time.sleep(0.1)
        else:
            # A failed launch must not keep an old token/process alive while a
            # retry replaces its configuration and starts a second daemon.
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            configuration_file.unlink(missing_ok=True)
            raise RuntimeError("Sandbox browser did not start; inspect /tmp/merlin-browser.log")
    return _request(configuration, request["operation"], arguments)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(sys.argv[2])
    else:
        try:
            print(
                json.dumps({"ok": True, "data": operate(json.loads(Path(sys.argv[1]).read_text()))})
            )
        except Exception as error:
            print(json.dumps({"ok": False, "error": str(error)}))
