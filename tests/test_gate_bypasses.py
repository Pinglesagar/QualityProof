"""Bypasses found by adversarially attacking this project's own guarantees.

Every test here corresponds to a confirmed way of making the tool report
something false. They are grouped separately from the rest of the trust-rule
tests because each one existed in shipped code and passed the whole suite.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest
import yaml

from qualityproof.audit import ProvenanceResolver
from qualityproof.models import Provenance, ProvenanceKind, Requirement
from qualityproof.repository import SQLiteRepository
from qualityproof.scenarios import load_requirements

SPEC_TEXT = "Every sign-in is written to the audit trail."


def _registry(project: Path) -> tuple[Path, tuple[Requirement, ...]]:
    spec = project / "requirements.yaml"
    spec.write_text(
        yaml.safe_dump({"requirements": [{"id": "REQ-AUDIT", "description": SPEC_TEXT}]}),
        encoding="utf-8",
    )
    requirement = Requirement(
        id="REQ-AUDIT",
        title="Audit trail",
        description=SPEC_TEXT,
        provenance=(
            Provenance(
                kind=ProvenanceKind.REQUIREMENT,
                source=str(spec.resolve()),
                locator="requirement:REQ-AUDIT",
                content_hash=hashlib.sha256(SPEC_TEXT.encode()).hexdigest(),
            ),
        ),
    )
    return spec, (requirement,)


def test_a_locator_cannot_make_the_requirement_check_weaker(tmp_path: Path) -> None:
    """Citing a self-authored file must not beat citing nothing.

    The locator branch checked only that the identifier appeared in some file and
    in the registry, so a test could write its own description of a requirement
    and be credited against the real one. The no-locator branch was stricter,
    which meant *adding* provenance detail weakened the claim.
    """
    _, registry = _registry(tmp_path)
    resolver = ProvenanceResolver(project=tmp_path, requirements=registry)
    mine = tmp_path / "NOTES.md"
    mine.write_text(
        yaml.safe_dump(
            {"requirements": [{"id": "REQ-AUDIT", "description": "whatever I feel like"}]}
        ),
        encoding="utf-8",
    )

    forged = Provenance(
        kind=ProvenanceKind.REQUIREMENT, source="NOTES.md", locator="requirement:REQ-AUDIT"
    )
    genuine = Provenance(
        kind=ProvenanceKind.REQUIREMENT,
        source="requirements.yaml",
        locator="requirement:REQ-AUDIT",
    )

    assert resolver.resolve(forged, ("REQ-AUDIT",)) is False
    assert resolver.resolve(genuine, ("REQ-AUDIT",)) is True


def test_a_located_fragment_must_match_the_registered_digest(tmp_path: Path) -> None:
    """Pointing at the right file is not enough if the text has drifted."""
    spec, registry = _registry(tmp_path)
    resolver = ProvenanceResolver(project=tmp_path, requirements=registry)
    spec.write_text(
        yaml.safe_dump(
            {"requirements": [{"id": "REQ-AUDIT", "description": "quietly reworded"}]}
        ),
        encoding="utf-8",
    )

    provenance = Provenance(
        kind=ProvenanceKind.REQUIREMENT,
        source="requirements.yaml",
        locator="requirement:REQ-AUDIT",
    )

    assert resolver.resolve(provenance, ("REQ-AUDIT",)) is False


def test_api_spec_requires_a_resolvable_operation(tmp_path: Path) -> None:
    """Hashing any readable file was the cheapest forgery in the tool.

    With no locator the API_SPEC branch checked only that the cited requirement
    ids were registered, so a digest of a shopping list satisfied it.
    """
    _, registry = _registry(tmp_path)
    resolver = ProvenanceResolver(project=tmp_path, requirements=registry)
    junk = tmp_path / "shopping.txt"
    junk.write_text("milk, eggs, bread", encoding="utf-8")
    api = tmp_path / "openapi.json"
    api.write_text(
        json.dumps({"paths": {"/x": {"get": {"operationId": "getX"}}}}), encoding="utf-8"
    )

    forged = Provenance(
        kind=ProvenanceKind.API_SPEC,
        source="shopping.txt",
        content_hash=hashlib.sha256(junk.read_bytes()).hexdigest(),
    )
    genuine = Provenance(
        kind=ProvenanceKind.API_SPEC, source="openapi.json", locator="operation:getX"
    )

    assert resolver.resolve(forged, ("REQ-AUDIT",)) is False
    assert resolver.resolve(genuine, ("REQ-AUDIT",)) is True


def test_an_absolute_source_path_cannot_escape_the_project(tmp_path: Path) -> None:
    """The containment check exempted absolute paths, defeating its own purpose.

    Evidence for a VERIFIED requirement could live entirely outside the
    repository, and therefore outside code review.
    """
    project = tmp_path / "project"
    project.mkdir()
    _, registry = _registry(project)
    resolver = ProvenanceResolver(project=project, requirements=registry)
    outside = Path(tempfile.mkdtemp()) / "evil.yaml"
    outside.write_text(
        yaml.safe_dump({"requirements": [{"id": "REQ-AUDIT", "description": SPEC_TEXT}]}),
        encoding="utf-8",
    )

    escaping = Provenance(
        kind=ProvenanceKind.REQUIREMENT,
        source=str(outside),
        locator="requirement:REQ-AUDIT",
    )

    assert resolver.resolve(escaping, ("REQ-AUDIT",)) is False


def test_a_seed_manifest_cannot_take_over_a_specification_id(tmp_path: Path) -> None:
    """Seed ids are namespaced so a fixture cannot impersonate a requirement.

    An un-namespaced projection let a four-line seed file replace a registered
    requirement, after which a test citing the attacker's own file was credited
    against the real identifier.
    """
    hijack = tmp_path / "hijack.json"
    hijack.write_text(
        json.dumps({"seeds": [{"id": "REQ-AUDIT", "category": "cosmetic"}]}), encoding="utf-8"
    )

    projected = load_requirements(hijack)

    assert [item.id for item in projected] == ["SEED-REQ-AUDIT"]
    # The locator still names the seed as written, so resolution still works.
    assert projected[0].provenance[0].locator == "seed:REQ-AUDIT"


def test_already_namespaced_seed_ids_are_not_double_prefixed(tmp_path: Path) -> None:
    manifest = tmp_path / "seeds.json"
    manifest.write_text(
        json.dumps({"seeds": [{"id": "SEED-LAYOUT-001", "category": "layout"}]}),
        encoding="utf-8",
    )

    assert [item.id for item in load_requirements(manifest)] == ["SEED-LAYOUT-001"]


def test_one_scope_cannot_delete_another_scopes_requirements(tmp_path: Path) -> None:
    """Two unrelated imports could remove a requirement and turn a gate green.

    Records are keyed globally while ownership is per-scope, so a scope's stale
    sweep deleted ids another manifest still claimed — silently, and while that
    manifest went on listing them.
    """
    repository = SQLiteRepository(tmp_path / "q.db")
    repository.initialize()
    repository.replace_manifested_set(
        "official",
        "requirement",
        [
            (name, Requirement(id=name, title=name, description="d"))
            for name in ("REQ-LOGIN", "REQ-AUDIT")
        ],
    )

    repository.replace_manifested_set(
        "housekeeping",
        "requirement",
        [("REQ-OTHER", Requirement(id="REQ-OTHER", title="t", description="d"))],
    )
    repository.replace_manifested_set("housekeeping", "requirement", [])

    remaining = {item.id for item in repository.list("requirement", Requirement)}
    assert {"REQ-LOGIN", "REQ-AUDIT"} <= remaining


def test_a_scope_cannot_hijack_ids_owned_by_another_scope(tmp_path: Path) -> None:
    """Overwriting another scope's payload must fail loudly, not last-writer-win."""
    repository = SQLiteRepository(tmp_path / "q.db")
    repository.initialize()
    repository.replace_manifested_set(
        "official",
        "requirement",
        [("REQ-LOGIN", Requirement(id="REQ-LOGIN", title="Login", description="real"))],
    )

    with pytest.raises(ValueError, match="already owned by another scope"):
        repository.replace_manifested_set(
            "attacker",
            "requirement",
            [("REQ-LOGIN", Requirement(id="REQ-LOGIN", title="hijacked", description="d"))],
        )

    survivor = repository.list("requirement", Requirement)[0]
    assert survivor.title == "Login"


def test_a_scope_may_still_replace_its_own_entries(tmp_path: Path) -> None:
    """The ownership check must not make re-import impossible."""
    repository = SQLiteRepository(tmp_path / "q.db")
    repository.initialize()
    for title in ("first", "second"):
        repository.replace_manifested_set(
            "official",
            "requirement",
            [("REQ-1", Requirement(id="REQ-1", title=title, description="d"))],
        )

    assert repository.list("requirement", Requirement)[0].title == "second"


def test_a_review_event_cannot_be_borrowed_by_substring(tmp_path: Path) -> None:
    """Corroboration was a substring test, so one approval unlocked everything.

    `provenance.source in event.id` meant a source of "e" matched any event whose
    id contained an "e" — which every `review-` prefixed id does. One genuine
    approval anywhere in the project could then mark an unrelated test verified.
    """
    from datetime import UTC, datetime

    from qualityproof.models import AuditEvent

    approved_at = datetime(2026, 1, 1, tzinfo=UTC)
    event = AuditEvent(
        id="review-abc123",
        event_type="scenario_approved",
        actor="alice",
        occurred_at=approved_at,
        details={"scenario_id": "journey-7"},
    )
    resolver = ProvenanceResolver(
        project=tmp_path, review_events=(event,), has_repository=True
    )

    def resolves(source: str) -> bool:
        return resolver.resolve(
            Provenance(
                kind=ProvenanceKind.HUMAN_APPROVED,
                source=source,
                approved_by="alice",
                approved_at=approved_at,
            ),
            ("REQ-1",),
        )

    assert resolves("e") is False
    assert resolves("review") is False
    # Naming the event, or the subject it reviewed, still works.
    assert resolves("review-abc123") is True
    assert resolves("journey-7") is True
    assert resolves("journey-9") is False


def test_an_external_manifest_must_live_inside_the_project(tmp_path: Path) -> None:
    """Foreign evidence reaches the same trust rules, so it gets the same checks."""
    from qualityproof.external import read_manifest

    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(
            {
                "schema_version": "qualityproof-external-run/v1",
                "run_id": "r",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "redacted": True,
                "tests": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inside the project"):
        read_manifest(outside, project)
