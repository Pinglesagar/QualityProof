"""Safe, bounded, deterministic Playwright application discovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from playwright.async_api import (
    BrowserContext,
    Page,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from qualityproof.models import (
    ActionEdge,
    DiscoveryResult,
    Evidence,
    EvidenceKind,
    Locator,
    NavigateAction,
    PageState,
    UnknownItem,
)
from qualityproof.repository import SQLiteRepository
from qualityproof.security import EvidenceRedactor, matches_unsafe_term

#: Link labels the crawler refuses to follow. Extend with --destructive-term.
DEFAULT_DESTRUCTIVE_TERMS = (
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
DEFAULT_DENIED_ROUTES = (
    "/delete",
    "/destroy",
    "/logout",
    "/pay",
    "/purchase",
    "/send",
)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
_HASH = re.compile(r"^[0-9a-f]{16,}$", re.I)


CONTROL_SELECTOR = "button,input,select,textarea,a[href],[role=button],[role=link]"

# Accessibility evidence that can be established without executing user actions.
# Deliberately native rather than axe-core: no third-party script is injected into
# the page under test, so the crawl's network policy stays absolute.
A11Y_SCRIPT = """() => {
  const findings = [];
  const boundLabel = (e) => {
    if (!e.id) return '';
    const found = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
    return found ? (found.innerText || '').trim() : '';
  };
  const named = (e) => ((e.getAttribute('aria-label') || '').trim()
    || boundLabel(e)
    || ((e.closest('label') || {}).innerText || '').trim()
    || (e.innerText || '').trim()
    || (e.getAttribute('value') || '').trim()
    || (e.getAttribute('title') || '').trim());
  for (const e of document.querySelectorAll('button,[role=button],a[href],[role=link]')) {
    if (!named(e)) findings.push('unnamed_control:' + e.tagName.toLowerCase());
  }
  for (const e of document.querySelectorAll('input,select,textarea')) {
    const type = (e.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button') continue;
    const label = boundLabel(e)
      || ((e.closest('label') || {}).innerText || '').trim();
    const aria = (e.getAttribute('aria-label') || '').trim()
      || (e.getAttribute('aria-labelledby') || '').trim();
    if (!label && !aria) {
      const key = e.getAttribute('name') || e.id || e.tagName.toLowerCase();
      findings.push('unlabelled_field:' + key);
    }
  }
  for (const e of document.querySelectorAll('img')) {
    if (e.getAttribute('alt') === null) {
      findings.push('image_missing_alt:' + (e.getAttribute('src') || '').split('/').pop());
    }
  }
  if (!document.querySelector('h1')) findings.push('missing_h1');
  const landmarks = document.querySelectorAll('main,[role=main]').length;
  if (landmarks === 0) findings.push('missing_main_landmark');
  if (landmarks > 1) findings.push('duplicate_main_landmark');
  return findings;
}"""

# Layout evidence: measured, not screenshotted, so it stays diffable and redactable.
LAYOUT_SCRIPT = """() => {
  const root = document.documentElement;
  const viewport = root.clientWidth;
  const offenders = [];
  for (const e of document.querySelectorAll('body *')) {
    const rect = e.getBoundingClientRect();
    if (rect.width > viewport + 1) {
      const id = e.getAttribute('data-testid') || e.id || e.tagName.toLowerCase();
      offenders.push(id + '@' + Math.round(rect.width));
    }
  }
  return {
    viewport,
    scrollWidth: root.scrollWidth,
    overflow: Math.max(0, root.scrollWidth - viewport),
    offenders: Array.from(new Set(offenders)).sort().slice(0, 10),
  };
}"""

DEFAULT_VIEWPORTS: tuple[tuple[int, int], ...] = ((375, 812), (768, 1024), (1280, 800))



# Runs in the page: reports every strategy a control supports so the preference
# order lives in Python (models.Locator.from_control) rather than being decided
# in browser JavaScript where it cannot be unit tested.
CONTROL_SCRIPT = """(els) => els.map((e) => {
  const tag = e.tagName.toLowerCase();
  const explicitRole = e.getAttribute('role');
  const inputType = (e.getAttribute('type') || '').toLowerCase();
  const implicitRole = tag === 'button' ? 'button'
    : tag === 'a' ? 'link'
    : tag === 'select' ? 'combobox'
    : tag === 'textarea' ? 'textbox'
    : tag === 'input'
      ? (inputType === 'checkbox' ? 'checkbox'
        : inputType === 'radio' ? 'radio'
        : inputType === 'submit' || inputType === 'button' ? 'button'
        : 'textbox')
      : '';
  const labelText = (() => {
    if (e.id) {
      const bound = e.ownerDocument.querySelector(
        'label[for="' + CSS.escape(e.id) + '"]');
      if (bound) return (bound.innerText || '').trim();
    }
    const wrapping = e.closest('label');
    return wrapping ? (wrapping.innerText || '').trim() : '';
  })();
  const ariaLabel = (e.getAttribute('aria-label') || '').trim();
  const placeholder = (e.getAttribute('placeholder') || '').trim();
  const ownText = (e.innerText || '').trim();
  const value = (e.getAttribute('value') || '').trim();
  const accessibleName = ariaLabel || labelText ||
    (tag === 'input' ? value : ownText) || '';
  const testId = (e.getAttribute('data-testid') || '').trim();
  let selector = '';
  if (e.id) selector = '#' + CSS.escape(e.id);
  else if (testId) selector = '[data-testid="' + CSS.escape(testId) + '"]';
  else if (e.getAttribute('name'))
    selector = tag + '[name="' + CSS.escape(e.getAttribute('name')) + '"]';
  else if (ariaLabel) selector = '[aria-label=' + JSON.stringify(ariaLabel) + ']';
  else if (tag === 'button' && ownText)
    selector = 'button:has-text(' + JSON.stringify(ownText) + ')';
  else if (placeholder) selector = '[placeholder=' + JSON.stringify(placeholder) + ']';
  return {
    tag,
    role: explicitRole || implicitRole,
    name: accessibleName,
    label: labelText,
    placeholder,
    text: ownText,
    testId,
    selector,
    action: ['input', 'select', 'textarea'].includes(tag) ? 'fill' : 'click',
  };
})"""


def _describe_control(control: dict[str, object]) -> dict[str, object]:
    """Attach the preferred locator, keeping every raw strategy for auditing.

    Controls that expose no usable strategy are still recorded. Discarding them
    would hide exactly the accessibility evidence a nameless control represents.
    """
    described = dict(control)
    try:
        described["locator"] = Locator.from_control(control).model_dump(
            mode="json", exclude_none=True
        )
    except ValueError:
        described["locator"] = None
    return described


@dataclass(frozen=True)
class RoleSpec:
    """One identity to crawl as. Secrets are named, never carried.

    Permission regressions are only observable from the *outside* of a
    privilege boundary. A crawl that authenticates as an administrator sees an
    admin page return 200 both before and after a defect removes the guard, so
    the regression cannot appear in its evidence at all. Crawling each role
    separately is what makes the boundary itself measurable.
    """

    name: str
    username_env: str = "QUALITYPROOF_USERNAME"
    password_env: str = "QUALITYPROOF_PASSWORD"
    storage_state: Path | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.name):
            raise ValueError("role name must be lowercase alphanumeric with - or _")

    @classmethod
    def parse(cls, value: str) -> RoleSpec:
        """Parse ``name=state.json`` or ``name:USER_ENV:PASSWORD_ENV``."""
        if "=" in value:
            name, _, state = value.partition("=")
            return cls(name=name.strip(), storage_state=Path(state.strip()))
        parts = [part.strip() for part in value.split(":")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                "role must be 'name=storage-state.json' or 'name:USERNAME_ENV:PASSWORD_ENV'"
            )
        return cls(name=parts[0], username_env=parts[1], password_env=parts[2])


@dataclass(frozen=True)
class DiscoveryOptions:
    max_pages: int = 50
    max_depth: int = 3
    max_actions: int = 100
    max_runtime_seconds: float = 120.0
    allowed_domains: tuple[str, ...] = ()
    destructive_terms: tuple[str, ...] = DEFAULT_DESTRUCTIVE_TERMS
    denied_routes: tuple[str, ...] = DEFAULT_DENIED_ROUTES
    viewports: tuple[tuple[int, int], ...] = DEFAULT_VIEWPORTS
    #: How long to wait for a client-side route to render content. Single-page
    #: applications complete their document load before the view exists.
    render_timeout_ms: float = 5_000.0
    role_name: str = "default"
    storage_state: Path | None = None
    #: Where to persist the authenticated session so the generated suite can
    #: reuse it instead of logging in per test. Opt-in, because the file contains
    #: live session credentials.
    save_storage_state: Path | None = None
    login_url: str | None = None
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None
    login_submit_method: str | None = None
    login_submit_path: str | None = None
    username_env: str = "QUALITYPROOF_USERNAME"
    password_env: str = "QUALITYPROOF_PASSWORD"
    headless: bool = True

    def __post_init__(self) -> None:
        if min(self.max_pages, self.max_actions) < 1 or self.max_depth < 0:
            raise ValueError("page/action limits must be positive and depth non-negative")
        if self.max_runtime_seconds <= 0:
            raise ValueError("runtime limit must be positive")
        if not self.viewports:
            raise ValueError("at least one viewport is required")
        if self.render_timeout_ms <= 0:
            raise ValueError("render timeout must be positive")
        if any(width < 200 or height < 200 for width, height in self.viewports):
            raise ValueError("viewports must be at least 200x200")
        if (self.login_submit_method is None) != (self.login_submit_path is None):
            raise ValueError("login submit method and path must be configured together")
        if self.login_submit_method is not None:
            method = self.login_submit_method.upper()
            if method in {"GET", "HEAD", "OPTIONS"}:
                raise ValueError("login submit method must be a mutation method")
            if not self.login_submit_path or not self.login_submit_path.startswith("/"):
                raise ValueError("login submit path must be an absolute path")


def is_fragment_route(fragment: str) -> bool:
    """Distinguish a client-side route from an in-page anchor.

    Single-page applications address views through the fragment: ``#/basket`` is a
    different screen, while ``#pricing`` scrolls the current one. The convention is
    reliable enough to rely on — a route fragment begins with a slash, optionally
    after the historical hash-bang — and getting this wrong in either direction is
    costly. Treating anchors as routes makes the crawler revisit one page under
    dozens of identities; treating routes as anchors collapses an entire
    application to a single page, which is what happened before this existed.
    """
    if not fragment:
        return False
    candidate = fragment[1:] if fragment.startswith("!") else fragment
    return candidate.startswith("/")


def _normalize_fragment(fragment: str) -> str:
    """Canonicalize a client-side route fragment, or drop an in-page anchor."""
    if not is_fragment_route(fragment):
        return ""
    candidate = fragment[1:] if fragment.startswith("!") else fragment
    path, separator, query = candidate.partition("?")
    path = re.sub(r"/{2,}", "/", path)
    if path != "/":
        path = path.rstrip("/")
    if not separator:
        return path
    normalized = urlencode(sorted(parse_qsl(query, keep_blank_values=True)))
    return f"{path}?{normalized}" if normalized else path


def normalize_url(url: str, base_url: str | None = None) -> str:
    """Resolve and canonicalize an HTTP(S) URL for deterministic deduplication.

    Client-side route fragments are preserved because they identify distinct
    application states; in-page anchors are discarded because they do not.
    """
    resolved = urljoin(base_url, url) if base_url else url
    parts = urlsplit(resolved)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("only absolute HTTP(S) URLs are discoverable")
    host = parts.hostname.lower()
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    if (parts.scheme.lower(), port) in {("http", 80), ("https", 443)}:
        netloc = host
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    fragment = _normalize_fragment(parts.fragment)
    return urlunsplit((parts.scheme.lower(), netloc, path, query, fragment))


def _tokenize(path: str) -> str:
    """Replace identifier-shaped path segments with stable parameter tokens."""
    segments: list[str] = []
    for segment in path.split("/"):
        if segment.isdigit():
            segments.append(":int")
        elif _UUID.fullmatch(segment):
            segments.append(":uuid")
        elif _HASH.fullmatch(segment):
            segments.append(":hash")
        else:
            segments.append(segment)
    return "/".join(segments)


def normalize_route(url: str) -> str:
    """Replace common path and query identifiers with stable parameter tokens.

    A client-side route becomes part of the route identity, so ``/#/basket`` and
    ``/#/search`` are two routes rather than two names for the document root.
    """
    parts = urlsplit(normalize_url(url))
    query = urlencode(
        [(key, ":param") for key, _ in parse_qsl(parts.query, keep_blank_values=True)],
        safe=":",
    )
    route = urlunsplit(("", "", _tokenize(parts.path) or "/", query, ""))
    if not parts.fragment:
        return route
    fragment_path, separator, fragment_query = parts.fragment.partition("?")
    tokenized = _tokenize(fragment_path)
    if separator:
        rendered = urlencode(
            [(key, ":param") for key, _ in parse_qsl(fragment_query, keep_blank_values=True)],
            safe=":",
        )
        if rendered:
            tokenized = f"{tokenized}?{rendered}"
    return f"{route}#{tokenized}"


def is_allowed_url(url: str, origin: str, allowed_domains: tuple[str, ...] = ()) -> bool:
    """Require same origin and, when supplied, an explicit host allow-list."""
    candidate = urlsplit(normalize_url(url))
    start = urlsplit(normalize_url(origin))
    if (candidate.scheme, candidate.netloc) != (start.scheme, start.netloc):
        return False
    host = candidate.hostname or ""
    return not allowed_domains or any(
        host == domain.lower().strip(".") or host.endswith(f".{domain.lower().strip('.')}")
        for domain in allowed_domains
    )


def is_destructive(label: str, terms: tuple[str, ...] = DEFAULT_DESTRUCTIVE_TERMS) -> bool:
    """True when a link's label suggests following it would change state."""
    return matches_unsafe_term(label, terms)


def is_denied_route(url: str, denied_routes: tuple[str, ...]) -> bool:
    """Match exact paths and path-prefix policies before issuing a request.

    Both the server path and the client-side route are checked: a policy denying
    ``/logout`` must also deny ``#/logout``, or a single-page application would
    quietly bypass every route rule.
    """
    parts = urlsplit(normalize_url(url))
    fragment_path = parts.fragment.partition("?")[0]
    candidates = [parts.path, fragment_path] if fragment_path else [parts.path]
    return any(
        candidate == denied.rstrip("/") or candidate.startswith(f"{denied.rstrip('/')}/")
        for candidate in candidates
        for denied in denied_routes
        if denied.startswith("/")
    )


def is_allowed_request(
    request: Request,
    origin: str,
    options: DiscoveryOptions,
    *,
    authenticating: bool,
) -> bool:
    """Enforce origin, method, and route policy before browser network I/O."""
    if not is_allowed_url(request.url, origin, options.allowed_domains):
        return False
    if is_denied_route(request.url, options.denied_routes):
        return False
    method = request.method.upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if not authenticating:
        return False
    if options.login_submit_method is None or options.login_submit_path is None:
        return False
    return (
        method == options.login_submit_method.upper()
        and urlsplit(normalize_url(request.url)).path == options.login_submit_path
    )


class Frontier:
    """FIFO frontier with deterministic URL deduplication and depth bounds."""

    def __init__(self, max_depth: int) -> None:
        self.max_depth = max_depth
        self._queue: deque[tuple[str, int, str | None]] = deque()
        self._seen: set[str] = set()

    def add(self, url: str, depth: int, source_id: str | None = None) -> bool:
        normalized = normalize_url(url)
        if depth > self.max_depth or normalized in self._seen:
            return False
        self._seen.add(normalized)
        self._queue.append((normalized, depth, source_id))
        return True

    def pop(self) -> tuple[str, int, str | None]:
        return self._queue.popleft()

    def __bool__(self) -> bool:
        return bool(self._queue)

    def __len__(self) -> int:
        return len(self._queue)


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode()).hexdigest()[:20]}"


def _unknown(reason: str, url: str, blocks: tuple[str, ...]) -> UnknownItem:
    return UnknownItem(
        id=_stable_id("unknown", f"{reason}:{url}"),
        question=f"Discovery frontier blocked: {reason}: {url}",
        blocks=blocks,
    )


def _remaining_seconds(started: float, options: DiscoveryOptions) -> float:
    remaining = options.max_runtime_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("discovery wall-clock limit exhausted")
    return remaining


async def _authenticate(
    context: BrowserContext,
    options: DiscoveryOptions,
    started: float,
) -> tuple[str, ...]:
    if options.login_url is None:
        return ()
    selectors = (
        options.username_selector,
        options.password_selector,
        options.submit_selector,
    )
    if any(selector is None for selector in selectors):
        raise ValueError("login requires username, password, and submit selectors")
    if options.login_submit_method is None or options.login_submit_path is None:
        raise ValueError("login requires an explicit submit method and path")
    username = os.environ.get(options.username_env)
    password = os.environ.get(options.password_env)
    if username is None or password is None:
        raise ValueError(
            f"login secrets must be set in {options.username_env} and {options.password_env}"
        )
    page = await context.new_page()
    try:
        page.set_default_timeout(_remaining_seconds(started, options) * 1000)
        await page.goto(
            options.login_url,
            wait_until="domcontentloaded",
            timeout=_remaining_seconds(started, options) * 1000,
        )
        page.set_default_timeout(_remaining_seconds(started, options) * 1000)
        await page.fill(options.username_selector or "", username)
        await page.fill(options.password_selector or "", password)
        await page.click(options.submit_selector or "")
        await page.wait_for_load_state(
            "domcontentloaded", timeout=_remaining_seconds(started, options) * 1000
        )
    finally:
        await page.close()
    return (username, password)


async def _measure_layout(
    page: Page, options: DiscoveryOptions, started: float
) -> tuple[str, ...]:
    """Measure horizontal overflow at each configured viewport.

    Measuring geometry instead of comparing screenshots keeps layout evidence
    textual: it can be diffed, reviewed and redacted, and it does not require
    storing an image of an authenticated page.
    """
    findings: list[str] = []
    original = page.viewport_size
    try:
        for width, height in options.viewports:
            _remaining_seconds(started, options)
            await page.set_viewport_size({"width": width, "height": height})
            measured = await page.evaluate(LAYOUT_SCRIPT)
            if not isinstance(measured, dict):
                continue
            overflow = int(measured.get("overflow") or 0)
            if overflow <= 0 and not measured.get("offenders"):
                continue
            offenders = measured.get("offenders")
            rendered = ",".join(str(item) for item in offenders) if offenders else "none"
            findings.append(f"{width}x{height}:overflow={overflow}:elements={rendered}")
    finally:
        if original is not None:
            await page.set_viewport_size(original)
    return tuple(sorted(findings))


async def discover_application(
    start_url: str,
    project: Path,
    options: DiscoveryOptions | None = None,
) -> DiscoveryResult:
    """Crawl safe links only; no model or generated instruction controls browser actions."""
    policy = options or DiscoveryOptions()
    start = normalize_url(start_url)
    if not is_allowed_url(start, start, policy.allowed_domains):
        raise ValueError("start URL is outside the allowed-domain policy")
    if policy.login_url is not None and not is_allowed_url(
        policy.login_url, start, policy.allowed_domains
    ):
        raise ValueError("login URL is outside the same-origin allowed-domain policy")
    artifacts = project / ".qualityproof" / "discovery"
    artifacts.mkdir(parents=True, exist_ok=True)
    for stale in (*artifacts.glob("page-*.png"), artifacts / "trace.zip"):
        stale.unlink(missing_ok=True)
    frontier = Frontier(policy.max_depth)
    frontier.add(start, 0)
    pages: dict[str, PageState] = {}
    edges: dict[str, ActionEdge] = {}
    evidence: dict[str, Evidence] = {}
    unknowns: dict[str, UnknownItem] = {}
    started = time.monotonic()
    redactor = EvidenceRedactor.from_environment()
    stop_reason = "frontier_exhausted"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=policy.headless)
        authentication_context = await browser.new_context(
            storage_state=str(policy.storage_state) if policy.storage_state is not None else None
        )
        authenticating = policy.login_url is not None
        blocked_requests: list[str] = []

        async def enforce_route(route: Route, request: Request) -> None:
            contains_secret = any(secret in request.url for secret in redactor.secrets)
            if not contains_secret and is_allowed_request(
                request, start, policy, authenticating=authenticating
            ):
                await route.continue_()
            else:
                blocked_requests.append(
                    redactor.text(f"{request.method} {request.url}")
                )
                await route.abort("blockedbyclient")

        await authentication_context.route("**/*", enforce_route)
        login_secrets = await _authenticate(authentication_context, policy, started)
        if policy.login_url is not None:
            authenticated_state = await authentication_context.storage_state()
            await authentication_context.close()
            context = await browser.new_context(storage_state=authenticated_state)
            await context.route("**/*", enforce_route)
        else:
            context = authentication_context
        if policy.save_storage_state is not None:
            # Reusing one authenticated state is the pattern that keeps a suite
            # fast and independent: no test performs a login, so no test can fail
            # because of another test's session.
            policy.save_storage_state.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(policy.save_storage_state))
        authenticating = False
        redactor = EvidenceRedactor.from_environment(additional=login_secrets)
        # Authenticated context options include cookies/storage and cannot be safely post-redacted.
        trace_enabled = (
            policy.login_url is None
            and policy.storage_state is None
            and not redactor.secrets
        )
        if trace_enabled:
            await context.tracing.start(screenshots=False, snapshots=False, sources=False)
        page = await context.new_page()
        console_errors: list[str] = []
        response_errors: list[str] = []
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )

        def record_response(response: Response) -> None:
            if response.status >= 400:
                response_errors.append(f"{response.status} {response.url}")

        page.on("response", record_response)
        page.on(
            "requestfailed",
            lambda request: response_errors.append(
                f"FAILED {request.method} {request.url}: {request.failure or 'unknown'}"
            ),
        )
        # The wall-clock guard raises from several points inside the loop, and
        # an escaping TimeoutError previously propagated out of the whole crawl,
        # discarding every page, edge and finding already collected. Exhausting
        # the budget is a reason to stop, not a reason to lose the evidence.
        try:
            while frontier:
                if time.monotonic() - started >= policy.max_runtime_seconds:
                    stop_reason = "max_runtime"
                    break
                if len(pages) >= policy.max_pages:
                    stop_reason = "max_pages"
                    break
                url, depth, source_id = frontier.pop()
                try:
                    page.set_default_timeout(_remaining_seconds(started, policy) * 1000)
                    if urlsplit(url).fragment:
                        # Force a document load for client-side routes. Moving
                        # between two fragments of one document is a same-document
                        # navigation: Playwright returns no Response, so the HTTP
                        # status would be unknowable, and the previous route's
                        # client state would leak into this one. Both matter here —
                        # status is how a privilege boundary is observed, and
                        # leaked state makes a fingerprint depend on visit order.
                        await page.goto("about:blank")
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=_remaining_seconds(started, policy) * 1000,
                    )
                    # A fixed sleep is the canonical Playwright anti-pattern: too
                    # short on a slow host, wasted time on a fast one. Wait for the
                    # network to settle instead, and treat a busy page as simply
                    # "settled enough" rather than a failure.
                    with suppress(PlaywrightTimeoutError):
                        await page.wait_for_load_state(
                            "networkidle",
                            timeout=min(2_000, _remaining_seconds(started, policy) * 1000),
                        )
                except Exception as error:
                    item = _unknown(f"navigation_failed ({type(error).__name__})", url, (url,))
                    unknowns[item.id] = item
                    continue
                final_url = normalize_url(page.url)
                if not is_allowed_url(final_url, start, policy.allowed_domains):
                    item = _unknown("external_redirect_denied", final_url, (url,))
                    unknowns[item.id] = item
                    continue
                page.set_default_timeout(_remaining_seconds(started, policy) * 1000)
                body_text = redactor.text(
                    await page.locator("body").inner_text()
                ).casefold()
                captcha = await page.locator(
                    "iframe[src*='captcha'], [class*='captcha' i], [id*='captcha' i]"
                ).count()
                if captcha or "captcha" in body_text or "verify you are human" in body_text:
                    item = _unknown("captcha_refused", final_url, (final_url,))
                    unknowns[item.id] = item
                    continue
                headings = tuple(
                    sorted(
                        {
                            redactor.text(text)
                            for text in await page.locator(
                                "h1,h2,h3,h4,h5,h6"
                            ).all_inner_texts()
                        }
                    )
                )
                raw_controls = await page.locator(CONTROL_SELECTOR).evaluate_all(CONTROL_SCRIPT)
                controls = tuple(
                    sorted(
                        {
                            json.dumps(
                                redactor.value(_describe_control(control)),
                                sort_keys=True,
                            )
                            for control in raw_controls
                            if isinstance(control, dict)
                        }
                    )
                )
                forms = tuple(
                    sorted(
                        set(
                            redactor.text(str(action))
                            for action in await page.locator("form").evaluate_all(
                                "(els) => els.map(e => e.getAttribute('action') || '')"
                            )
                        )
                    )
                )
                raw_links = await page.locator("a[href]").evaluate_all(
                    "(els) => els.map(e => ["
                    "e.href, (e.innerText || e.getAttribute('aria-label') || '')])"
                )
                links: list[str] = []
                blocked_for_page: list[UnknownItem] = []
                for raw_href, raw_label in raw_links:
                    try:
                        href = normalize_url(str(raw_href), final_url)
                    except ValueError:
                        continue
                    label = redactor.text(str(raw_label))
                    if any(secret in href for secret in redactor.secrets):
                        blocked_for_page.append(
                            _unknown(
                                "secret_bearing_navigation_denied",
                                redactor.text(href),
                                (final_url,),
                            )
                        )
                    elif not is_allowed_url(href, start, policy.allowed_domains):
                        blocked_for_page.append(
                            _unknown("external_navigation_denied", href, (final_url,))
                        )
                    elif is_destructive(label, policy.destructive_terms):
                        blocked_for_page.append(
                            _unknown("destructive_action_guard", href, (final_url,))
                        )
                    else:
                        links.append(href)
                links_tuple = tuple(sorted(set(links)))
                title = redactor.text(await page.title())
                raw_a11y = await page.evaluate(A11Y_SCRIPT)
                accessibility = tuple(
                    sorted({redactor.text(str(item)) for item in raw_a11y})
                ) if isinstance(raw_a11y, list) else ()
                layout = await _measure_layout(page, policy, started)
                status = response.status if response is not None else None
                semantic = json.dumps(
                    {
                        "route": normalize_route(final_url),
                        "title": title,
                        "headings": headings,
                        "forms": forms,
                        "controls": controls,
                        "status": status,
                        "accessibility": accessibility,
                        "layout": layout,
                    },
                    sort_keys=True,
                )
                # The identity of a page state excludes volatile facets so that a
                # permission or layout regression is reported as the *same* route
                # changing, not as one state vanishing and an unrelated one appearing.
                identity = json.dumps(
                    {
                        "route": normalize_route(final_url),
                        "title": title,
                        "headings": headings,
                        "forms": forms,
                    },
                    sort_keys=True,
                )
                state_id = _stable_id(
                    "page", f"{policy.role_name}|{normalize_route(final_url)}{identity}"
                )
                evidence_ids: list[str] = []
                observations = [
                    (
                        EvidenceKind.RESPONSE,
                        final_url,
                        f"HTTP {response.status if response is not None else 'unknown'}",
                    ),
                ]
                if not redactor.secrets:
                    screenshot = artifacts / f"{state_id}.png"
                    page.set_default_timeout(_remaining_seconds(started, policy) * 1000)
                    await page.screenshot(path=screenshot, full_page=True)
                    observations.insert(
                        0, (EvidenceKind.SCREENSHOT, screenshot.as_uri(), "Page screenshot")
                    )
                errors = sorted(
                    {
                        redactor.text(error)
                        for error in (*console_errors, *response_errors, *blocked_requests)
                    }
                )
                if errors:
                    observations.append((EvidenceKind.LOG, final_url, "; ".join(errors)))
                for kind, uri, summary in observations:
                    evidence_id = _stable_id("evidence", f"{state_id}:{kind}:{summary}")
                    evidence[evidence_id] = Evidence(
                        id=evidence_id, kind=kind, uri=uri, summary=summary
                    )
                    evidence_ids.append(evidence_id)
                pages[state_id] = PageState(
                    id=state_id,
                    url=final_url,
                    route=normalize_route(final_url),
                    title=title,
                    fingerprint=hashlib.sha256(semantic.encode()).hexdigest(),
                    headings=headings,
                    links=links_tuple,
                    forms=forms,
                    controls=controls,
                    status=status,
                    accessibility=accessibility,
                    layout=layout,
                    role=policy.role_name,
                    depth=depth,
                    evidence_ids=tuple(sorted(evidence_ids)),
                )
                for item in blocked_for_page:
                    unknowns[item.id] = item
                if source_id is not None:
                    edge_id = _stable_id("edge", f"{source_id}:{state_id}:{url}")
                    edges[edge_id] = ActionEdge(
                        id=edge_id,
                        source_state_id=source_id,
                        target_state_id=state_id,
                        action=NavigateAction(url=url),
                    )
                for href in links_tuple:
                    if len(edges) + len(frontier) >= policy.max_actions:
                        item = _unknown("max_actions", href, (state_id,))
                        unknowns[item.id] = item
                        stop_reason = "max_actions"
                        continue
                    if depth >= policy.max_depth:
                        item = _unknown("max_depth", href, (state_id,))
                        unknowns[item.id] = item
                        continue
                    frontier.add(href, depth + 1, state_id)
                console_errors.clear()
                response_errors.clear()
                blocked_requests.clear()
        except TimeoutError:
            stop_reason = "max_runtime"
        while frontier:
            pending_url, _, source_id = frontier.pop()
            item = _unknown(stop_reason, pending_url, (source_id,) if source_id else (pending_url,))
            unknowns[item.id] = item
        if trace_enabled:
            trace = artifacts / "trace.zip"
            await context.tracing.stop(path=trace)
            trace_id = _stable_id("evidence", str(trace))
            evidence[trace_id] = Evidence(
                id=trace_id,
                kind=EvidenceKind.TRACE,
                uri=trace.as_uri(),
                summary="Playwright trace",
            )
        await context.close()
        await browser.close()

    return DiscoveryResult(
        pages=tuple(sorted(pages.values(), key=lambda item: item.id)),
        edges=tuple(sorted(edges.values(), key=lambda item: item.id)),
        evidence=tuple(sorted(evidence.values(), key=lambda item: item.id)),
        unknowns=tuple(sorted(unknowns.values(), key=lambda item: item.id)),
        stop_reason=stop_reason,
    )


def persist_discovery(result: DiscoveryResult, repository: SQLiteRepository) -> None:
    record_sets = {
        "page_state": tuple((record.id, record) for record in result.pages),
        "action_edge": tuple((record.id, record) for record in result.edges),
        "evidence": tuple((record.id, record) for record in result.evidence),
        "unknown_item": tuple((record.id, record) for record in result.unknowns),
    }
    repository.replace_sets(record_sets)


def run_discovery(
    start_url: str,
    project: Path,
    options: DiscoveryOptions | None = None,
) -> DiscoveryResult:
    return asyncio.run(discover_application(start_url, project, options))


def authorization_findings(pages: Sequence[PageState]) -> tuple[str, ...]:
    """Report routes whose reachability differs between crawled roles.

    This is a within-release observation, not a verdict: two roles *should*
    differ on a protected route. It becomes a regression only when compared
    against an earlier release, which the snapshot diff does via the status
    facet.
    """
    by_route: dict[str, dict[str, int | None]] = {}
    for page in pages:
        by_route.setdefault(page.route, {})[page.role or "default"] = page.status
    findings: list[str] = []
    for route, statuses in sorted(by_route.items()):
        if len(statuses) < 2:
            continue
        distinct = {status for status in statuses.values()}
        if len(distinct) > 1:
            rendered = ",".join(
                f"{role}={statuses[role]}" for role in sorted(statuses)
            )
            findings.append(f"role_reachability_differs:{route}:{rendered}")
    return tuple(findings)


def run_role_discovery(
    start_url: str,
    project: Path,
    roles: Sequence[RoleSpec],
    options: DiscoveryOptions | None = None,
) -> DiscoveryResult:
    """Crawl once per role and merge the evidence, keeping states role-tagged."""
    if not roles:
        raise ValueError("at least one role is required")
    base = options or DiscoveryOptions()
    pages: list[PageState] = []
    edges: list[ActionEdge] = []
    evidence: list[Evidence] = []
    unknowns: list[UnknownItem] = []
    stop_reasons: list[str] = []
    for role in roles:
        policy = replace(
            base,
            role_name=role.name,
            username_env=role.username_env,
            password_env=role.password_env,
            storage_state=role.storage_state if role.storage_state else base.storage_state,
            save_storage_state=(
                base.save_storage_state.parent / f"{role.name}.json"
                if base.save_storage_state is not None
                else None
            ),
        )
        result = asyncio.run(discover_application(start_url, project, policy))
        pages.extend(result.pages)
        edges.extend(result.edges)
        evidence.extend(result.evidence)
        unknowns.extend(result.unknowns)
        stop_reasons.append(f"{role.name}:{result.stop_reason}")
    for finding in authorization_findings(tuple(pages)):
        item = UnknownItem(
            id=_stable_id("unknown", finding),
            question=f"Cross-role observation: {finding}",
            blocks=(finding.split(":", 2)[1],),
        )
        unknowns.append(item)
    return DiscoveryResult(
        pages=tuple(sorted({page.id: page for page in pages}.values(), key=lambda i: i.id)),
        edges=tuple(sorted({edge.id: edge for edge in edges}.values(), key=lambda i: i.id)),
        evidence=tuple(sorted({e.id: e for e in evidence}.values(), key=lambda i: i.id)),
        unknowns=tuple(sorted({u.id: u for u in unknowns}.values(), key=lambda i: i.id)),
        stop_reason="; ".join(stop_reasons),
    )
