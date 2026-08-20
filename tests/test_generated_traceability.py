"""The generate → audit edge must actually carry traceability.

Before this, `emit_pytest` wrote no metadata, so every generated test audited as
UNKNOWN and the pipeline's `generate → audit → ledger` edge did nothing. These
tests pin the round trip: what the emitter writes, the auditor must read back,
and the ledger must classify on the same terms as a hand-written test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from qualityproof.audit import audit_path, build_ledger
from qualityproof.generation import emit_pytest, generate_approved
from qualityproof.models import (
    LedgerStatus,
    Locator,
    LocatorStrategy,
    NavigateStep,
    Provenance,
    ProvenanceKind,
    ScenarioSpec,
    ScenarioStatus,
    TitleAssertion,
    VisibleAssertion,
)


def _approved(provenance: tuple[Provenance, ...], scenario_id: str = "checkout") -> ScenarioSpec:
    return ScenarioSpec(
        id=scenario_id,
        title="Checkout is reachable",
        status=ScenarioStatus.APPROVED,
        requirement_ids=("REQ-1",),
        steps=(NavigateStep(url="https://shop.example.test/products"),),
        assertions=(
            TitleAssertion(expected="Products", soft=True),
            VisibleAssertion(
                locator=Locator(strategy=LocatorStrategy.ROLE, role="link", name="Cart"),
                soft=True,
            ),
        ),
        provenance=provenance,
    )


def _write_project(tmp_path: Path, scenario: ScenarioSpec) -> Path:
    approved = tmp_path / "scenarios" / "generated" / "approved"
    approved.mkdir(parents=True)
    (approved / f"{scenario.id}.yaml").write_text(
        yaml.safe_dump(scenario.model_dump(mode="json", exclude_none=True), sort_keys=True),
        encoding="utf-8",
    )
    return tmp_path


def test_a_generated_test_reaches_verified_with_authoritative_provenance(
    tmp_path: Path,
) -> None:
    """The flagship path: reviewed scenario in, defensible ledger row out."""
    requirements = tmp_path / "requirements.yaml"
    requirements.write_text(
        yaml.safe_dump(
            {"requirements": [{"id": "REQ-1", "description": "The catalogue is reachable."}]}
        ),
        encoding="utf-8",
    )
    scenario = _approved(
        (
            Provenance(
                kind=ProvenanceKind.REQUIREMENT,
                source=str(requirements),
                locator="requirement:REQ-1",
            ),
        )
    )
    project = _write_project(tmp_path, scenario)

    generate_approved(project, validate=False)
    audited = audit_path(project / ".qualityproof" / "generated")
    entries = build_ledger(audited, project=project)

    generated = [entry for entry in entries if "checkout" in entry.test.name]
    assert generated, "the generated test must be visible to the auditor"
    assert generated[0].status is LedgerStatus.VERIFIED
    # The soft assertions the emitter writes must be counted, not skipped.
    assert len(generated[0].test.assertions) == 2


def test_a_mined_observation_scenario_is_partial_not_verified(tmp_path: Path) -> None:
    """Crawling something is not the same as being told it matters.

    Mined journeys carry OBSERVATION provenance, which must never be enough. This
    is the honest ceiling on automatic generation, and it is why the demo's
    generated suite is PARTIAL rather than green.
    """
    scenario = _approved(
        (
            Provenance(
                kind=ProvenanceKind.OBSERVATION,
                source="persisted-page-action-graph",
                locator="page-a|page-b",
            ),
        ),
        scenario_id="journey-observed",
    )
    project = _write_project(tmp_path, scenario)

    generate_approved(project, validate=False)
    entries = build_ledger(
        audit_path(project / ".qualityproof" / "generated"), project=project
    )

    assert entries[0].status is LedgerStatus.PARTIAL


def test_emitted_metadata_round_trips_through_the_auditor(tmp_path: Path) -> None:
    """Whatever the emitter writes, the auditor must read back identically."""
    approved_at = datetime(2026, 1, 1, tzinfo=UTC)
    scenario = _approved(
        (
            Provenance(
                kind=ProvenanceKind.HUMAN_APPROVED,
                source="review:alice",
                approved_by="alice",
                approved_at=approved_at,
            ),
        )
    )
    module = tmp_path / "test_emitted.py"
    module.write_text(emit_pytest(scenario, Path("s/checkout.yaml")), encoding="utf-8")

    audited = audit_path(module)

    metadata = audited[0].metadata
    assert metadata is not None
    assert metadata.requirement_ids == ("REQ-1",)
    assert metadata.provenance[0].kind is ProvenanceKind.HUMAN_APPROVED
    assert metadata.provenance[0].approved_by == "alice"
    assert metadata.provenance[0].approved_at == approved_at


def test_a_scenario_without_metadata_emits_no_decorator(tmp_path: Path) -> None:
    """No metadata must mean no import and no decorator, not an empty one."""
    scenario = _approved(()).model_copy(update={"requirement_ids": ()})

    source = emit_pytest(scenario, Path("s/bare.yaml"))

    assert "@qualityproof" not in source
    assert "from qualityproof import" not in source
