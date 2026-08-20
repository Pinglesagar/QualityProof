from __future__ import annotations

from pathlib import Path

import pytest

from qualityproof.security import EvidenceRedactor
from scripts.run_azure_job import _validated_run_id


def test_evidence_redaction_removes_sentinel_secrets_from_nested_artifacts() -> None:
    sentinel = "QP_SENTINEL_SECRET_41f8f1"
    redactor = EvidenceRedactor.from_environment(
        {"QUALITYPROOF_PASSWORD": sentinel, "ORDINARY": "visible"}
    )
    payload = {
        "log": f"failed Authorization: Bearer {sentinel}",
        "nested": [{"password": sentinel}, f"https://user:{sentinel}@example.test/"],
    }

    rendered = str(redactor.value(payload))

    assert sentinel not in rendered
    assert "user:" not in rendered
    assert rendered.count("<REDACTED>") >= 3


def test_browser_job_docker_contract_installs_and_smokes_required_tools() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )

    browser_stage = dockerfile.split("FROM runtime AS browser-job", maxsplit=1)[1]
    assert "uv sync --frozen" in browser_stage
    assert "scripts.image_smoke --launch-browser" in workflow
    assert "docker run --rm qualityproof-job:test" in workflow


def test_azure_run_ids_are_unique_and_queued_ids_are_strict() -> None:
    first = _validated_run_id(None)
    second = _validated_run_id(None)

    assert first != second
    assert len(first) == len("run-") + 32
    assert _validated_run_id("run-" + "a" * 32) == "run-" + "a" * 32
    with pytest.raises(ValueError):
        _validated_run_id("../../overwrite")


def test_azure_blob_writes_use_immutability_or_etag_conditions() -> None:
    source = (Path(__file__).parents[1] / "scripts/run_azure_job.py").read_text(
        encoding="utf-8"
    )

    assert "overwrite=False" in source
    assert "MatchConditions.IfNotModified" in source
    assert 'f"runs/{run_id}/' in source


def test_supply_chain_and_azure_defaults_are_narrow_and_immutable() -> None:
    root = Path(__file__).parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    deployment = (root / "infra/main.bicep").read_text(encoding="utf-8")

    assert "@sha256:" in dockerfile.splitlines()[1]
    # The scanner is installed from a pinned release asset with its checksum
    # verified, rather than through an installer that resolves a tag over the
    # GitHub API. That lookup is rate limited, and when it failed the step looked
    # exactly like a vulnerability finding. What this test guards is unchanged:
    # the scanner version must be fixed and its download verified.
    assert "TRIVY_VERSION:" in workflow
    assert "releases/download/v${TRIVY_VERSION}" in workflow
    assert "sha256sum -c -" in workflow
    assert "aquasecurity/trivy-action" not in workflow, (
        "the tag-resolving installer must not come back"
    )
    # Both images are still scanned, and the gate still enforces.
    assert "qualityproof-control qualityproof-job" in workflow
    assert "--severity CRITICAL,HIGH --exit-code 1" in workflow
    assert "--ignore-unfixed" in workflow
    assert "allowSharedKeyAccess: jobTriggerType == 'Event'" in deployment
    assert "param controlApiToken string" in deployment
    assert "param controlReportToken string" in deployment
    assert "secretRef: 'api-token'" in deployment
    assert "secretRef: 'report-token'" in deployment
