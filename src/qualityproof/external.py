"""Ingest evidence produced by non-Python runners.

The interop direction is deliberately one-way. A foreign runner reports what it
ran and what provenance each test declared; this module projects that report onto
the auditor's own vocabulary and hands it to the existing ledger. There is no
second evidence engine, no second database and no second set of trust rules —
which is the only way two languages can produce one auditable answer.
"""

from __future__ import annotations

from pathlib import Path

from qualityproof.models import (
    AuditedTest,
    AuditEvent,
    ExternalRunManifest,
    LedgerEntry,
)
from qualityproof.repository import SQLiteRepository
from qualityproof.security import EvidenceRedactor, is_within


def read_manifest(path: Path, project: Path | None = None) -> ExternalRunManifest:
    """Load and validate an external run manifest.

    Validation is strict: the manifest is untrusted input from another process,
    and a shape the engine cannot audit must fail loudly rather than contribute
    unverifiable rows to a ledger.
    """
    if project is not None and not is_within(path, project):
        raise ValueError("external manifest must live inside the project directory")
    if path.stat().st_size > 25_000_000:
        raise ValueError("external manifest exceeds the 25 MB safety limit")
    return ExternalRunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def external_audited_tests(manifest: ExternalRunManifest) -> tuple[AuditedTest, ...]:
    return tuple(record.to_audited_test() for record in manifest.tests)


def ingest_manifest(
    manifest: ExternalRunManifest,
    project: Path,
    repository: SQLiteRepository,
) -> tuple[LedgerEntry, ...]:
    """Merge external evidence into the ledger under the normal trust rules.

    Nothing is granted VERIFIED for having come from a real browser run: the same
    provenance resolution applies, so a TypeScript test with no requirement
    annotation lands as UNKNOWN exactly like an unannotated Python one.
    """
    from qualityproof.audit import build_ledger

    redactor = EvidenceRedactor.from_environment()
    if not manifest.redacted:
        raise ValueError(
            "external manifest declares redacted=false; refusing to ingest unredacted evidence"
        )
    tests = external_audited_tests(manifest)
    entries = build_ledger(tests, project=project, repository=repository)
    repository.replace_manifested_set(
        f"external-{manifest.framework.value}",
        "ledger",
        ((entry.id, entry) for entry in entries),
    )
    repository.replace_manifested_set(
        f"external-verdicts-{manifest.framework.value}",
        "verdict",
        ((verdict.assertion_id, verdict) for verdict in manifest.verdicts()),
    )
    repository.append_event(
        AuditEvent(
            id=f"external-ingest-{manifest.run_id}",
            event_type="external_run_ingested",
            details={
                "framework": manifest.framework.value,
                "run_id": redactor.text(manifest.run_id),
                "tests": len(manifest.tests),
                "shard": manifest.shard,
                "artifact_policy": manifest.artifact_policy,
            },
        )
    )
    return entries
