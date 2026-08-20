"""Coverage answers the question a release review actually asks.

The ledger reports what is proven. These tests pin the inverse view: what is not
proven, what is not tested at all, and what claims to cover something that was
never specified.
"""

from __future__ import annotations

import json
from pathlib import Path

from qualityproof.coverage import (
    ExecutionState,
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
    RequirementPriority,
    SourceAssertion,
    TestMetadata,
    Verdict,
    VerdictStatus,
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


def _verdict(name: str, status: VerdictStatus, *, param: str = "") -> Verdict:
    """A verdict keyed the way execution actually stores one.

    Deliberately spelled as a JUnit identifier -- dotted module path, and a
    parametrisation suffix -- because that is what lands in the repository and the
    ledger keys rows as ``t.py::name``. A test that used the ledger spelling on
    both sides would prove the join works only in a world where it is not needed.
    """
    return Verdict(
        assertion_id=f"pkg.module.t::{name}{param}",
        status=status,
        rationale="fixture",
    )


def test_a_traceable_requirement_with_a_failing_test_is_not_demonstrated() -> None:
    """Traceability and truth are different questions.

    This project's own engagement reported 21/21 verified while an open defect sat
    against one of those requirements, because coverage never consulted the
    execution verdicts it already stored.
    """
    report = compute_coverage(
        (_requirement("R-1"),),
        (_entry("t1", LedgerStatus.VERIFIED, ("R-1",)),),
        (_verdict("t1", VerdictStatus.FAIL),),
    )
    item = report.requirements[0]
    assert item.status is RequirementStatus.VERIFIED
    assert item.execution is ExecutionState.FAILING
    assert item.is_proven is False
    assert report.traced_not_demonstrated == ("R-1",)
    assert report.summary()["demonstrated"] == 0
    assert report.summary()["verified"] == 1


def test_an_expected_failure_reports_not_demonstrated_rather_than_verified() -> None:
    """A strict xfail is recorded by pytest as skipped, so it lands inconclusive.

    This is the shape of a tracked open finding: the requirement is perfectly
    traceable, and the application does not meet it.
    """
    report = compute_coverage(
        (_requirement("R-1"),),
        (_entry("t1", LedgerStatus.VERIFIED, ("R-1",)),),
        (_verdict("t1", VerdictStatus.INCONCLUSIVE),),
    )
    item = report.requirements[0]
    assert item.execution is ExecutionState.NOT_DEMONSTRATED
    assert item.failing_tests == ("t.py::t1",)


def test_a_passing_test_demonstrates_its_requirement() -> None:
    report = compute_coverage(
        (_requirement("R-1"),),
        (_entry("t1", LedgerStatus.VERIFIED, ("R-1",)),),
        (_verdict("t1", VerdictStatus.PASS, param="[chromium]"),),
    )
    item = report.requirements[0]
    assert item.execution is ExecutionState.DEMONSTRATED
    assert item.is_proven is True
    assert item.failing_tests == ()
    assert report.traced_not_demonstrated == ()


def test_a_passing_projection_does_not_excuse_a_failing_one() -> None:
    """One test, two browsers, one verdict each. The worse one is the truth."""
    report = compute_coverage(
        (_requirement("R-1"),),
        (_entry("t1", LedgerStatus.VERIFIED, ("R-1",)),),
        (
            _verdict("t1", VerdictStatus.PASS, param="[chromium]"),
            _verdict("t1", VerdictStatus.FAIL, param="[webkit]"),
        ),
    )
    assert report.requirements[0].execution is ExecutionState.FAILING


def test_passing_tests_do_not_offset_a_failing_one_on_the_same_requirement() -> None:
    """Worst-wins across tests too: a requirement is not satisfied on average."""
    report = compute_coverage(
        (_requirement("R-1"),),
        (
            _entry("t1", LedgerStatus.VERIFIED, ("R-1",)),
            _entry("t2", LedgerStatus.VERIFIED, ("R-1",)),
        ),
        (
            _verdict("t1", VerdictStatus.PASS),
            _verdict("t2", VerdictStatus.FAIL),
        ),
    )
    item = report.requirements[0]
    assert item.execution is ExecutionState.FAILING
    assert item.failing_tests == ("t.py::t2",)


def test_a_rerun_pass_is_not_a_demonstration() -> None:
    """FLAKY was introduced so instability could not be laundered into green."""
    report = compute_coverage(
        (_requirement("R-1"),),
        (_entry("t1", LedgerStatus.VERIFIED, ("R-1",)),),
        (_verdict("t1", VerdictStatus.FLAKY),),
    )
    assert report.requirements[0].execution is ExecutionState.UNSTABLE
    assert report.requirements[0].is_proven is False


def test_without_any_verdicts_nothing_is_reported_as_demonstrated() -> None:
    """Absent execution data must read as unknown, never as success."""
    report = compute_coverage(
        (_requirement("R-1"),),
        (_entry("t1", LedgerStatus.VERIFIED, ("R-1",)),),
    )
    item = report.requirements[0]
    assert item.status is RequirementStatus.VERIFIED
    assert item.execution is ExecutionState.NOT_RUN
    assert item.is_proven is False
    assert report.demonstrated == 0


def test_the_two_priority_gates_ask_different_questions() -> None:
    """A traceability gate passes a build whose behaviour is broken.

    ``unproven_at`` asks whether a resolvable test claims the requirement, and
    ``undemonstrated_at`` asks whether the software actually does it. The
    distinction is the point: on the real engagement the same requirement passed
    the first gate and failed the second.
    """
    requirement = Requirement(
        id="R-1", title="t", description="d", priority=RequirementPriority.P1
    )
    report = compute_coverage(
        (requirement,),
        (_entry("t1", LedgerStatus.VERIFIED, ("R-1",)),),
        (_verdict("t1", VerdictStatus.INCONCLUSIVE),),
    )
    assert report.unproven_at("P1") == ()
    assert report.undemonstrated_at("P1") == ("R-1",)
    assert report.by_priority()["P1"] == {
        "total": 1,
        "verified": 1,
        "demonstrated": 0,
    }


def test_reports_name_which_test_holds_a_requirement_back(tmp_path: Path) -> None:
    report = compute_coverage(
        (_requirement("R-1"),),
        (_entry("t1", LedgerStatus.VERIFIED, ("R-1",)),),
        (_verdict("t1", VerdictStatus.INCONCLUSIVE),),
    )
    json_path, markdown_path = write_coverage_reports(report, tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["demonstrated"] == 0
    assert payload["summary"]["traced_not_demonstrated"] == ["R-1"]
    assert payload["schema_version"] == "qualityproof-coverage/v2"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Traceable but not demonstrated" in markdown
    assert "NOT_DEMONSTRATED" in markdown
    assert "t.py::t1" in markdown
