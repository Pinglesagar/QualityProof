"""Tracker-agnostic finding synchronization.

A finding is evidence about the system under test. Which tracker records it is a
deployment detail, so everything that decides *identity* and *idempotency* lives
here and is shared: the fingerprint, the stored mapping, the cross-scope refusal
and the dry-run contract. Only two things are tracker-specific, and both are
delegated: how a payload is spelled, and how it is transmitted.

That split is what stops a second tracker from becoming a second implementation
of the same rules. Jira takes a field object over ``POST /issue``; Azure Boards
takes a JSON Patch array over ``POST /_apis/wit/workitems/$Type``. Neither of
those differences is allowed to reach the synchronization logic.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, TypeVar

from qualityproof.models import (
    IssueTracker,
    JiraFinding,
    JiraIssueMapping,
    JiraIssueResult,
)
from qualityproof.repository import SQLiteRepository

#: Jira renders an object, Azure Boards renders a patch array. The union is the
#: honest type; narrowing it would force one tracker to pretend to be the other.
IssuePayload = dict[str, object] | list[dict[str, object]]

#: The renderer produces a payload and the transport consumes one, so the pair is
#: parameterised on the same type. That makes handing a Jira field object to the
#: Azure Boards transport a type error rather than a runtime rejection from a
#: remote API, which is the difference between catching it here and catching it
#: after a write attempt against somebody's real board.
PayloadT_co = TypeVar("PayloadT_co", covariant=True)
PayloadT_contra = TypeVar("PayloadT_contra", contravariant=True)


class IssueRenderer(Protocol[PayloadT_co]):
    """Turns a finding into one tracker's request payload.

    Rendering is separated from transport so the local mock can stand in for
    either tracker without inventing a third payload dialect. A mock that stored
    a shape no real tracker accepts would make the dry run a rehearsal of the
    wrong thing.
    """

    tracker: IssueTracker

    def validate_project(self, project: str) -> str:
        """Return the canonical project identifier, or raise if unusable."""
        ...

    def render(
        self, finding: JiraFinding, fingerprint: str, project: str, item_type: str
    ) -> PayloadT_co: ...


class IssueTransport(Protocol[PayloadT_contra]):
    """Sends a rendered payload. Never receives or persists a credential."""

    adapter_name: str
    account_id: str

    def create_issue(self, payload: PayloadT_contra) -> str: ...

    def update_issue(self, issue_key: str, payload: PayloadT_contra) -> None: ...


def mapping_identity(
    tracker: IssueTracker,
    adapter_name: str,
    account_id: str,
    project: str,
    fingerprint: str,
) -> str:
    """Derive the stored mapping key.

    The tracker is part of the identity. Without it, syncing one finding through
    the local mock for Jira and again for Azure Boards would compute the same key
    for two genuinely different records, and the cross-scope guard below would
    reject the second as a corrupted mapping when nothing was wrong.
    """
    return hashlib.sha256(
        "\0".join(
            (tracker.value, adapter_name, account_id, project.upper(), fingerprint)
        ).encode()
    ).hexdigest()


def synchronize_finding[PayloadT](
    finding: JiraFinding,
    project: str,
    renderer: IssueRenderer[PayloadT],
    transport: IssueTransport[PayloadT],
    repository: SQLiteRepository,
    *,
    fingerprint: str,
    dry_run: bool = True,
    item_type: str,
) -> JiraIssueResult:
    """Create or update one tracker record for one finding.

    Dry run is the default everywhere. The payload returned by a dry run is the
    exact payload a write would send, so reviewing it is a real review rather
    than an approximation of one.
    """
    canonical = renderer.validate_project(project)
    if not item_type.strip():
        raise ValueError("item_type must not be empty")
    payload = renderer.render(finding, fingerprint, canonical, item_type.strip())

    identity = mapping_identity(
        renderer.tracker, transport.adapter_name, transport.account_id, canonical, fingerprint
    )
    mapping = repository.get("jira_mapping", identity, JiraIssueMapping)
    if mapping is not None and (
        mapping.tracker is not renderer.tracker
        or mapping.adapter != transport.adapter_name
        or mapping.account != transport.account_id
        or mapping.project_key != canonical.upper()
        or mapping.fingerprint != fingerprint
    ):
        # Reached only if a stored row was written under a colliding key. Refusing
        # is the only safe response: updating would edit somebody else's record.
        raise ValueError("stored issue mapping identity does not match this synchronization")

    adapter_name = transport.adapter_name
    if adapter_name not in {"mock", "cloud", "azure"}:
        # Recorded in the mapping, so an unknown transport must fail here rather
        # than write a row that cannot be read back.
        raise ValueError(f"unsupported issue transport: {adapter_name}")

    action = "update" if mapping else "create"
    if dry_run:
        return JiraIssueResult(
            fingerprint=fingerprint,
            action=action,
            issue_key=mapping.issue_key if mapping else None,
            request=payload,
        )
    if mapping:
        transport.update_issue(mapping.issue_key, payload)
        issue_key = mapping.issue_key
    else:
        issue_key = transport.create_issue(payload)
        repository.put(
            "jira_mapping",
            identity,
            JiraIssueMapping(
                fingerprint=fingerprint,
                issue_key=issue_key,
                adapter=adapter_name,
                account=transport.account_id,
                project_key=canonical.upper(),
                tracker=renderer.tracker,
            ),
        )
    return JiraIssueResult(
        fingerprint=fingerprint,
        action=action,
        issue_key=issue_key,
        dry_run=False,
        request=payload,
    )
