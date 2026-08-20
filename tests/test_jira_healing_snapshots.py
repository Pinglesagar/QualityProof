import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from qualityproof.cli import app
from qualityproof.healing import propose_locator_healing, review_proposal, write_proposals
from qualityproof.jira import (
    JiraCloudAdapter,
    LocalJSONJiraAdapter,
    authorization_url,
    create_pkce_pair,
    finding_fingerprint,
    redact,
    sync_finding,
)
from qualityproof.models import (
    FailedLocatorEvidence,
    JiraFinding,
    JiraIssueMapping,
    LocatorSemantics,
    PageState,
    Requirement,
    ScenarioSpec,
    ScenarioStatus,
    SemanticCandidate,
    UnknownItem,
    Verdict,
    VerdictStatus,
)
from qualityproof.repository import SQLiteRepository
from qualityproof.snapshots import capture_snapshot, compare_snapshots


def test_jira_mock_is_dry_run_idempotent_and_redacted(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    adapter = LocalJSONJiraAdapter(tmp_path / "issues.json")
    finding = JiraFinding(
        title="Checkout fails",
        summary="Payment cannot finish",
        requirement_ids=("REQ-1",),
        evidence={"authorization": "Bearer dangerous", "user": "person@example.com"},
    )

    preview = sync_finding(finding, "QP", adapter, repository)
    assert preview.dry_run and not (tmp_path / "issues.json").exists()
    created = sync_finding(finding, "QP", adapter, repository, dry_run=False)
    updated = sync_finding(finding, "QP", adapter, repository, dry_run=False)

    assert created.issue_key == updated.issue_key == "MOCK-1"
    assert updated.action == "update"
    assert finding_fingerprint(finding) == created.fingerprint
    assert redact(finding.evidence) == {
        "authorization": "<REDACTED>",
        "user": "<REDACTED_EMAIL>",
    }
    stored = (tmp_path / "issues.json").read_text(encoding="utf-8")
    assert "dangerous" not in stored
    assert "person@example.com" not in stored


def test_jira_3lo_authorization_uses_state_and_pkce() -> None:
    verifier, challenge = create_pkce_pair()
    url = authorization_url(
        "client",
        "http://127.0.0.1/callback",
        ("read:jira-work",),
        state="expected-state",
        code_challenge=challenge,
    )

    assert len(verifier) >= 43
    assert "state=expected-state" in url
    assert f"code_challenge={challenge}" in url
    assert "code_challenge_method=S256" in url


def test_jira_mapping_is_scoped_by_adapter_account_project_and_fingerprint(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    finding = JiraFinding(title="Scoped", summary="Same finding")
    first = LocalJSONJiraAdapter(tmp_path / "account-one.json")
    second = LocalJSONJiraAdapter(tmp_path / "account-two.json")

    one = sync_finding(finding, "ONE", first, repository, dry_run=False)
    other_project = sync_finding(finding, "TWO", first, repository, dry_run=False)
    other_account = sync_finding(finding, "ONE", second, repository, dry_run=False)
    repeated = sync_finding(finding, "ONE", first, repository, dry_run=False)

    class CloudStub:
        adapter_name = "cloud"
        account_id = first.account_id
        created = 0

        def create_issue(self, fields: dict[str, object]) -> str:
            del fields
            self.created += 1
            return "CLOUD-1"

        def update_issue(self, issue_key: str, fields: dict[str, object]) -> None:
            raise AssertionError((issue_key, fields))

    cloud = CloudStub()
    cloud_result = sync_finding(finding, "ONE", cloud, repository, dry_run=False)

    assert one.action == other_project.action == other_account.action == "create"
    assert repeated.action == "update"
    assert cloud_result.action == "create"
    assert cloud.created == 1
    assert len(repository.list("jira_mapping", JiraIssueMapping)) == 4


def test_jira_cloud_bearer_is_restricted_to_validated_https_atlassian_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUALITYPROOF_JIRA_BEARER_TOKEN", "sentinel-jira-secret")
    for invalid in (
        "http://tenant.atlassian.net",
        "https://evil.test",
        "https://api.atlassian.com/other",
        "https://tenant.atlassian.net@evil.test",
    ):
        with pytest.raises(ValueError):
            JiraCloudAdapter(invalid)

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Response:
        content = b'{"key":"QP-1"}'

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"key": "QP-1"}

    def request(*args: object, **kwargs: object) -> Response:
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr("qualityproof.jira.httpx.request", request)
    adapter = JiraCloudAdapter("https://api.atlassian.com/ex/jira/cloud-id")
    assert adapter.create_issue({}) == "QP-1"
    assert calls[0][1]["follow_redirects"] is False
    assert str(calls[0][0][1]).startswith(
        "https://api.atlassian.com/ex/jira/cloud-id/rest/api/3/"
    )


def _healing_inputs() -> tuple[FailedLocatorEvidence, tuple[SemanticCandidate, ...]]:
    semantics = LocatorSemantics(
        precondition="signed in",
        user_intent="submit order",
        postcondition="confirmation shown",
    )
    failed = FailedLocatorEvidence(
        test_path="tests/test_checkout.py",
        line=20,
        old_locator='page.get_by_text("Buy")',
        role="button",
        name="Buy",
        context=("checkout",),
        semantics=semantics,
        assertion="confirmation is visible",
    )
    candidates = (
        SemanticCandidate(
            locator='page.get_by_role("button", name="Buy now")',
            role="button",
            name="Buy now",
            context=("checkout",),
            semantics=semantics,
        ),
        SemanticCandidate(
            locator='page.get_by_text("Delete")',
            role="button",
            name="Delete",
            semantics={
                "precondition": "signed in",
                "user_intent": "delete order",
                "postcondition": "order removed",
            },
        ),
    )
    return failed, candidates


def test_healing_preserves_contract_and_approval_only_emits_patch(tmp_path: Path) -> None:
    failed, candidates = _healing_inputs()
    source = tmp_path / failed.test_path
    source.parent.mkdir(parents=True)
    source.write_text(
        "def test_checkout(page):\n"
        '    expect(page.get_by_text("Buy")).to_be_visible()\n',
        encoding="utf-8",
    )
    failed = failed.model_copy(
        update={
            "line": 2,
            "assertion": 'expect(page.get_by_text("Buy")).to_be_visible()',
        }
    )
    proposals = propose_locator_healing(failed, candidates)
    paths = write_proposals(tmp_path, proposals)
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()

    review = review_proposal(
        tmp_path, repository, paths[0], "approve", "reviewer", "same semantic target"
    )

    assert len(proposals) == 1
    assert proposals[0].candidate.name == "Buy now"
    assert review.patch_path is not None
    assert source.read_text(encoding="utf-8").endswith(
        'expect(page.get_by_text("Buy")).to_be_visible()\n'
    )
    patch_path = tmp_path / review.patch_path
    assert patch_path.read_text(encoding="utf-8").startswith("--- a/")
    applied = subprocess.run(
        ["patch", "--dry-run", "-p1", "-i", str(patch_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr


def test_healing_rejects_more_than_ten_candidates() -> None:
    failed, candidates = _healing_inputs()
    repeated = tuple(
        candidates[0].model_copy(update={"locator": f'page.locator("#candidate-{index}")'})
        for index in range(11)
    )

    with pytest.raises(ValueError, match="must not exceed 10"):
        propose_locator_healing(failed, repeated)


def test_healing_rejects_injection_traversal_and_stale_source_context(tmp_path: Path) -> None:
    failed, candidates = _healing_inputs()
    with pytest.raises(ValidationError, match="safe relative"):
        FailedLocatorEvidence.model_validate({**failed.model_dump(), "test_path": "../escape.py"})
    with pytest.raises(ValidationError, match="single-line"):
        SemanticCandidate.model_validate(
            {**candidates[0].model_dump(), "locator": 'page.locator("x")\nraise SystemExit'}
        )

    source = tmp_path / failed.test_path
    source.parent.mkdir(parents=True)
    source.write_text(
        "def test_checkout(page):\n"
        '    expect(page.get_by_text("Different")).to_be_visible()\n',
        encoding="utf-8",
    )
    proposal = propose_locator_healing(
        failed.model_copy(
            update={
                "line": 2,
                "assertion": 'expect(page.get_by_text("Buy")).to_be_visible()',
            }
        ),
        candidates,
    )
    path = write_proposals(tmp_path, proposal)[0]
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    with pytest.raises(ValueError, match="exactly once"):
        review_proposal(tmp_path, repository, path, "approve", "reviewer", "stale")


def test_healing_proposal_redacts_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUALITYPROOF_TEST_TOKEN", "sentinel-healing-secret")
    failed, candidates = _healing_inputs()
    failed = failed.model_copy(
        update={
            "evidence": {
                "authorization": "Bearer dangerous",
                "detail": "sentinel-healing-secret",
            }
        }
    )
    path = write_proposals(tmp_path, propose_locator_healing(failed, candidates))[0]
    rendered = path.read_text(encoding="utf-8")
    assert "dangerous" not in rendered
    assert "sentinel-healing-secret" not in rendered


def test_snapshots_are_immutable_and_diff_is_deterministic(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    repository.put(
        "page_state",
        "one",
        PageState(id="one", url="https://example.test/a", route="/a", fingerprint="old"),
    )
    first, _ = capture_snapshot("release-1", tmp_path, repository)
    repository.put(
        "page_state",
        "one",
        PageState(id="one", url="https://example.test/a", route="/a", fingerprint="new"),
    )
    repository.put(
        "page_state",
        "two",
        PageState(id="two", url="https://example.test/b", route="/b", fingerprint="two"),
    )
    second, _ = capture_snapshot("release-2", tmp_path, repository)

    comparison = compare_snapshots(first, second)

    assert comparison.routes.added == ("/b",)
    assert comparison.page_fingerprints.added == ("/b#two",)
    assert comparison.page_fingerprints.changed == ("/a#one",)
    assert comparison.coverage_delta["pages"] == 1


def test_snapshot_diff_covers_all_advertised_dimensions_and_route_collisions(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    first_records = {
        "requirement": (
            ("REQ-1", Requirement(id="REQ-1", title="One", description="First")),
        ),
        "page_state": (
            (
                "page-a",
                PageState(
                    id="page-a",
                    url="https://example.test/items/1",
                    route="/items/{id}",
                    fingerprint="a",
                ),
            ),
            (
                "page-b",
                PageState(
                    id="page-b",
                    url="https://example.test/items/2",
                    route="/items/{id}",
                    fingerprint="b",
                ),
            ),
        ),
        "scenario": (
            (
                "scenario-a",
                ScenarioSpec(
                    id="scenario-a",
                    title="A",
                    status=ScenarioStatus.APPROVED,
                    steps=({"type": "navigate", "url": "https://example.test"},),
                ),
            ),
        ),
        "verdict": (
            (
                "assertion-a",
                Verdict(
                    assertion_id="assertion-a",
                    status=VerdictStatus.PASS,
                    rationale="passed",
                ),
            ),
        ),
        "unknown_item": (
            ("unknown-a", UnknownItem(id="unknown-a", question="Question A")),
        ),
    }
    repository.replace_sets(first_records)
    first, _ = capture_snapshot(
        "all-before",
        tmp_path,
        repository,
        {"version": "1", "features": {"checkout": False}},
    )
    repository.replace_sets(
        {
            "requirement": (
                ("REQ-2", Requirement(id="REQ-2", title="Two", description="Second")),
            ),
            "page_state": (
                (
                    "page-a",
                    PageState(
                        id="page-a",
                        url="https://example.test/items/1",
                        route="/items/{id}",
                        fingerprint="changed",
                    ),
                ),
            ),
            "scenario": (
                (
                    "scenario-b",
                    ScenarioSpec(
                        id="scenario-b",
                        title="B",
                        status=ScenarioStatus.APPROVED,
                        steps=({"type": "navigate", "url": "https://example.test/b"},),
                    ),
                ),
            ),
            "verdict": (
                (
                    "assertion-a",
                    Verdict(
                        assertion_id="assertion-a",
                        status=VerdictStatus.FAIL,
                        rationale="failed",
                    ),
                ),
            ),
            "unknown_item": (
                ("unknown-b", UnknownItem(id="unknown-b", question="Question B")),
            ),
        }
    )
    second, _ = capture_snapshot(
        "all-after",
        tmp_path,
        repository,
        {"version": "2", "features": {"checkout": True}, "commit": "abc"},
    )

    comparison = compare_snapshots(first, second)

    assert set(first.page_fingerprints) == {"/items/{id}#page-a", "/items/{id}#page-b"}
    assert comparison.requirements.added == ("REQ-2",)
    assert comparison.page_fingerprints.changed == ("/items/{id}#page-a",)
    assert comparison.page_fingerprints.removed == ("/items/{id}#page-b",)
    assert comparison.scenarios.added == ("scenario-b",)
    assert comparison.verdicts.changed == ("assertion-a",)
    assert comparison.unknowns.added == ("unknown-b",)
    assert comparison.application_metadata.added == ("commit",)
    assert comparison.application_metadata.changed == ("features", "version")


def test_cli_jira_dry_run_and_snapshot_diff(tmp_path: Path) -> None:
    finding = tmp_path / "finding.json"
    finding.write_text(
        JiraFinding(title="Bug", summary="Broken").model_dump_json(),
        encoding="utf-8",
    )
    runner = CliRunner()

    jira_result = runner.invoke(
        app,
        ["jira", "sync", str(finding), "--project-key", "QP", "--project", str(tmp_path)],
    )
    first = runner.invoke(app, ["snapshot", "create", "one", "--project", str(tmp_path)])
    second = runner.invoke(app, ["snapshot", "create", "two", "--project", str(tmp_path)])
    diff = runner.invoke(app, ["diff", "one", "two", "--project", str(tmp_path)])

    assert jira_result.exit_code == 0, jira_result.output
    assert json.loads(jira_result.output)["dry_run"] is True
    assert first.exit_code == second.exit_code == diff.exit_code == 0
