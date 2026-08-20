import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from qualityproof.audit import ProvenanceResolver, audit_file, build_ledger, classify
from qualityproof.metadata import qualityproof
from qualityproof.models import (
    AuditedTest,
    AuditEvent,
    LedgerStatus,
    Provenance,
    ProvenanceKind,
    SourceAssertion,
    TestMetadata,
)


def _write_source(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "test_example.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_audits_pytest_assert_and_playwright_expect_with_metadata(tmp_path: Path) -> None:
    path = _write_source(
        tmp_path,
        """
import pytest
from qualityproof import qualityproof

@qualityproof(
    requirements=["REQ-1"],
    provenance=[{"kind": "REQUIREMENT", "source": "requirements.md", "locator": "#login"}],
)
def test_login(page):
    assert page.url
    expect(page.get_by_text("Welcome")).to_be_visible()

@pytest.mark.qualityproof(
    requirements=["REQ-2"],
    provenance=[{"kind": "API_SPEC", "source": "openapi.json", "locator": "operation:getHealth"}],
)
def test_api():
    assert 200 == 200
""",
    )

    tests = audit_file(path, tmp_path)

    assert [item.name for item in tests] == ["test_login", "test_api"]
    assert [assertion.kind for assertion in tests[0].assertions] == ["assert", "expect"]
    assert tests[0].framework == "playwright"
    assert all(entry.status is LedgerStatus.VERIFIED for entry in build_ledger(tests))


def test_scenario_class_metadata_is_inherited(tmp_path: Path) -> None:
    path = _write_source(
        tmp_path,
        """
from qualityproof import qualityproof

@qualityproof(
    requirements=["REQ-CHECKOUT"],
    provenance=[{
        "kind": "HUMAN_APPROVED",
        "source": "review/42",
        "approved_by": "reviewer",
        "approved_at": "2026-01-01T00:00:00Z",
    }],
)
class TestCheckout:
    def test_total(self):
        assert 2 + 2 == 4
""",
    )

    audited = audit_file(path, tmp_path)

    assert audited[0].name == "TestCheckout.test_total"
    assert audited[0].metadata is not None
    assert audited[0].metadata.requirement_ids == ("REQ-CHECKOUT",)


def test_assertion_collection_excludes_nested_functions_classes_and_lambdas(
    tmp_path: Path,
) -> None:
    path = _write_source(
        tmp_path,
        """
def test_outer():
    assert True
    def nested():
        assert False
        expect(page).to_be_visible()
    class Nested:
        assert False
    callback = lambda: expect(page).to_be_hidden()
""",
    )

    assertions = audit_file(path, tmp_path)[0].assertions

    assert len(assertions) == 1
    assert assertions[0].expression == "True"


def test_zero_config_is_unknown() -> None:
    test = AuditedTest(
        id="test_sample.py::test_plain",
        path="test_sample.py",
        name="test_plain",
        line=1,
        framework="pytest",
        assertions=(SourceAssertion(kind="assert", line=2, expression="value"),),
    )

    result = classify(test)

    assert result.status is LedgerStatus.UNKNOWN
    assert "zero-config" in result.reason


def test_observation_and_baseline_alone_are_partial() -> None:
    test = AuditedTest(
        id="test_sample.py::test_visual",
        path="test_sample.py",
        name="test_visual",
        line=1,
        framework="playwright",
        assertions=(SourceAssertion(kind="expect", line=2, expression="expect(page).to_match()"),),
        metadata=TestMetadata(
            requirement_ids=("REQ-1",),
            provenance=(
                Provenance(kind=ProvenanceKind.OBSERVATION, source="runtime-report.json"),
                Provenance(kind=ProvenanceKind.BASELINE, source="snapshots/home.png"),
            ),
        ),
    )

    assert classify(test).status is LedgerStatus.PARTIAL


def test_ai_hypothesis_needs_approval_and_expired_sources_are_partial() -> None:
    now = datetime.now(UTC)
    base = {
        "id": "test_ai.py::test_ai",
        "path": "test_ai.py",
        "name": "test_ai",
        "line": 1,
        "framework": "pytest",
        "assertions": (SourceAssertion(kind="assert", line=2, expression="result"),),
    }
    unapproved = AuditedTest(
        **base,
        metadata=TestMetadata(
            requirement_ids=("REQ-AI",),
            provenance=(Provenance(kind=ProvenanceKind.AI_HYPOTHESIS, source="model"),),
        ),
    )
    approved = AuditedTest(
        **base,
        metadata=TestMetadata(
            requirement_ids=("REQ-AI",),
            provenance=(
                Provenance(
                    kind=ProvenanceKind.AI_HYPOTHESIS,
                    source="model",
                    approved_by="reviewer@example.com",
                    approved_at=now,
                ),
            ),
        ),
    )
    expired = AuditedTest(
        **base,
        metadata=TestMetadata(
            requirement_ids=("REQ-AI",),
            provenance=(
                Provenance(
                    kind=ProvenanceKind.REQUIREMENT,
                    source="old.md",
                    locator="#old",
                    captured_at=now - timedelta(days=2),
                    expires_at=now - timedelta(days=1),
                ),
            ),
        ),
    )

    assert classify(unapproved).status is LedgerStatus.PARTIAL
    assert classify(approved).status is LedgerStatus.VERIFIED
    assert classify(expired).status is LedgerStatus.PARTIAL


def test_decorator_attaches_validated_metadata() -> None:
    @qualityproof(
        requirements=["REQ-1"],
        provenance=[{
            "kind": "REQUIREMENT",
            "source": "requirements.md",
            "locator": "#REQ-1",
        }],
    )
    def sample() -> None:
        pass

    metadata = sample.__qualityproof__  # type: ignore[attr-defined]
    assert metadata.requirement_ids == ("REQ-1",)


def test_defining_provenance_requires_approval_or_resolvable_source_integrity() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="HUMAN_APPROVED requires"):
        Provenance(kind=ProvenanceKind.HUMAN_APPROVED, source="review/42")
    with pytest.raises(ValidationError, match="locator or content_hash"):
        Provenance(kind=ProvenanceKind.API_SPEC, source="openapi.json")

    content = '{"openapi":"3.1.0"}'
    provenance = Provenance(
        kind=ProvenanceKind.API_SPEC,
        source="openapi.json",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    approved = Provenance(
        kind=ProvenanceKind.HUMAN_APPROVED,
        source="review/42",
        approved_by="reviewer",
        approved_at=now,
    )

    assert provenance.validates_content(content)
    assert not provenance.validates_content(content + " ")
    assert approved.is_authoritative(now)


def test_classification_resolves_requirement_locator_hash_and_association(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.yaml"
    requirements.write_text(
        "requirements:\n- id: REQ-1\n  title: Login\n  description: Users can log in.\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(b"Users can log in.").hexdigest()
    base = AuditedTest(
        id="test.py::test_login",
        path="test.py",
        name="test_login",
        line=1,
        framework="pytest",
        assertions=(SourceAssertion(kind="assert", line=2, expression="result"),),
        metadata=TestMetadata(
            requirement_ids=("REQ-1",),
            provenance=(
                Provenance(
                    kind=ProvenanceKind.REQUIREMENT,
                    source="requirements.yaml",
                    locator="requirement:REQ-1",
                    content_hash=digest,
                ),
            ),
        ),
    )
    resolver = ProvenanceResolver(tmp_path)

    assert classify(base, resolver=resolver).status is LedgerStatus.VERIFIED
    mismatched = base.model_copy(
        update={
            "metadata": base.metadata.model_copy(
                update={"requirement_ids": ("REQ-OTHER",)}
            )
            if base.metadata
            else None
        }
    )
    assert classify(mismatched, resolver=resolver).status is LedgerStatus.PARTIAL
    requirements.write_text(
        "requirements:\n- id: REQ-1\n  title: Login\n  description: Changed.\n",
        encoding="utf-8",
    )
    assert classify(base, resolver=resolver).status is LedgerStatus.PARTIAL


def test_human_approval_matches_persisted_review_event_when_available(tmp_path: Path) -> None:
    approved_at = datetime.now(UTC)
    test = AuditedTest(
        id="test.py::test_reviewed",
        path="test.py",
        name="test_reviewed",
        line=1,
        framework="pytest",
        assertions=(SourceAssertion(kind="assert", line=2, expression="result"),),
        metadata=TestMetadata(
            requirement_ids=("REQ-1",),
            provenance=(
                Provenance(
                    kind=ProvenanceKind.HUMAN_APPROVED,
                    source="review-42",
                    approved_by="alice",
                    approved_at=approved_at,
                ),
            ),
        ),
    )
    matching = AuditEvent(
        id="review-42",
        event_type="scenario_approved",
        actor="alice",
        occurred_at=approved_at,
        details={"scenario_id": "scenario-1"},
    )
    mismatched = matching.model_copy(update={"actor": "mallory"})

    assert classify(
        test,
        resolver=ProvenanceResolver(tmp_path, review_events=(matching,)),
    ).status is LedgerStatus.VERIFIED
    assert classify(
        test,
        resolver=ProvenanceResolver(tmp_path, review_events=(mismatched,)),
    ).status is LedgerStatus.PARTIAL
