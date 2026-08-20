"""Answer the question a release review actually asks.

A ledger says what *is* proven. Nobody asks that. They ask what is **not**: which
requirements have no test at all, which have tests that fail to establish
anything, and which tests claim coverage of requirements that do not exist.

This module is the inverse view of the ledger, and it is deliberately pessimistic:
a requirement is only ``VERIFIED`` when at least one test reaches ``VERIFIED``
against it, and an identifier no registry knows is reported as an orphan rather
than quietly counted.
"""

from __future__ import annotations

import json
from collections import defaultdict
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from qualityproof.models import (
    DomainModel,
    LedgerEntry,
    LedgerStatus,
    Requirement,
    RequirementPriority,
    Verdict,
    VerdictStatus,
)


class RequirementStatus(StrEnum):
    """How well one requirement is evidenced."""

    #: At least one test reaches VERIFIED against it.
    VERIFIED = "VERIFIED"
    #: Tests reference it, but none of them establish provenance.
    PARTIAL = "PARTIAL"
    #: No test references it at all. The answer to "what is untested?".
    UNCOVERED = "UNCOVERED"


class ExecutionState(StrEnum):
    """Whether the application actually satisfies a requirement, as observed.

    ``RequirementStatus`` answers a different question: whether a test *claims*
    the requirement and whether that claim resolves. Traceability and truth are
    not the same thing, and conflating them is how a coverage report ends up
    saying 21/21 verified while an open defect sits against one of them. That was
    literally the state of this project's own worked engagement: the catalogue
    heading requirement read VERIFIED while its only test was a strict expected
    failure recording that the application does not meet it.
    """

    #: A traced test ran and passed, and none failed or flapped.
    DEMONSTRATED = "DEMONSTRATED"
    #: A traced test failed. The requirement is contradicted by evidence.
    FAILING = "FAILING"
    #: A traced test passed only after a rerun. Not a pass.
    UNSTABLE = "UNSTABLE"
    #: Traced tests ran but none of them passed: skipped, xfailed, inconclusive.
    #: A known open finding lives here, which is exactly where it should live.
    NOT_DEMONSTRATED = "NOT_DEMONSTRATED"
    #: No traced test has a recorded verdict, so nothing has been observed.
    NOT_RUN = "NOT_RUN"


#: Worst-wins, because a requirement is not satisfied by its better outcomes. Two
#: passing tests do not offset a third that fails against the same requirement.
_EXECUTION_PRECEDENCE = (
    ExecutionState.FAILING,
    ExecutionState.UNSTABLE,
    ExecutionState.NOT_DEMONSTRATED,
    ExecutionState.DEMONSTRATED,
)

_VERDICT_TO_EXECUTION = {
    VerdictStatus.FAIL: ExecutionState.FAILING,
    VerdictStatus.FLAKY: ExecutionState.UNSTABLE,
    VerdictStatus.INCONCLUSIVE: ExecutionState.NOT_DEMONSTRATED,
    VerdictStatus.NOT_RUN: ExecutionState.NOT_DEMONSTRATED,
    VerdictStatus.PASS: ExecutionState.DEMONSTRATED,
}


def _test_key(identifier: str) -> str:
    """Reduce a test identifier to something comparable across writers.

    A stored verdict is keyed by the JUnit identifier
    ``path.to.module::test_name[param]`` while a ledger row is keyed by the pytest
    node id ``path/to/module.py::test_name``. The test-name tail is the only part
    both spell the same way, and parametrisation has to come off or a browser
    projection would never match its own ledger row.
    """
    tail = identifier.rsplit("::", 1)[-1]
    return tail.split("[", 1)[0].strip()


class RequirementCoverage(DomainModel):
    requirement_id: str
    title: str | None = None
    area: str | None = None
    priority: RequirementPriority | None = None
    status: RequirementStatus
    verified_tests: tuple[str, ...] = ()
    partial_tests: tuple[str, ...] = ()
    unknown_tests: tuple[str, ...] = ()
    #: What execution observed, independent of whether the claim is traceable.
    execution: ExecutionState = ExecutionState.NOT_RUN
    #: Traced tests that did not pass, with the observed status, so the report says
    #: which test is holding the requirement back rather than only that one is.
    failing_tests: tuple[str, ...] = ()

    @property
    def referencing_test_count(self) -> int:
        return len(self.verified_tests) + len(self.partial_tests) + len(self.unknown_tests)

    @property
    def is_proven(self) -> bool:
        """Traceable *and* demonstrated. The only state that means what it says."""
        return (
            self.status is RequirementStatus.VERIFIED
            and self.execution is ExecutionState.DEMONSTRATED
        )


class CoverageReport(DomainModel):
    schema_version: str = "qualityproof-coverage/v2"
    requirements: tuple[RequirementCoverage, ...] = ()
    #: Requirement identifiers cited by tests that the registry does not contain.
    #: An orphan link is a traceability defect: the test believes it covers
    #: something that was never specified.
    orphan_links: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    #: Tests carrying assertions but no requirement identifier at all.
    untraced_tests: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.requirements)

    @property
    def verified(self) -> int:
        return sum(1 for item in self.requirements if item.status is RequirementStatus.VERIFIED)

    @property
    def uncovered(self) -> tuple[str, ...]:
        return tuple(
            item.requirement_id
            for item in self.requirements
            if item.status is RequirementStatus.UNCOVERED
        )

    @property
    def demonstrated(self) -> int:
        """Requirements that are traceable *and* observed to hold."""
        return sum(1 for item in self.requirements if item.is_proven)

    @property
    def traced_not_demonstrated(self) -> tuple[str, ...]:
        """Requirements a test claims and resolves, but execution does not support.

        This is the honest headline. A requirement in this list has perfect
        traceability and a defect against it, and reporting it as VERIFIED without
        this column is the single most misleading thing a coverage report can do.
        """
        return tuple(
            item.requirement_id
            for item in self.requirements
            if item.status is RequirementStatus.VERIFIED
            and item.execution is not ExecutionState.DEMONSTRATED
        )

    @property
    def verified_ratio(self) -> float:
        """Share of registered requirements with at least one verified test."""
        return self.verified / self.total if self.total else 0.0

    def unproven_at(self, priority: RequirementPriority | str) -> tuple[str, ...]:
        """Requirements of a given priority that no test has established.

        This is the question a release review actually gates on: not the overall
        percentage, but whether anything critical is unproven.

        A plain string is accepted and coerced. Comparing an enum member with
        ``is`` against a string silently yields nothing, and a risk gate that
        quietly reports no problems is worse than one that fails loudly, so an
        unrecognised band raises instead.
        """
        band = (
            priority
            if isinstance(priority, RequirementPriority)
            else RequirementPriority(str(priority).strip().upper())
        )
        return tuple(
            item.requirement_id
            for item in self.requirements
            if item.priority is band and item.status is not RequirementStatus.VERIFIED
        )

    def undemonstrated_at(self, priority: RequirementPriority | str) -> tuple[str, ...]:
        """Requirements of a given priority that execution does not support.

        The execution-aware counterpart of :meth:`unproven_at`. A release gate that
        only asks about traceability passes a build whose P1 behaviour is broken.
        """
        band = (
            priority
            if isinstance(priority, RequirementPriority)
            else RequirementPriority(str(priority).strip().upper())
        )
        return tuple(
            item.requirement_id
            for item in self.requirements
            if item.priority is band and not item.is_proven
        )

    def by_priority(self) -> dict[str, dict[str, int]]:
        bands: dict[str, dict[str, int]] = {}
        for item in self.requirements:
            band = item.priority.value if item.priority else "unbanded"
            counts = bands.setdefault(band, {"total": 0, "verified": 0, "demonstrated": 0})
            counts["total"] += 1
            if item.status is RequirementStatus.VERIFIED:
                counts["verified"] += 1
            if item.is_proven:
                counts["demonstrated"] += 1
        return dict(sorted(bands.items()))

    def summary(self) -> dict[str, object]:
        return {
            "requirements": self.total,
            "verified": self.verified,
            "demonstrated": self.demonstrated,
            "traced_not_demonstrated": list(self.traced_not_demonstrated),
            "partial": sum(
                1 for item in self.requirements if item.status is RequirementStatus.PARTIAL
            ),
            "uncovered": len(self.uncovered),
            "verified_ratio": round(self.verified_ratio, 4),
            "orphan_link_count": len(self.orphan_links),
            "untraced_test_count": len(self.untraced_tests),
            "by_priority": self.by_priority(),
        }


def compute_coverage(
    requirements: tuple[Requirement, ...],
    ledger: tuple[LedgerEntry, ...],
    verdicts: tuple[Verdict, ...] = (),
) -> CoverageReport:
    """Invert the ledger into a per-requirement view.

    ``verdicts`` are optional so that a coverage report can still be produced
    before anything has been executed; without them every requirement reports
    ``NOT_RUN`` rather than silently reporting success.
    """
    observed: dict[str, ExecutionState] = {}
    for verdict in verdicts:
        key = _test_key(verdict.assertion_id)
        state = _VERDICT_TO_EXECUTION[verdict.status]
        existing = observed.get(key)
        if existing is None:
            observed[key] = state
        else:
            # One test can yield several verdicts, one per browser projection.
            # The worst is the truth: passing in Chromium does not excuse failing
            # in WebKit.
            observed[key] = min(
                (existing, state), key=lambda item: _EXECUTION_PRECEDENCE.index(item)
            )
    by_requirement: dict[str, dict[LedgerStatus, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    registered = {requirement.id: requirement for requirement in requirements}
    orphans: dict[str, list[str]] = defaultdict(list)
    untraced: list[str] = []

    for entry in ledger:
        metadata = entry.test.metadata
        ids = metadata.requirement_ids if metadata else ()
        if not ids:
            if entry.test.assertions:
                # A test that asserts something but names no requirement is
                # unattributed work: real effort the ledger cannot credit.
                untraced.append(entry.id)
            continue
        for identifier in ids:
            if identifier in registered:
                by_requirement[identifier][entry.status].append(entry.id)
            else:
                orphans[identifier].append(entry.id)

    coverage: list[RequirementCoverage] = []
    for identifier, requirement in sorted(registered.items()):
        buckets = by_requirement.get(identifier, {})
        verified = tuple(sorted(buckets.get(LedgerStatus.VERIFIED, ())))
        partial = tuple(sorted(buckets.get(LedgerStatus.PARTIAL, ())))
        unknown = tuple(sorted(buckets.get(LedgerStatus.UNKNOWN, ())))
        if verified:
            status = RequirementStatus.VERIFIED
        elif partial or unknown:
            status = RequirementStatus.PARTIAL
        else:
            status = RequirementStatus.UNCOVERED

        traced = (*verified, *partial, *unknown)
        states = {test: observed[_test_key(test)] for test in traced if _test_key(test) in observed}
        if not states:
            execution = ExecutionState.NOT_RUN
        else:
            execution = min(
                states.values(), key=lambda item: _EXECUTION_PRECEDENCE.index(item)
            )
        holding_back = tuple(
            sorted(
                test
                for test, state in states.items()
                if state is not ExecutionState.DEMONSTRATED
            )
        )

        coverage.append(
            RequirementCoverage(
                requirement_id=identifier,
                title=requirement.title,
                area=requirement.area,
                priority=requirement.priority,
                status=status,
                verified_tests=verified,
                partial_tests=partial,
                unknown_tests=unknown,
                execution=execution,
                failing_tests=holding_back,
            )
        )

    return CoverageReport(
        requirements=tuple(coverage),
        orphan_links={key: tuple(sorted(value)) for key, value in sorted(orphans.items())},
        untraced_tests=tuple(sorted(untraced)),
    )


def write_coverage_reports(report: CoverageReport, directory: Path) -> tuple[Path, Path]:
    """Write machine-readable and human-readable coverage side by side."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        **report.model_dump(mode="json"),
        "summary": report.summary(),
    }
    json_path = directory / "coverage.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary = report.summary()
    lines = [
        "# Requirement coverage",
        "",
        f"- Registered requirements: {summary['requirements']}",
        f"- Traceable to at least one verified test: {summary['verified']}",
        f"- **Traceable and demonstrated by execution: {summary['demonstrated']}**",
        f"- Referenced but not established: {summary['partial']}",
        f"- **Uncovered (no test references them): {summary['uncovered']}**",
        f"- Orphan links (test cites an unregistered id): {summary['orphan_link_count']}",
        f"- Untraced tests (assert something, name no requirement): "
        f"{summary['untraced_test_count']}",
        "",
        "| Requirement | Priority | Area | Traceability | Execution | Verified | Partial | "
        "Unknown |",
        "|---|---|---|---|---|---:|---:|---:|",
    ]
    for item in report.requirements:
        lines.append(
            f"| `{item.requirement_id}` | {item.priority.value if item.priority else '—'} | "
            f"{item.area or '—'} | {item.status.value} | {item.execution.value} | "
            f"{len(item.verified_tests)} | {len(item.partial_tests)} | "
            f"{len(item.unknown_tests)} |"
        )
    if report.traced_not_demonstrated:
        lines.extend(
            [
                "",
                "## Traceable but not demonstrated",
                "",
                "Perfect traceability and a defect against it. Reporting these as "
                "verified without saying so is the most misleading thing this report "
                "could do.",
                "",
            ]
        )
        for item in report.requirements:
            if item.requirement_id in report.traced_not_demonstrated:
                held = ", ".join(f"`{test}`" for test in item.failing_tests) or "—"
                lines.append(f"- `{item.requirement_id}` — {item.execution.value}: {held}")
    bands = report.by_priority()
    if bands:
        lines.extend(
            [
                "",
                "## By priority",
                "",
                "Percentages hide risk. A release gate should ask whether anything "
                "critical is unproven, not what the average looks like.",
                "",
                "| Priority | Verified | Total |",
                "|---|---:|---:|",
                *(
                    f"| {band} | {counts['verified']} | {counts['total']} |"
                    for band, counts in bands.items()
                ),
            ]
        )
    if report.uncovered:
        lines.extend(
            [
                "",
                "## Uncovered requirements",
                "",
                *(f"- `{identifier}`" for identifier in report.uncovered),
            ]
        )
    if report.orphan_links:
        lines.extend(
            [
                "",
                "## Orphan links",
                "",
                "These tests cite requirement identifiers no registry contains, so they "
                "believe they cover something that was never specified.",
                "",
                *(
                    f"- `{identifier}` cited by {len(tests)} test(s)"
                    for identifier, tests in report.orphan_links.items()
                ),
            ]
        )
    lines.extend(
        [
            "",
            "A requirement is VERIFIED only when a test reaches VERIFIED against it. "
            "That means traceable and attributable, never correct.",
        ]
    )
    markdown_path = directory / "coverage.md"
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path
