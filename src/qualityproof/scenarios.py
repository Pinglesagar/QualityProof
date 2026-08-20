"""Scenario planning, YAML persistence, review, and proposer boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml
from pydantic import TypeAdapter, ValidationError

from qualityproof.models import (
    ActionEdge,
    ApiAssertion,
    AriaSnapshotAssertion,
    AuditEvent,
    ClickStep,
    Locator,
    LocatorStrategy,
    NavigateStep,
    PageState,
    PressStep,
    Provenance,
    ProvenanceKind,
    Requirement,
    ScenarioReview,
    ScenarioSpec,
    ScenarioStatus,
    TextAssertion,
    TitleAssertion,
    UrlAssertion,
    VisibleAssertion,
)
from qualityproof.repository import SQLiteRepository
from qualityproof.security import (
    matches_unsafe_term,
    reject_custom_path,
    validate_http_origin,
)

SCENARIO_SCHEMA_VERSION = "1.0"
PLANNER_TEMPLATE = "qualityproof-scenario-proposer-v1"
#: Reserved namespace for requirements projected from a seeded-defect manifest.
SEED_PREFIX = "SEED-"
_SCENARIOS = TypeAdapter(list[ScenarioSpec])
_DETERMINISTIC_TIME = datetime(1970, 1, 1, tzinfo=UTC)
#: Labels whose activation could change or destroy application state.
#: Deliberately broad and deliberately not exhaustive; see security.matches_unsafe_term.
_UNSAFE_ACTION_TERMS = (
    # Destruction and removal
    "delete",
    "destroy",
    "remove",
    "erase",
    "discard",
    "purge",
    "wipe",
    "empty",
    # Money movement. Ambiguous nouns such as "order" and "checkout" are
    # deliberately absent: they appear constantly in read-only navigation
    # ("Order history", "PayPal checkout options"), and the step that actually
    # commits money is caught by pay/purchase/buy/confirm/submit.
    "pay",
    "purchase",
    "buy",
    "subscribe",
    "donate",
    "transfer",
    "withdraw",
    "refund",
    "place order",
    "submit order",
    "submit-order",
    "confirm order",
    # Communication that leaves the system
    "send",
    "invite",
    "publish",
    "notify",
    "broadcast",
    # Account and session lifecycle
    "logout",
    "log out",
    "sign out",
    "deactivate",
    "disable",
    "suspend",
    "cancel",
    "revoke",
    "reset",
    "close account",
    "unsubscribe",
    # State-changing submission
    "submit",
    "confirm",
    "overwrite",
    "deploy",
    "restart",
    "reboot",
    "terminate",
)


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def custom_tree_digest(project: Path) -> str:
    """Fingerprint custom scenarios/tests so commands can enforce immutability."""
    root = project / "scenarios" / "custom"
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def assert_custom_unchanged(project: Path, before: str) -> None:
    if custom_tree_digest(project) != before:
        raise RuntimeError("scenarios/custom is immutable to QualityProof commands")


def validate_scenario_policy(scenario: ScenarioSpec) -> None:
    """Bind generated behavior to one HTTP(S) origin and safe action surface."""
    navigations = [step for step in scenario.steps if isinstance(step, NavigateStep)]
    if not navigations:
        raise ValueError(f"scenario {scenario.id} must begin with an HTTP(S) navigation")
    if scenario.steps[0] is not navigations[0]:
        raise ValueError(f"scenario {scenario.id} must bind its origin before other actions")
    origin = validate_http_origin(navigations[0].url)
    for navigation in navigations:
        validate_http_origin(navigation.url, origin)
        route = navigation.url.casefold()
        if any(f"/{term}" in route for term in _UNSAFE_ACTION_TERMS):
            raise ValueError(f"scenario {scenario.id} contains unsafe navigation")
    for action in scenario.steps:
        if isinstance(action, ClickStep) and is_destructive_semantic(action.locator.describes):
            raise ValueError(f"scenario {scenario.id} contains an unsafe click target")
        if isinstance(action, PressStep) and action.key.casefold() in {
            "enter",
            "numpadenter",
        }:
            raise ValueError(f"scenario {scenario.id} contains an unsafe submit key")


def _semantic_controls(pages: Sequence[PageState]) -> dict[str, tuple[str, str]]:
    """Index discovered controls by every identity a locator could present.

    A proposal may reference a control by role+name, test id or raw CSS. All of
    them resolve to the same discovered control, so each identity is registered
    against the same (action, accessible name) pair. Controls without a usable
    locator are skipped here — they are accessibility findings, not automation
    targets — but they are never dropped from the page state itself.
    """
    controls: dict[str, tuple[str, str]] = {}
    for page in pages:
        for raw in page.controls:
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            action = item.get("action")
            name = item.get("name")
            if not isinstance(action, str) or not action:
                continue
            if not isinstance(name, str) or not name:
                continue
            payload = item.get("locator")
            identities: list[str] = []
            if isinstance(payload, dict):
                try:
                    identities.extend(Locator.model_validate(payload).keys)
                except ValidationError:
                    identities = []
            selector = item.get("selector")
            if isinstance(selector, str) and selector:
                identities.extend((selector, f"css:{selector}"))
            for identity in identities:
                controls[identity] = (action, name)
    return controls


def validate_model_proposals(
    proposals: Sequence[ScenarioSpec],
    candidates: Sequence[ScenarioSpec],
    pages: Sequence[PageState],
) -> None:
    """Reject model output that is not grounded in the persisted discovery graph."""
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    page_by_url = {page.url: page for page in pages}
    if len({proposal.id for proposal in proposals}) != len(proposals):
        raise ValueError("model proposals must have unique persisted candidate identifiers")
    for proposal in proposals:
        candidate = candidate_by_id.get(proposal.id)
        if candidate is None:
            raise ValueError("model proposal substituted an unknown discovery candidate")
        candidate_navigations = tuple(
            step.url for step in candidate.steps if isinstance(step, NavigateStep)
        )
        proposal_navigations = tuple(
            step.url for step in proposal.steps if isinstance(step, NavigateStep)
        )
        if not candidate_navigations or proposal_navigations != candidate_navigations:
            raise ValueError("model proposal substituted the persisted candidate route or origin")
        if proposal.requirement_ids != candidate.requirement_ids:
            raise ValueError("model proposal substituted persisted requirement associations")
        candidate_pages = tuple(
            page_by_url[url] for url in candidate_navigations if url in page_by_url
        )
        if len(candidate_pages) != len(candidate_navigations):
            raise ValueError("persisted discovery candidate pages are unavailable")
        controls = _semantic_controls(candidate_pages)
        for step in proposal.steps:
            if isinstance(step, NavigateStep):
                continue
            semantic = _resolve_control(controls, step.locator)
            if semantic is None:
                raise ValueError("model proposal used a generic or undiscovered selector")
            action, name = semantic
            if isinstance(step, ClickStep):
                expected_action = "click"
            elif hasattr(step, "value"):
                expected_action = "fill"
            else:
                raise ValueError("model proposal used an undiscovered control action")
            if action != expected_action:
                raise ValueError("model proposal changed the discovered control semantics")
            if is_destructive_semantic(name):
                raise ValueError("model proposal selected a destructive semantic action")
        for assertion in (*proposal.assertions, *proposal.hypothesis_assertions):
            if isinstance(assertion, (VisibleAssertion, TextAssertion, AriaSnapshotAssertion)):
                semantic = _resolve_control(controls, assertion.locator)
                if semantic is None:
                    raise ValueError("model proposal used a generic or undiscovered selector")
                if is_destructive_semantic(semantic[1]):
                    raise ValueError("model proposal selected a destructive semantic control")
            elif isinstance(assertion, ApiAssertion):
                raise ValueError("model proposals may not introduce API assertions")
            elif isinstance(assertion, UrlAssertion):
                if assertion.expected not in candidate_navigations:
                    raise ValueError("model proposal asserted an undiscovered URL")
            elif isinstance(assertion, TitleAssertion):
                if assertion.expected not in {
                    page.title for page in candidate_pages if page.title is not None
                }:
                    raise ValueError("model proposal asserted an undiscovered title")


def _resolve_control(
    controls: dict[str, tuple[str, str]], locator: Locator
) -> tuple[str, str] | None:
    """Match a proposed locator against a discovered control, or refuse it."""
    for identity in locator.keys:
        semantic = controls.get(identity)
        if semantic is not None:
            return semantic
    return None


def is_destructive_semantic(name: str) -> bool:
    """True when a control's accessible name suggests activating it is unsafe."""
    return matches_unsafe_term(name, _UNSAFE_ACTION_TERMS)


def requires_discovery_validation(scenario: ScenarioSpec) -> bool:
    """Decide whether a scenario must be checked against persisted discovery.

    Keying only on ``proposer`` trusted a self-declared string inside the very
    artifact being validated: relabelling a model proposal as "deterministic"
    skipped every check. Any AI_HYPOTHESIS provenance, or any hypothesis
    assertion, now also demands validation, so the gate cannot be opted out of
    by editing the file it guards.
    """
    if scenario.proposer != DeterministicProposer.name:
        return True
    if scenario.hypothesis_assertions:
        return True
    return any(item.kind is ProvenanceKind.AI_HYPOTHESIS for item in scenario.provenance)


def load_requirements(path: Path | None) -> tuple[Requirement, ...]:
    """Load requirements from YAML/JSON collections or Markdown headings.

    A ``seeds`` collection is accepted alongside ``requirements`` because a seeded
    defect manifest is a specification of expected behaviour: it states what the
    application should do and what changing it looks like. Registering seeds means
    a test citing a seed identifier can be checked against a registry rather than
    against a file it chose for itself.
    """
    if path is None:
        return ()
    text = path.read_text(encoding="utf-8")
    if path.suffix.casefold() in {".yaml", ".yml", ".json"}:
        raw = yaml.safe_load(text) or []
        if isinstance(raw, Mapping):
            collection = raw.get("requirements")
            if collection is None:
                collection = [
                    _requirement_from_seed(item, path)
                    for item in raw.get("seeds", [])
                    if isinstance(item, Mapping)
                ]
            raw = collection
        if not isinstance(raw, list):
            raise ValueError("requirements YAML must be a list or a requirements list")
        yaml_requirements: list[Requirement] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("each requirement must be an object")
            payload = dict(item)
            identifier = str(payload.get("id", ""))
            description = str(payload.get("description", ""))
            expected_hash = stable_hash(description)
            if not payload.get("provenance"):
                payload["provenance"] = (
                    {
                        "kind": ProvenanceKind.REQUIREMENT,
                        "source": str(path.resolve()),
                        "locator": f"requirement:{identifier}",
                        "content_hash": expected_hash,
                        "captured_at": _DETERMINISTIC_TIME,
                    },
                )
            requirement = Requirement.model_validate(payload)
            for provenance in requirement.provenance:
                if (
                    provenance.kind is ProvenanceKind.REQUIREMENT
                    and provenance.content_hash is not None
                    and not provenance.validates_content(requirement.description)
                ):
                    raise ValueError(f"requirement source hash mismatch: {requirement.id}")
            yaml_requirements.append(requirement)
        return tuple(sorted(yaml_requirements, key=lambda item: item.id))
    requirements: list[Requirement] = []
    matches = list(re.finditer(r"(?m)^#{1,6}\s+([A-Za-z0-9_.-]+)\s*[:—-]\s*(.+)$", text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip() or match.group(2).strip()
        requirements.append(
            Requirement(
                id=match.group(1),
                title=match.group(2).strip(),
                description=body,
                provenance=(
                    Provenance(
                        kind=ProvenanceKind.REQUIREMENT,
                        source=str(path),
                        locator=f"heading:{match.group(1)}",
                        captured_at=_DETERMINISTIC_TIME,
                        content_hash=stable_hash(body),
                    ),
                ),
            )
        )
    if not requirements and text.strip():
        requirements.append(
            Requirement(
                id="REQ-1",
                title=path.stem.replace("_", " ").replace("-", " ").title(),
                description=text.strip(),
                provenance=(
                    Provenance(
                        kind=ProvenanceKind.REQUIREMENT,
                        source=str(path),
                        captured_at=_DETERMINISTIC_TIME,
                        content_hash=stable_hash(text),
                    ),
                ),
            )
        )
    return tuple(requirements)


def _requirement_from_seed(seed: Mapping[str, object], source: Path) -> dict[str, object]:
    """Project a seeded-defect record onto the requirement shape.

    The seed's own fields describe the behaviour under test, so they become the
    requirement's description verbatim rather than being paraphrased. The
    description must match what a provenance locator resolves to, or a recorded
    content hash would never validate.
    """
    raw_identifier = str(seed.get("id", ""))
    # Namespaced so a seed manifest can never take over a specification id. An
    # un-namespaced projection let a four-line file replace a registered
    # requirement, after which a test citing the attacker's own seed file audited
    # as verified against the real requirement's identifier.
    identifier = raw_identifier if raw_identifier.startswith(SEED_PREFIX) else (
        f"{SEED_PREFIX}{raw_identifier}"
    )
    rendered = json.dumps(dict(seed), sort_keys=True, separators=(",", ":"))
    title = str(seed.get("category") or seed.get("expected_signal") or identifier)
    return {
        "id": identifier,
        "title": title,
        "description": rendered,
        "provenance": (
            {
                "kind": ProvenanceKind.REQUIREMENT,
                # The real manifest path, not a label. A source that cannot be
                # resolved makes the registry unable to say where a requirement is
                # specified, which is precisely what binds a test's citation to it.
                "source": str(source.resolve()),
                "locator": f"seed:{raw_identifier}",
                "captured_at": _DETERMINISTIC_TIME,
                "content_hash": stable_hash(rendered),
            },
        ),
    }


def _candidate_paths(
    pages: Sequence[PageState], edges: Sequence[ActionEdge]
) -> tuple[tuple[PageState, ...], ...]:
    page_by_id = {page.id: page for page in pages}
    outgoing: dict[str, list[ActionEdge]] = defaultdict(list)
    targets = {edge.target_state_id for edge in edges}
    for edge in sorted(edges, key=lambda item: item.id):
        outgoing[edge.source_state_id].append(edge)
    roots = sorted((page for page in pages if page.id not in targets), key=lambda item: item.id)
    if not roots:
        roots = sorted(pages, key=lambda item: item.id)[:1]
    paths: list[tuple[PageState, ...]] = []

    def walk(state: PageState, path: tuple[PageState, ...]) -> None:
        next_edges = [
            edge
            for edge in outgoing.get(state.id, [])
            if edge.target_state_id in page_by_id
            and edge.target_state_id not in {item.id for item in path}
        ]
        if not next_edges:
            paths.append((*path, state))
            return
        for edge in next_edges:
            walk(page_by_id[edge.target_state_id], (*path, state))

    for root in roots:
        walk(root, ())
    return tuple(sorted(set(paths), key=lambda path: tuple(page.id for page in path)))


class ScenarioProposer(Protocol):
    name: str

    def propose(
        self,
        candidates: tuple[ScenarioSpec, ...],
        requirements: tuple[Requirement, ...],
    ) -> tuple[ScenarioSpec, ...]: ...


class DeterministicProposer:
    name = "deterministic"

    def propose(
        self,
        candidates: tuple[ScenarioSpec, ...],
        requirements: tuple[Requirement, ...],
    ) -> tuple[ScenarioSpec, ...]:
        del requirements
        return candidates


class HTTPProposer:
    """Bounded Ollama/OpenAI-compatible proposer with optional cassette replay."""

    name = "http"

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        cassette: Path | None = None,
        replay: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.cassette = cassette
        self.replay = replay

    def propose(
        self,
        candidates: tuple[ScenarioSpec, ...],
        requirements: tuple[Requirement, ...],
    ) -> tuple[ScenarioSpec, ...]:
        user_content = json.dumps(
            {
                "candidates": [item.model_dump(mode="json") for item in candidates],
                "requirements": [item.model_dump(mode="json") for item in requirements],
            },
            sort_keys=True,
        )
        request = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{PLANNER_TEMPLATE}. Return JSON only matching the supplied scenario "
                        "objects. Do not return reasoning."
                    ),
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
            "temperature": 0,
        }
        request_hash = stable_hash(json.dumps(request, sort_keys=True))
        if self.replay:
            if self.cassette is None or not self.cassette.is_file():
                raise ValueError("replay requires an existing cassette")
            cassette = json.loads(self.cassette.read_text(encoding="utf-8"))
            if cassette.get("request_hash") != request_hash:
                raise ValueError("cassette request hash does not match")
            content = cassette["response"]
        else:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            response = httpx.post(
                self.endpoint,
                json=request,
                headers=headers,
                timeout=httpx.Timeout(self.timeout_seconds),
            )
            response.raise_for_status()
            payload: Any = response.json()
            content = (
                payload.get("choices", [{}])[0].get("message", {}).get("content")
                or payload.get("message", {}).get("content")
                or payload.get("response")
            )
            if not isinstance(content, str):
                raise ValueError("provider response did not contain text content")
            if self.cassette is not None:
                self.cassette.parent.mkdir(parents=True, exist_ok=True)
                self.cassette.write_text(
                    json.dumps(
                        {"version": 1, "request_hash": request_hash, "response": content},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        raw = json.loads(content)
        if isinstance(raw, Mapping):
            raw = raw.get("scenarios")
        proposed = tuple(_SCENARIOS.validate_python(raw))
        prompt_hash = stable_hash(user_content)
        template_hash = stable_hash(PLANNER_TEMPLATE)
        return tuple(
            item.model_copy(
                update={
                    "status": ScenarioStatus.DRAFT,
                    "assertions": (),
                    "hypothesis_assertions": item.assertions,
                    "proposer": self.name,
                    "prompt_hash": prompt_hash,
                    "template_hash": template_hash,
                    "provenance": (
                        *item.provenance,
                        Provenance(
                            kind=ProvenanceKind.AI_HYPOTHESIS,
                            source=self.model,
                            captured_at=_DETERMINISTIC_TIME,
                            content_hash=request_hash,
                        ),
                    ),
                }
            )
            for item in proposed
        )


MAX_MINED_ASSERTIONS = 3


def _unique_role_locators(page: PageState) -> tuple[Locator, ...]:
    """Role locators that resolve to exactly one element on this page.

    Playwright runs locators in strict mode: asserting on a locator that matches
    three "Add to cart" buttons raises rather than silently picking one. Mining
    only the role/name pairs that are unique on the page keeps generated
    assertions strict-mode-safe by construction instead of hiding ambiguity
    behind ``.first``.
    """
    counts: dict[tuple[str, str], int] = {}
    ordered: list[Locator] = []
    for raw in page.controls:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        payload = item.get("locator")
        if not isinstance(payload, dict):
            continue
        try:
            locator = Locator.model_validate(payload)
        except ValidationError:
            continue
        if locator.strategy is not LocatorStrategy.ROLE or not locator.role or not locator.name:
            continue
        if is_destructive_semantic(locator.describes):
            continue
        key = (locator.role, locator.name)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] == 1:
            ordered.append(locator)
    unique = [
        locator
        for locator in ordered
        if counts[(str(locator.role), str(locator.name))] == 1
    ]
    unique.sort(key=lambda item: item.semantic_key)
    return tuple(unique[:MAX_MINED_ASSERTIONS])


def mine_scenarios(
    pages: Sequence[PageState],
    edges: Sequence[ActionEdge],
    requirements: Sequence[Requirement] = (),
    role: str | None = None,
) -> tuple[ScenarioSpec, ...]:
    """Mine deterministic root-to-leaf journeys, with read-only assertions.

    Only observations are mined: navigation, title and visibility. Activating a
    control is never inferred, because clicking an unreviewed button can mutate
    the application under test; interactions stay human-authored.

    ``role`` restricts mining to states observed as one identity. Multi-role
    crawling exists to make privilege boundaries measurable, but a *generated
    suite* runs as a single identity, and an assertion mined as an administrator
    would fail for a shopper. Detection uses every role; generation uses one.
    """
    if role is not None:
        selected = {page.id for page in pages if (page.role or "default") == role}
        pages = tuple(page for page in pages if page.id in selected)
        edges = tuple(
            edge
            for edge in edges
            if edge.source_state_id in selected and edge.target_state_id in selected
        )
    requirement_ids = tuple(sorted(item.id for item in requirements))
    scenarios: list[ScenarioSpec] = []
    for path in _candidate_paths(pages, edges):
        if not path:
            continue
        signature = "|".join(page.id for page in path)
        scenario_id = f"journey-{stable_hash(signature)[:12]}"
        destination = path[-1]
        assertions: list[TitleAssertion | VisibleAssertion] = []
        if destination.title:
            assertions.append(TitleAssertion(expected=destination.title, soft=True))
        assertions.extend(
            VisibleAssertion(locator=locator, soft=True)
            for locator in _unique_role_locators(destination)
        )
        scenarios.append(
            ScenarioSpec(
                id=scenario_id,
                title=" → ".join((page.title or page.route) for page in path),
                requirement_ids=requirement_ids,
                steps=tuple(NavigateStep(url=page.url) for page in path),
                assertions=tuple(assertions),
                provenance=(
                    Provenance(
                        kind=ProvenanceKind.OBSERVATION,
                        source="persisted-page-action-graph",
                        locator=signature,
                        captured_at=_DETERMINISTIC_TIME,
                        content_hash=stable_hash(signature),
                    ),
                ),
            )
        )
    return tuple(sorted(scenarios, key=lambda item: item.id))


def scenario_path(project: Path, scenario: ScenarioSpec) -> Path:
    folder = "approved" if scenario.status is ScenarioStatus.APPROVED else "drafts"
    return project / "scenarios" / "generated" / folder / f"{scenario.id}.yaml"


def write_scenario(project: Path, scenario: ScenarioSpec) -> Path:
    validate_scenario_policy(scenario)
    path = scenario_path(project, scenario)
    reject_custom_path(project, path, "generated scenario output")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = scenario.model_dump(mode="json", exclude_none=True)
    path.write_text(
        "# QualityProof scenario; schema 1.0\n"
        + yaml.safe_dump(payload, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def read_scenario(path: Path) -> ScenarioSpec:
    return ScenarioSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def list_drafts(project: Path) -> tuple[Path, ...]:
    return tuple(sorted((project / "scenarios" / "generated" / "drafts").glob("*.yaml")))


def review_scenario(
    project: Path,
    repository: SQLiteRepository,
    path: Path,
    decision: str,
    actor: str,
    reason: str,
    edited: ScenarioSpec | None = None,
) -> ScenarioReview:
    drafts_root = (project / "scenarios" / "generated" / "drafts").resolve()
    if not path.resolve().is_relative_to(drafts_root):
        raise ValueError("review may only modify generated scenario drafts")
    original = read_scenario(path)
    validate_scenario_policy(original)
    review = ScenarioReview(
        scenario_id=original.id,
        decision=decision,
        actor=actor,
        reason=reason,
    )
    if decision in {"approve", "edit"}:
        selected = edited or original
        validate_scenario_policy(selected)
        if selected.id != original.id:
            raise ValueError("editing a scenario cannot change its id")
        if (
            selected.proposer != original.proposer
            or selected.prompt_hash != original.prompt_hash
            or selected.template_hash != original.template_hash
        ):
            raise ValueError("editing cannot remove or replace proposer provenance")
        if requires_discovery_validation(selected) or requires_discovery_validation(original):
            pages = repository.list("page_state", PageState)
            validate_model_proposals(
                (selected,),
                mine_scenarios(
                    pages,
                    repository.list("action_edge", ActionEdge),
                    repository.list("requirement", Requirement),
                ),
                pages,
            )
        approved_provenance = tuple(
            item.model_copy(update={"approved_by": actor, "approved_at": review.reviewed_at})
            if item.kind is ProvenanceKind.AI_HYPOTHESIS and not item.is_approved
            else item
            for item in selected.provenance
        )
        approved = selected.model_copy(
            update={
                "status": ScenarioStatus.APPROVED,
                "provenance": approved_provenance,
                "assertions": (*selected.assertions, *selected.hypothesis_assertions),
                "hypothesis_assertions": (),
            }
        )
        write_scenario(project, approved)
        repository.put("scenario", approved.id, approved)
    elif decision == "reject":
        repository.delete("scenario", original.id)
    path.unlink()
    repository.put(
        "scenario_review",
        f"{review.scenario_id}:{review.reviewed_at.isoformat()}",
        review,
    )
    repository.append_event(
        AuditEvent(
            id=f"review-{stable_hash(f'{original.id}:{review.reviewed_at.isoformat()}')[:24]}",
            event_type={
                "approve": "scenario_approved",
                "edit": "scenario_edited",
                "reject": "scenario_rejected",
            }[decision],
            actor=actor,
            occurred_at=review.reviewed_at,
            details={"scenario_id": original.id, "reason": reason},
        )
    )
    return review


def plan_from_repository(
    project: Path,
    repository: SQLiteRepository,
    requirements_path: Path | None,
    proposer: ScenarioProposer,
    role: str | None = None,
) -> tuple[Path, ...]:
    requirements = load_requirements(requirements_path)
    repository.replace_manifested_set(
        "planning-requirements",
        "requirement",
        ((requirement.id, requirement) for requirement in requirements),
    )
    candidates = mine_scenarios(
        repository.list("page_state", PageState),
        repository.list("action_edge", ActionEdge),
        requirements,
        role=role,
    )
    proposals = proposer.propose(candidates, requirements)
    if proposer.name != DeterministicProposer.name or any(
        requires_discovery_validation(item) for item in proposals
    ):
        validate_model_proposals(
            proposals,
            candidates,
            repository.list("page_state", PageState),
        )
    paths = tuple(write_scenario(project, item) for item in sorted(proposals, key=lambda x: x.id))
    repository.append_event(
        AuditEvent(
            id=f"plan-{stable_hash(datetime.now(UTC).isoformat())[:24]}",
            event_type="scenario_plan_created",
            details={"drafts": len(paths), "proposer": proposer.name},
        )
    )
    return paths
