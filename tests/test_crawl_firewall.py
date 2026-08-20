"""The crawl firewall, artifact quarantine, and cross-role observation.

These paths had no coverage at all: an inverted condition in `is_allowed_request`
would have passed the entire suite while letting the crawler issue cross-origin
and state-changing requests against a real application. Two of these tests drive
a real Chromium context so the abort actually happens rather than being asserted
about in the abstract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from qualityproof.discovery import (
    DiscoveryOptions,
    RoleSpec,
    authorization_findings,
    is_allowed_request,
)
from qualityproof.models import PageState
from qualityproof.security import ArtifactMode, ArtifactPolicy


class _FakeRequest:
    """Minimal stand-in for a Playwright Request, for policy-level assertions."""

    def __init__(self, url: str, method: str = "GET") -> None:
        self.url = url
        self.method = method


ORIGIN = "https://app.example.test"


def _options(**overrides: object) -> DiscoveryOptions:
    return DiscoveryOptions(allowed_domains=("app.example.test",), **overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("url", "method"),
    [
        ("https://evil.test/steal", "GET"),
        ("https://other.example.test/x", "GET"),
        ("http://app.example.test/x", "GET"),
        ("https://app.example.test/logout", "GET"),
        ("https://app.example.test/delete/1", "GET"),
    ],
)
def test_requests_outside_policy_are_refused(url: str, method: str) -> None:
    request = _FakeRequest(url, method)

    assert not is_allowed_request(request, ORIGIN, _options(), authenticating=False)  # type: ignore[arg-type]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutating_methods_are_refused_outside_login(method: str) -> None:
    """A crawler that can POST can create, pay for, or destroy things."""
    request = _FakeRequest(f"{ORIGIN}/anything", method)

    assert not is_allowed_request(request, ORIGIN, _options(), authenticating=False)  # type: ignore[arg-type]


def test_exactly_one_login_mutation_is_permitted_and_only_while_authenticating() -> None:
    options = _options(login_submit_method="POST", login_submit_path="/login")
    login = _FakeRequest(f"{ORIGIN}/login", "POST")
    other = _FakeRequest(f"{ORIGIN}/orders", "POST")
    wrong_method = _FakeRequest(f"{ORIGIN}/login", "PUT")

    assert is_allowed_request(login, ORIGIN, options, authenticating=True)  # type: ignore[arg-type]
    assert not is_allowed_request(other, ORIGIN, options, authenticating=True)  # type: ignore[arg-type]
    assert not is_allowed_request(wrong_method, ORIGIN, options, authenticating=True)  # type: ignore[arg-type]
    # The permission closes as soon as login finishes.
    assert not is_allowed_request(login, ORIGIN, options, authenticating=False)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [f"{ORIGIN}/products", f"{ORIGIN}/products/1?page=2"],
)
def test_same_origin_reads_are_permitted(url: str) -> None:
    """The firewall must not be vacuously restrictive, or discovery finds nothing."""
    assert is_allowed_request(_FakeRequest(url), ORIGIN, _options(), authenticating=False)  # type: ignore[arg-type]


@pytest.mark.browser
def test_the_firewall_actually_aborts_a_cross_origin_request() -> None:
    """Drive a real context so the abort happens, not just the decision.

    Everything above tests the predicate. This tests the wiring: a page that
    requests a third-party asset must have that request aborted before it leaves
    the browser.
    """
    from playwright.async_api import Request, Route, async_playwright

    blocked: list[str] = []

    async def run() -> int:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context()

            async def enforce(route: Route, request: Request) -> None:
                if is_allowed_request(
                    request, ORIGIN, _options(), authenticating=False
                ):
                    await route.continue_()
                else:
                    blocked.append(f"{request.method} {request.url}")
                    await route.abort("blockedbyclient")

            await context.route("**/*", enforce)
            page = await context.new_page()
            # A data: URL needs no network, so only the injected fetches are routed.
            await page.goto("data:text/html,<h1>probe</h1>")
            for target in (
                "https://evil.test/tracker.js",
                f"{ORIGIN}/allowed.json",
            ):
                await page.evaluate(
                    "url => fetch(url).catch(() => null)",
                    target,
                )
            await page.wait_for_timeout(600)
            await context.close()
            await browser.close()
        return len(blocked)

    asyncio.run(run())

    assert any("evil.test" in entry for entry in blocked), blocked
    assert not any("allowed.json" in entry for entry in blocked), blocked


def test_role_specs_parse_both_supported_forms(tmp_path: Path) -> None:
    state = tmp_path / "shopper.json"
    state.write_text("{}", encoding="utf-8")

    by_state = RoleSpec.parse(f"shopper={state}")
    by_env = RoleSpec.parse("admin:ADMIN_USER:ADMIN_PASSWORD")

    assert by_state.name == "shopper"
    assert by_state.storage_state == state
    assert by_env.name == "admin"
    assert (by_env.username_env, by_env.password_env) == ("ADMIN_USER", "ADMIN_PASSWORD")


@pytest.mark.parametrize(
    "value",
    ["", "Shopper:A:B", "shopper:A", "shopper:A:B:C", "bad name=x.json"],
)
def test_malformed_role_specs_are_refused(value: str) -> None:
    """A role name reaches page-state ids and file paths, so it is constrained."""
    with pytest.raises(ValueError):
        RoleSpec.parse(value)


def test_a_bare_role_name_is_an_anonymous_identity() -> None:
    """Anonymous is a first-class role, not the absence of one.

    "What can someone with no session reach" is the question a privilege boundary
    is defined against, so it must be crawlable in the same pass as the
    authenticated roles — otherwise the comparison has nothing to compare to.
    """
    role = RoleSpec.parse("anonymous")

    assert role.name == "anonymous"
    assert role.anonymous is True
    assert role.storage_state is None


def test_a_status_difference_between_roles_is_reported() -> None:
    """A server-rendered application answers 403 to the wrong role."""
    pages = (
        PageState(id="s1", url="https://a.test/admin", route="/admin", status=403, role="shopper"),
        PageState(id="a1", url="https://a.test/admin", route="/admin", status=200, role="admin"),
        PageState(id="s2", url="https://a.test/cart", route="/cart", status=200, role="shopper"),
        PageState(id="a2", url="https://a.test/cart", route="/cart", status=200, role="admin"),
    )

    findings = authorization_findings(pages)

    assert len(findings) == 1
    assert findings[0].startswith("role_status_differs:/admin:")
    assert "shopper=403" in findings[0] and "admin=200" in findings[0]


def test_a_route_absent_for_one_role_is_reported_as_a_reachability_difference() -> None:
    """The signal a single-page application actually produces.

    A client-side guard redirects and never renders the link, so every status is
    200 and the boundary shows up as the route being absent for a role. Comparing
    only statuses reported nothing at all for such applications, which is most
    modern ones.
    """
    pages = (
        PageState(id="a1", url="https://a.test/admin", route="/admin", status=200, role="admin"),
        PageState(id="s1", url="https://a.test/shop", route="/shop", status=200, role="admin"),
        PageState(
            id="s2", url="https://a.test/shop", route="/shop", status=200, role="shopper"
        ),
    )

    findings = authorization_findings(pages)

    assert len(findings) == 1
    assert findings[0].startswith("role_reachability_differs:/admin:")
    assert "reached=admin" in findings[0]
    assert "absent=shopper" in findings[0]


def test_a_route_reached_by_every_role_is_not_a_difference() -> None:
    """The finding must not fire on ordinary shared pages."""
    pages = (
        PageState(id="a", url="https://a.test/x", route="/x", status=200, role="admin"),
        PageState(id="b", url="https://a.test/x", route="/x", status=200, role="shopper"),
    )

    assert authorization_findings(pages) == ()


def test_quarantine_marker_describes_why_artifacts_are_isolated(tmp_path: Path) -> None:
    """The quarantine path had no coverage and had never executed.

    It is the mechanism that makes "authenticated capture is opt-in" true rather
    than aspirational, so its output is asserted directly.
    """
    from qualityproof.execution import _UNREDACTABLE_SUFFIXES

    policy = ArtifactPolicy.from_environment(
        {
            "QUALITYPROOF_STORAGE_STATE": "auth.json",
            "QUALITYPROOF_ALLOW_UNREDACTABLE_ARTIFACTS": "1",
            "QUALITYPROOF_ARTIFACTS": "on_failure",
        }
    )

    assert policy.mode is ArtifactMode.ON_FAILURE
    assert policy.quarantined is True
    assert ".zip" in _UNREDACTABLE_SUFFIXES
    assert ".png" in _UNREDACTABLE_SUFFIXES
    # A text log is redactable and must not be quarantined.
    assert ".txt" not in _UNREDACTABLE_SUFFIXES


def test_artifact_policy_reports_which_variable_disabled_capture() -> None:
    """Silent evaporation of evidence was the real defect.

    A single unrelated variable such as GITHUB_TOKEN marks a run authenticated and
    turns capture off. That is the right default, but it must be visible, so the
    policy names the variables responsible.
    """
    policy = ArtifactPolicy.from_environment({"GITHUB_TOKEN": "ghp_example_value"})

    assert policy.mode is ArtifactMode.OFF
    assert "GITHUB_TOKEN" in policy.reasons
    assert "GITHUB_TOKEN" in policy.describe()
    assert "ghp_example_value" not in policy.describe(), "must not echo the value"
