from __future__ import annotations

import ast
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from qualityproof.cli import app
from qualityproof.execution import execute_tests
from qualityproof.generation import emit_pytest, generate_approved
from qualityproof.models import (
    ActionEdge,
    NavigateStep,
    PageState,
    Provenance,
    ProvenanceKind,
    Requirement,
    ScenarioReview,
    ScenarioSpec,
    ScenarioStatus,
    TitleAssertion,
    Verdict,
    VisibleAssertion,
)
from qualityproof.repository import SQLiteRepository
from qualityproof.scenarios import (
    DeterministicProposer,
    HTTPProposer,
    custom_tree_digest,
    load_requirements,
    mine_scenarios,
    read_scenario,
    review_scenario,
    stable_hash,
    validate_model_proposals,
    write_scenario,
)
from qualityproof.security import reject_custom_path


def _scenario(status: ScenarioStatus = ScenarioStatus.DRAFT) -> ScenarioSpec:
    return ScenarioSpec(
        id="login",
        title="Login",
        status=status,
        requirement_ids=("REQ-1",),
        steps=(
            {"type": "navigate", "url": "https://example.test/login"},
            {"type": "fill", "selector": "#email", "value": "person@example.test"},
            {"type": "click", "selector": "button"},
        ),
        assertions=({"type": "visible", "selector": "h1"},),
        provenance=(
            Provenance(
                kind=ProvenanceKind.REQUIREMENT,
                source="requirements.yaml",
                locator="requirement:REQ-1",
                captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
    )


def test_graph_mining_is_deterministic_and_tracks_provenance() -> None:
    pages = (
        PageState(id="b", url="https://example.test/b", title="B"),
        PageState(id="a", url="https://example.test/a", title="A"),
    )
    edges = (
        ActionEdge(
            id="edge",
            source_state_id="a",
            target_state_id="b",
            action={"type": "navigate", "url": "https://example.test/b"},
        ),
    )

    first = mine_scenarios(pages, edges)
    second = mine_scenarios(tuple(reversed(pages)), tuple(reversed(edges)))

    assert first == second
    assert [step.url for step in first[0].steps if isinstance(step, NavigateStep)] == [
        "https://example.test/a",
        "https://example.test/b",
    ]
    assert first[0].provenance[0].source == "persisted-page-action-graph"


def test_yaml_requirements_and_scenario_round_trip(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.yaml"
    requirements.write_text(
        yaml.safe_dump(
            [{"id": "REQ-1", "title": "Login", "description": "A user can log in."}]
        ),
        encoding="utf-8",
    )
    assert load_requirements(requirements)[0].id == "REQ-1"

    path = write_scenario(tmp_path, _scenario())

    assert "schema_version: '1.0'" in path.read_text(encoding="utf-8")
    assert read_scenario(path) == _scenario()


def test_unapproved_ai_assertions_are_not_executable() -> None:
    with pytest.raises(ValidationError, match="human approval"):
        ScenarioSpec(
            id="ai",
            title="AI",
            steps=({"type": "navigate", "url": "https://example.test"},),
            assertions=({"type": "title", "expected": "Example"},),
            provenance=(
                Provenance(kind=ProvenanceKind.AI_HYPOTHESIS, source="model"),
            ),
        )


def test_review_promotes_hypotheses_and_appends_reason(tmp_path: Path) -> None:
    """Approval turns an AI hypothesis into an executable assertion.

    The scenario must be grounded in persisted discovery first. An earlier version
    of this test approved an ungrounded AI scenario successfully, which only
    worked because the gate keyed on the `proposer` string and this scenario left
    it at the default — the bypass is covered by the companion test below.
    """
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    repository.initialize()
    page = PageState(id="a", url="https://example.test", title="Example")
    repository.put("page_state", page.id, page)
    candidate = mine_scenarios((page,), ())[0]
    scenario = candidate.model_copy(
        update={
            "assertions": (),
            "hypothesis_assertions": (TitleAssertion(expected="Example"),),
            "provenance": (Provenance(kind=ProvenanceKind.AI_HYPOTHESIS, source="model"),),
        }
    )
    draft = write_scenario(tmp_path, scenario)

    review_scenario(tmp_path, repository, draft, "approve", "alice", "Matches requirement")

    approved = read_scenario(
        tmp_path / "scenarios" / "generated" / "approved" / f"{scenario.id}.yaml"
    )
    assert approved.status is ScenarioStatus.APPROVED
    assert len(approved.assertions) == 1
    assert approved.hypothesis_assertions == ()
    assert approved.provenance[0].approved_by == "alice"
    assert repository.list("scenario_review", ScenarioReview)[0].actor == "alice"
    assert repository.list_events()[0].details["reason"] == "Matches requirement"


def test_an_ungrounded_ai_scenario_cannot_be_approved(tmp_path: Path) -> None:
    """Relabelling a model proposal must not buy it approval.

    The scenario below claims `proposer: deterministic` while carrying
    AI_HYPOTHESIS provenance and a hypothesis assertion. Because the gate used to
    trust that self-declared string, a hand-edited YAML file could be approved
    with no discovery grounding at all.
    """
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    repository.initialize()
    scenario = ScenarioSpec(
        id="ungrounded",
        title="Invented",
        proposer="deterministic",
        steps=({"type": "navigate", "url": "https://example.test/invented"},),
        hypothesis_assertions=({"type": "title", "expected": "Invented"},),
        provenance=(Provenance(kind=ProvenanceKind.AI_HYPOTHESIS, source="model"),),
    )
    draft = write_scenario(tmp_path, scenario)

    with pytest.raises(ValueError, match="unknown discovery candidate"):
        review_scenario(tmp_path, repository, draft, "approve", "alice", "Looks fine to me")


def test_model_review_preserves_persisted_requirement_associations(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    repository.initialize()
    requirement = Requirement(id="REQ-1", title="Login", description="A user can log in.")
    pages = (
        PageState(id="a", url="https://example.test/login", title="Login"),
        PageState(id="b", url="https://example.test/dashboard", title="Dashboard"),
    )
    edge = ActionEdge(
        id="edge",
        source_state_id="a",
        target_state_id="b",
        action={"type": "navigate", "url": "https://example.test/dashboard"},
    )
    repository.put("requirement", requirement.id, requirement)
    for page in pages:
        repository.put("page_state", page.id, page)
    repository.put("action_edge", edge.id, edge)
    candidate = mine_scenarios(pages, (edge,), (requirement,))[0]
    # A model proposal must demote its assertions to hypotheses, exactly as
    # HTTPProposer does: an AI-authored expectation is not executable until a
    # human has approved it.
    proposal = candidate.model_copy(
        update={
            "proposer": "http",
            "prompt_hash": "prompt",
            "template_hash": "template",
            "assertions": (),
            "hypothesis_assertions": candidate.assertions,
            "provenance": (
                Provenance(kind=ProvenanceKind.AI_HYPOTHESIS, source="model"),
            ),
        }
    )
    draft = write_scenario(tmp_path, proposal)

    review_scenario(tmp_path, repository, draft, "approve", "alice", "Requirement verified")

    approved = read_scenario(
        tmp_path / "scenarios" / "generated" / "approved" / f"{proposal.id}.yaml"
    )
    assert approved.requirement_ids == ("REQ-1",)
    # Approval promotes the hypothesis into an executable assertion.
    assert approved.hypothesis_assertions == ()
    assert approved.assertions == candidate.assertions


def test_mined_assertions_reject_ambiguous_role_locators() -> None:
    """Only role/name pairs unique on the page become assertions.

    Playwright resolves locators strictly, so an assertion on a repeated
    "Add to cart" button would raise instead of passing. Ambiguity is excluded at
    mining time rather than papered over with .first at run time.
    """
    duplicated = json.dumps(
        {
            "action": "click",
            "name": "Add to cart",
            "role": "button",
            "selector": "#a",
            "locator": {"strategy": "role", "role": "button", "name": "Add to cart"},
        },
        sort_keys=True,
    )
    other = json.dumps(
        {
            "action": "click",
            "name": "Add to cart",
            "role": "button",
            "selector": "#b",
            "locator": {"strategy": "role", "role": "button", "name": "Add to cart"},
        },
        sort_keys=True,
    )
    unique = json.dumps(
        {
            "action": "click",
            "name": "Checkout",
            "role": "link",
            "selector": "#c",
            "locator": {"strategy": "role", "role": "link", "name": "Checkout"},
        },
        sort_keys=True,
    )
    page = PageState(
        id="p",
        url="https://example.test/products",
        title="Products",
        controls=(duplicated, other, unique),
    )

    scenario = mine_scenarios((page,), ())[0]

    visible = [item for item in scenario.assertions if isinstance(item, VisibleAssertion)]
    assert [item.locator.name for item in visible] == ["Checkout"]


def test_emitter_is_stable_parseable_and_approved_only(tmp_path: Path) -> None:
    approved = _scenario(ScenarioStatus.APPROVED)

    source = emit_pytest(approved, Path("scenarios/generated/approved/login.yaml"))

    ast.parse(source)
    assert source == emit_pytest(approved, Path("scenarios/generated/approved/login.yaml"))
    assert "Generated by QualityProof" in source
    with pytest.raises(ValueError, match="not approved"):
        emit_pytest(_scenario(), Path("draft.yaml"))

    navigation_only = approved.model_copy(update={"assertions": ()})
    navigation_source = emit_pytest(navigation_only, Path("navigation.yaml"))
    assert "from playwright.sync_api import Page\n" in navigation_source
    assert "expect" not in navigation_source


def test_generation_never_writes_custom_tree(tmp_path: Path) -> None:
    custom = tmp_path / "scenarios" / "custom"
    custom.mkdir(parents=True)
    custom_file = custom / "test_owned.py"
    custom_file.write_text("def test_owned(): assert True\n", encoding="utf-8")
    approved_path = write_scenario(tmp_path, _scenario(ScenarioStatus.APPROVED))
    before = custom_tree_digest(tmp_path)

    outputs = generate_approved(tmp_path, validate=False)

    assert outputs[0].is_relative_to(tmp_path / ".qualityproof" / "generated")
    assert custom_tree_digest(tmp_path) == before
    assert custom_file.read_text(encoding="utf-8") == "def test_owned(): assert True\n"

    approved_path.unlink()
    assert generate_approved(tmp_path, validate=False) == ()
    assert not outputs[0].exists()
    assert custom_file.read_text(encoding="utf-8") == "def test_owned(): assert True\n"


def test_generate_command_reconciles_deleted_approved_yaml_and_database(tmp_path: Path) -> None:
    approved = write_scenario(tmp_path, _scenario(ScenarioStatus.APPROVED))
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["generate", "--project", str(tmp_path), "--no-validate"],
    )
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    assert first.exit_code == 0, first.output
    assert [item.id for item in repository.list("scenario", ScenarioSpec)] == ["login"]

    approved.unlink()
    second = runner.invoke(
        app,
        ["generate", "--project", str(tmp_path), "--no-validate"],
    )

    assert second.exit_code == 0, second.output
    assert repository.list("scenario", ScenarioSpec) == ()
    assert not (tmp_path / ".qualityproof" / "generated" / "test_login.py").exists()


def test_review_refuses_custom_scenario(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    repository.initialize()
    custom = tmp_path / "scenarios" / "custom"
    custom.mkdir(parents=True)
    custom_scenario = custom / "owned.yaml"
    custom_scenario.write_text(
        yaml.safe_dump(_scenario().model_dump(mode="json")),
        encoding="utf-8",
    )
    before = custom_scenario.read_bytes()

    with pytest.raises(ValueError, match="generated scenario drafts"):
        review_scenario(
            tmp_path,
            repository,
            custom_scenario,
            "approve",
            "alice",
            "Should be refused",
        )

    assert custom_scenario.read_bytes() == before


def test_scenario_policy_rejects_cross_origin_and_unsafe_actions(tmp_path: Path) -> None:
    cross_origin = ScenarioSpec.model_validate(
        {
            **_scenario().model_dump(mode="json"),
            "steps": (
                {"type": "navigate", "url": "https://example.test/login"},
                {"type": "navigate", "url": "https://evil.test/steal"},
            ),
        }
    )
    unsafe = ScenarioSpec.model_validate(
        {
            **_scenario().model_dump(mode="json"),
            "steps": (
                {"type": "navigate", "url": "https://example.test/login"},
                {"type": "click", "selector": "button[data-action=delete]"},
            ),
        }
    )

    with pytest.raises(ValueError, match="bound origin"):
        write_scenario(tmp_path, cross_origin)
    with pytest.raises(ValueError, match="unsafe click"):
        write_scenario(tmp_path, unsafe)


def test_custom_tree_rejects_cassette_and_generated_output_paths(tmp_path: Path) -> None:
    custom = tmp_path / "scenarios" / "custom"
    custom.mkdir(parents=True)

    with pytest.raises(ValueError, match="scenarios/custom"):
        reject_custom_path(tmp_path, custom / "cassette.json", "provider cassette")


def test_http_provider_records_and_replays_validated_cassette(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cassette = tmp_path / "provider.json"
    content = json.dumps([_scenario().model_dump(mode="json")])

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr("qualityproof.scenarios.httpx.post", lambda *args, **kwargs: Response())
    candidate = _scenario()
    recorded = HTTPProposer(
        "http://localhost/v1/chat/completions", "demo", cassette=cassette
    ).propose((candidate,), ())
    replayed = HTTPProposer(
        "http://localhost/v1/chat/completions",
        "demo",
        cassette=cassette,
        replay=True,
    ).propose((candidate,), ())

    assert recorded == replayed
    assert recorded[0].assertions == ()
    assert len(recorded[0].hypothesis_assertions) == 1
    assert recorded[0].prompt_hash is not None
    assert len(recorded[0].prompt_hash) == len(stable_hash(content))


def test_model_proposals_are_bound_to_persisted_origin_and_semantic_controls() -> None:
    control = json.dumps(
        {
            "action": "click",
            "name": "Continue",
            "role": "button",
            "selector": "#continue",
        },
        sort_keys=True,
    )
    destructive = json.dumps(
        {
            "action": "click",
            "name": "Delete account",
            "role": "button",
            "selector": "#delete",
        },
        sort_keys=True,
    )
    page = PageState(
        id="home",
        url="https://example.test/home",
        controls=(control, destructive),
    )
    candidate = ScenarioSpec(
        id="candidate",
        title="Candidate",
        steps=({"type": "navigate", "url": page.url},),
    )
    valid = ScenarioSpec.model_validate(
        {
            **candidate.model_dump(mode="json"),
            "steps": (
                {"type": "navigate", "url": page.url},
                {"type": "click", "selector": "#continue"},
            ),
        }
    )
    validate_model_proposals((valid,), (candidate,), (page,))

    invalid_steps = (
        ({"type": "navigate", "url": "https://evil.test/home"},),
        (
            {"type": "navigate", "url": page.url},
            {"type": "click", "selector": "button"},
        ),
        (
            {"type": "navigate", "url": page.url},
            {"type": "click", "selector": "#delete"},
        ),
    )
    for steps in invalid_steps:
        proposal = ScenarioSpec.model_validate(
            {**candidate.model_dump(mode="json"), "steps": steps}
        )
        with pytest.raises(ValueError):
            validate_model_proposals((proposal,), (candidate,), (page,))


def test_cli_plan_uses_persisted_graph_and_keeps_custom_read_only(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    repository.initialize()
    repository.put(
        "page_state",
        "home",
        PageState(id="home", url="https://example.test", title="Home"),
    )
    custom = tmp_path / "scenarios" / "custom"
    custom.mkdir(parents=True)
    owned = custom / "owned.yaml"
    owned.write_text("owner: human\n", encoding="utf-8")

    result = CliRunner().invoke(
        app, ["plan", "--project", str(tmp_path), "--provider", "deterministic"]
    )

    assert result.exit_code == 0, result.output
    assert "Wrote 1 generated drafts" in result.output
    assert owned.read_text(encoding="utf-8") == "owner: human\n"
    assert isinstance(DeterministicProposer().propose((), ()), tuple)


def test_execution_persists_run_and_current_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "scenarios" / "custom"
    custom.mkdir(parents=True)
    test_path = custom / "test_owned.py"
    test_path.write_text("def test_owned():\n    assert True\n", encoding="utf-8")
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    repository.initialize()

    monkeypatch.setattr(
        "qualityproof.execution.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "1 passed", ""),
    )
    result = execute_tests(tmp_path, repository=repository)

    assert repository.get("test_run", result.run_id, type(result)) == result
    verdicts = repository.list("verdict", Verdict)
    assert [verdict.status.value for verdict in verdicts] == ["pass"]
    assert verdicts[0].assertion_id == "scenarios/custom/test_owned.py"
