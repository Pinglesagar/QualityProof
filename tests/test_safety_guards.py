"""The guards that stop an automated crawl from damaging a real application.

Every test here defends a decision where the wrong answer means either activating
a destructive control on someone's system, or letting an unvalidated model
proposal become executable.
"""

from __future__ import annotations

import pytest

from qualityproof.discovery import DEFAULT_DESTRUCTIVE_TERMS, is_destructive
from qualityproof.models import (
    Locator,
    LocatorStrategy,
    NavigateStep,
    Provenance,
    ProvenanceKind,
    ScenarioSpec,
    ScenarioStatus,
    VisibleAssertion,
)
from qualityproof.scenarios import (
    is_destructive_semantic,
    requires_discovery_validation,
)
from qualityproof.security import matches_unsafe_term


@pytest.mark.parametrize(
    "label",
    [
        "Place order",
        "Buy now",
        "Transfer funds",
        "Deactivate account",
        "Remove item",
        "Reset password",
        "Empty basket",
        "Cancel subscription",
        "Delete account",
        "Log out",
        "Sign out",
        "Send message",
        "Publish post",
        "Withdraw funds",
        "Terminate instance",
        "Unsubscribe",
    ],
)
def test_common_destructive_phrasings_are_refused(label: str) -> None:
    """Substring matching missed most of these.

    "Log out" was not caught because the term was spelled "logout", and a
    seven-term English list let "Buy now" and "Transfer funds" through — both of
    which move money or end a session on a live system.
    """
    assert is_destructive_semantic(label)
    assert is_destructive(label, DEFAULT_DESTRUCTIVE_TERMS)


@pytest.mark.parametrize(
    "label",
    ["Products", "Add to cart", "View basket", "Help", "Payment history", "Sender details"],
)
def test_read_only_navigation_is_not_refused(label: str) -> None:
    """Boundary matching stops 'pay' inside 'Payment' from blocking a report page."""
    assert not is_destructive_semantic(label)


def test_matching_is_on_word_boundaries_not_substrings() -> None:
    """The old matcher was simultaneously too weak and too strong."""
    assert matches_unsafe_term("Log out", ("log out",))
    assert matches_unsafe_term("DELETE ACCOUNT", ("delete",))
    assert matches_unsafe_term("Remove-item", ("remove",))
    # "pay" must not fire inside another word.
    assert not matches_unsafe_term("Paypal", ("pay",))
    assert not matches_unsafe_term("Repayment schedule", ("pay",))


def test_ambiguous_nouns_do_not_block_read_only_navigation() -> None:
    """"Order" and "checkout" are nouns in most navigation labels.

    Including them blocked "Order history" and "PayPal checkout options", and
    stopped assertion mining on a "Checkout" link. The step that actually commits
    is caught by pay/purchase/buy/confirm/submit and by the explicit phrases
    "place order" and "confirm order".
    """
    assert not is_destructive_semantic("Order history")
    assert not is_destructive_semantic("Checkout")
    assert is_destructive_semantic("Place order")
    assert is_destructive_semantic("Confirm order")


def test_over_blocking_is_the_deliberate_failure_direction() -> None:
    """A refusal is recorded and overridable; an under-block activates a control.

    Where the guard cannot tell, it refuses: the refusal becomes a visible
    `destructive_action_guard` unknown that an operator can narrow with
    --destructive-term, whereas an under-block is silent and irreversible.
    """
    assert is_destructive_semantic("Disable two-factor authentication")
    assert is_destructive_semantic("Reset all preferences")


def _scenario(**overrides: object) -> ScenarioSpec:
    base = ScenarioSpec(
        id="s",
        title="t",
        status=ScenarioStatus.APPROVED,
        steps=(NavigateStep(url="https://example.test/a"),),
    )
    return base.model_copy(update=overrides)


def test_relabelling_a_model_proposal_cannot_skip_validation() -> None:
    """The gate keyed on a string inside the artifact it was guarding.

    Setting `proposer: deterministic` in a hand-edited YAML file used to bypass
    every discovery check. Provenance and hypothesis assertions are now also
    consulted, so the artifact cannot vouch for itself.
    """
    disguised = _scenario(
        proposer="deterministic",
        provenance=(Provenance(kind=ProvenanceKind.AI_HYPOTHESIS, source="some-model"),),
    )

    assert requires_discovery_validation(disguised)


def test_a_hypothesis_assertion_also_demands_validation() -> None:
    disguised = _scenario(
        proposer="deterministic",
        hypothesis_assertions=(
            VisibleAssertion(
                locator=Locator(strategy=LocatorStrategy.ROLE, role="button", name="Go")
            ),
        ),
    )

    assert requires_discovery_validation(disguised)


def test_a_genuinely_deterministic_scenario_needs_no_validation() -> None:
    """The gate must not become unconditional, or mining stops working."""
    mined = _scenario(
        provenance=(
            Provenance(kind=ProvenanceKind.OBSERVATION, source="persisted-page-action-graph"),
        ),
    )

    assert not requires_discovery_validation(mined)


def test_a_named_proposer_always_demands_validation() -> None:
    assert requires_discovery_validation(_scenario(proposer="http"))
