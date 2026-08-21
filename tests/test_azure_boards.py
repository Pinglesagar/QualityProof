"""Azure Boards synchronization, held to the same rules as Jira.

The point of these tests is not that a second tracker exists. It is that adding
one did not fork the rules: the same finding produces the same fingerprint, the
same idempotency, the same refusal to reuse a mapping across scopes, and the same
dry-run-by-default contract, while the payload and transport differ because the
products genuinely differ.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from qualityproof.azure_boards import (
    PAT_ENV,
    AzureBoardsAdapter,
    AzureBoardsRenderer,
    LocalJSONAzureBoardsAdapter,
    html_description,
)
from qualityproof.cli import app
from qualityproof.jira import JiraRenderer, LocalJSONJiraAdapter, finding_fingerprint
from qualityproof.models import IssueTracker, JiraFinding, JiraIssueResult
from qualityproof.repository import SQLiteRepository
from qualityproof.trackers import synchronize_finding

FINDING = JiraFinding(
    title="Catalogue presents no level-one heading",
    summary="No heading names the page",
    requirement_ids=("JS-CAT-2",),
    route="/#/search",
    severity="medium",
    evidence={"observed_heading_count": 0, "authorization": "Bearer secret-value"},
)


def _sync(
    tmp_path: Path, finding: JiraFinding = FINDING, **kwargs: object
) -> tuple[JiraIssueResult, SQLiteRepository, LocalJSONAzureBoardsAdapter]:
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    transport = LocalJSONAzureBoardsAdapter(tmp_path / "work-items.json")
    return (
        synchronize_finding(
            finding,
            "Quality Proof",
            AzureBoardsRenderer(),
            transport,
            repository,
            fingerprint=finding_fingerprint(finding),
            item_type="Bug",
            **kwargs,  # type: ignore[arg-type]
        ),
        repository,
        transport,
    )


def _fields(payload: list[dict[str, object]]) -> dict[str, object]:
    return {str(op["path"]): op["value"] for op in payload}


def test_a_work_item_is_a_json_patch_document(tmp_path: Path) -> None:
    """Azure Boards is written with a patch array, not a field object.

    Sending a field object is rejected, and the error does not say why, so the
    payload shape is pinned here rather than discovered against a real board.
    """
    result, _, _ = _sync(tmp_path)
    assert isinstance(result.request, list)
    assert all(op["op"] == "add" for op in result.request)
    fields = _fields(result.request)
    assert fields["/fields/System.Title"] == FINDING.title
    assert fields["/fields/System.Tags"] == (
        f"qualityproof; qp-{finding_fingerprint(FINDING)[:12]}"
    )


def test_dry_run_is_the_default_and_writes_nothing(tmp_path: Path) -> None:
    result, _, _ = _sync(tmp_path)
    assert result.dry_run is True
    assert result.action == "create"
    assert result.issue_key is None
    assert not (tmp_path / "work-items.json").exists()


def test_a_repeated_sync_updates_the_same_work_item(tmp_path: Path) -> None:
    """Idempotency is the whole reason the fingerprint is tagged onto the item."""
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    transport = LocalJSONAzureBoardsAdapter(tmp_path / "work-items.json")

    def run() -> object:
        return synchronize_finding(
            FINDING,
            "Quality Proof",
            AzureBoardsRenderer(),
            transport,
            repository,
            fingerprint=finding_fingerprint(FINDING),
            dry_run=False,
            item_type="Bug",
        )

    created = run()
    updated = run()
    assert created.issue_key == updated.issue_key == "1"  # type: ignore[attr-defined]
    assert created.action == "create"  # type: ignore[attr-defined]
    assert updated.action == "update"  # type: ignore[attr-defined]
    stored = json.loads((tmp_path / "work-items.json").read_text(encoding="utf-8"))
    assert list(stored) == ["1"], "a repeated sync must not file a duplicate"


def test_the_same_finding_has_one_identity_across_both_trackers(tmp_path: Path) -> None:
    """A finding is evidence about the application, not about a tracker."""
    assert finding_fingerprint(FINDING) == finding_fingerprint(FINDING)
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    fingerprint = finding_fingerprint(FINDING)

    jira = synchronize_finding(
        FINDING,
        "QP",
        JiraRenderer(),
        LocalJSONJiraAdapter(tmp_path / "issues.json"),
        repository,
        fingerprint=fingerprint,
        dry_run=False,
        item_type="Bug",
    )
    boards = synchronize_finding(
        FINDING,
        "Quality Proof",
        AzureBoardsRenderer(),
        LocalJSONAzureBoardsAdapter(tmp_path / "work-items.json"),
        repository,
        fingerprint=fingerprint,
        dry_run=False,
        item_type="Bug",
    )
    assert jira.fingerprint == boards.fingerprint == fingerprint
    # Both are creates, because the tracker is part of the mapping identity. If it
    # were not, the second call would either be mistaken for an update of the
    # first or rejected as a corrupted mapping.
    assert jira.action == boards.action == "create"


def test_evidence_is_escaped_before_it_reaches_an_html_field() -> None:
    """A description field is HTML, so unescaped evidence could rewrite it.

    Evidence that can close its own container and inject markup into a work item
    is not evidence, so this is checked with a payload that tries.
    """
    hostile = JiraFinding(
        title="t",
        summary="</p><script>alert(1)</script>",
        evidence={"note": "</pre><img src=x onerror=alert(1)>"},
    )
    rendered = html_description(hostile, "f" * 64)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "onerror=alert(1)>" not in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered


def test_credentials_in_evidence_are_redacted_before_transmission(tmp_path: Path) -> None:
    result, _, _ = _sync(tmp_path)
    assert isinstance(result.request, list)
    description = str(_fields(result.request)["/fields/System.Description"])
    assert "secret-value" not in description
    assert "REDACTED" in description


@pytest.mark.parametrize(
    "name",
    ["Quality Proof", "QP", "a" * 64, "Production Bug Support"],
)
def test_project_names_azure_devops_allows_are_accepted(name: str) -> None:
    """Jira's key pattern would reject a valid Azure DevOps project with a space."""
    assert AzureBoardsRenderer().validate_project(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "   ", "a" * 65, "_leading", "trailing.", "has/slash", "has:colon", "has?query"],
)
def test_project_names_azure_devops_forbids_are_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        AzureBoardsRenderer().validate_project(name)


def test_a_personal_access_token_is_sent_as_basic_with_an_empty_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure DevOps expects ``:<pat>``. Any other spelling fails opaquely."""
    monkeypatch.setenv(PAT_ENV, "pat-value")
    adapter = AzureBoardsAdapter(
        "https://dev.azure.com/example", "Quality Proof", "Bug"
    )
    scheme, _, encoded = adapter._authorization.partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == ":pat-value"


def test_a_missing_token_names_the_variable_and_the_scope_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PAT_ENV, raising=False)
    with pytest.raises(ValueError, match=PAT_ENV):
        AzureBoardsAdapter("https://dev.azure.com/example", "P", "Bug")


def test_a_token_containing_a_colon_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``user:token`` pasted whole would authenticate as neither half."""
    monkeypatch.setenv(PAT_ENV, "user:pat-value")
    with pytest.raises(ValueError, match="no ':'"):
        AzureBoardsAdapter("https://dev.azure.com/example", "P", "Bug")


@pytest.mark.parametrize(
    "url",
    [
        "http://dev.azure.com/example",
        "https://user:pw@dev.azure.com/example",
        "https://dev.azure.com/example?x=1",
        "https://dev.azure.com/example#frag",
        "https://dev.azure.com/example:8443",
        "https://dev.azure.com",
        "https://dev.azure.com/example/extra",
        "https://evil.test/example",
        "https://dev.azure.com.evil.test/example",
    ],
)
def test_credentials_are_restricted_to_validated_azure_devops_hosts(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token is only ever sent to a host that is provably Azure DevOps."""
    monkeypatch.setenv(PAT_ENV, "pat-value")
    with pytest.raises(ValueError):
        AzureBoardsAdapter(url, "P", "Bug")


@pytest.mark.parametrize(
    "url",
    ["https://dev.azure.com/example", "https://example.visualstudio.com"],
)
def test_both_supported_organization_url_forms_are_accepted(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PAT_ENV, "pat-value")
    assert AzureBoardsAdapter(url, "P", "Bug").organization_url == url


def test_an_update_requires_a_numeric_work_item_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A work item id is numeric, so a non-numeric key means a corrupted mapping."""
    monkeypatch.setenv(PAT_ENV, "pat-value")
    adapter = AzureBoardsAdapter("https://dev.azure.com/example", "P", "Bug")
    with pytest.raises(ValueError, match="numeric"):
        adapter.update_issue("MOCK-1", [{"op": "add"}])


def test_an_empty_work_item_type_is_refused(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    with pytest.raises(ValueError, match="item_type"):
        synchronize_finding(
            FINDING,
            "Quality Proof",
            AzureBoardsRenderer(),
            LocalJSONAzureBoardsAdapter(tmp_path / "work-items.json"),
            repository,
            fingerprint=finding_fingerprint(FINDING),
            item_type="   ",
        )


def test_the_renderer_declares_its_tracker() -> None:
    assert AzureBoardsRenderer().tracker is IssueTracker.AZURE_BOARDS
    assert JiraRenderer().tracker is IssueTracker.JIRA


def test_cli_boards_sync_is_a_dry_run_by_default(tmp_path: Path) -> None:
    finding_path = tmp_path / "finding.json"
    finding_path.write_text(FINDING.model_dump_json(), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "boards",
            "sync",
            str(finding_path),
            "--ado-project",
            "Quality Proof",
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["issue_key"] is None
    assert isinstance(payload["request"], list)
    assert not (tmp_path / ".qualityproof" / "boards" / "work-items.json").exists()


def test_cli_boards_sync_requires_an_organization_for_the_real_adapter(
    tmp_path: Path,
) -> None:
    """A missing organization must fail before anything reads a credential."""
    finding_path = tmp_path / "finding.json"
    finding_path.write_text(FINDING.model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "boards",
            "sync",
            str(finding_path),
            "--ado-project",
            "P",
            "--adapter",
            "azure",
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "--organization-url" in result.output
