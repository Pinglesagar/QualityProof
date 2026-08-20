"""Evidence and process requirements: JS-EV-1 through JS-EV-4.

These constrain how the quality programme itself behaves, not how the application
behaves. They exist because a programme that cannot demonstrate its own integrity
is asking to be trusted rather than believed.

Each test exercises the guarantee directly, so the requirement is evidenced rather
than asserted in prose. The tests are deliberately small: a broad integration test
would pass for reasons unrelated to the property.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from qualityproof import qualityproof
from qualityproof.audit import ProvenanceResolver
from qualityproof.coverage import RequirementStatus, compute_coverage
from qualityproof.discovery import DiscoveryOptions, is_allowed_request, is_destructive
from qualityproof.models import (
    AuditedTest,
    LedgerEntry,
    LedgerStatus,
    Provenance,
    ProvenanceKind,
    Requirement,
    SourceAssertion,
    TestMetadata,
)
from qualityproof.security import EvidenceRedactor

ORIGIN = "https://app.example.test"


class _Request:
    def __init__(self, url: str, method: str) -> None:
        self.url = url
        self.method = method


@qualityproof(
    requirements=["JS-EV-1"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-EV-1",
        }
    ],
)
def test_exploration_cannot_change_application_state() -> None:
    """Read-only by policy, refused before the request leaves the browser.

    The check is on the outbound request rather than on the response, because a
    state change that is merely *reported* has already happened.
    """
    options = DiscoveryOptions(allowed_domains=("app.example.test",))

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        request = _Request(f"{ORIGIN}/orders", method)
        assert not is_allowed_request(request, ORIGIN, options, authenticating=False)  # type: ignore[arg-type]

    # Reads are permitted, or discovery would find nothing.
    read = _Request(f"{ORIGIN}/products", "GET")
    assert is_allowed_request(read, ORIGIN, options, authenticating=False)  # type: ignore[arg-type]
    # And a labelled destructive control is refused rather than activated.
    assert is_destructive("Place order")
    assert is_destructive("Delete account")


@qualityproof(
    requirements=["JS-EV-2"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-EV-2",
        }
    ],
)
def test_credentials_do_not_survive_into_retained_evidence() -> None:
    """The credential the target application actually uses must not appear."""
    redactor = EvidenceRedactor.from_environment(
        {"JS_CUSTOMER_PASS": "ncc-1701-secret", "JS_CUSTOMER_USER": "jim@juice-sh.op"}
    )

    rendered = redactor.text(
        "login failed for jim@juice-sh.op using ncc-1701-secret via "
        "https://jim:ncc-1701-secret@app.example.test/login"
    )

    assert "ncc-1701-secret" not in rendered
    assert "jim@juice-sh.op" not in rendered
    assert "<REDACTED>" in rendered


@qualityproof(
    requirements=["JS-EV-3"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-EV-3",
        }
    ],
)
def test_verification_requires_a_registered_source(tmp_path: Path) -> None:
    """A test may not credit itself against a requirement it invented.

    The registry is the authority. Without this, citing an identifier that appears
    in any file of the test's own choosing would establish coverage of a
    requirement nobody ever wrote.
    """
    statement = "The catalogue is reachable without authentication."
    registered = tmp_path / "requirements.yaml"
    registered.write_text(
        yaml.safe_dump({"requirements": [{"id": "JS-CAT-1", "description": statement}]}),
        encoding="utf-8",
    )
    invented = tmp_path / "my-own-notes.yaml"
    invented.write_text(
        yaml.safe_dump({"requirements": [{"id": "JS-CAT-1", "description": "whatever"}]}),
        encoding="utf-8",
    )
    resolver = ProvenanceResolver(
        project=tmp_path,
        requirements=(
            Requirement(
                id="JS-CAT-1",
                title="Catalogue",
                description=statement,
                provenance=(
                    Provenance(
                        kind=ProvenanceKind.REQUIREMENT,
                        source=str(registered.resolve()),
                        locator="requirement:JS-CAT-1",
                        content_hash=hashlib.sha256(statement.encode()).hexdigest(),
                    ),
                ),
            ),
        ),
    )

    genuine = Provenance(
        kind=ProvenanceKind.REQUIREMENT,
        source="requirements.yaml",
        locator="requirement:JS-CAT-1",
    )
    forged = genuine.model_copy(update={"source": "my-own-notes.yaml"})

    assert resolver.resolve(genuine, ("JS-CAT-1",)) is True
    assert resolver.resolve(forged, ("JS-CAT-1",)) is False


@qualityproof(
    requirements=["JS-EV-4"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-EV-4",
        }
    ],
)
def test_unproven_requirements_are_reported_not_omitted() -> None:
    """Silence about a requirement must never read as success."""
    requirements = (
        Requirement(id="JS-A", title="Covered", description="d", priority="P1"),
        Requirement(id="JS-B", title="Untested", description="d", priority="P1"),
    )
    ledger = (
        LedgerEntry(
            id="t.py::covered",
            status=LedgerStatus.VERIFIED,
            reason="fixture",
            test=AuditedTest(
                id="t.py::covered",
                path="t.py",
                name="covered",
                line=1,
                framework="playwright",
                assertions=(SourceAssertion(kind="expect", line=2, expression="expect(x)"),),
                metadata=TestMetadata(requirement_ids=("JS-A",)),
            ),
        ),
    )

    report = compute_coverage(requirements, ledger)

    statuses = {item.requirement_id: item.status for item in report.requirements}
    assert statuses["JS-A"] is RequirementStatus.VERIFIED
    assert statuses["JS-B"] is RequirementStatus.UNCOVERED
    assert report.uncovered == ("JS-B",)
    # And the risk band is reported, because a percentage hides which one is missing.
    assert report.unproven_at("P1") == ("JS-B",)  # type: ignore[arg-type]


@qualityproof(
    requirements=["JS-ADMIN-4"],
    provenance=[
        {
            "kind": "REQUIREMENT",
            "source": "docs/project/requirements.yaml",
            "locator": "requirement:JS-ADMIN-4",
        }
    ],
)
def test_a_change_in_role_reachability_is_detectable_between_releases() -> None:
    """Evidenced on constructed snapshots, and why that is the right evidence.

    The requirement is about the *comparison*, not about the application: given
    two releases where a role's reachability differs, the diff must report it.
    Constructed snapshots evidence exactly that, and do so deterministically.

    Running it against two real releases of the target would evidence something
    weaker -- that these two particular builds happen to differ -- and would fail
    for unrelated reasons every time the application changed. The real two-release
    comparison was run separately and is recorded in the RTM; it confirms the
    mechanism reports facet-level change without spurious additions or removals.
    """
    from qualityproof.models import EvidenceSnapshot
    from qualityproof.snapshots import compare_snapshots

    def snapshot(name: str, status: str, headings: str) -> EvidenceSnapshot:
        return EvidenceSnapshot(
            name=name,
            routes=("/admin",),
            page_fingerprints={"/admin#state": name},
            page_facets={"/admin#state": {"status": status, "headings": headings}},
            page_roles={"/admin#state": "customer"},
        )

    comparison = compare_snapshots(
        snapshot("before", "denied", "none"),
        snapshot("after", "served", "administration"),
    )

    assert comparison.page_facet_changes == {"/admin": ("headings", "status")}
