"""Pytest-Playwright fixtures shared by every generated QualityProof suite.

This module is the single place where run-time behaviour is decided, which is
the point: a generated test contains only intent (navigate here, assert that),
while determinism, authentication, artifact policy and timeouts are supplied
around it. Editing one fixture retunes the whole suite, and a generated test
never grows an explicit wait or an environment-specific URL.

Import it from a project ``conftest.py``::

    from qualityproof.fixtures import *  # noqa: F403
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import APIRequestContext, Browser, BrowserContext, Page, Playwright, expect

from qualityproof.config import ProjectConfig, load_config
from qualityproof.security import ArtifactPolicy

__all__ = [
    "api_request_context",
    "artifact_policy",
    "base_url",
    "browser_context_args",
    "context",
    "evidence_directory",
    "page",
    "qualityproof_config",
    "qualityproof_project",
    "qualityproof_storage_state",
    "worker_name",
]


@pytest.fixture(scope="session")
def qualityproof_project() -> Path:
    """Project root, taken from the environment so runners stay path-agnostic."""
    return Path(os.environ.get("QUALITYPROOF_PROJECT", ".")).resolve()


@pytest.fixture(scope="session")
def qualityproof_config(qualityproof_project: Path) -> ProjectConfig:
    return load_config(qualityproof_project)


@pytest.fixture(scope="session")
def base_url(qualityproof_config: ProjectConfig) -> str | None:
    """Origin under test.

    Deliberately advisory: generated tests navigate to the absolute URL recorded
    in their approved scenario and merely *assert* that it matches this origin.
    Configuration can therefore select an environment but cannot silently point a
    reviewed scenario at a different host.
    """
    return qualityproof_config.base_url or os.environ.get("QUALITYPROOF_BASE_URL")


@pytest.fixture(scope="session")
def worker_name() -> str:
    """Identify the xdist worker, or ``master`` when running single-process."""
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


@pytest.fixture(scope="session")
def artifact_policy() -> ArtifactPolicy:
    """Resolve trace and screenshot capture from the environment's secret exposure."""
    return ArtifactPolicy.from_environment()


@pytest.fixture(scope="session")
def evidence_directory(qualityproof_project: Path, worker_name: str) -> Path:
    """Per-worker artifact directory, so parallel workers never race on a path."""
    directory = qualityproof_project / ".qualityproof" / "runs" / "current" / worker_name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture(scope="session")
def qualityproof_storage_state() -> Path | None:
    """Reuse an already-authenticated session instead of logging in per test.

    The path is read from the environment and never from project configuration,
    because a storage state file contains live session credentials.
    """
    raw = os.environ.get("QUALITYPROOF_STORAGE_STATE")
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_file():
        raise pytest.UsageError(f"QUALITYPROOF_STORAGE_STATE does not exist: {candidate}")
    return candidate


@pytest.fixture
def browser_context_args(
    browser_context_args: dict[str, Any],
    qualityproof_config: ProjectConfig,
    qualityproof_storage_state: Path | None,
) -> dict[str, Any]:
    """Pin every context setting that could otherwise vary between machines.

    Locale, timezone and viewport are the usual causes of a suite that passes
    locally and fails in CI. Fixing them here makes a failure mean the
    application changed, not that the host did.
    """
    arguments = {
        **browser_context_args,
        "locale": qualityproof_config.locale,
        "timezone_id": qualityproof_config.timezone_id,
        "viewport": {"width": 1280, "height": 800},
        "ignore_https_errors": False,
    }
    if qualityproof_storage_state is not None:
        arguments["storage_state"] = str(qualityproof_storage_state)
    return arguments


@pytest.fixture
def context(
    browser: Browser,
    browser_context_args: dict[str, Any],
    artifact_policy: ArtifactPolicy,
    evidence_directory: Path,
    qualityproof_config: ProjectConfig,
    request: pytest.FixtureRequest,
) -> Iterator[BrowserContext]:
    """Provide an isolated context, tracing only when the policy permits it."""
    browser_context = browser.new_context(**browser_context_args)
    browser_context.set_default_timeout(qualityproof_config.action_timeout_ms)
    browser_context.set_default_navigation_timeout(qualityproof_config.navigation_timeout_ms)
    tracing = artifact_policy.traces_enabled
    if tracing:
        browser_context.tracing.start(screenshots=True, snapshots=True, sources=False)
    try:
        yield browser_context
    finally:
        if tracing:
            failed = getattr(request.node, "qualityproof_failed", False)
            if failed or artifact_policy.retain_always:
                target = evidence_directory / f"{request.node.name}-trace.zip"
                browser_context.tracing.stop(path=target)
            else:
                browser_context.tracing.stop()
        browser_context.close()


@pytest.fixture
def page(context: BrowserContext, qualityproof_config: ProjectConfig) -> Iterator[Page]:
    """A page with the configured web-first assertion timeout already applied."""
    expect.set_options(timeout=float(qualityproof_config.expect_timeout_ms))
    new_page = context.new_page()
    try:
        yield new_page
    finally:
        new_page.close()


@pytest.fixture
def api_request_context(
    playwright: Playwright,
    base_url: str | None,
    qualityproof_config: ProjectConfig,
) -> Iterator[APIRequestContext]:
    """An origin-bound HTTP client for asserting API contracts alongside the UI.

    Bound to ``base_url`` so a path-only assertion cannot reach another host.
    """
    if base_url is None:
        pytest.skip("api assertions require QUALITYPROOF_BASE_URL or config base_url")
    request_context = playwright.request.new_context(
        base_url=base_url,
        timeout=float(qualityproof_config.action_timeout_ms),
    )
    try:
        yield request_context
    finally:
        request_context.dispose()


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item: pytest.Item) -> Iterator[None]:
    """Record failure on the item so the context fixture can retain its trace."""
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    if report.when == "call" and report.failed:
        item.qualityproof_failed = True  # type: ignore[attr-defined]


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record retried tests, because pytest's JUnit writer does not.

    pytest-rerunfailures signals a retry by setting ``report.outcome`` to
    ``"rerun"``, and that outcome never reaches the JUnit XML: a test that fails
    and then passes is written as a single clean ``<testcase>``. Reading the XML
    alone therefore reports a retried test as a straight pass — exactly the
    collapse that makes an unstable suite look healthy. The only reliable source
    is this hook, so retries are appended to a sidecar log that the runner merges
    when it derives verdicts.
    """
    # Compared as a string because pytest types `outcome` as
    # passed/failed/skipped; pytest-rerunfailures sets "rerun" at run time, which
    # the published type does not admit.
    if str(report.outcome) != "rerun":
        return
    destination = os.environ.get("QUALITYPROOF_RERUN_LOG")
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps({"nodeid": report.nodeid, "when": report.when}, sort_keys=True)
    # Append-only and one JSON object per line, so parallel xdist workers can
    # write concurrently without coordinating.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{record}\n")
