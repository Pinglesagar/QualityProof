from __future__ import annotations

from pathlib import Path

import pytest

from qualityproof.security import ArtifactMode, ArtifactPolicy, EvidenceRedactor
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


def test_configuration_flag_values_are_not_treated_as_secrets() -> None:
    """A flag named like a secret must not rewrite digits across the evidence.

    ``CLAUDE_CODE_CHILD_SESSION=1`` matched the secret-name pattern through its
    name, and short values are redacted on word boundaries, so the digit 1 was
    replaced everywhere. A real Juice Shop run therefore reported
    ``"<REDACTED> xfailed"`` instead of ``"1 xfailed"``: redaction had corrupted
    the count it existed to protect.
    """
    redactor = EvidenceRedactor.from_environment(
        {"CHILD_SESSION": "1", "DEBUG_TOKEN": "true", "REAL_TOKEN": "s3cret-value"}
    )
    summary = redactor.text("20 passed, 1 xfailed in 12.86s")
    assert summary == "20 passed, 1 xfailed in 12.86s"
    assert redactor.text("flag true here") == "flag true here"
    assert "s3cret-value" not in redactor.text("header s3cret-value")


def test_short_credentials_are_still_redacted_on_word_boundaries() -> None:
    """Excluding flag values must not excuse a genuinely short credential."""
    redactor = EvidenceRedactor.from_environment({"CARD_PASS": "4821"})
    assert redactor.text("pin 4821 entered") == "pin <REDACTED> entered"
    # Bounded, so an unrelated number that merely contains it survives intact.
    assert redactor.text("order 148213") == "order 148213"


def test_working_directory_is_not_redacted_from_evidence_paths() -> None:
    """PWD matches the secret-name pattern but its value is a public path.

    Redacting it replaced every absolute path in an evidence bundle, including
    trace and artifact locations, which makes a failure undiagnosable.
    """
    redactor = EvidenceRedactor.from_environment(
        {"PWD": "/Users/me/proj", "OLDPWD": "/Users/me", "API_TOKEN": "s3cret-value"}
    )
    trace = "trace at /Users/me/proj/.qualityproof/runs/run-1/trace.zip"
    assert redactor.text(trace) == trace
    assert "s3cret-value" not in redactor.text("token s3cret-value")


def test_a_plain_shell_environment_still_captures_failure_artifacts() -> None:
    """PWD and USER are set by every POSIX shell and are not credentials.

    Both name-matched the redaction pattern, which ArtifactPolicy reused as its
    authentication signal, so every run on every machine was classed as
    authenticated and traces were disabled unconditionally. The feature was dead
    in practice and only surfaced when a real Juice Shop run reported
    ``artifacts=off ... disabled_by=...,PWD,USER,...``.
    """
    policy = ArtifactPolicy.from_environment(
        {"PWD": "/Users/me/proj", "USER": "me", "HOME": "/Users/me", "SHELL": "/bin/zsh"}
    )
    assert policy.mode is ArtifactMode.ON_FAILURE
    assert policy.authenticated is False
    assert policy.reasons == ()


def test_a_real_credential_still_disables_artifact_capture() -> None:
    """Narrowing the signal must not stop a genuine secret from closing the gate."""
    policy = ArtifactPolicy.from_environment(
        {"PWD": "/Users/me/proj", "USER": "me", "GITHUB_TOKEN": "ghp_realvalue"}
    )
    assert policy.mode is ArtifactMode.OFF
    assert policy.authenticated is True
    assert policy.reasons == ("GITHUB_TOKEN",)


def test_a_credential_named_variable_holding_a_flag_is_not_a_credential() -> None:
    """A variable set to "1" carries no secret, whatever it is called."""
    policy = ArtifactPolicy.from_environment({"PWD": "/x", "CHILD_SESSION": "1"})
    assert policy.authenticated is False
    assert policy.mode is ArtifactMode.ON_FAILURE


def test_an_account_name_is_redacted_without_implying_a_credential() -> None:
    """The two questions are distinct: redact the identity, do not gate on it."""
    environ = {"QUALITYPROOF_USERNAME": "real.person@example.test"}
    assert ArtifactPolicy.from_environment(environ).authenticated is False
    redacted = EvidenceRedactor.from_environment(environ).text(
        "login failed for real.person@example.test"
    )
    assert "real.person@example.test" not in redacted
