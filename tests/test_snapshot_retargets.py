"""The route-retarget rule must collapse only genuine link moves."""

from __future__ import annotations

from qualityproof.models import EvidenceSnapshot
from qualityproof.snapshots import compare_snapshots


def _snapshot(
    name: str,
    routes: tuple[str, ...],
    links: dict[str, tuple[str, ...]],
) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        name=name,
        routes=routes,
        page_fingerprints={key: "fingerprint" for key in links},
        page_links=links,
    )


def test_same_referrer_link_move_is_one_retarget() -> None:
    before = _snapshot("before", ("/", "/help"), {"/#page-root": ("/help",)})
    after = _snapshot("after", ("/", "/missing-help"), {"/#page-root": ("/missing-help",)})

    comparison = compare_snapshots(before, after)

    assert len(comparison.route_retargets) == 1
    retarget = comparison.route_retargets[0]
    assert (retarget.referrer, retarget.removed_route, retarget.added_route) == (
        "/",
        "/help",
        "/missing-help",
    )
    # The raw diff stays lossless; only consumers collapse the pair.
    assert comparison.routes.removed == ("/help",)
    assert comparison.routes.added == ("/missing-help",)


def test_unpaired_removal_is_not_absorbed_into_a_retarget() -> None:
    """A removed journey with no replacement must survive as a removal.

    This is the discrimination case: if the rule were tuned to raise scores it
    would happily pair any removal with any addition. It must not.
    """
    before = _snapshot(
        "before",
        ("/", "/legacy-order"),
        {"/#page-root": ("/legacy-order",)},
    )
    after = _snapshot("after", ("/",), {"/#page-root": ()})

    comparison = compare_snapshots(before, after)

    assert comparison.route_retargets == ()
    assert comparison.routes.removed == ("/legacy-order",)


def test_removal_and_addition_behind_different_referrers_stay_separate() -> None:
    before = _snapshot(
        "before",
        ("/a", "/b", "/gone"),
        {"/a#page-a": ("/gone",), "/b#page-b": ()},
    )
    after = _snapshot(
        "after",
        ("/a", "/b", "/fresh"),
        {"/a#page-a": (), "/b#page-b": ("/fresh",)},
    )

    comparison = compare_snapshots(before, after)

    assert comparison.route_retargets == ()
    assert comparison.routes.removed == ("/gone",)
    assert comparison.routes.added == ("/fresh",)


def test_ambiguous_many_to_one_move_is_not_paired() -> None:
    before = _snapshot(
        "before",
        ("/", "/first", "/second"),
        {"/#page-root": ("/first", "/second")},
    )
    after = _snapshot("after", ("/", "/merged"), {"/#page-root": ("/merged",)})

    comparison = compare_snapshots(before, after)

    assert comparison.route_retargets == ()
    assert comparison.routes.removed == ("/first", "/second")


def test_schema_1_0_snapshots_without_link_maps_still_compare() -> None:
    before = EvidenceSnapshot(schema_version="1.0", name="old-before", routes=("/", "/help"))
    after = EvidenceSnapshot(schema_version="1.0", name="old-after", routes=("/", "/missing-help"))

    comparison = compare_snapshots(before, after)

    assert comparison.route_retargets == ()
    assert comparison.routes.removed == ("/help",)


def _facet_snapshot(
    name: str,
    states: dict[str, dict[str, str]],
    roles: dict[str, str] | None = None,
) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        name=name,
        routes=tuple(sorted({key.rsplit("#", 1)[0] for key in states})),
        page_fingerprints={key: "fp" for key in states},
        page_facets=states,
        page_roles=roles or {},
    )


def test_a_facet_change_on_any_state_under_a_route_is_reported() -> None:
    """One route commonly maps to many states, and all of them must count.

    ``/products/:int`` is a single normalized route covering every product. An
    earlier implementation merged those states with dict.update() and kept only
    the last one's digests, so a regression on any other product was erased.
    """
    before = _facet_snapshot(
        "before",
        {
            "/products/:int#page-a": {"controls": "aaa", "layout": "L"},
            "/products/:int#page-b": {"controls": "bbb", "layout": "L"},
            "/products/:int#page-z": {"controls": "zzz", "layout": "L"},
        },
    )
    # Only the FIRST state's controls change; the last-sorted state is untouched.
    after = _facet_snapshot(
        "after",
        {
            "/products/:int#page-a": {"controls": "CHANGED", "layout": "L"},
            "/products/:int#page-b": {"controls": "bbb", "layout": "L"},
            "/products/:int#page-z": {"controls": "zzz", "layout": "L"},
        },
    )

    comparison = compare_snapshots(before, after)

    assert comparison.page_facet_changes == {"/products/:int": ("controls",)}


def test_identical_multi_state_routes_report_no_facet_change() -> None:
    states = {
        "/products/:int#page-a": {"controls": "aaa"},
        "/products/:int#page-b": {"controls": "bbb"},
    }

    comparison = compare_snapshots(
        _facet_snapshot("before", states), _facet_snapshot("after", dict(states))
    )

    assert comparison.page_facet_changes == {}


def test_a_state_appearing_or_vanishing_under_a_route_is_a_facet_change() -> None:
    """Losing a product page is a change to what that route covers."""
    before = _facet_snapshot("before", {"/p/:int#a": {"controls": "x"}})
    after = _facet_snapshot(
        "after", {"/p/:int#a": {"controls": "x"}, "/p/:int#b": {"controls": "y"}}
    )

    assert compare_snapshots(before, after).page_facet_changes == {"/p/:int": ("controls",)}


def test_facet_changes_are_not_conflated_across_roles() -> None:
    """A shopper seeing a change must not be reported as an admin change."""
    before = _facet_snapshot(
        "before",
        {"/admin#a": {"status": "403"}, "/admin#b": {"status": "200"}},
        roles={"/admin#a": "shopper", "/admin#b": "admin"},
    )
    after = _facet_snapshot(
        "after",
        {"/admin#a": {"status": "200"}, "/admin#b": {"status": "200"}},
        roles={"/admin#a": "shopper", "/admin#b": "admin"},
    )

    comparison = compare_snapshots(before, after)

    assert comparison.page_facet_changes == {"/admin": ("status",)}


def test_a_role_reachability_change_between_releases_is_reported() -> None:
    """A route becoming reachable for a role is the regression that matters.

    A privilege boundary is only observable as a change: one release denies a
    route to a customer, the next serves it. Reporting that is the whole purpose
    of keeping snapshots immutable.
    """
    before = EvidenceSnapshot(
        name="before",
        routes=("/admin", "/shop"),
        page_fingerprints={"/admin#admin-state": "a", "/shop#shop-state": "s"},
        page_facets={
            "/admin#admin-state": {"status": "denied", "headings": "none"},
            "/shop#shop-state": {"status": "ok", "headings": "shop"},
        },
        page_roles={"/admin#admin-state": "customer", "/shop#shop-state": "customer"},
    )
    after = EvidenceSnapshot(
        name="after",
        routes=("/admin", "/shop"),
        page_fingerprints={"/admin#admin-state": "b", "/shop#shop-state": "s"},
        page_facets={
            # The customer now receives administrative content at the same route.
            "/admin#admin-state": {"status": "ok", "headings": "administration"},
            "/shop#shop-state": {"status": "ok", "headings": "shop"},
        },
        page_roles={"/admin#admin-state": "customer", "/shop#shop-state": "customer"},
    )

    comparison = compare_snapshots(before, after)

    assert comparison.page_facet_changes["/admin"] == ("headings", "status")
    # The unchanged route must stay silent, or the signal is worthless.
    assert "/shop" not in comparison.page_facet_changes


def test_a_page_identity_is_stable_across_runs() -> None:
    """Identity must not absorb content, or every diff reads as churn.

    An earlier version folded title, headings and forms into the state id. Any
    content difference therefore minted a new identity, and a release diff of ten
    changed pages reported fifteen added and fifteen removed instead. Identity is
    the observing role and the concrete URL; everything else is a facet.
    """
    from qualityproof.discovery import _stable_id

    first = _stable_id("page", "customer|https://a.test/#/basket")
    again = _stable_id("page", "customer|https://a.test/#/basket")
    other_role = _stable_id("page", "admin|https://a.test/#/basket")
    other_page = _stable_id("page", "customer|https://a.test/#/orders")

    assert first == again
    assert first != other_role, "the observing role is part of identity"
    assert first != other_page, "two URLs under one route stay distinct states"
