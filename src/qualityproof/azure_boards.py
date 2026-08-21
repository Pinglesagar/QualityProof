"""Azure Boards work-item synchronization.

The same finding, the same fingerprint, the same idempotency rules as Jira. Only
two things differ, and both are real differences in the product rather than
stylistic ones:

* Azure Boards is written with a **JSON Patch array** and the content type
  ``application/json-patch+json``, not a field object. Creating uses
  ``POST /_apis/wit/workitems/$<Type>`` where the type is part of the path.
* A personal access token authenticates over HTTP Basic with an **empty
  username**, so the header is ``base64(":" + pat)``.

Severity is deliberately written into the description rather than into
``Microsoft.VSTS.Common.Severity``. That field exists on Bug in the Agile and
CMMI processes and does not exist on Issue in Basic, so setting it
unconditionally would turn a correct configuration into a rejected write
depending on which process template the project happens to use.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from qualityproof.jira import redact
from qualityproof.models import IssueTracker, JiraFinding
from qualityproof.trackers import IssuePayload

#: A personal access token. Read from the environment only, held in process
#: memory only, and never written to configuration, a payload or a report.
PAT_ENV = "QUALITYPROOF_AZURE_DEVOPS_PAT"

#: Azure DevOps project names allow spaces and a range of punctuation, so Jira's
#: key pattern would reject a perfectly valid project such as "Production Bug
#: Support". These are the characters Azure DevOps itself forbids.
_FORBIDDEN_PROJECT_CHARACTERS = set('/:\\~&%;@\'"?<>|#$*}{,+=[]')
_TAG_SEPARATOR = "; "
_API_VERSION = "7.1"


def _escape_block(text: str) -> str:
    """Escape for an HTML description field, preserving line structure."""
    return html.escape(text, quote=False).replace("\n", "<br/>")


def html_description(finding: JiraFinding, fingerprint: str) -> str:
    """Render the finding as the HTML that Azure Boards description fields take.

    Azure Boards stores descriptions as HTML, so the evidence is escaped before
    it is embedded. Skipping that would let a finding's own text close the tag and
    inject markup into a work item, and evidence that can rewrite its own
    container is not evidence.
    """
    evidence = json.dumps(redact(finding.evidence), indent=2, sort_keys=True)
    requirements = ", ".join(sorted(finding.requirement_ids)) or "none"
    return (
        f"<div><p>{_escape_block(finding.summary)}</p>"
        f"<p>Severity: {_escape_block(finding.severity)}</p>"
        f"<p>Finding fingerprint: {_escape_block(fingerprint)}</p>"
        f"<p>Requirements: {_escape_block(requirements)}</p>"
        f"<p>Redacted evidence:</p>"
        f"<pre>{html.escape(evidence, quote=False)}</pre></div>"
    )


class AzureBoardsRenderer:
    """Renders a finding as an Azure Boards JSON Patch document."""

    tracker = IssueTracker.AZURE_BOARDS

    def validate_project(self, project: str) -> str:
        name = project.strip()
        if not name:
            raise ValueError("an Azure DevOps project name is required")
        if len(name) > 64:
            raise ValueError("an Azure DevOps project name is at most 64 characters")
        if name.startswith("_") or name.endswith("."):
            raise ValueError(
                "an Azure DevOps project name cannot start with '_' or end with '.'"
            )
        offending = sorted(set(name) & _FORBIDDEN_PROJECT_CHARACTERS)
        if offending:
            raise ValueError(
                f"an Azure DevOps project name cannot contain: {''.join(offending)}"
            )
        if any(character.isspace() and character != " " for character in name):
            raise ValueError("an Azure DevOps project name cannot contain control whitespace")
        return name

    def render(
        self, finding: JiraFinding, fingerprint: str, project: str, item_type: str
    ) -> list[dict[str, object]]:
        # The fingerprint tag is what makes a repeated sync find its own work item
        # rather than filing a duplicate, and it is also queryable in Azure Boards,
        # so a human can find the record from the evidence and back again.
        tags = _TAG_SEPARATOR.join(("qualityproof", f"qp-{fingerprint[:12]}"))
        return [
            {"op": "add", "path": "/fields/System.Title", "value": finding.title},
            {
                "op": "add",
                "path": "/fields/System.Description",
                "value": html_description(finding, fingerprint),
            },
            {"op": "add", "path": "/fields/System.Tags", "value": tags},
        ]


class AzureBoardsAdapter:
    """Azure DevOps work-item transport. The token exists only in memory."""

    adapter_name = "azure"

    def __init__(
        self,
        organization_url: str,
        project: str,
        item_type: str,
        *,
        pat_env: str = PAT_ENV,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.organization_url = self._validated_organization_url(organization_url)
        self.project = project
        self.item_type = item_type
        self.account_id = f"{self.organization_url}/{project}"
        self._authorization = self._resolve_authorization(pat_env)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _resolve_authorization(pat_env: str) -> str:
        """Build the Basic header for a personal access token.

        Azure DevOps expects an empty username, so the credential is ``:<pat>``.
        Sending the token as a bearer or as the username silently fails
        authentication in a way the error message does not explain.
        """
        pat = os.environ.get(pat_env)
        if not pat:
            raise ValueError(
                f"set {pat_env} to an Azure DevOps personal access token with "
                "Work Items (Read & write) scope"
            )
        if ":" in pat:
            # A colon would be parsed as the username/password boundary, so a
            # pasted "user:token" would authenticate as neither.
            raise ValueError(f"{pat_env} must be the token alone, with no ':' in it")
        encoded = base64.b64encode(f":{pat}".encode()).decode()
        return f"Basic {encoded}"

    @staticmethod
    def _validated_organization_url(organization_url: str) -> str:
        """Accept only a credential-free HTTPS Azure DevOps organization URL."""
        parts = urlsplit(organization_url)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or (parts.port not in {None, 443})
        ):
            raise ValueError(
                "Azure DevOps credentials require a credential-free HTTPS organization URL"
            )
        path = parts.path.rstrip("/")
        modern = host == "dev.azure.com" and bool(
            re.fullmatch(r"/[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", path)
        )
        legacy = bool(re.fullmatch(r"[a-z0-9-]+\.visualstudio\.com", host)) and path == ""
        if not (modern or legacy):
            raise ValueError(
                "Azure DevOps base must be https://dev.azure.com/<organization> "
                "or https://<organization>.visualstudio.com"
            )
        return urlunsplit(("https", host, path, "", ""))

    def _request(self, method: str, path: str, payload: IssuePayload) -> dict[str, object]:
        response = httpx.request(
            method,
            f"{self.organization_url}/{path.lstrip('/')}",
            headers={
                "Authorization": self._authorization,
                "Accept": "application/json",
                # Required by the work-item API. A plain application/json body is
                # rejected, and the resulting error does not name the content type.
                "Content-Type": "application/json-patch+json",
            },
            json=payload,
            timeout=self.timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Azure DevOps returned a non-object response")
        return dict(body)

    def create_issue(self, payload: IssuePayload) -> str:
        # The work-item type is part of the path and is dollar-prefixed. Quoting it
        # matters: "User Story" contains a space.
        project = quote(self.project, safe="")
        item_type = quote(f"${self.item_type}", safe="$")
        result = self._request(
            "POST",
            f"{project}/_apis/wit/workitems/{item_type}?api-version={_API_VERSION}",
            payload,
        )
        identifier = result.get("id")
        if not isinstance(identifier, int):
            raise ValueError("Azure DevOps create response did not include a work item id")
        return str(identifier)

    def update_issue(self, issue_key: str, payload: IssuePayload) -> None:
        if not re.fullmatch(r"[0-9]+", issue_key):
            raise ValueError("an Azure Boards work item id is numeric")
        project = quote(self.project, safe="")
        self._request(
            "PATCH",
            f"{project}/_apis/wit/workitems/{issue_key}?api-version={_API_VERSION}",
            payload,
        )


class LocalJSONAzureBoardsAdapter:
    """Deterministic local mock, so a dry run can be rehearsed without a token."""

    adapter_name = "mock"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.account_id = f"local:{hashlib.sha256(str(path.resolve()).encode()).hexdigest()}"

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("mock Azure Boards store must contain a JSON object")
        return dict(raw)

    def _write(self, items: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def create_issue(self, payload: IssuePayload) -> str:
        items = self._read()
        # Numeric, because a real work item id is numeric and code downstream is
        # allowed to rely on that.
        key = str(len(items) + 1)
        items[key] = payload
        self._write(items)
        return key

    def update_issue(self, issue_key: str, payload: IssuePayload) -> None:
        items = self._read()
        if issue_key not in items:
            raise ValueError(f"mock work item not found: {issue_key}")
        items[issue_key] = payload
        self._write(items)
