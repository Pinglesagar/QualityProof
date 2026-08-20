"""Coverage answers the question a release review actually asks.

The ledger reports what is proven. These tests pin the inverse view: what is not
proven, what is not tested at all, and what claims to cover something that was
never specified.
"""

from __future__ import annotations

import json
from pathlib import Path

from qualityproof.coverage import (
    RequirementStatus,
    compute_coverage,
    write_coverage_reports,
)
from qualityproof.models import (
    AuditedTest,
    LedgerEntry,
    LedgerStatus,
    Provenance,
    ProvenanceKind,
    Requirement,
    SourceAssertion,
    TestMetadata,
)

ASSERTION = SourceAssertion(kind="expect", line=1, expression="expect(x).to_be_visible()")


def _requirement(identifier: str) -> Requirement:
    return Requirement(id=identifier, title=f"Title {identifier}", description="d")


def _entry(
    name: str,
    status: LedgerStatus,
    requirement_ids: tuple[str, ...] = (),
    *,
    assertions: tuple[SourceAssertion, ...] = (ASSERTION,),
) -> LedgerEntry:
    metadata = (
        TestMetadata(
            requirement_ids=requirement_ids,
            provenance=(Provenance(kind=ProvenanceKind.OBSERVATION, source="crawl"),),
        )
        if requirement_ids
        else None
    )
    return LedgerEntry(
        id=f"t.py::{name}",
        status=status,
        reason="fixture",
        test=AuditedTest(
            id=f"t.py::{name}",
            path="t.py",
            name=name,
            line=1,
            framework="playwright",
            assertions=assertions,
            metadata=metadata,
        ),
    )


def test_a_requirement_with_no_referencing_test_is_uncovered() -> None:
    """This is the whole point: silence about a requirement is not success."""
    report = compute_coverage(
        (_requirement("REQ-1"), _requirement("REQ-2")),
        (_entry("covers_one", LedgerStatus.VERIFIED, ("REQ-1",)),),
    )

    statuses = {item.requirement_id: item.status for item in report.requirements}
    assert statuses["REQ-1"] is RequirementStatus.VERIFIED
    assert statuses["REQ-2"] is RequirementStatus.UNCOVERED
    assert report.uncovered == ("REQ-2",)


def test_a_requirement_referenced_only_by_unproven_tests_is_partial() -> None:
    """Referencing a requirement is not the same as establishing it."""
    report = compute_coverage(
        (_requirement("REQ-1"),),
        (
            _entry("weak", LedgerStatus.PARTIAL, ("REQ-1",)),
            _entry("unknown", LedgerStatus.UNKNOWN, ("REQ-1",)),
        ),
    )

    assert report.requirements[0].status is RequirementStatus.PARTIAL
    assert report.verified == 0


def test_one_verified_test_is_enough_to_verify_a_requirement() -> None:
    report = compute_coverage(
        (_requirement("REQ-1"),),
        (
            _entry("weak", LedgerStatus.PARTIAL, ("REQ-1",)),
            _entry("strong", LedgerStatus.VERIFIED, ("REQ-1",)),
        ),
    )

    item = report.requirements[0]
    assert item.status is RequirementStatus.VERIFIED
    assert item.verified_tests == ("t.py::strong",)
    assert item.partial_tests == ("t.py::weak",)


def test_a_test_citing_an_unregistered_requirement_is_an_orphan() -> None:
    """A test may not mint coverage by naming an identifier nobody specified."""
    report = compute_coverage(
        (_requirement("REQ-1"),),
        (_entry("invents", LedgerStatus.VERIFIED, ("REQ-INVENTED",)),),
    )

    assert report.orphan_links == {"REQ-INVENTED": ("t.py::invents",)}
    # And it must not be credited anywhere.
    assert report.requirements[0].status is RequirementStatus.UNCOVERED
    assert report.verified == 0


def test_a_test_that_asserts_without_naming_a_requirement_is_untraced() -> None:
    """Real effort the ledger cannot credit is worth surfacing, not hiding."""
    report = compute_coverage(
        (_requirement("REQ-1"),),
        (_entry("anonymous", LedgerStatus.UNKNOWN),),
    )

    assert report.untraced_tests == ("t.py::anonymous",)


def test_a_test_with_no_assertions_and_no_requirement_is_not_counted_as_untraced() -> None:
    """Untraced means unattributed *work*, not an empty test."""
    report = compute_coverage(
        (_requirement("REQ-1"),),
        (_entry("empty", LedgerStatus.UNKNOWN, assertions=()),),
    )

    assert report.untraced_tests == ()


def test_verified_ratio_is_over_registered_requirements() -> None:
    report = compute_coverage(
        tuple(_requirement(f"REQ-{index}") for index in range(4)),
        (_entry("one", LedgerStatus.VERIFIED, ("REQ-0",)),),
    )

    assert report.verified_ratio == 0.25
    assert report.summary()["verified_ratio"] == 0.25


def test_an_empty_registry_reports_nothing_rather_than_dividing_by_zero() -> None:
    report = compute_coverage((), ())

    assert report.total == 0
    assert report.verified_ratio == 0.0


def test_reports_name_the_uncovered_and_orphaned_explicitly(tmp_path: Path) -> None:
    """The Markdown is the artifact a human reads, so it must state the gaps."""
    report = compute_coverage(
        (_requirement("REQ-1"), _requirement("REQ-2")),
        (
            _entry("covers", LedgerStatus.VERIFIED, ("REQ-1",)),
            _entry("invents", LedgerStatus.VERIFIED, ("REQ-GHOST",)),
        ),
    )

    json_path, markdown_path = write_coverage_reports(report, tmp_path)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["uncovered"] == 1
    assert payload["summary"]["orphan_link_count"] == 1
    body = markdown_path.read_text(encoding="utf-8")
    assert "## Uncovered requirements" in body
    assert "`REQ-2`" in body
    assert "## Orphan links" in body
    assert "REQ-GHOST" in body
    # The honesty caveat must travel with the report.
    assert "never correct" in body
