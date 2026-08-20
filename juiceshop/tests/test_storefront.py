"""Requirement-traced tests for the OWASP Juice Shop storefront.

Each test cites the requirement it evidences, and the citation resolves against
the registered requirement's own source. A test that merely passes proves it
passed; the citation is what lets the ledger say which requirement it establishes.

Known application defects are marked `xfail(strict=True)` with the finding
recorded. Strict matters: if the application is fixed, the marker fails the suite
rather than silently rotting into a lie about what is broken.
"""

from __future__ import annotations

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
    expect(anonymous_page.locator(".mat-mdc-card").first).to_be_visible()
    expect(anonymous_page.locator("#searchQuery")).to_be_attached()
    assert anonymous_page.locator(".mat-mdc-card").count() > 1, "expected a product grid"


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
