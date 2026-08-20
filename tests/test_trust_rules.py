"""Boundary tests for the functions that decide what evidence proves.

Written in response to a mutation run: `_resolve_locator`, `_find_identifier`,
`classify` and `validate_http_origin` carried the most surviving logic mutants,
meaning their decision boundaries could be changed without any test objecting.
These are the highest-stakes functions in the project — a wrong answer here is a
false claim about verification — so each branch and each boundary is pinned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from qualityproof.audit import (
    ProvenanceResolver,
    _find_identifier,
    _find_operation,
    _merge_metadata,
    _resolve_locator,
    build_ledger,
    classify,
)
from qualityproof.models import (
    AuditedTest,
    AuditEvent,
    LedgerStatus,
    Provenance,
    ProvenanceKind,
    Requirement,
    SourceAssertion,
    TestMetadata,
)
from qualityproof.security import validate_http_origin

ASSERTION = SourceAssertion(kind="expect", line=3, expression="expect(page).to_have_title('x')")


def _test(
    metadata: TestMetadata | None,
    *,
    assertions: tuple[SourceAssertion, ...] = (ASSERTION,),
) -> AuditedTest:
    return AuditedTest(
        id="t.py::test_x",
        path="t.py",
        name="test_x",
        line=1,
        framework="playwright",
        assertions=assertions,
        metadata=metadata,
    )


# --------------------------------------------------------------------------
# classify: the ledger decision itself
# --------------------------------------------------------------------------


def test_metadata_present_but_entirely_empty_is_unknown_not_partial() -> None:
    """Empty metadata must be indistinguishable from no metadata.

    A test that carries a decorator with no requirements and no provenance has
    declared nothing, so treating it as PARTIAL would credit an empty gesture.
    """
    entry = classify(_test(TestMetadata()))

    assert entry.status is LedgerStatus.UNKNOWN


def test_requirement_link_without_any_assertion_is_partial() -> None:
    """Claiming coverage without asserting anything must never reach VERIFIED."""
    metadata = TestMetadata(requirement_ids=("REQ-1",))

    entry = classify(_test(metadata, assertions=()))

    assert entry.status is LedgerStatus.PARTIAL
    assert "no source assertion" in entry.reason


def test_provenance_without_a_requirement_link_is_partial() -> None:
    """Authoritative provenance alone does not establish which requirement is met."""
    metadata = TestMetadata(
        provenance=(
            Provenance(
                kind=ProvenanceKind.HUMAN_APPROVED,
                source="review",
                approved_by="alice",
                approved_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
    )

    entry = classify(_test(metadata))

    assert entry.status is LedgerStatus.PARTIAL
    assert "no requirement identifier" in entry.reason


def test_requirement_and_assertion_without_a_resolver_trusts_declared_provenance() -> None:
    """With no resolver the rule is deliberately weaker, and must stay that way.

    `audit` can run standalone with no project context. In that mode an
    authoritative *kind* is accepted, which is why the resolver path exists and
    why the demo workflow always supplies one.
    """
    metadata = TestMetadata(
        requirement_ids=("REQ-1",),
        provenance=(
            Provenance(
                kind=ProvenanceKind.REQUIREMENT,
                source="docs/requirements.md",
                locator="requirement:REQ-1",
            ),
        ),
    )

    assert classify(_test(metadata)).status is LedgerStatus.VERIFIED


def test_an_unresolvable_source_downgrades_verified_to_partial(tmp_path: Path) -> None:
    metadata = TestMetadata(
        requirement_ids=("REQ-1",),
        provenance=(
            Provenance(
                kind=ProvenanceKind.REQUIREMENT,
                source="does-not-exist.yaml",
                locator="requirement:REQ-1",
            ),
        ),
    )

    entry = classify(_test(metadata), resolver=ProvenanceResolver(project=tmp_path))

    assert entry.status is LedgerStatus.PARTIAL


@pytest.mark.parametrize(
    "kind",
    [ProvenanceKind.BASELINE, ProvenanceKind.OBSERVATION],
    ids=["baseline", "observation"],
)
def test_baselines_and_observations_can_never_reach_verified(kind: ProvenanceKind) -> None:
    metadata = TestMetadata(
        requirement_ids=("REQ-1",),
        provenance=(Provenance(kind=kind, source="crawl"),),
    )

    assert classify(_test(metadata)).status is LedgerStatus.PARTIAL


def test_an_expired_authoritative_source_is_partial() -> None:
    """Provenance is a claim with a shelf life; an expired one proves nothing now."""
    captured = datetime.now(UTC) - timedelta(days=10)
    metadata = TestMetadata(
        requirement_ids=("REQ-1",),
        provenance=(
            Provenance(
                kind=ProvenanceKind.REQUIREMENT,
                source="docs/requirements.md",
                locator="requirement:REQ-1",
                captured_at=captured,
                expires_at=captured + timedelta(days=1),
            ),
        ),
    )

    assert classify(_test(metadata)).status is LedgerStatus.PARTIAL


def test_one_authoritative_source_among_weak_ones_is_enough() -> None:
    metadata = TestMetadata(
        requirement_ids=("REQ-1",),
        provenance=(
            Provenance(kind=ProvenanceKind.OBSERVATION, source="crawl"),
            Provenance(
                kind=ProvenanceKind.HUMAN_APPROVED,
                source="review",
                approved_by="alice",
                approved_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
    )

    assert classify(_test(metadata)).status is LedgerStatus.VERIFIED


# --------------------------------------------------------------------------
# _resolve_locator: whether a provenance source actually points at anything
# --------------------------------------------------------------------------


def test_requirement_locator_resolves_to_the_description_not_the_whole_record() -> None:
    """The hash must cover the requirement text, not its surrounding metadata.

    Hashing the whole record would make any unrelated field edit invalidate every
    linked test, and would let a description change slip through unnoticed if the
    record were normalised differently.
    """
    content = yaml.safe_dump(
        {"requirements": [{"id": "REQ-1", "title": "Login", "description": "A user logs in."}]}
    )

    resolved, associated = _resolve_locator(content, "requirement:REQ-1", ".yaml")

    assert resolved == b"A user logs in."
    assert associated == "REQ-1"


def test_requirement_without_a_description_falls_back_to_the_whole_record() -> None:
    content = yaml.safe_dump({"requirements": [{"id": "REQ-2", "title": "Only a title"}]})

    resolved, associated = _resolve_locator(content, "requirement:REQ-2", ".yaml")

    assert resolved is not None
    assert b"Only a title" in resolved
    assert associated == "REQ-2"


def test_seed_locator_returns_no_association_confusion_with_requirements() -> None:
    content = json.dumps({"seeds": [{"id": "SEED-1", "category": "layout"}]})

    resolved, associated = _resolve_locator(content, "seed:SEED-1", ".json")

    assert resolved is not None
    assert b"layout" in resolved
    assert associated == "SEED-1"


def test_operation_locator_never_reports_a_requirement_association() -> None:
    """An API operation is not a requirement id; conflating them would let an
    unrelated spec operation satisfy a requirement association check."""
    content = json.dumps({"paths": {"/x": {"get": {"operationId": "getX", "summary": "s"}}}})

    resolved, associated = _resolve_locator(content, "operation:getX", ".json")

    assert resolved is not None
    assert associated is None


def test_a_missing_identifier_resolves_to_nothing() -> None:
    content = yaml.safe_dump({"requirements": [{"id": "REQ-1", "description": "x"}]})

    assert _resolve_locator(content, "requirement:REQ-404", ".yaml") == (None, None)


def test_malformed_source_content_resolves_to_nothing_rather_than_raising() -> None:
    """A corrupt requirements file must degrade to PARTIAL, not crash an audit."""
    assert _resolve_locator("{not: valid: json", "requirement:REQ-1", ".json") == (None, None)
    assert _resolve_locator("a: b:\n  - [", "requirement:REQ-1", ".yaml") == (None, None)


def test_json_suffix_is_parsed_as_json_and_other_suffixes_as_yaml() -> None:
    payload = {"requirements": [{"id": "REQ-1", "description": "shared"}]}
    as_json = json.dumps(payload)

    # JSON is also valid YAML, so a correct implementation resolves both.
    assert _resolve_locator(as_json, "requirement:REQ-1", ".json")[0] == b"shared"
    assert _resolve_locator(as_json, "requirement:REQ-1", ".yaml")[0] == b"shared"


def test_heading_locator_captures_the_body_until_the_next_heading() -> None:
    content = (
        "# REQ-1: Login\nThe user logs in.\nStill the same body.\n"
        "# REQ-2: Other\nElsewhere.\n"
    )

    resolved, associated = _resolve_locator(content, "heading:REQ-1", ".md")

    assert resolved is not None
    assert b"Still the same body." in resolved
    assert b"Elsewhere." not in resolved
    assert associated == "REQ-1"


def test_a_heading_with_no_body_falls_back_to_the_heading_text() -> None:
    content = "# REQ-1: Login\n# REQ-2: Other\n"

    resolved, _ = _resolve_locator(content, "heading:REQ-1", ".md")

    assert resolved == b"REQ-1: Login"


def test_a_bare_hash_prefix_is_accepted_as_a_heading_locator() -> None:
    content = "# REQ-1: Login\nbody\n"

    assert _resolve_locator(content, "#REQ-1", ".md")[1] == "REQ-1"


def test_an_unrecognised_locator_scheme_resolves_to_nothing() -> None:
    assert _resolve_locator("# REQ-1: x\nbody\n", "mystery:REQ-1", ".md") == (None, None)


# --------------------------------------------------------------------------
# _find_identifier / _find_operation: the recursive search
# --------------------------------------------------------------------------


def test_identifier_search_descends_into_nested_containers() -> None:
    payload = {"outer": {"inner": {"requirements": [{"id": "REQ-9", "description": "deep"}]}}}

    assert _find_identifier(payload, "requirements", "REQ-9") == {
        "id": "REQ-9",
        "description": "deep",
    }


def test_identifier_search_ignores_a_collection_of_the_wrong_name() -> None:
    payload = {"seeds": [{"id": "REQ-9"}]}

    assert _find_identifier(payload, "requirements", "REQ-9") is None


def test_identifier_search_skips_non_object_entries_without_raising() -> None:
    payload = {"requirements": ["a string", 7, None, {"id": "REQ-1"}]}

    assert _find_identifier(payload, "requirements", "REQ-1") == {"id": "REQ-1"}


def test_identifier_search_traverses_lists_at_the_top_level() -> None:
    payload = [{"requirements": [{"id": "REQ-1", "description": "in a list"}]}]

    assert _find_identifier(payload, "requirements", "REQ-1") is not None


def test_operation_search_matches_only_an_exact_operation_id() -> None:
    payload = {"paths": {"/a": {"get": {"operationId": "listThings"}}}}

    assert _find_operation(payload, "listThings") is not None
    assert _find_operation(payload, "listThing") is None
    assert _find_operation(payload, "listThingsExtra") is None


# --------------------------------------------------------------------------
# _merge_metadata: class-level metadata inheritance
# --------------------------------------------------------------------------


def test_child_requirements_extend_the_parent_without_duplicating() -> None:
    parent = TestMetadata(requirement_ids=("REQ-1", "REQ-2"))
    child = TestMetadata(requirement_ids=("REQ-2", "REQ-3"))

    merged = _merge_metadata(parent, child)

    assert merged is not None
    # Order preserved, duplicates collapsed: the ledger keys off these ids.
    assert merged.requirement_ids == ("REQ-1", "REQ-2", "REQ-3")


def test_merging_with_a_missing_side_returns_the_other_side_unchanged() -> None:
    only = TestMetadata(requirement_ids=("REQ-1",))

    assert _merge_metadata(None, only) is only
    assert _merge_metadata(only, None) is only
    assert _merge_metadata(None, None) is None


def test_merged_provenance_keeps_both_sides() -> None:
    parent = TestMetadata(provenance=(Provenance(kind=ProvenanceKind.OBSERVATION, source="a"),))
    child = TestMetadata(provenance=(Provenance(kind=ProvenanceKind.OBSERVATION, source="b"),))

    merged = _merge_metadata(parent, child)

    assert merged is not None
    assert [item.source for item in merged.provenance] == ["a", "b"]


# --------------------------------------------------------------------------
# build_ledger: resolver wiring
# --------------------------------------------------------------------------


def test_build_ledger_without_a_project_skips_source_resolution(tmp_path: Path) -> None:
    """Two call sites, two strengths — the difference must stay observable."""
    metadata = TestMetadata(
        requirement_ids=("REQ-1",),
        provenance=(
            Provenance(
                kind=ProvenanceKind.REQUIREMENT,
                source="absent.yaml",
                locator="requirement:REQ-1",
            ),
        ),
    )
    tests = (_test(metadata),)

    assert build_ledger(tests)[0].status is LedgerStatus.VERIFIED
    assert build_ledger(tests, project=tmp_path)[0].status is LedgerStatus.PARTIAL


def test_api_spec_provenance_requires_every_requirement_to_be_known(tmp_path: Path) -> None:
    """An API spec attests to an operation, so the requirements it is offered
    against must all be ones the project actually knows about."""
    spec = tmp_path / "openapi.json"
    spec.write_text(
        json.dumps({"paths": {"/x": {"get": {"operationId": "getX"}}}}), encoding="utf-8"
    )
    provenance = Provenance(
        kind=ProvenanceKind.API_SPEC, source=str(spec), locator="operation:getX"
    )
    known = ProvenanceResolver(
        project=tmp_path,
        requirements=(Requirement(id="REQ-1", title="t", description="d"),),
    )

    assert known.resolve(provenance, ("REQ-1",)) is True
    assert known.resolve(provenance, ("REQ-1", "REQ-UNKNOWN")) is False
    # With no known requirements at all there is nothing to attest against.
    assert ProvenanceResolver(project=tmp_path).resolve(provenance, ("REQ-1",)) is False


def test_an_oversized_source_file_is_refused(tmp_path: Path) -> None:
    """A resolver that will read any file is a denial-of-service surface."""
    big = tmp_path / "huge.yaml"
    big.write_text("x" * (5_000_001), encoding="utf-8")
    provenance = Provenance(
        kind=ProvenanceKind.REQUIREMENT, source=str(big), locator="requirement:REQ-1"
    )

    assert ProvenanceResolver(project=tmp_path).resolve(provenance, ("REQ-1",)) is False


# --------------------------------------------------------------------------
# validate_http_origin: the origin binding behind every generated test
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "/relative/path",
        "example.test/x",
        "http:///nohost",
    ],
)
def test_non_http_or_hostless_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        validate_http_origin(url)


@pytest.mark.parametrize(
    "url",
    ["https://user:pw@example.test/x", "https://user@example.test/x", "https://:pw@example.test/x"],
)
def test_urls_carrying_credentials_are_refused(url: str) -> None:
    """Credentials in a URL end up in logs, evidence and error messages."""
    with pytest.raises(ValueError, match="credentials are forbidden"):
        validate_http_origin(url)


def test_origin_is_normalised_to_lowercase_scheme_and_host() -> None:
    assert validate_http_origin("HTTPS://Example.TEST/Path") == "https://example.test"


def test_an_explicit_port_is_part_of_the_origin_identity() -> None:
    """Same host, different port is a different origin. Treating them as equal
    would let a generated test cross from staging to production."""
    assert validate_http_origin("http://example.test:8080/a") == "http://example.test:8080"

    with pytest.raises(ValueError, match="outside the bound origin"):
        validate_http_origin("http://example.test:8081/a", "http://example.test:8080")


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://other.test/a", "https://example.test/a"),
        ("http://example.test/a", "https://example.test/a"),
        ("https://sub.example.test/a", "https://example.test/a"),
    ],
    ids=["different-host", "different-scheme", "subdomain"],
)
def test_origin_binding_rejects_any_component_mismatch(url: str, origin: str) -> None:
    with pytest.raises(ValueError, match="outside the bound origin"):
        validate_http_origin(url, origin)


def test_origin_binding_accepts_a_different_path_on_the_same_origin() -> None:
    assert validate_http_origin("https://example.test/deep/page?q=1", "https://example.test/")


# --------------------------------------------------------------------------
# _is_expect_call: which calls count as assertions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("expect(page).to_have_title('x')", 1),
        ("expect.soft(page).to_have_title('x')", 1),
        ("expect(loc).not_to_be_visible()", 1),
        ("expect.poll(fn).to_be(1)", 1),
        ("expect(page).to_have_title('x')\nexpect.soft(loc).to_be_visible()", 2),
        # A bare subject is not an assertion; only the terminating matcher is.
        ("handle = expect.soft(page)", 0),
        ("handle = expect(page)", 0),
        ("other.soft(page).to_have_title('x')", 0),
    ],
)
def test_expect_chain_shapes_are_counted_exactly_once(source: str, expected: int) -> None:
    """Soft assertions must count, and subjects must not.

    The generator emits `expect.soft(...)` for mined assertions, and an auditor
    blind to that form reports its own generated tests as having no assertions —
    which silently downgrades them in the ledger.
    """
    from qualityproof.audit import audit_file

    module = Path("/tmp/qp_expect_probe.py")
    module.write_text(f"def test_probe(page):\n    {source.replace(chr(10), chr(10) + '    ')}\n")

    audited = audit_file(module)

    assert len(audited) == 1
    assert len(audited[0].assertions) == expected


# --------------------------------------------------------------------------
# The three gate holes an adversarial review found
# --------------------------------------------------------------------------


def test_a_human_approval_is_not_self_certifying(tmp_path: Path) -> None:
    """An approval record must be corroborated by a persisted review event.

    Previously, a repository holding no review events caused the resolver to fall
    back to trusting the record, so any hand-written HUMAN_APPROVED provenance was
    authoritative on sight — the approval vouched for itself.
    """
    approved = Provenance(
        kind=ProvenanceKind.HUMAN_APPROVED,
        source="review:alice",
        approved_by="alice",
        approved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    consulted = ProvenanceResolver(project=tmp_path, has_repository=True)
    standalone = ProvenanceResolver(project=tmp_path, has_repository=False)

    assert consulted.resolve(approved, ("REQ-1",)) is False
    # With no repository at all, the weaker documented mode still applies.
    assert standalone.resolve(approved, ("REQ-1",)) is True


def test_a_corroborated_human_approval_still_resolves(tmp_path: Path) -> None:
    """The gate must not become unconditional, or review stops meaning anything."""
    approved_at = datetime(2026, 1, 1, tzinfo=UTC)
    approved = Provenance(
        kind=ProvenanceKind.HUMAN_APPROVED,
        source="scenario-42",
        approved_by="alice",
        approved_at=approved_at,
    )
    event = AuditEvent(
        id="review-scenario-42",
        event_type="scenario_approved",
        actor="alice",
        occurred_at=approved_at,
        details={"scenario_id": "scenario-42"},
    )

    resolver = ProvenanceResolver(
        project=tmp_path, review_events=(event,), has_repository=True
    )

    assert resolver.resolve(approved, ("REQ-1",)) is True


def test_a_whole_file_hash_cannot_satisfy_a_fragment_locator(tmp_path: Path) -> None:
    """A locator narrows the claim, so the digest must cover the fragment.

    Accepting a whole-file digest let a hash validate while the locator pointed at
    something else entirely — the hash proved the file existed, not that the cited
    requirement said what the test claimed.
    """
    import hashlib

    source = tmp_path / "requirements.yaml"
    body = yaml.safe_dump(
        {"requirements": [{"id": "REQ-1", "description": "The real requirement."}]}
    )
    source.write_text(body, encoding="utf-8")
    whole_file_digest = hashlib.sha256(body.encode()).hexdigest()
    fragment_digest = hashlib.sha256(b"The real requirement.").hexdigest()
    resolver = ProvenanceResolver(project=tmp_path)

    with_whole_file = Provenance(
        kind=ProvenanceKind.REQUIREMENT,
        source="requirements.yaml",
        locator="requirement:REQ-1",
        content_hash=whole_file_digest,
    )
    with_fragment = with_whole_file.model_copy(update={"content_hash": fragment_digest})

    assert resolver.resolve(with_whole_file, ("REQ-1",)) is False
    assert resolver.resolve(with_fragment, ("REQ-1",)) is True


def test_a_provenance_source_outside_the_project_is_refused(tmp_path: Path) -> None:
    """A working-directory fallback made an audit's result depend on where it ran.

    The same test could pass from one directory and fail from another, which is
    disqualifying for a tool whose output is meant to be evidence.
    """
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(
        yaml.safe_dump({"requirements": [{"id": "REQ-1", "description": "d"}]}),
        encoding="utf-8",
    )
    resolver = ProvenanceResolver(project=project)

    escaping = Provenance(
        kind=ProvenanceKind.REQUIREMENT,
        source="../elsewhere.yaml",
        locator="requirement:REQ-1",
    )

    assert resolver.resolve(escaping, ("REQ-1",)) is False


def test_a_test_cannot_mint_a_requirement_the_registry_does_not_know(
    tmp_path: Path,
) -> None:
    """The registry is the authority on which requirements exist.

    Without this, a test could cite any identifier as long as it also wrote a file
    containing that identifier — inventing coverage of a requirement nobody ever
    specified.
    """
    source = tmp_path / "invented.yaml"
    source.write_text(
        yaml.safe_dump({"requirements": [{"id": "REQ-INVENTED", "description": "mine"}]}),
        encoding="utf-8",
    )
    provenance = Provenance(
        kind=ProvenanceKind.REQUIREMENT,
        source="invented.yaml",
        locator="requirement:REQ-INVENTED",
    )
    registry = (Requirement(id="REQ-1", title="Real", description="A real one."),)

    with_registry = ProvenanceResolver(project=tmp_path, requirements=registry)
    without_registry = ProvenanceResolver(project=tmp_path)

    assert with_registry.resolve(provenance, ("REQ-INVENTED",)) is False
    # With no registry configured the check cannot apply; that is the documented
    # zero-configuration mode, not an endorsement.
    assert without_registry.resolve(provenance, ("REQ-INVENTED",)) is True


def test_a_registered_requirement_still_resolves(tmp_path: Path) -> None:
    source = tmp_path / "requirements.yaml"
    source.write_text(
        yaml.safe_dump({"requirements": [{"id": "REQ-1", "description": "Real."}]}),
        encoding="utf-8",
    )
    provenance = Provenance(
        kind=ProvenanceKind.REQUIREMENT,
        source="requirements.yaml",
        locator="requirement:REQ-1",
    )
    resolver = ProvenanceResolver(
        project=tmp_path,
        requirements=(Requirement(id="REQ-1", title="Real", description="Real."),),
    )

    assert resolver.resolve(provenance, ("REQ-1",)) is True
