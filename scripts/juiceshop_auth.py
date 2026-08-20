"""Prepare authenticated storage states for the Juice Shop target application.

Target-specific setup lives here rather than in the tool. Juice Shop presents a
welcome dialog and a cookie banner over the login form on first visit, and those
overlays intercept clicks. Teaching the crawler to dismiss named dialogs would
make it a bundle of per-application special cases; instead this script performs
the login once, saves the resulting session, and the crawl reuses it — which is
also the pattern Playwright itself recommends.

The saved files contain live session credentials. They are written outside the
repository tree by default and are covered by .gitignore.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page, async_playwright

DISMISS_SELECTORS = (
    "button[aria-label='Close Welcome Banner']",
    "a[aria-label='dismiss cookie message']",
    ".cc-btn.cc-dismiss",
)


@dataclass(frozen=True)
class Identity:
    role: str
    username_env: str
    password_env: str

    def credentials(self) -> tuple[str, str]:
        username = os.environ.get(self.username_env)
        password = os.environ.get(self.password_env)
        if not username or not password:
            raise SystemExit(
                f"set {self.username_env} and {self.password_env} for role '{self.role}'"
            )
        return username, password


async def _dismiss_overlays(page: Page) -> None:
    """Clear the dialogs that sit over the login form."""
    for selector in DISMISS_SELECTORS:
        locator = page.locator(selector)
        try:
            if await locator.count():
                await locator.first.click(timeout=3_000)
        except Exception:
            continue


async def _login(base_url: str, identity: Identity, destination: Path) -> dict[str, object]:
    username, password = identity.credentials()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{base_url}/#/login", wait_until="domcontentloaded")
        await _dismiss_overlays(page)
        await page.fill("#email", username)
        await page.fill("#password", password)
        # Submitting from the field avoids the same overlaid touch-target problem
        # on the login button itself.
        await page.press("#password", "Enter")
        # The application is a single-page app: a successful login replaces the
        # view rather than navigating, so wait for the route to change.
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(destination))
        landing = page.url
        await context.close()
        await browser.close()
    state = json.loads(destination.read_text(encoding="utf-8"))
    origins = state.get("origins") or []
    tokens = sum(len(entry.get("localStorage") or []) for entry in origins)
    return {
        "role": identity.role,
        "state": str(destination),
        "landed_on": landing,
        "cookies": len(state.get("cookies") or []),
        "local_storage_entries": tokens,
    }


IDENTITIES = (
    Identity("customer", "JS_CUSTOMER_USER", "JS_CUSTOMER_PASS"),
    Identity("admin", "JS_ADMIN_USER", "JS_ADMIN_PASS"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".qualityproof-auth" / "juiceshop",
        help="Directory for the saved sessions. Outside the repo by default.",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    results = [
        asyncio.run(_login(base_url, identity, args.output / f"{identity.role}.json"))
        for identity in IDENTITIES
    ]
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
