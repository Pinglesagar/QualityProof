"""Client-side routes must be distinguished from in-page anchors.

Single-page applications address views through the URL fragment. Before this
existed, `normalize_url` discarded fragments, so an entire Angular application
collapsed to one page: the crawler reported one route where a user sees twenty.
Getting the distinction wrong in the other direction is just as bad — treating
`#pricing` as a route makes the crawler revisit one page under many identities.
"""

from __future__ import annotations

import pytest

from qualityproof.discovery import (
    is_denied_route,
    is_fragment_route,
    normalize_route,
    normalize_url,
)


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ("/basket", True),
        ("/", True),
        ("!/legacy", True),
        ("pricing", False),
        ("section-2", False),
        ("", False),
    ],
)
def test_a_route_fragment_starts_with_a_slash(fragment: str, expected: bool) -> None:
    """The convention is reliable: a view path begins with a slash, an anchor does not."""
    assert is_fragment_route(fragment) is expected


def test_client_side_routes_survive_normalisation() -> None:
    assert normalize_url("http://a.test/#/basket") == "http://a.test/#/basket"
    # The historical hash-bang form is the same route.
    assert normalize_url("http://a.test/#!/basket") == "http://a.test/#/basket"


def test_in_page_anchors_are_discarded() -> None:
    """Otherwise one page is crawled once per anchor on it."""
    assert normalize_url("http://a.test/about#team") == "http://a.test/about"
    assert normalize_url("http://a.test/#pricing") == "http://a.test/"


def test_distinct_views_are_distinct_urls() -> None:
    """The defect this fixes: every view collapsing to the document root."""
    views = {
        normalize_url(f"http://a.test/#/{name}")
        for name in ("login", "basket", "search", "about")
    }

    assert len(views) == 4


def test_fragment_route_identifiers_are_tokenised() -> None:
    assert normalize_route("http://a.test/#/products/42") == "/#/products/:int"
    assert normalize_route("http://a.test/#/order/7/item/9") == "/#/order/:int/item/:int"


def test_fragment_query_parameters_are_tokenised_and_ordered() -> None:
    first = normalize_route("http://a.test/#/search?q=apple&page=2")
    second = normalize_route("http://a.test/#/search?page=3&q=pear")

    assert first == second, "two searches are one route"
    # The token stays readable rather than percent-encoded: a route identifier is
    # human-facing, appearing in reports and CI gate messages beside ":int".
    assert first == "/#/search?page=:param&q=:param"


def test_server_path_and_client_route_both_contribute_to_identity() -> None:
    assert normalize_route("http://a.test/app#/basket") == "/app#/basket"
    assert normalize_route("http://a.test/app") == "/app"


def test_deny_policy_applies_to_the_client_side_route() -> None:
    """A policy denying /logout must also deny #/logout.

    Otherwise every route rule is trivially bypassed by a single-page
    application — the crawler would follow a sign-out link because the server
    path is only "/".
    """
    denied = ("/logout", "/delete")

    assert is_denied_route("http://a.test/#/logout", denied)
    assert is_denied_route("http://a.test/#/delete/7", denied)
    assert is_denied_route("http://a.test/logout", denied)
    assert not is_denied_route("http://a.test/#/login", denied)


def test_an_anchor_cannot_smuggle_a_denied_route() -> None:
    """`#logout` is an anchor, not a route, so it is not a route to deny."""
    assert not is_denied_route("http://a.test/#logout", ("/logout",))


@pytest.mark.parametrize(
    "url",
    ["http://a.test/#/a", "http://a.test/#/a/", "http://a.test/#//a"],
)
def test_route_fragments_are_canonicalised(url: str) -> None:
    """Trailing and duplicated slashes must not create phantom routes."""
    assert normalize_url(url) == "http://a.test/#/a"
