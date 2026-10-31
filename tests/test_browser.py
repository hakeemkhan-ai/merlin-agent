import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import ValidationError

from merlin.browser import validate_arguments
from merlin.browser._remote import BrowserSession, allowed_url


class BrowserPolicyTests(unittest.TestCase):
    def test_public_get_allowed_and_external_post_denied(self) -> None:
        self.assertTrue(allowed_url("https://docs.example.com/page", ["docs.example.com"]))
        self.assertFalse(
            allowed_url("https://docs.example.com/submit", ["docs.example.com"], "POST")
        )
        self.assertFalse(allowed_url("https://unapproved.example/page", ["docs.example.com"]))

    def test_local_application_interactions_allowed(self) -> None:
        self.assertTrue(allowed_url("http://localhost:3000/form", [], "POST"))
        self.assertTrue(allowed_url("http://127.0.0.1:8000/", [], "GET"))

    def test_unsafe_urls_rejected(self) -> None:
        for url in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://169.254.169.254/",
            "http://user:password@docs.example.com/",
            "http://docs.example.com:99999/",
        ]:
            with self.subTest(url=url):
                self.assertFalse(allowed_url(url, ["docs.example.com"]))

    def test_external_click_denied_before_interaction(self) -> None:
        session = BrowserSession.__new__(BrowserSession)
        session.page = Mock(url="https://docs.example.com/")
        with self.assertRaisesRegex(ValueError, "External website interactions"):
            session.execute("browser_click", {"selector": "button"})
        session.page.locator.assert_not_called()

    def test_route_aborts_unapproved_network_requests(self) -> None:
        session = BrowserSession.__new__(BrowserSession)
        session.domains = ["docs.example.com"]
        session.entries = []
        route = Mock(request=SimpleNamespace(url="https://docs.example.com/submit", method="POST"))
        session.route(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()
        self.assertEqual(session.entries[0]["kind"], "blocked_request")

    def test_model_cannot_supply_domain_policy(self) -> None:
        with self.assertRaises(ValidationError):
            validate_arguments(
                "browser_open", {"url": "https://evil.example", "allowed_domains": ["evil.example"]}
            )
