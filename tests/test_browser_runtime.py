"""Opt-in Chromium policy checks. Run inside E2B with MERLIN_BROWSER_TESTS=1."""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from merlin.browser._remote import BrowserSession

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("MERLIN_BROWSER_TESTS") != "1", reason="E2B browser checks only"),
]


@pytest.fixture
def local_application():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(("GET", self.path, self.headers.get("Upgrade")))
            body = b"<body>Local fixture application</body>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            requests.append(("POST", self.path, None))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def browser_session():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        session = BrowserSession(playwright, {"allowed_domains": ["docs.example.com"]})
        session.context.route(
            "http://docs.example.com/**",
            lambda route: route.fulfill(
                body="<body>Untrusted public fixture</body>", content_type="text/html"
            ),
        )
        try:
            yield session
        finally:
            session.context.close()
            session.browser.close()


def test_external_page_cannot_post_to_trailing_dot_localhost(browser_session, local_application):
    port, requests = local_application
    session = browser_session
    session.execute("browser_open", {"url": "http://docs.example.com/page"})
    session.page.evaluate(
        """async url => {
            try { await fetch(url, {method: 'POST', mode: 'no-cors', body: 'fixture'}); }
            catch (_) { /* The policy must reject this request. */ }
        }""",
        f"http://localhost.:{port}/private",
    )
    assert not any(path == "/private" for _, path, _ in requests)
    assert any(entry["kind"] == "blocked_request" for entry in session.entries)


def test_websockets_never_connect_to_the_network(browser_session, local_application):
    port, requests = local_application
    session = browser_session
    session.execute("browser_open", {"url": "http://docs.example.com/page"})
    session.page.evaluate(
        """url => new Promise(resolve => {
            const socket = new WebSocket(url);
            socket.onopen = () => { socket.send('forbidden write'); socket.close(); resolve(); };
            socket.onerror = socket.onclose = () => resolve();
            setTimeout(resolve, 2000);
        })""",
        f"ws://127.0.0.1:{port}/socket",
    )
    assert not any(path == "/socket" for _, path, _ in requests)
    assert any(entry["kind"] == "blocked_websocket" for entry in session.entries)


def test_direct_local_navigation_and_http_interaction_still_work(
    browser_session, local_application
):
    port, requests = local_application
    session = browser_session
    session.execute("browser_open", {"url": f"http://localhost.:{port}/"})
    status = session.page.evaluate(
        "async () => (await fetch('/save', {method: 'POST', body: 'fixture'})).status"
    )
    assert status == 204
    assert ("POST", "/save", None) in requests
