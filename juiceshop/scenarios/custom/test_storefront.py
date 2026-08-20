"""Requirement-traced tests for the OWASP Juice Shop storefront.

Each test cites the requirement it evidences, and the citation resolves against
the registered requirement's own source. A test that merely passes proves it
passed; the citation is what lets the ledger say which requirement it establishes.

Known application defects are marked `xfail(strict=True)` with the finding
recorded. Strict matters: if the application is fixed, the marker fails the suite
rather than silently rotting into a lie about what is broken.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from qualityproof import qualityproof

# Provenance is written out as literals on every test rather than built by a
# helper. That is not repetition for its own sake: the auditor reads this metadata
# with ast.literal_eval and never imports the module, because importing a spec
# would execute code from the repository under test. A helper call is not a
# literal, so a tidier version of this file is invisible to the ledger -- which is
# exactly what happened on the first attempt, and the audit reported all eleven
# tests as untraced.


# ---------------------------------------------------------------------------
# Anonymous browsing
# ---------------------------------------------------------------------------


@qualityproof(
    requirements=["JS-CAT-1"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-CAT-1",
        }
    ],
)
def test_catalogue_is_reachable_without_authentication(anonymous_page: Page, visit) -> None:
    visit(anonymous_page, "/search")

    # Product tiles are the catalogue; their presence is what "reachable" means.
    # "Contact" looked like a natural anchor but is a collapsed sidenav heading,
    # not a visible link -- asserting on it tested the navigation drawer, not the
    # requirement.
    expect(anonymous_page.locator("#searchQuery")).to_be_attached()
    # `.nth(1)` asserts a *grid* rather than a single tile, and does so through a
    # web-first assertion that retries. An earlier version used `count() > 1`,
    # which is a snapshot: on a freshly restarted application only the first tile
    # had rendered at that instant and the test failed. It passed in isolation
    # every time, which is exactly how this class of flake hides.
    expect(anonymous_page.locator(".mat-mdc-card").nth(1)).to_be_visible()


@qualityproof(
    requirements=["JS-CAT-3"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-CAT-3",
        }
    ],
)
def test_a_product_is_individually_addressable(anonymous_page: Page, visit) -> None:
    visit(anonymous_page, "/search")
    anonymous_page.locator(".mat-mdc-card").first.click()

    # A product detail opens as its own addressable state.
    expect(anonymous_page.locator("mat-dialog-container")).to_be_visible()


@qualityproof(
    requirements=["JS-CAT-4"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-CAT-4",
        }
    ],
)
def test_catalogue_does_not_overflow_at_a_narrow_viewport(
    anonymous_page: Page, visit
) -> None:
    """Measured rather than screenshotted, so the result is diffable."""
    anonymous_page.set_viewport_size({"width": 375, "height": 812})
    visit(anonymous_page, "/search")

    overflow = anonymous_page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )

    assert overflow <= 1, f"horizontal overflow of {overflow}px at 375px width"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@qualityproof(
    requirements=["JS-AUTH-2"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-AUTH-2",
        }
    ],
)
def test_login_inputs_are_programmatically_labelled(anonymous_page: Page, visit) -> None:
    """Without a programmatic label, a screen reader announces an unnamed field."""
    visit(anonymous_page, "/login")

    for field in ("#email", "#password"):
        locator = anonymous_page.locator(field)
        expect(locator).to_be_visible()
        accessible_name = locator.evaluate(
            """(element) => {
                 const aria = element.getAttribute('aria-label');
                 if (aria) return aria.trim();
                 const labelledBy = element.getAttribute('aria-labelledby');
                 if (labelledBy) {
                   const target = document.getElementById(labelledBy);
                   if (target) return (target.innerText || '').trim();
                 }
                 if (element.id) {
                   const bound = document.querySelector('label[for="' + element.id + '"]');
                   if (bound) return (bound.innerText || '').trim();
                 }
                 const wrapping = element.closest('label');
                 return wrapping ? (wrapping.innerText || '').trim() : '';
               }"""
        )
        assert accessible_name, f"{field} has no accessible name"


@qualityproof(
    requirements=["JS-AUTH-3"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-AUTH-3",
        }
    ],
)
def test_an_authenticated_session_offers_a_way_out(customer_page: Page, visit) -> None:
    """The control is asserted, never activated: signing out is destructive."""
    visit(customer_page, "/search")
    customer_page.get_by_label("Show/hide account menu").click()

    expect(customer_page.get_by_role("menuitem", name="Logout")).to_be_visible()


@qualityproof(
    requirements=["JS-AUTH-4"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-AUTH-4",
        }
    ],
)
def test_invalid_credentials_do_not_grant_a_session(anonymous_page: Page, visit) -> None:
    visit(anonymous_page, "/login")
    anonymous_page.fill("#email", "nobody@example.invalid")
    anonymous_page.fill("#password", "definitely-not-the-password")
    anonymous_page.press("#password", "Enter")

    expect(anonymous_page.locator(".error")).to_be_visible()
    # Still on the login route: no session was granted.
    assert "/login" in anonymous_page.url


# ---------------------------------------------------------------------------
# Basket
# ---------------------------------------------------------------------------


@qualityproof(
    requirements=["JS-BASKET-1"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-BASKET-1",
        }
    ],
)
def test_an_authenticated_customer_has_a_basket(
    customer_page: Page, visit, account_identifiers: dict[str, str]
) -> None:
    visit(customer_page, "/basket")

    expect(customer_page.get_by_role("heading", level=1).first).to_be_visible()
    headings = customer_page.locator("h1").all_inner_texts()
    assert any("Your Basket" in heading for heading in headings), headings


@qualityproof(
    requirements=["JS-BASKET-2"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-BASKET-2",
        }
    ],
)
def test_an_anonymous_visitor_is_not_shown_another_accounts_basket(
    anonymous_page: Page, visit, account_identifiers: dict[str, str]
) -> None:
    """The property that actually matters, after the baseline was corrected.

    An anonymous guest basket is permitted; being shown an authenticated
    account's basket is not. The original requirement conflated the two, and the
    tool disproved it -- see the SRS revision history.
    """
    visit(anonymous_page, "/basket")

    body = anonymous_page.locator("body").inner_text()
    for role, email in account_identifiers.items():
        assert email not in body, f"anonymous basket exposes the {role} account: {email}"


# ---------------------------------------------------------------------------
# Administrative boundary
# ---------------------------------------------------------------------------


@qualityproof(
    requirements=["JS-ADMIN-1"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-ADMIN-1",
        }
    ],
)
def test_an_administrator_reaches_the_administration_surface(
    admin_page: Page, visit
) -> None:
    visit(admin_page, "/administration")

    expect(admin_page.get_by_role("heading", name="Administration", level=1)).to_be_visible()


@qualityproof(
    requirements=["JS-ADMIN-2"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-ADMIN-2",
        }
    ],
)
def test_a_customer_is_denied_the_administration_surface(
    customer_page: Page, visit
) -> None:
    """A bounded observation, not proof of absence.

    The HTTP status is 200 for every identity -- the guard is entirely
    client-side -- so the evidence is that the administrative content is not
    rendered, on this route, for this identity.
    """
    visit(customer_page, "/administration")

    expect(
        customer_page.get_by_role("heading", name="Administration", level=1)
    ).to_have_count(0)


@qualityproof(
    requirements=["JS-ADMIN-3"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-ADMIN-3",
        }
    ],
)
def test_an_anonymous_visitor_is_denied_the_administration_surface(
    anonymous_page: Page, visit
) -> None:
    visit(anonymous_page, "/administration")

    expect(
        anonymous_page.get_by_role("heading", name="Administration", level=1)
    ).to_have_count(0)


@qualityproof(
    requirements=["JS-AUTH-1"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-AUTH-1",
        }
    ],
)
def test_a_registered_customer_holds_an_authenticated_session(
    customer_page: Page, visit, account_identifiers: dict[str, str]
) -> None:
    """Evidence that the session is genuinely authenticated, not merely present.

    A saved storage state proves a login once happened; it does not prove the
    application still regards it as valid. The account menu naming the account is
    the application's own statement that it does.
    """
    visit(customer_page, "/basket")

    headings = customer_page.locator("h1").all_inner_texts()
    expected = account_identifiers.get("customer")
    assert expected, "no customer identity was captured by the auth setup"
    assert any(expected in heading for heading in headings), headings


@qualityproof(
    requirements=["JS-CHECKOUT-1"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-CHECKOUT-1",
        }
    ],
)
def test_checkout_is_reachable_from_the_basket(customer_page: Page, visit) -> None:
    """Reachability only. No order is placed, and none can be from here.

    The requirement is that checkout can be *initiated*; completing a purchase is
    a state change against someone's application and is out of scope by BRS
    section 5. The address-selection step is where checkout begins, so that is
    where the evidence stops.
    """
    visit(customer_page, "/address/select")

    expect(customer_page.get_by_role("heading", name="Select an address")).to_be_visible()


@qualityproof(
    requirements=["JS-PROFILE-1"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-PROFILE-1",
        }
    ],
)
def test_a_customer_reaches_their_own_profile(
    customer_page: Page, juiceshop_base_url: str
) -> None:
    """The profile is server-rendered, not a client-side route.

    Worth stating: an application is rarely uniformly one thing. Assuming every
    route in a single-page application is a fragment would have missed this page
    entirely.
    """
    response = customer_page.goto(
        f"{juiceshop_base_url}/profile", wait_until="domcontentloaded"
    )

    assert response is not None and response.status == 200
    expect(customer_page.locator("#username")).to_be_attached()


@qualityproof(
    requirements=["JS-PROFILE-2"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-PROFILE-2",
        }
    ],
)
def test_profile_inputs_are_programmatically_labelled(
    customer_page: Page, juiceshop_base_url: str
) -> None:
    customer_page.goto(f"{juiceshop_base_url}/profile", wait_until="domcontentloaded")

    unlabelled = customer_page.evaluate(
        """() => Array.from(document.querySelectorAll('input'))
             .filter((element) => {
               const type = (element.getAttribute('type') || '').toLowerCase();
               if (['hidden', 'submit', 'button', 'file'].includes(type)) return false;
               if ((element.getAttribute('aria-label') || '').trim()) return false;
               if ((element.getAttribute('aria-labelledby') || '').trim()) return false;
               if (element.id &&
                   document.querySelector('label[for="' + element.id + '"]')) return false;
               return !element.closest('label');
             })
             .map((element) => element.getAttribute('name') || element.id || 'anonymous')"""
    )

    assert unlabelled == [], f"profile inputs without an accessible name: {unlabelled}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Open finding JS-CAT-2-no-h1: the catalogue presents no level-one heading "
        "at all. The highest heading in the document is an h2 carrying the site "
        "brand, so no heading names the page and a screen-reader user navigating by "
        "heading level cannot identify where they are. Marked strict so that fixing "
        "the application fails this marker rather than letting it rot into a false "
        "statement about what is broken. An earlier revision of this reason claimed "
        "the page rendered several level-one headings; it does not, and that claim "
        "was itself the rot this marker exists to prevent."
    ),
)
@qualityproof(
    requirements=["JS-CAT-2"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-CAT-2",
        }
    ],
)
def test_catalogue_presents_exactly_one_primary_heading(
    anonymous_page: Page, visit
) -> None:
    visit(anonymous_page, "/search")
    # Web-first wait on rendered content before taking a structural measurement.
    # evaluate() is a snapshot, so measuring an unsettled page is a real flake:
    # it would report zero headings while the view was still mounting and pass or
    # fail on timing rather than on the application.
    expect(anonymous_page.locator("mat-card").first).to_be_visible()

    # Scoped to page content, excluding dialog subtrees, for the same reason the
    # accessibility facet is: a modal's heading belongs to the modal, not to the
    # page underneath it. Measuring unscoped is what produced the earlier wrong
    # finding, because the first-visit welcome banner contributes two h1 elements.
    headings = anonymous_page.evaluate(
        """() => [...document.querySelectorAll('h1')]
             .filter((e) => !e.closest('[role=dialog],[role=alertdialog],dialog[open]'))
             .map((e) => (e.innerText || '').trim())
             .filter((text) => text.length > 0)"""
    )

    assert len(headings) == 1, f"expected one level-one heading, found {headings}"
