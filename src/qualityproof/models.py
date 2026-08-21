"""Typed domain objects used by QualityProof."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, ClassVar, Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator, model_validator


def _as_text(value: object) -> str | None:
    """Coerce browser-sourced JSON values to a non-empty stripped string."""
    if not isinstance(value, str):
        return None
    stripped = " ".join(value.split())
    return stripped or None


class DomainModel(BaseModel):
    """Shared validation policy for persisted domain objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProvenanceKind(StrEnum):
    REQUIREMENT = "REQUIREMENT"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    API_SPEC = "API_SPEC"
    BASELINE = "BASELINE"
    OBSERVATION = "OBSERVATION"
    AI_HYPOTHESIS = "AI_HYPOTHESIS"


class Provenance(DomainModel):
    kind: ProvenanceKind
    source: str = Field(min_length=1)
    locator: str | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    content_hash: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Provenance:
        if (self.approved_by is None) != (self.approved_at is None):
            raise ValueError("approved_by and approved_at must be provided together")
        if self.kind is ProvenanceKind.HUMAN_APPROVED and not self.is_approved:
            raise ValueError("HUMAN_APPROVED requires approved_by and approved_at")
        if self.kind in {ProvenanceKind.REQUIREMENT, ProvenanceKind.API_SPEC} and not (
            self.locator or self.content_hash
        ):
            raise ValueError("REQUIREMENT and API_SPEC require a locator or content_hash")
        if self.content_hash is not None and re.fullmatch(
            r"(?:sha256:)?[0-9a-fA-F]{64}", self.content_hash
        ) is None:
            raise ValueError("content_hash must be a SHA-256 digest")
        if self.expires_at is not None and self.expires_at <= self.captured_at:
            raise ValueError("expires_at must be later than captured_at")
        return self

    def is_expired(self, at: datetime | None = None) -> bool:
        instant = at or datetime.now(UTC)
        return self.expires_at is not None and self.expires_at <= instant

    @property
    def is_approved(self) -> bool:
        return self.approved_by is not None

    def is_authoritative(self, at: datetime | None = None) -> bool:
        if self.is_expired(at):
            return False
        if self.kind in {
            ProvenanceKind.REQUIREMENT,
            ProvenanceKind.HUMAN_APPROVED,
            ProvenanceKind.API_SPEC,
        }:
            return True
        return self.kind is ProvenanceKind.AI_HYPOTHESIS and self.is_approved

    def validates_content(self, content: str | bytes) -> bool:
        """Validate supplied source bytes against the recorded SHA-256 digest."""
        if self.content_hash is None:
            return False
        payload = content.encode("utf-8") if isinstance(content, str) else content
        expected = self.content_hash.removeprefix("sha256:").casefold()
        return hashlib.sha256(payload).hexdigest() == expected


class RequirementPriority(StrEnum):
    """Risk banding, so coverage can be judged by consequence, not by count.

    "80% of requirements verified" says nothing if the unverified fifth is the
    payment path. A release gate wants to know whether every P1 is proven.
    """

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Requirement(DomainModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: Section of the specification this requirement came from, for grouping.
    area: str | None = None
    priority: RequirementPriority | None = None
    acceptance_criteria: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = ()


class AssertionKind(StrEnum):
    TEXT = "text"
    VISIBLE = "visible"
    HIDDEN = "hidden"
    URL = "url"
    ATTRIBUTE = "attribute"
    ACCESSIBLE = "accessible"


class Assertion(DomainModel):
    id: str = Field(min_length=1)
    kind: AssertionKind
    target: str = Field(min_length=1)
    expected: str | bool | int | float
    provenance: tuple[Provenance, ...] = ()


class Scenario(DomainModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    steps: tuple[str, ...] = Field(min_length=1)
    assertions: tuple[Assertion, ...] = ()
    provenance: tuple[Provenance, ...] = ()


class EvidenceKind(StrEnum):
    SCREENSHOT = "screenshot"
    TRACE = "trace"
    LOG = "log"
    RESPONSE = "response"
    MANUAL_NOTE = "manual_note"


class Evidence(DomainModel):
    id: str = Field(min_length=1)
    kind: EvidenceKind
    uri: AnyUrl | None = None
    summary: str = Field(min_length=1)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provenance: tuple[Provenance, ...] = ()


class VerdictStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    #: Observed both passing and failing within one run. Recorded distinctly
    #: because collapsing a rerun pass into PASS is how instability becomes
    #: invisible; Playwright reports this natively as ``flaky`` and pytest does
    #: not, so both runners land on the same vocabulary here.
    FLAKY = "flaky"
    INCONCLUSIVE = "inconclusive"
    NOT_RUN = "not_run"


class Verdict(DomainModel):
    assertion_id: str = Field(min_length=1)
    status: VerdictStatus
    rationale: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()


class LedgerStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class SourceAssertion(DomainModel):
    kind: Literal["assert", "expect"]
    line: int = Field(ge=1)
    expression: str = Field(min_length=1)


class TestMetadata(DomainModel):
    __test__: ClassVar[bool] = False
    requirement_ids: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = ()


class AuditedTest(DomainModel):
    id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    line: int = Field(ge=1)
    #: Which runner produced the test. Externally analysed runners are named
    #: explicitly so a ledger row always says where its evidence came from.
    framework: Literal[
        "pytest",
        "playwright",
        "python",
        "playwright-typescript",
        "playwright-python",
    ]
    assertions: tuple[SourceAssertion, ...] = ()
    metadata: TestMetadata | None = None


class LedgerEntry(DomainModel):
    id: str = Field(min_length=1)
    status: LedgerStatus
    reason: str = Field(min_length=1)
    test: AuditedTest
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuditEvent(DomainModel):
    id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: str = Field(default="qualityproof", min_length=1)
    details: dict[str, str | int | bool | None] = Field(default_factory=dict)


class UnknownItem(DomainModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    blocks: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    resolved: bool = False
    resolution: str | None = None

    @model_validator(mode="after")
    def resolution_matches_state(self) -> UnknownItem:
        if self.resolved != (self.resolution is not None):
            raise ValueError("resolved must be true exactly when resolution is provided")
        return self


class PageFacet(StrEnum):
    """Independently observed aspects of one page state.

    Attributing a change to a facet is what stops a page-state difference from
    being credited to the wrong cause: a heading rewrite, a permission change and
    a responsive-layout break are all "the page changed", but only one of them
    explains any given defect.
    """

    TITLE = "title"
    HEADINGS = "headings"
    FORMS = "forms"
    CONTROLS = "controls"
    STATUS = "status"
    ACCESSIBILITY = "accessibility"
    LAYOUT = "layout"


class PageState(DomainModel):
    id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    route: str = Field(default="/", min_length=1)
    title: str | None = None
    fingerprint: str | None = None
    headings: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    forms: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()
    status: int | None = Field(default=None, ge=100, le=599)
    accessibility: tuple[str, ...] = ()
    layout: tuple[str, ...] = ()
    role: str | None = None
    depth: int = Field(default=0, ge=0)
    evidence_ids: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = ()

    def facet_values(self) -> dict[str, str]:
        """Canonical serialization of every facet, ready for hashing."""
        return {
            PageFacet.TITLE.value: json.dumps(self.title, sort_keys=True),
            PageFacet.HEADINGS.value: json.dumps(self.headings, sort_keys=True),
            PageFacet.FORMS.value: json.dumps(self.forms, sort_keys=True),
            PageFacet.CONTROLS.value: json.dumps(self.controls, sort_keys=True),
            PageFacet.STATUS.value: json.dumps(self.status, sort_keys=True),
            PageFacet.ACCESSIBILITY.value: json.dumps(self.accessibility, sort_keys=True),
            PageFacet.LAYOUT.value: json.dumps(self.layout, sort_keys=True),
        }

    def facet_digests(self) -> dict[str, str]:
        return {
            name: hashlib.sha256(value.encode()).hexdigest()
            for name, value in self.facet_values().items()
        }


class ClickAction(DomainModel):
    type: Literal["click"] = "click"
    selector: str = Field(min_length=1)


class FillAction(DomainModel):
    type: Literal["fill"] = "fill"
    selector: str = Field(min_length=1)
    value_hint: str = Field(min_length=1)


class NavigateAction(DomainModel):
    type: Literal["navigate"] = "navigate"
    url: str = Field(min_length=1)


Action = Annotated[ClickAction | FillAction | NavigateAction, Field(discriminator="type")]


class ActionEdge(DomainModel):
    id: str = Field(min_length=1)
    source_state_id: str = Field(min_length=1)
    target_state_id: str = Field(min_length=1)
    action: Action
    provenance: tuple[Provenance, ...] = ()


class DiscoveryResult(DomainModel):
    """Deterministically ordered records produced by one bounded crawl."""

    pages: tuple[PageState, ...] = ()
    edges: tuple[ActionEdge, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    unknowns: tuple[UnknownItem, ...] = ()
    stop_reason: str = Field(min_length=1)


class LocatorStrategy(StrEnum):
    """Element-reference strategies, ordered from most to least user-facing."""

    ROLE = "role"
    TEST_ID = "test_id"
    LABEL = "label"
    PLACEHOLDER = "placeholder"
    TEXT = "text"
    CSS = "css"


#: Preference order applied when a discovered control supports several strategies.
#: Role first because it is the only strategy that survives a structural rewrite
#: while still asserting what a user actually perceives.
LOCATOR_PREFERENCE: tuple[LocatorStrategy, ...] = (
    LocatorStrategy.ROLE,
    LocatorStrategy.TEST_ID,
    LocatorStrategy.LABEL,
    LocatorStrategy.PLACEHOLDER,
    LocatorStrategy.TEXT,
    LocatorStrategy.CSS,
)


class Locator(DomainModel):
    """How to reach one element, preferring user-facing semantics over structure.

    ``css`` is retained even when a semantic strategy wins, because it is the
    contract the element was discovered under. Keeping both is what lets a later
    crawl distinguish "this control was restyled" from "this control is gone":
    the semantic locator still resolves while the CSS contract no longer matches.

    A bare string is accepted anywhere a locator is expected and is read as a CSS
    selector, so scenario YAML written against the pre-1.1 ``selector`` field
    keeps loading unchanged.
    """

    strategy: LocatorStrategy = LocatorStrategy.CSS
    role: str | None = None
    name: str | None = None
    value: str | None = None
    css: str | None = None
    exact: bool = True

    @model_validator(mode="before")
    @classmethod
    def accept_plain_selector(cls, data: object) -> object:
        if isinstance(data, str):
            return {"strategy": LocatorStrategy.CSS, "css": data}
        return data

    @model_validator(mode="after")
    def validate_strategy(self) -> Locator:
        if self.strategy is LocatorStrategy.ROLE:
            if not self.role or not self.name:
                raise ValueError("role locators require both a role and an accessible name")
        elif self.strategy is LocatorStrategy.CSS:
            if not self.css:
                raise ValueError("css locators require a css selector")
        elif not self.value:
            raise ValueError(f"{self.strategy.value} locators require a value")
        return self

    @classmethod
    def from_control(cls, control: dict[str, object]) -> Locator:
        """Choose the most durable strategy a discovered control supports."""
        role = _as_text(control.get("role"))
        name = _as_text(control.get("name"))
        test_id = _as_text(control.get("testId"))
        label = _as_text(control.get("label"))
        placeholder = _as_text(control.get("placeholder"))
        text = _as_text(control.get("text"))
        css = _as_text(control.get("selector"))
        for strategy in LOCATOR_PREFERENCE:
            if strategy is LocatorStrategy.ROLE and role and name:
                return cls(strategy=strategy, role=role, name=name, css=css)
            if strategy is LocatorStrategy.TEST_ID and test_id:
                return cls(strategy=strategy, value=test_id, role=role, name=name, css=css)
            if strategy is LocatorStrategy.LABEL and label:
                return cls(strategy=strategy, value=label, role=role, name=name, css=css)
            if strategy is LocatorStrategy.PLACEHOLDER and placeholder:
                return cls(strategy=strategy, value=placeholder, role=role, name=name, css=css)
            if strategy is LocatorStrategy.TEXT and text:
                return cls(strategy=strategy, value=text, role=role, name=name, css=css)
            if strategy is LocatorStrategy.CSS and css:
                return cls(strategy=strategy, role=role, name=name, css=css)
        raise ValueError("control exposes no usable locator strategy")

    @property
    def semantic_key(self) -> str:
        """Strategy-scoped identity, stable across CSS churn."""
        if self.strategy is LocatorStrategy.ROLE:
            return f"role:{self.role}:{self.name}"
        if self.strategy is LocatorStrategy.CSS:
            return f"css:{self.css}"
        return f"{self.strategy.value}:{self.value}"

    @property
    def keys(self) -> tuple[str, ...]:
        """Every identity this locator can be matched against during validation."""
        candidates = [self.semantic_key]
        if self.css:
            candidates.append(f"css:{self.css}")
            candidates.append(self.css)
        return tuple(dict.fromkeys(candidates))

    @property
    def describes(self) -> str:
        """Everything human-readable about the target, for safety screening."""
        parts = (self.role, self.name, self.value, self.css)
        return " ".join(part for part in parts if part)


class _LocatorHolder(DomainModel):
    """Mixin accepting either a structured ``locator`` or a legacy ``selector``."""

    locator: Locator

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_selector(cls, data: object) -> object:
        if isinstance(data, dict) and "selector" in data and "locator" not in data:
            payload = dict(data)
            payload["locator"] = payload.pop("selector")
            return payload
        return data


class _SoftCapable(DomainModel):
    """Mixin for assertions that may be evaluated softly.

    A soft assertion records its failure and lets the test continue, so one run
    reports every broken expectation instead of only the first. The verdict is
    still a failure; nothing is downgraded.
    """

    soft: bool = False


class NavigateStep(DomainModel):
    type: Literal["navigate"] = "navigate"
    url: str = Field(min_length=1)


class ClickStep(_LocatorHolder):
    type: Literal["click"] = "click"


class FillStep(_LocatorHolder):
    type: Literal["fill"] = "fill"
    value: str


class PressStep(_LocatorHolder):
    type: Literal["press"] = "press"
    key: str = Field(min_length=1)


ScenarioStep = Annotated[
    NavigateStep | ClickStep | FillStep | PressStep, Field(discriminator="type")
]


class VisibleAssertion(_LocatorHolder, _SoftCapable):
    type: Literal["visible"] = "visible"


class TextAssertion(_LocatorHolder, _SoftCapable):
    type: Literal["text"] = "text"
    expected: str


class AriaSnapshotAssertion(_LocatorHolder, _SoftCapable):
    """Assert a subtree's accessibility tree, in Playwright's ARIA snapshot syntax.

    Unlike a screenshot this is plain text, so it can be diffed, reviewed and
    redacted — which is why it is allowed where pixel comparison is not.
    """

    type: Literal["aria_snapshot"] = "aria_snapshot"
    expected: str = Field(min_length=1)


class ApiAssertion(_SoftCapable):
    """Assert an HTTP contract through Playwright's APIRequestContext.

    Deliberately read-only and origin-relative: a path is resolved against the
    scenario's bound base URL, so an API assertion cannot reach a different host
    and cannot mutate state.
    """

    type: Literal["api"] = "api"
    path: str = Field(min_length=1)
    method: Literal["GET", "HEAD"] = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    json_subset: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_path(self) -> ApiAssertion:
        if not self.path.startswith("/"):
            raise ValueError("api assertion path must be origin-relative and start with /")
        if "//" in self.path or ".." in self.path:
            raise ValueError("api assertion path must not escape the bound origin")
        return self


class UrlAssertion(_SoftCapable):
    type: Literal["url"] = "url"
    expected: str = Field(min_length=1)


class TitleAssertion(_SoftCapable):
    type: Literal["title"] = "title"
    expected: str = Field(min_length=1)


ScenarioAssertion = Annotated[
    VisibleAssertion
    | TextAssertion
    | AriaSnapshotAssertion
    | ApiAssertion
    | UrlAssertion
    | TitleAssertion,
    Field(discriminator="type"),
]


class ScenarioStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class ScenarioSpec(DomainModel):
    """Language-neutral v1 scenario exchanged as YAML."""

    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    status: ScenarioStatus = ScenarioStatus.DRAFT
    requirement_ids: tuple[str, ...] = ()
    steps: tuple[ScenarioStep, ...] = Field(min_length=1)
    assertions: tuple[ScenarioAssertion, ...] = ()
    hypothesis_assertions: tuple[ScenarioAssertion, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    proposer: str = Field(default="deterministic", min_length=1)
    prompt_hash: str | None = None
    template_hash: str | None = None

    @model_validator(mode="after")
    def prevent_unapproved_ai_assertions(self) -> ScenarioSpec:
        if self.assertions and any(
            item.kind is ProvenanceKind.AI_HYPOTHESIS and not item.is_approved
            for item in self.provenance
        ):
            raise ValueError("AI hypotheses require human approval before executable assertions")
        return self


class ScenarioReview(DomainModel):
    scenario_id: str = Field(min_length=1)
    decision: Literal["approve", "edit", "reject"]
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TestRunResult(DomainModel):
    run_id: str = Field(min_length=1)
    status: Literal["passed", "failed", "error"]
    exit_code: int
    test_paths: tuple[str, ...]
    result_path: str
    evidence_paths: tuple[str, ...] = ()
    started_at: datetime
    finished_at: datetime


class MaterializationManifest(DomainModel):
    """Current IDs owned by one replaceable workflow materialization."""

    scope: str = Field(min_length=1)
    record_kind: str = Field(min_length=1)
    record_ids: tuple[str, ...] = ()
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JiraFinding(DomainModel):
    """Redacted, stable finding input suitable for issue synchronization."""

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = ()
    scenario_id: str | None = None
    route: str | None = None
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    evidence: dict[str, object] = Field(default_factory=dict)


class IssueTracker(StrEnum):
    """Which issue tracker a mapping belongs to.

    Separate from ``adapter`` because the two answer different questions: the
    adapter is the transport, the tracker is the system of record. Without this,
    a mock run against Jira and a mock run against Azure Boards would produce the
    same mapping identity for the same finding and collide.
    """

    JIRA = "jira"
    AZURE_BOARDS = "azure_boards"


class JiraIssueMapping(DomainModel):
    """Recorded identity of a synchronized finding.

    Named for Jira because Jira came first; it now covers any tracker. The stored
    rows are keyed by tracker, so widening this did not invalidate them.
    """

    fingerprint: str = Field(min_length=64, max_length=64)
    issue_key: str = Field(min_length=1)
    adapter: Literal["mock", "cloud", "azure"]
    account: str = Field(min_length=1)
    project_key: str = Field(min_length=1)
    #: Defaulted so a mapping written before trackers existed still loads as Jira,
    #: which is what it was.
    tracker: IssueTracker = IssueTracker.JIRA
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JiraIssueResult(DomainModel):
    fingerprint: str = Field(min_length=64, max_length=64)
    action: Literal["create", "update", "unchanged"]
    issue_key: str | None = None
    dry_run: bool = True
    #: Jira takes a field object; Azure Boards takes a JSON Patch array. The
    #: payload shape belongs to the tracker, so this holds either rather than
    #: forcing one tracker's spelling onto the other.
    request: dict[str, object] | list[dict[str, object]]


class LocatorSemantics(DomainModel):
    precondition: str = Field(min_length=1)
    user_intent: str = Field(min_length=1)
    postcondition: str = Field(min_length=1)


class FailedLocatorEvidence(DomainModel):
    test_path: str = Field(min_length=1)
    line: int = Field(ge=1)
    old_locator: str = Field(min_length=1)
    role: str | None = None
    name: str | None = None
    test_id: str | None = None
    context: tuple[str, ...] = ()
    semantics: LocatorSemantics
    assertion: str = Field(min_length=1)
    evidence: dict[str, object] = Field(default_factory=dict)

    @field_validator("test_path")
    @classmethod
    def validate_test_path(cls, value: str) -> str:
        if (
            not value
            or PurePosixPath(value).is_absolute()
            or ".." in PurePosixPath(value).parts
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("test_path must be a safe relative POSIX path")
        return value

    @field_validator("old_locator", "assertion")
    @classmethod
    def validate_single_line_context(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("locator and assertion context must be single-line text")
        return value


class SemanticCandidate(DomainModel):
    locator: str = Field(min_length=1)
    role: str | None = None
    name: str | None = None
    test_id: str | None = None
    context: tuple[str, ...] = ()
    semantics: LocatorSemantics

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("candidate locator must be a single-line expression")
        return value


class LocatorHealingProposal(DomainModel):
    id: str = Field(min_length=1)
    failed: FailedLocatorEvidence
    candidate: SemanticCandidate
    confidence: float = Field(ge=0, le=1)
    score_evidence: dict[str, float]
    locator_diff: str = Field(min_length=1)
    status: Literal["proposed", "approved", "rejected"] = "proposed"


class HealingReview(DomainModel):
    proposal_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    patch_path: str | None = None
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def approved_review_has_patch(self) -> HealingReview:
        if self.decision == "approve" and self.patch_path is None:
            raise ValueError("approved healing review must reference a patch artifact")
        if self.decision == "reject" and self.patch_path is not None:
            raise ValueError("rejected healing review cannot reference a patch artifact")
        return self


class CoverageCounts(DomainModel):
    requirements: int = Field(default=0, ge=0)
    routes: int = Field(default=0, ge=0)
    pages: int = Field(default=0, ge=0)
    scenarios: int = Field(default=0, ge=0)
    verdicts: int = Field(default=0, ge=0)
    unknowns: int = Field(default=0, ge=0)


class EvidenceSnapshot(DomainModel):
    """Immutable, named normalized release/application evidence.

    Schema 1.1 adds ``page_links``, the per-page outbound route map that lets
    release comparison distinguish a link *retarget* from an unrelated pair of
    route additions and removals. Snapshots written by schema 1.0 remain
    readable; they simply carry no link map.
    """

    schema_version: Literal["1.0", "1.1"] = "1.1"
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    requirements: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    page_fingerprints: dict[str, str | None] = Field(default_factory=dict)
    page_links: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    page_facets: dict[str, dict[str, str]] = Field(default_factory=dict)
    page_roles: dict[str, str] = Field(default_factory=dict)
    scenarios: tuple[str, ...] = ()
    verdicts: dict[str, str] = Field(default_factory=dict)
    unknowns: tuple[str, ...] = ()
    coverage: CoverageCounts = Field(default_factory=CoverageCounts)
    application: dict[str, object] = Field(default_factory=dict)


class SnapshotSectionDiff(DomainModel):
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()


class RouteRetarget(DomainModel):
    """One referrer page whose outbound link moved from one route to another.

    Reported alongside the raw route diff, never instead of it: the diff stays
    lossless and consumers decide whether to treat the pair as one finding.
    """

    referrer: str = Field(min_length=1)
    removed_route: str = Field(min_length=1)
    added_route: str = Field(min_length=1)


class EvidenceSnapshotDiff(DomainModel):
    before: str
    after: str
    requirements: SnapshotSectionDiff
    routes: SnapshotSectionDiff
    page_fingerprints: SnapshotSectionDiff
    scenarios: SnapshotSectionDiff
    verdicts: SnapshotSectionDiff
    unknowns: SnapshotSectionDiff
    application_metadata: SnapshotSectionDiff
    coverage_delta: dict[str, int]
    route_retargets: tuple[RouteRetarget, ...] = ()
    #: Route -> the facets whose evidence differs, so a page-state change can be
    #: attributed to a cause instead of being credited to whatever seed shares
    #: its route.
    page_facet_changes: dict[str, tuple[str, ...]] = Field(default_factory=dict)

class ExternalFramework(StrEnum):
    """Runners that can contribute evidence to the ledger."""

    PLAYWRIGHT_TYPESCRIPT = "playwright-typescript"
    PLAYWRIGHT_PYTHON = "playwright-python"
    PYTEST = "pytest"


class ExternalTestRecord(DomainModel):
    """One test observed by an external runner.

    Deliberately the same shape the Python auditor produces, so an externally
    analysed test flows through ``build_ledger`` and the provenance resolver
    unchanged. The evidence engine has one implementation, not one per language.
    """

    id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    line: int = Field(default=1, ge=1)
    framework: ExternalFramework = ExternalFramework.PLAYWRIGHT_TYPESCRIPT
    status: VerdictStatus = VerdictStatus.NOT_RUN
    duration_ms: int = Field(default=0, ge=0)
    requirement_ids: tuple[str, ...] = ()
    assertions: tuple[SourceAssertion, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    attachments: tuple[str, ...] = ()
    quarantined_attachments: tuple[str, ...] = ()

    def to_audited_test(self) -> AuditedTest:
        """Project this record onto the auditor's own vocabulary."""
        metadata = (
            TestMetadata(requirement_ids=self.requirement_ids, provenance=self.provenance)
            if self.requirement_ids or self.provenance
            else None
        )
        return AuditedTest(
            id=self.id,
            path=self.path,
            name=self.name,
            line=self.line,
            framework=self.framework.value,
            assertions=self.assertions,
            metadata=metadata,
        )


class ExternalRunManifest(DomainModel):
    """Language-neutral evidence produced by a non-Python runner.

    This is the whole interop contract: a foreign runner reports what it ran,
    what it asserted and what provenance each test declared, and the Python
    engine remains the only thing that decides what any of it proves.
    """

    schema_version: Literal["qualityproof-external-run/v1"] = "qualityproof-external-run/v1"
    run_id: str = Field(min_length=1)
    framework: ExternalFramework = ExternalFramework.PLAYWRIGHT_TYPESCRIPT
    started_at: datetime
    finished_at: datetime
    #: Present when the producing run was one shard of a fan-out.
    shard: str | None = None
    #: Mirrors ArtifactPolicy.describe() from the producing runner.
    artifact_policy: str | None = None
    redacted: bool = True
    tests: tuple[ExternalTestRecord, ...] = ()

    @model_validator(mode="after")
    def validate_window(self) -> ExternalRunManifest:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if len({test.id for test in self.tests}) != len(self.tests):
            raise ValueError("external test ids must be unique within a manifest")
        return self

    def verdicts(self) -> tuple[Verdict, ...]:
        return tuple(
            Verdict(
                assertion_id=test.id,
                status=test.status,
                rationale=(
                    f"{self.framework.value} reported {test.status.value} in run {self.run_id}"
                ),
                evidence_ids=test.attachments,
            )
            for test in self.tests
        )
