"""Per-identity fixtures for the Juice Shop suite.

Three privilege levels are needed by the requirement baseline, and a single
`page` fixture cannot express that: an anonymous test must run with no session,
and the two authenticated tests must not share one. Each identity therefore gets
its own browser context, created from a session captured once by
`scripts.juiceshop_auth` rather than by logging in per test.

That is deliberate. A suite that logs in inside each test is slow, and worse, it
couples tests together: a login failure in one becomes an unexplained failure in
another. One login, many contexts, no shared state.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page

AUTH_DIRECTORY = Path(
    os.environ.get("JUICESHOP_AUTH_DIR", Path.home() / ".qualityproof-auth" / "juiceshop")
)
BASE_URL = os.environ.get("JUICESHOP_BASE_URL", "http://127.0.0.1:3000")


def _storage_state(role: str) -> Path:
    path = AUTH_DIRECTORY / f"{role}.json"
    if not path.is_file():
        pytest.skip(
            f"no saved session for '{role}'. Run: python -m scripts.juiceshop_auth"
        )
    return path


@pytest.fixture(scope="session")
def juiceshop_base_url() -> str:
    return BASE_URL.rstrip("/")


#: The application reads dismissal of its welcome banner and cookie notice from
#: *cookies*, not local storage -- confirmed by reading the shipped bundle rather
#: than guessing. Seeding them suppresses both before the first paint.
#:
#: The alternative -- clicking the dialogs -- is flaky by construction: Angular
#: Material renders a touch-target span over each button that swallows the pointer
#: event, and the modal backdrop then intercepts every subsequent click. Setup
#: should never be a source of failure.
DISMISSAL_COOKIES = ("welcomebanner_status", "cookieconsent_status")


def _context(
    browser: Browser, storage_state: Path | None, base_url: str
) -> BrowserContext:
    context = browser.new_context(
        storage_state=str(storage_state) if storage_state else None,
        locale="en-GB",
        timezone_id="Europe/London",
        viewport={"width": 1280, "height": 800},
    )
    context.add_cookies(
        [
            {"name": name, "value": "dismiss", "url": base_url}
            for name in DISMISSAL_COOKIES
        ]
    )
    return context


@pytest.fixture
def anonymous_page(browser: Browser, juiceshop_base_url: str) -> Iterator[Page]:
    """A visitor with no session at all."""
    context = _context(browser, None, juiceshop_base_url)
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def customer_page(browser: Browser, juiceshop_base_url: str) -> Iterator[Page]:
    """An authenticated customer holding no administrative privilege."""
    context = _context(browser, _storage_state("customer"), juiceshop_base_url)
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def admin_page(browser: Browser, juiceshop_base_url: str) -> Iterator[Page]:
    context = _context(browser, _storage_state("admin"), juiceshop_base_url)
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def visit(juiceshop_base_url: str):
    """Navigate to a client-side route and wait for the view to render.

    A single-page application finishes its document load before the view exists,
    so there is no load event that means "ready". Waiting for content is
    framework-agnostic and avoids a fixed sleep, which would be too short on a
    slow machine and wasted time on a fast one.
    """

    def _visit(page: Page, route: str) -> None:
        page.goto(f"{juiceshop_base_url}/#{route}", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => (document.body.innerText || '').trim().length > 0", timeout=15_000
        )
        _dismiss_overlays(page)

    return _visit


def _dismiss_overlays(page: Page) -> None:
    """Clear the welcome dialog and cookie banner.

    Forced because Angular Material renders a touch-target span over each button
    that swallows the pointer event. This is chrome, not behaviour under test.
    """
    for selector in (
        "button[aria-label='Close Welcome Banner']",
        "a[aria-label='dismiss cookie message']",
    ):
        locator = page.locator(selector)
        try:
            if locator.count():
                locator.first.click(timeout=3_000, force=True)
        except Exception:
            continue


@pytest.fixture(scope="session")
def account_identifiers() -> dict[str, str]:
    """The email each saved session belongs to, read from the session itself.

    Read rather than hard-coded so the assertion cannot silently drift from the
    account actually in use.
    """
    identifiers: dict[str, str] = {}
    for role in ("customer", "admin"):
        path = AUTH_DIRECTORY / f"{role}.json"
        if not path.is_file():
            continue
        state = json.loads(path.read_text(encoding="utf-8"))
        for origin in state.get("origins") or []:
            for item in origin.get("localStorage") or []:
                if item.get("name") == "email":
                    identifiers[role] = str(item.get("value", ""))
    return identifiers
