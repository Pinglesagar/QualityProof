from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Request

from qualityproof.discovery import (
    DiscoveryOptions,
    Frontier,
    discover_application,
    is_allowed_request,
    is_allowed_url,
    is_denied_route,
    is_destructive,
    normalize_route,
    normalize_url,
)


def test_normalize_url_is_deterministic() -> None:
    assert normalize_url("HTTPS://Example.COM:443/a//b/?z=2&a=1#part") == (
        "https://example.com/a/b?a=1&z=2"
    )
    assert normalize_url("../next", "https://example.com/a/page") == "https://example.com/next"


def test_normalize_route_replaces_identifiers() -> None:
    assert normalize_route("https://example.com/users/123?tab=profile") == (
        "/users/:int?tab=%3Aparam"
    )
    assert normalize_route(
        "https://example.com/items/550e8400-e29b-41d4-a716-446655440000"
    ) == "/items/:uuid"


def test_origin_domain_and_destructive_policies() -> None:
    origin = "https://app.example.com/start"
    assert is_allowed_url("https://app.example.com/ok", origin, ("example.com",))
    assert not is_allowed_url("https://example.com/other", origin, ("example.com",))
    assert not is_allowed_url("https://evil.test/", origin)
    assert is_destructive("Submit Order")
    assert is_destructive("Archive record", ("archive",))
    assert not is_destructive("View order")
    assert is_denied_route("https://app.example.com/admin/delete/42", ("/admin/delete",))
    assert not is_denied_route("https://app.example.com/admin/view", ("/admin/delete",))


def test_frontier_is_fifo_deduplicated_and_depth_bounded() -> None:
    frontier = Frontier(max_depth=1)
    assert frontier.add("https://example.com/b", 1)
    assert frontier.add("https://example.com/a", 0)
    assert not frontier.add("https://example.com/b#fragment", 1)
    assert not frontier.add("https://example.com/deep", 2)
    assert frontier.pop()[:2] == ("https://example.com/b", 1)
    assert frontier.pop()[:2] == ("https://example.com/a", 0)
    assert not frontier


def test_auth_preflight_allows_only_safe_methods_and_exact_login_submission() -> None:
    options = DiscoveryOptions(
        login_submit_method="POST",
        login_submit_path="/login",
    )

    def request(method: str, url: str) -> Request:
        return cast(Request, SimpleNamespace(method=method, url=url))

    assert is_allowed_request(
        request("GET", "https://example.test/app.js"),
        "https://example.test",
        options,
        authenticating=True,
    )
    assert is_allowed_request(
        request("POST", "https://example.test/login"),
        "https://example.test",
        options,
        authenticating=True,
    )
    for method, path in (
        ("POST", "/profile"),
        ("PUT", "/login"),
        ("PATCH", "/login"),
        ("DELETE", "/account"),
    ):
        assert not is_allowed_request(
            request(method, f"https://example.test{path}"),
            "https://example.test",
            options,
            authenticating=True,
        )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        pages = {
            "/": (
                "<title>Home</title><h1>Home</h1>"
                "<a href='/users/123'>User</a>"
                "<a href='https://example.invalid/'>External</a>"
                "<a href='/delete' aria-label='Delete account'>Delete</a>"
            ),
            "/users/123": (
                "<title>User</title><h1>Profile</h1>"
                "<form action='/save'><input name='name'><button>Save</button></form>"
            ),
            "/login": (
                "<title>Login</title><form method='post'>"
                "<input id='username' name='username'>"
                "<input id='password' name='password' type='password'>"
                "<button id='submit' type='submit'>Sign in</button></form>"
            ),
        }
        body = pages.get(self.path, "<h1>Missing</h1>")
        status = 200 if self.path in pages else 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", "session=opaque-session-id; HttpOnly")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def local_site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join()


@pytest.mark.asyncio
@pytest.mark.browser
async def test_bounded_local_discovery(local_site: str, tmp_path: Path) -> None:
    try:
        result = await discover_application(
            local_site,
            tmp_path,
            DiscoveryOptions(max_pages=5, max_depth=2, max_actions=5, max_runtime_seconds=10),
        )
    except PlaywrightError as error:
        if "Executable doesn't exist" in str(error):
            pytest.skip("Playwright Chromium is not installed")
        raise
    assert sorted(page.title for page in result.pages if page.title is not None) == ["Home", "User"]
    assert len(result.edges) == 1
    reasons = {item.question for item in result.unknowns}
    assert any("external_navigation_denied" in reason for reason in reasons)
    assert any("destructive_action_guard" in reason for reason in reasons)
    assert any(item.kind.value == "response" for item in result.evidence)


@pytest.mark.asyncio
@pytest.mark.browser
async def test_authentication_secrets_are_absent_from_trace(
    local_site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    username = "QP_SENTINEL_USERNAME_09ab"
    password = "QP_SENTINEL_PASSWORD_71cd"
    monkeypatch.setenv("QP_TEST_USERNAME", username)
    monkeypatch.setenv("QP_TEST_PASSWORD", password)

    result = await discover_application(
        local_site,
        tmp_path,
        DiscoveryOptions(
            max_pages=1,
            max_depth=0,
            max_actions=1,
            max_runtime_seconds=10,
            login_url=f"{local_site}login",
            username_selector="#username",
            password_selector="#password",
            submit_selector="#submit",
            login_submit_method="POST",
            login_submit_path="/login",
            username_env="QP_TEST_USERNAME",
            password_env="QP_TEST_PASSWORD",
        ),
    )

    artifacts = tmp_path / ".qualityproof" / "discovery"
    assert not (artifacts / "trace.zip").exists()
    assert not tuple(artifacts.glob("*.png"))
    serialized = result.model_dump_json()
    assert username not in serialized
    assert password not in serialized
