"""Governed Jira adapters with local idempotency and secret-safe defaults."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from qualityproof.models import JiraFinding, JiraIssueMapping, JiraIssueResult
from qualityproof.repository import SQLiteRepository

TOKEN_ENV = "QUALITYPROOF_JIRA_BEARER_TOKEN"
#: Atlassian API tokens authenticate with HTTP Basic over `email:token`, not with
#: a bearer header. Both schemes are supported because they are reached by very
#: different routes: an API token is three clicks in account settings, while a
#: bearer token requires registering an OAuth application and completing a 3LO
#: exchange. Refusing the simpler one would push every user towards the heavier
#: setup for no security benefit -- an API token is scoped to the account either
#: way.
API_TOKEN_ENV = "QUALITYPROOF_JIRA_API_TOKEN"
EMAIL_ENV = "QUALITYPROOF_JIRA_EMAIL"
_SENSITIVE_KEYS = re.compile(r"(token|secret|password|authorization|cookie)", re.I)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)


class JiraPort(Protocol):
    """Minimal issue boundary; adapters never receive credential persistence."""

    adapter_name: str
    account_id: str

    def create_issue(self, fields: dict[str, object]) -> str: ...

    def update_issue(self, issue_key: str, fields: dict[str, object]) -> None: ...


def redact(value: object) -> object:
    """Recursively remove likely credentials and direct email identifiers."""
    if isinstance(value, dict):
        return {
            str(key): (
                "<REDACTED>" if _SENSITIVE_KEYS.search(str(key)) else redact(nested)
            )
            for key, nested in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _EMAIL.sub("<REDACTED_EMAIL>", _BEARER.sub("Bearer <REDACTED>", value))
    return value


def finding_fingerprint(finding: JiraFinding) -> str:
    """Stable identity excludes volatile evidence while retaining finding intent."""
    identity = {
        "requirement_ids": sorted(finding.requirement_ids),
        "route": finding.route,
        "scenario_id": finding.scenario_id,
        "severity": finding.severity,
        "summary": finding.summary.strip(),
        "title": finding.title.strip(),
    }
    encoded = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def adf_description(finding: JiraFinding, fingerprint: str) -> dict[str, object]:
    evidence = json.dumps(redact(finding.evidence), indent=2, sort_keys=True)
    lines = [
        finding.summary,
        f"Severity: {finding.severity}",
        f"Finding fingerprint: {fingerprint}",
        f"Requirements: {', '.join(sorted(finding.requirement_ids)) or 'none'}",
        "Redacted evidence:",
        evidence,
    ]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line[:30000]}],
            }
            for line in lines
        ],
    }


class LocalJSONJiraAdapter:
    """Deterministic local mock used by demos and tests."""

    adapter_name = "mock"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.account_id = f"local:{hashlib.sha256(str(path.resolve()).encode()).hexdigest()}"

    def _read(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("mock Jira store must contain a JSON object")
        return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}

    def _write(self, issues: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(issues, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def create_issue(self, fields: dict[str, object]) -> str:
        issues = self._read()
        key = f"MOCK-{len(issues) + 1}"
        issues[key] = fields
        self._write(issues)
        return key

    def update_issue(self, issue_key: str, fields: dict[str, object]) -> None:
        issues = self._read()
        if issue_key not in issues:
            raise ValueError(f"mock issue not found: {issue_key}")
        issues[issue_key] = fields
        self._write(issues)


class JiraCloudAdapter:
    """Jira Cloud REST v3 adapter; the credential exists only in process memory.

    Supports both an Atlassian API token over HTTP Basic and an OAuth bearer
    token. Neither is read from or written to project configuration.
    """

    adapter_name = "cloud"

    def __init__(
        self,
        base_url: str,
        *,
        token_env: str = TOKEN_ENV,
        api_token_env: str = API_TOKEN_ENV,
        email_env: str = EMAIL_ENV,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.base_url = self._validated_base_url(base_url)
        self.account_id = self.base_url
        self._authorization, self.auth_scheme = self._resolve_authorization(
            token_env, api_token_env, email_env
        )
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _resolve_authorization(
        token_env: str, api_token_env: str, email_env: str
    ) -> tuple[str, str]:
        """Build the Authorization header, preferring an API token when present.

        The header is assembled once and held only in process memory. It is never
        logged, never written to configuration, and never included in a payload:
        `redact` strips bearer and basic material from anything that is persisted.
        """
        api_token = os.environ.get(api_token_env)
        email = os.environ.get(email_env)
        if api_token and email:
            encoded = base64.b64encode(f"{email}:{api_token}".encode()).decode()
            return f"Basic {encoded}", "basic"
        if api_token and not email:
            raise ValueError(
                f"{api_token_env} is set but {email_env} is not; an Atlassian API "
                "token authenticates as email:token"
            )
        bearer = os.environ.get(token_env)
        if bearer:
            return f"Bearer {bearer}", "bearer"
        raise ValueError(
            f"set {api_token_env} with {email_env} for an API token, "
            f"or {token_env} for an OAuth bearer token"
        )

    @staticmethod
    def _validated_base_url(base_url: str) -> str:
        parts = urlsplit(base_url)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or (parts.port not in {None, 443})
        ):
            raise ValueError("Jira credentials require a credential-free HTTPS base URL")
        path = parts.path.rstrip("/")
        tenant_host = host.endswith(".atlassian.net") and host.count(".") >= 2
        cloud_api = host == "api.atlassian.com" and bool(
            re.fullmatch(r"/ex/jira/[A-Za-z0-9_-]+", path)
        )
        if not ((tenant_host and path == "") or cloud_api):
            raise ValueError(
                "Jira base must be an Atlassian tenant host or api.atlassian.com/ex/jira/<cloud-id>"
            )
        netloc = host if parts.port is None else f"{host}:{parts.port}"
        return urlunsplit(("https", netloc, path, "", ""))

    def _request(self, method: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        response = httpx.request(
            method,
            f"{self.base_url}/rest/api/3/{path.lstrip('/')}",
            headers={"Authorization": self._authorization, "Accept": "application/json"},
            json=payload,
            timeout=self.timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Jira returned a non-object response")
        return dict(body)

    def create_issue(self, fields: dict[str, object]) -> str:
        result = self._request("POST", "issue", {"fields": fields})
        key = result.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("Jira create response did not include an issue key")
        return key

    def update_issue(self, issue_key: str, fields: dict[str, object]) -> None:
        self._request("PUT", f"issue/{issue_key}", {"fields": fields})


def sync_finding(
    finding: JiraFinding,
    project_key: str,
    adapter: JiraPort,
    repository: SQLiteRepository,
    *,
    dry_run: bool = True,
    issue_type: str = "Bug",
) -> JiraIssueResult:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", project_key):
        raise ValueError("project_key must be a Jira project identifier")
    # Configurable because issue types are per-project. "Bug" is absent from plenty
    # of Jira projects, and hardcoding it turns a working configuration into an
    # opaque 400 at write time.
    if not issue_type.strip():
        raise ValueError("issue_type must not be empty")
    fingerprint = finding_fingerprint(finding)
    fields: dict[str, object] = {
        "project": {"key": project_key},
        "summary": finding.title,
        "description": adf_description(finding, fingerprint),
        "issuetype": {"name": issue_type.strip()},
        "labels": ["qualityproof", f"qp-{fingerprint[:12]}"],
    }
    mapping_id = hashlib.sha256(
        "\0".join(
            (adapter.adapter_name, adapter.account_id, project_key.upper(), fingerprint)
        ).encode()
    ).hexdigest()
    mapping = repository.get("jira_mapping", mapping_id, JiraIssueMapping)
    if mapping is not None and (
        mapping.adapter != adapter.adapter_name
        or mapping.account != adapter.account_id
        or mapping.project_key != project_key.upper()
        or mapping.fingerprint != fingerprint
    ):
        raise ValueError("stored Jira mapping identity does not match this synchronization")
    action = "update" if mapping else "create"
    if dry_run:
        return JiraIssueResult(
            fingerprint=fingerprint,
            action=action,
            issue_key=mapping.issue_key if mapping else None,
            request=fields,
        )
    if mapping:
        adapter.update_issue(mapping.issue_key, fields)
        issue_key = mapping.issue_key
    else:
        issue_key = adapter.create_issue(fields)
        repository.put(
            "jira_mapping",
            mapping_id,
            JiraIssueMapping(
                fingerprint=fingerprint,
                issue_key=issue_key,
                adapter=adapter.adapter_name,
                account=adapter.account_id,
                project_key=project_key.upper(),
            ),
        )
    return JiraIssueResult(
        fingerprint=fingerprint,
        action=action,
        issue_key=issue_key,
        dry_run=False,
        request=fields,
    )


def create_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    *,
    state: str,
    code_challenge: str,
) -> str:
    if not state or not code_challenge:
        raise ValueError("state and PKCE code challenge are required")
    query = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": client_id,
            "scope": " ".join(scopes),
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"https://auth.atlassian.com/authorize?{query}"


def exchange_authorization_code(
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, object]:
    """Exchange a 3LO code in memory. Callers must not persist the returned tokens."""
    response = httpx.post(
        "https://auth.atlassian.com/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=timeout_seconds,
        follow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("OAuth token response must be an object")
    return dict(payload)
