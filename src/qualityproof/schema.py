"""Versioned JSON Schema export for public domain models."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from qualityproof.models import (
    ActionEdge,
    Assertion,
    AuditedTest,
    AuditEvent,
    DiscoveryResult,
    Evidence,
    EvidenceSnapshot,
    EvidenceSnapshotDiff,
    ExternalRunManifest,
    ExternalTestRecord,
    FailedLocatorEvidence,
    HealingReview,
    JiraFinding,
    JiraIssueMapping,
    JiraIssueResult,
    LedgerEntry,
    LocatorHealingProposal,
    PageState,
    Provenance,
    Requirement,
    Scenario,
    ScenarioReview,
    ScenarioSpec,
    SemanticCandidate,
    SourceAssertion,
    TestMetadata,
    TestRunResult,
    UnknownItem,
    Verdict,
)

SCHEMA_VERSION = "v1"
SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    Provenance,
    Requirement,
    Scenario,
    ScenarioSpec,
    ScenarioReview,
    Assertion,
    Evidence,
    Verdict,
    UnknownItem,
    PageState,
    ActionEdge,
    SourceAssertion,
    TestMetadata,
    AuditedTest,
    LedgerEntry,
    AuditEvent,
    DiscoveryResult,
    TestRunResult,
    JiraFinding,
    JiraIssueMapping,
    JiraIssueResult,
    FailedLocatorEvidence,
    SemanticCandidate,
    LocatorHealingProposal,
    HealingReview,
    EvidenceSnapshot,
    EvidenceSnapshotDiff,
    ExternalTestRecord,
    ExternalRunManifest,
)


def export_schemas(output_directory: Path, version: str = SCHEMA_VERSION) -> tuple[Path, ...]:
    """Write deterministic per-model JSON schemas under a version directory."""
    if not version or "/" in version or version in {".", ".."}:
        raise ValueError("version must be a non-empty path segment")

    destination = output_directory / version
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in SCHEMA_MODELS:
        path = destination / f"{model.__name__}.schema.json"
        path.write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return tuple(written)
