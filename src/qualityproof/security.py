"""Shared security policy and redaction primitives."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|secret|token|api[_-]?key)", re.I
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_BASIC = re.compile(r"\bBasic\s+[A-Za-z0-9+/=]+", re.I)
_URL_CREDENTIAL = re.compile(r"(https?://)[^/@\s:]+(?::[^/@\s]*)?@", re.I)
#: Matched on underscore or string boundaries rather than as a substring. The
#: previous form looked for "PASSWORD" and therefore missed "JS_CUSTOMER_PASS" --
#: a real credential in a conventional variable name went unredacted, which this
#: project's own test suite caught. Boundary matching also avoids the reverse
#: error: "TESTS_PASSED" is not a secret.
_ENV_SECRET_NAME = re.compile(
    r"(?:^|_)(?:"
    r"PASS|PASSWORD|PASSPHRASE|PWD|SECRET|TOKEN|KEY|APIKEY|CREDENTIAL|CREDENTIALS"
    r"|AUTH|AUTHORIZATION|COOKIE|SESSION|BEARER"
    # A username is not a secret in the way a password is, but it identifies a
    # real account and appears in the same evidence, so it is redacted too.
    r"|USER|USERNAME|EMAIL|LOGIN"
    r")(?:_|$)",
    re.I,
)
#: Below this length a value is too generic to replace safely: substituting every
#: occurrence of a two-character secret would corrupt unrelated evidence and make
#: the output useless for diagnosis.
MIN_REDACTABLE_LENGTH = 6


class EvidenceRedactor:
    """Redact known runtime secrets and common credential shapes."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self._secrets = tuple(
            sorted(
                {value for value in secrets if len(value) >= MIN_REDACTABLE_LENGTH},
                key=len,
                reverse=True,
            )
        )
        self._short_secrets = tuple(
            sorted(
                {value for value in secrets if 0 < len(value) < MIN_REDACTABLE_LENGTH},
                key=len,
                reverse=True,
            )
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        additional: Sequence[str] = (),
    ) -> EvidenceRedactor:
        source = os.environ if environ is None else environ
        values = [
            value
            for name, value in source.items()
            if value and _ENV_SECRET_NAME.search(name)
        ]
        return cls((*values, *additional))

    @property
    def secrets(self) -> tuple[str, ...]:
        """Return secret values for in-process DOM masking only."""
        return self._secrets

    def text(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "<REDACTED>")
        for secret in self._short_secrets:
            # Word-bounded so a short value cannot shred unrelated text.
            redacted = re.sub(rf"\b{re.escape(secret)}\b", "<REDACTED>", redacted)
        redacted = _BEARER.sub("Bearer <REDACTED>", redacted)
        redacted = _BASIC.sub("Basic <REDACTED>", redacted)
        return _URL_CREDENTIAL.sub(r"\1<REDACTED>@", redacted)

    def value(self, value: object) -> object:
        if isinstance(value, Mapping):
            return {
                str(key): (
                    "<REDACTED>"
                    if _SENSITIVE_KEY.search(str(key))
                    else self.value(nested)
                )
                for key, nested in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    def json_bytes(self, payload: bytes) -> bytes:
        parsed = json.loads(payload)
        return (json.dumps(self.value(parsed), sort_keys=True) + "\n").encode()


class ArtifactMode(StrEnum):
    """How much browser artifact capture is permitted for a run."""

    OFF = "off"
    ON_FAILURE = "on_failure"
    FULL = "full"


@dataclass(frozen=True)
class ArtifactPolicy:
    """Decide trace and screenshot capture from the run's secret exposure.

    Traces and screenshots cannot be reliably redacted after the fact: a trace is
    a zip of DOM snapshots and network payloads, and a screenshot is pixels. So
    capture is enabled by default only when nothing sensitive is present, and an
    authenticated run must opt in explicitly. Opting in does not make the
    artifacts safe, it makes them *quarantined*: they are written to a marked
    directory and excluded from reports, snapshots and published output.
    """

    mode: ArtifactMode = ArtifactMode.OFF
    authenticated: bool = False
    acknowledged_unredactable: bool = False
    #: Names of the environment variables that marked this run authenticated.
    #: Reported because evidence silently disappearing is worse than no evidence:
    #: one unrelated variable such as GITHUB_TOKEN is enough to turn capture off,
    #: and an operator needs to know which one did it. Only names, never values.
    reasons: tuple[str, ...] = ()

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> ArtifactPolicy:
        source = os.environ if environ is None else environ
        acknowledged = source.get("QUALITYPROOF_ALLOW_UNREDACTABLE_ARTIFACTS") == "1"
        reasons = tuple(
            sorted(
                name
                for name, value in source.items()
                if value and _ENV_SECRET_NAME.search(name)
            )
        )
        if source.get("QUALITYPROOF_STORAGE_STATE"):
            reasons = tuple(sorted({*reasons, "QUALITYPROOF_STORAGE_STATE"}))
        authenticated = bool(reasons)
        requested = source.get("QUALITYPROOF_ARTIFACTS", "").strip().lower()
        if requested in {mode.value for mode in ArtifactMode}:
            mode = ArtifactMode(requested)
        else:
            mode = ArtifactMode.OFF if authenticated else ArtifactMode.ON_FAILURE
        if authenticated and not acknowledged:
            mode = ArtifactMode.OFF
        return cls(
            mode=mode,
            authenticated=authenticated,
            acknowledged_unredactable=acknowledged,
            reasons=reasons,
        )

    @property
    def traces_enabled(self) -> bool:
        return self.mode is not ArtifactMode.OFF

    @property
    def retain_always(self) -> bool:
        return self.mode is ArtifactMode.FULL

    @property
    def quarantined(self) -> bool:
        """True when captured artifacts must not be published anywhere."""
        return self.traces_enabled and self.authenticated

    def pytest_arguments(self) -> tuple[str, ...]:
        if self.mode is ArtifactMode.OFF:
            return ("--tracing=off", "--screenshot=off", "--video=off")
        if self.mode is ArtifactMode.FULL:
            return ("--tracing=on", "--screenshot=on", "--video=off")
        return (
            "--tracing=retain-on-failure",
            "--screenshot=only-on-failure",
            "--video=off",
        )

    def describe(self) -> str:
        """Summarize the policy, naming what triggered it but never its values."""
        summary = (
            f"artifacts={self.mode.value} authenticated={self.authenticated} "
            f"quarantined={self.quarantined}"
        )
        if self.mode is ArtifactMode.OFF and self.reasons:
            summary += f" disabled_by={','.join(self.reasons)}"
        return summary


def _normalized_words(label: str) -> str:
    """Lowercase a label and pad it so word-boundary matching is simple."""
    collapsed = " ".join(re.split(r"[^0-9a-z]+", label.casefold()))
    return f" {collapsed.strip()} "


def matches_unsafe_term(label: str, terms: Sequence[str]) -> bool:
    """Match a destructive term on word boundaries, not as a substring.

    Substring matching was both too weak and too strong: it missed "Log out"
    because the term was spelled "logout", and it flagged "PayPal" because "pay"
    appears inside it. Boundary matching fixes both directions.

    This list is a mitigation, not a guarantee. It is English-only, it cannot
    know that "Process" means "charge the card" in some application, and it is
    documented as extensible for exactly that reason.
    """
    haystack = _normalized_words(label)
    for term in terms:
        needle = _normalized_words(term).strip()
        if needle and f" {needle} " in haystack:
            return True
    return False


def is_within(path: Path, root: Path) -> bool:
    """Resolve paths without requiring their final components to exist."""
    return path.resolve().is_relative_to(root.resolve())


def reject_custom_path(project: Path, path: Path, description: str) -> None:
    custom = project / "scenarios" / "custom"
    candidate = path if path.is_absolute() else project / path
    if is_within(candidate, custom):
        raise ValueError(f"{description} may not be inside scenarios/custom")


def validate_http_origin(url: str, origin: str | None = None) -> str:
    """Validate an absolute HTTP(S) URL and optional exact origin binding."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must be absolute HTTP(S)")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are forbidden")
    if origin is not None:
        expected = urlsplit(origin)
        if (parts.scheme.lower(), parts.hostname.lower(), parts.port) != (
            expected.scheme.lower(),
            (expected.hostname or "").lower(),
            expected.port,
        ):
            raise ValueError("URL is outside the bound origin")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
