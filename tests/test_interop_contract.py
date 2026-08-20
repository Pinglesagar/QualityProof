"""The Python half of the cross-language contract tests.

The TypeScript suite validates these same fixtures with ajv. Two independent
validators over one set of files is what makes "language-neutral" a checked
property rather than a design intention: if either side drifts, a suite fails
instead of an unauditable manifest reaching the ledger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qualityproof.audit import build_ledger
from qualityproof.external import external_audited_tests, ingest_manifest, read_manifest
from qualityproof.models import ExternalRunManifest, LedgerStatus, VerdictStatus
from qualityproof.repository import SQLiteRepository


def _fixture_directory() -> Path:
    """Locate the shared fixtures without assuming the test tree's depth.

    Tools such as mutation runners execute the suite from a copied directory, so
    a path built from a fixed number of parents silently resolves to nothing.
    """
    for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents):
        target = candidate / "interop" / "fixtures"
        if target.is_dir():
            return target
    raise RuntimeError("interop fixture directory not found")


FIXTURES = _fixture_directory()


def _fixtures() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def test_shared_fixtures_exist_for_both_validators() -> None:
    assert _fixtures(), "interop fixtures are the contract; there must be at least one"


@pytest.mark.parametrize("path", _fixtures(), ids=lambda item: item.name)
def test_every_shared_fixture_validates_against_the_pydantic_contract(path: Path) -> None:
    manifest = ExternalRunManifest.model_validate_json(path.read_text(encoding="utf-8"))

    assert manifest.schema_version == "qualityproof-external-run/v1"
    assert manifest.redacted is True
    assert manifest.finished_at >= manifest.started_at


def test_a_drifted_schema_version_is_refused() -> None:
    payload = json.loads(_fixtures()[0].read_text(encoding="utf-8"))
    payload["schema_version"] = "qualityproof-external-run/v2"

    with pytest.raises(ValueError):
        ExternalRunManifest.model_validate(payload)


def test_playwright_flaky_survives_the_crossing_as_a_flaky_verdict() -> None:
    """Playwright's native flaky outcome must not decay into a pass.

    pytest has no such concept, so both runners meet on VerdictStatus.FLAKY.
    """
    manifest = ExternalRunManifest.model_validate_json(
        (FIXTURES / "flaky-shard-run.json").read_text(encoding="utf-8")
    )

    verdicts = {verdict.assertion_id: verdict.status for verdict in manifest.verdicts()}

    assert VerdictStatus.FLAKY in verdicts.values()
    assert manifest.shard == "1/2"


def test_unredacted_manifests_are_refused_at_the_boundary(tmp_path: Path) -> None:
    payload = json.loads(_fixtures()[0].read_text(encoding="utf-8"))
    payload["redacted"] = False
    manifest = ExternalRunManifest.model_validate(payload)
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    repository.initialize()

    with pytest.raises(ValueError, match="refusing to ingest unredacted"):
        ingest_manifest(manifest, tmp_path, repository)


def test_external_tests_are_judged_by_the_same_ledger_rules(tmp_path: Path) -> None:
    """A TypeScript test earns nothing for having run in a real browser.

    An annotated test without resolvable provenance is PARTIAL and an
    unannotated one is UNKNOWN, exactly as for Python sources.
    """
    manifest = ExternalRunManifest.model_validate_json(
        (FIXTURES / "flaky-shard-run.json").read_text(encoding="utf-8")
    )

    entries = {entry.id: entry for entry in build_ledger(external_audited_tests(manifest))}

    traced = entries["example/tests/cart.spec.ts::cart survives a reload"]
    untraced = entries["example/tests/cart.spec.ts::untraceable helper check"]
    assert traced.status is LedgerStatus.PARTIAL
    assert untraced.status is LedgerStatus.UNKNOWN


def test_ingest_records_external_evidence_in_the_ledger(tmp_path: Path) -> None:
    source = FIXTURES / "traced-run.json"
    target = tmp_path / "manifest.json"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    repository.initialize()

    manifest = read_manifest(target)
    entries = ingest_manifest(manifest, tmp_path, repository)

    assert len(entries) == 1
    assert entries[0].test.framework == "playwright-typescript"
    events = [event.event_type for event in repository.list_events()]
    assert "external_run_ingested" in events
