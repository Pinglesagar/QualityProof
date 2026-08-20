"""One reviewed scenario must describe the same behaviour in either language.

"Language-neutral" is only meaningful if it is checked. These tests compare what
the two emitters produce from a single approved scenario, so a change to one
without the other fails here rather than silently producing two suites that
assert different things.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from qualityproof.generation import (
    emit_playwright_typescript,
    emit_pytest,
    python_locator,
    typescript_locator,
)
from qualityproof.models import (
    ClickStep,
    FillStep,
    Locator,
    LocatorStrategy,
    NavigateStep,
    Provenance,
    ProvenanceKind,
    ScenarioSpec,
    ScenarioStatus,
    TextAssertion,
    TitleAssertion,
    VisibleAssertion,
)

SOURCE = Path("scenarios/generated/approved/checkout.yaml")


def _scenario() -> ScenarioSpec:
    return ScenarioSpec(
        id="checkout",
        title="Checkout totals a basket",
        status=ScenarioStatus.APPROVED,
        requirement_ids=("CHECKOUT-014",),
        steps=(
            NavigateStep(url="https://shop.example.test/products/1"),
            FillStep(
                locator=Locator(strategy=LocatorStrategy.LABEL, value="Quantity", css="#qty"),
                value="2",
            ),
            ClickStep(
                locator=Locator(
                    strategy=LocatorStrategy.ROLE,
                    role="button",
                    name="Add to cart",
                    css="#add-to-cart",
                )
            ),
        ),
        assertions=(
            TitleAssertion(expected="Basket"),
            VisibleAssertion(
                locator=Locator(strategy=LocatorStrategy.TEST_ID, value="total", css="#total")
            ),
            TextAssertion(
                locator=Locator(strategy=LocatorStrategy.TEST_ID, value="total"),
                expected="£28.00",
                soft=True,
            ),
        ),
        provenance=(
            Provenance(
                kind=ProvenanceKind.HUMAN_APPROVED,
                source="review",
                approved_by="alice",
                approved_at="2026-01-01T00:00:00Z",
            ),
        ),
    )


def test_both_emitters_produce_the_same_locator_strategies() -> None:
    scenario = _scenario()

    python_source = emit_pytest(scenario, SOURCE)
    typescript_source = emit_playwright_typescript(scenario, SOURCE)

    # Same strategy, same target, idiomatic spelling in each language.
    assert "page.get_by_role('button', name='Add to cart')" in python_source
    assert 'page.getByRole("button", { name: "Add to cart" })' in typescript_source
    assert "page.get_by_test_id('total')" in python_source
    assert 'page.getByTestId("total")' in typescript_source
    assert "page.get_by_label('Quantity'" in python_source
    assert 'page.getByLabel("Quantity"' in typescript_source


def test_both_emitters_agree_on_step_and_assertion_counts() -> None:
    scenario = _scenario()

    python_source = emit_pytest(scenario, SOURCE)
    typescript_source = emit_playwright_typescript(scenario, SOURCE)

    assert python_source.count(".click()") == typescript_source.count(".click()")
    assert python_source.count(".fill(") == typescript_source.count(".fill(")
    assert python_source.count("page.goto(") == typescript_source.count("page.goto(")
    # Three assertions in both, one of them soft.
    assert len(re.findall(r"expect(?:\.soft)?\(", python_source)) == 3
    assert len(re.findall(r"expect(?:\.soft)?\(", typescript_source)) == 3
    assert "expect.soft(" in python_source
    assert "expect.soft(" in typescript_source


def test_the_css_contract_is_recorded_in_both_languages() -> None:
    """The semantic locator drives the test; the CSS stays as auditable context."""
    scenario = _scenario()

    for source in (
        emit_pytest(scenario, SOURCE),
        emit_playwright_typescript(scenario, SOURCE),
    ):
        assert "# contract: #add-to-cart" in source or "// contract: #add-to-cart" in source


def test_requirement_traceability_survives_into_typescript() -> None:
    scenario = _scenario()

    typescript_source = emit_playwright_typescript(scenario, SOURCE)

    assert '{ type: "requirement", description: "CHECKOUT-014" }' in typescript_source
    assert '{ type: "provenance", description: "HUMAN_APPROVED:review" }' in typescript_source


def test_python_output_stays_syntactically_valid_and_deterministic() -> None:
    scenario = _scenario()

    first = emit_pytest(scenario, SOURCE)
    ast.parse(first)
    assert first == emit_pytest(scenario, SOURCE)


def test_typescript_output_is_deterministic_and_json_safe() -> None:
    scenario = _scenario()

    first = emit_playwright_typescript(scenario, SOURCE)

    assert first == emit_playwright_typescript(scenario, SOURCE)
    # Quotes and currency symbols must survive as valid TypeScript literals.
    assert '"£28.00"' in first


def test_neither_emitter_will_render_an_unapproved_scenario() -> None:
    draft = _scenario().model_copy(update={"status": ScenarioStatus.DRAFT})

    with pytest.raises(ValueError, match="not approved"):
        emit_pytest(draft, SOURCE)
    with pytest.raises(ValueError, match="not approved"):
        emit_playwright_typescript(draft, SOURCE)


def test_css_fallback_is_used_only_when_nothing_semantic_exists() -> None:
    css_only = Locator(strategy=LocatorStrategy.CSS, css="[data-x=1]")

    assert python_locator(css_only) == "page.locator('[data-x=1]')"
    assert typescript_locator(css_only) == 'page.locator("[data-x=1]")'
