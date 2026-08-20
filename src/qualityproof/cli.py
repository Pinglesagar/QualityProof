"""QualityProof command-line interface."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
import yaml

from qualityproof.audit import audit_path, build_ledger
from qualityproof.config import load_config, write_default_config
from qualityproof.coverage import compute_coverage, write_coverage_reports
from qualityproof.discovery import (
    DiscoveryOptions,
    RoleSpec,
    persist_discovery,
    run_discovery,
    run_role_discovery,
)
from qualityproof.execution import execute_tests
from qualityproof.external import ingest_manifest, read_manifest
from qualityproof.generation import generate_approved
from qualityproof.healing import propose_locator_healing, review_proposal, write_proposals
from qualityproof.jira import (
    API_TOKEN_ENV,
    EMAIL_ENV,
    TOKEN_ENV,
    JiraCloudAdapter,
    JiraPort,
    LocalJSONJiraAdapter,
    authorization_url,
    create_pkce_pair,
    sync_finding,
)
from qualityproof.models import (
    AuditEvent,
    FailedLocatorEvidence,
    JiraFinding,
    LedgerEntry,
    Requirement,
    RequirementPriority,
    ScenarioSpec,
    SemanticCandidate,
)
from qualityproof.reporting import write_html_report, write_json_report
from qualityproof.repository import SQLiteRepository
from qualityproof.scenarios import (
    DeterministicProposer,
    HTTPProposer,
    ScenarioProposer,
    assert_custom_unchanged,
    custom_tree_digest,
    list_drafts,
    load_requirements,
    plan_from_repository,
    read_scenario,
    review_scenario,
)
from qualityproof.schema import export_schemas
from qualityproof.security import reject_custom_path
from qualityproof.snapshots import (
    capture_snapshot,
    compare_snapshots,
    read_snapshot,
    write_diff_report,
)

app = typer.Typer(
    name="qualityproof",
    help="Build traceable quality evidence locally.",
    no_args_is_help=True,
)
jira_app = typer.Typer(help="Synchronize findings with Jira; dry-run by default.")
healing_app = typer.Typer(help="Create and review governed locator proposals.")
snapshot_app = typer.Typer(help="Capture immutable named evidence snapshots.")
app.add_typer(jira_app, name="jira")
app.add_typer(healing_app, name="heal")
app.add_typer(snapshot_app, name="snapshot")
ProjectOption = Annotated[
    Path,
    typer.Option("--project", "-p", help="Project directory.", file_okay=False, resolve_path=True),
]


def _edit_yaml(initial: str) -> str | None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise RuntimeError("$VISUAL or $EDITOR is required for interactive editing")
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w+", encoding="utf-8") as temporary:
        temporary.write(initial)
        temporary.flush()
        completed = subprocess.run(
            [*shlex.split(editor), temporary.name],
            check=False,
        )
        if completed.returncode:
            return None
        temporary.seek(0)
        return temporary.read()


def _parse_shard(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    index, _, total = value.partition("/")
    if not index.isdigit() or not total.isdigit():
        raise ValueError(f"shard must be INDEX/TOTAL, received {value!r}")
    return int(index), int(total)


def _parse_viewport(value: str) -> tuple[int, int]:
    width, _, height = value.lower().partition("x")
    if not width.isdigit() or not height.isdigit():
        raise ValueError(f"viewport must be WIDTHxHEIGHT, received {value!r}")
    return int(width), int(height)


def _not_implemented(capability: str) -> None:
    typer.echo(f"Not implemented: {capability} is outside the foundation release.")
    raise typer.Exit(code=2)


@app.command()
def init(project: ProjectOption = Path(".")) -> None:
    """Initialize local configuration, schemas, and SQLite storage."""
    project.mkdir(parents=True, exist_ok=True)
    config_path = write_default_config(project)
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    schemas = export_schemas(project / config.schema_directory)
    typer.echo(f"Created {config_path}")
    typer.echo(f"Initialized {repository.database_path}")
    typer.echo(f"Exported {len(schemas)} versioned schemas")


@app.command()
def audit(
    path: Annotated[
        Path,
        typer.Argument(
            help="Python test file or directory to audit.",
            exists=True,
            resolve_path=True,
        ),
    ],
    project: ProjectOption = Path("."),
) -> None:
    """Statically audit Python pytest/Playwright source into the local ledger."""
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    try:
        ledger = build_ledger(
            audit_path(path),
            project=project,
            repository=repository,
        )
    except SyntaxError as error:
        typer.echo(f"Unable to parse {error.filename}:{error.lineno}: {error.msg}", err=True)
        raise typer.Exit(code=1) from error
    # Auditing a directory supersedes any earlier audit of a path inside it.
    released = repository.release_nested_scopes(str(path.resolve()), "ledger")
    repository.replace_manifested_set(
        str(path.resolve()),
        "ledger",
        ((entry.id, entry) for entry in ledger),
    )
    if released:
        typer.echo(f"Superseded {len(released)} narrower audit scope(s).")
    repository.append_event(
        AuditEvent(
            id=str(uuid4()),
            event_type="source_audit_completed",
            details={"path": str(path), "tests": len(ledger)},
        )
    )
    counts = {status: sum(entry.status.value == status for entry in ledger) for status in (
        "VERIFIED",
        "PARTIAL",
        "UNKNOWN",
    )}
    typer.echo(
        f"Audited {len(ledger)} tests: {counts['VERIFIED']} verified, "
        f"{counts['PARTIAL']} partial, {counts['UNKNOWN']} unknown."
    )


@app.command()
def discover(
    url: Annotated[str, typer.Argument(help="Authenticated application URL to discover.")],
    project: ProjectOption = Path("."),
    max_pages: Annotated[int, typer.Option(min=1)] = 50,
    max_depth: Annotated[int, typer.Option(min=0)] = 3,
    max_actions: Annotated[int, typer.Option(min=1)] = 100,
    max_runtime: Annotated[float, typer.Option("--max-runtime", min=0.1)] = 120.0,
    allowed_domain: Annotated[list[str] | None, typer.Option("--allowed-domain")] = None,
    destructive_term: Annotated[list[str] | None, typer.Option("--destructive-term")] = None,
    deny_route: Annotated[list[str] | None, typer.Option("--deny-route")] = None,
    storage_state: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    login_url: Annotated[str | None, typer.Option()] = None,
    username_selector: Annotated[str | None, typer.Option()] = None,
    password_selector: Annotated[str | None, typer.Option()] = None,
    submit_selector: Annotated[str | None, typer.Option()] = None,
    login_submit_method: Annotated[str | None, typer.Option()] = None,
    login_submit_path: Annotated[str | None, typer.Option()] = None,
    username_env: Annotated[str, typer.Option()] = "QUALITYPROOF_USERNAME",
    password_env: Annotated[str, typer.Option()] = "QUALITYPROOF_PASSWORD",
    role: Annotated[
        list[str] | None,
        typer.Option(
            "--role",
            help=(
                "Repeatable identity to crawl as, either 'name=storage-state.json' or "
                "'name:USERNAME_ENV:PASSWORD_ENV'. Crawling every privilege level is "
                "what makes an authorization boundary observable."
            ),
        ),
    ] = None,
    viewport: Annotated[
        list[str] | None,
        typer.Option("--viewport", help="Repeatable WIDTHxHEIGHT measured for layout overflow."),
    ] = None,
    seed_route: Annotated[
        list[str] | None,
        typer.Option(
            "--seed-route",
            help=(
                "Repeatable route to probe in addition to followed links. A crawl "
                "cannot find what is never linked, and an administrative surface is "
                "routinely unlinked for the roles that must not reach it. Seeds obey "
                "every other policy."
            ),
        ),
    ] = None,
    save_storage_state: Annotated[
        Path | None,
        typer.Option(
            "--save-storage-state",
            dir_okay=False,
            help=(
                "Persist the authenticated session so the generated suite can reuse it. "
                "Contains live session credentials; never commit it."
            ),
        ),
    ] = None,
    headed: Annotated[bool, typer.Option("--headed")] = False,
) -> None:
    """Safely discover same-origin application states using deterministic BFS."""
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    defaults = DiscoveryOptions()
    try:
        roles = tuple(RoleSpec.parse(item) for item in (role or ()))
        viewports = tuple(_parse_viewport(item) for item in (viewport or ())) or defaults.viewports
        options = DiscoveryOptions(
            max_pages=max_pages,
            max_depth=max_depth,
            max_actions=max_actions,
            max_runtime_seconds=max_runtime,
            allowed_domains=tuple(allowed_domain or ()),
            destructive_terms=tuple(destructive_term or defaults.destructive_terms),
            denied_routes=tuple(deny_route or defaults.denied_routes),
            storage_state=storage_state,
            save_storage_state=save_storage_state,
            login_url=login_url,
            username_selector=username_selector,
            password_selector=password_selector,
            submit_selector=submit_selector,
            login_submit_method=login_submit_method,
            login_submit_path=login_submit_path,
            username_env=username_env,
            password_env=password_env,
            viewports=viewports,
            seed_routes=tuple(seed_route or ()),
            headless=not headed,
        )
        result = (
            run_role_discovery(url, project, roles, options)
            if roles
            else run_discovery(url, project, options)
        )
    except (ValueError, RuntimeError) as error:
        typer.echo(f"Discovery refused: {error}", err=True)
        raise typer.Exit(code=1) from error
    persist_discovery(result, repository)
    repository.append_event(
        AuditEvent(
            id=str(uuid4()),
            event_type="discovery_completed",
            details={
                "pages": len(result.pages),
                "edges": len(result.edges),
                "unknowns": len(result.unknowns),
                "stop_reason": result.stop_reason,
            },
        )
    )
    typer.echo(
        f"Discovered {len(result.pages)} pages and {len(result.edges)} actions; "
        f"{len(result.unknowns)} blocked/unknown ({result.stop_reason})."
    )


@app.command()
def plan(
    project: ProjectOption = Path("."),
    requirements: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, resolve_path=True)
    ] = None,
    provider: Annotated[str, typer.Option(help="deterministic or http")] = "deterministic",
    endpoint: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str, typer.Option()] = "qualityproof",
    timeout: Annotated[float, typer.Option(min=0.1)] = 15.0,
    cassette: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    replay: Annotated[bool, typer.Option()] = False,
    scenario_role: Annotated[
        str | None,
        typer.Option(
            "--scenario-role",
            help=(
                "Mine journeys observed as this identity only. A generated suite runs as "
                "one identity, so mixing roles would produce assertions that cannot all hold."
            ),
        ),
    ] = None,
) -> None:
    """Mine graph journeys and write generated scenario drafts."""
    before = custom_tree_digest(project)
    if cassette is not None:
        reject_custom_path(project, cassette, "provider cassette")
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    if provider == "deterministic":
        proposer: ScenarioProposer = DeterministicProposer()
    elif provider == "http":
        if endpoint is None:
            raise typer.BadParameter("--endpoint is required for the HTTP provider")
        proposer = HTTPProposer(
            endpoint,
            model,
            api_key=os.environ.get("QUALITYPROOF_LLM_API_KEY"),
            timeout_seconds=timeout,
            cassette=cassette,
            replay=replay,
        )
    else:
        raise typer.BadParameter("provider must be deterministic or http")
    try:
        paths = plan_from_repository(
            project, repository, requirements, proposer, role=scenario_role
        )
        assert_custom_unchanged(project, before)
    except (ValueError, RuntimeError) as error:
        typer.echo(f"Planning failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Wrote {len(paths)} generated drafts.")


@app.command()
def review(
    project: ProjectOption = Path("."),
    scenario: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    decision: Annotated[str | None, typer.Option(help="approve, edit, or reject")] = None,
    actor: Annotated[str, typer.Option()] = "human",
    reason: Annotated[str | None, typer.Option()] = None,
    edited: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
) -> None:
    """Interactively or noninteractively approve, edit, or reject drafts."""
    before = custom_tree_digest(project)
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    targets = (scenario,) if scenario is not None else list_drafts(project)
    if not targets:
        typer.echo("No scenario drafts to review.")
        return
    for target in targets:
        selected_decision = decision or typer.prompt(
            f"{target.name}: approve, edit, or reject"
        ).strip()
        if selected_decision not in {"approve", "edit", "reject"}:
            raise typer.BadParameter("decision must be approve, edit, or reject")
        selected_reason = reason or typer.prompt("Reason").strip()
        replacement = read_scenario(edited) if edited is not None else None
        if selected_decision == "edit" and replacement is None:
            original_text = target.read_text(encoding="utf-8")
            edited_text = _edit_yaml(original_text)
            if edited_text is None:
                raise typer.Abort()
            replacement = ScenarioSpec.model_validate(yaml.safe_load(edited_text))
        review_scenario(
            project,
            repository,
            target,
            selected_decision,
            actor,
            selected_reason,
            replacement,
        )
        typer.echo(f"{selected_decision.title()}d {target.stem}.")
    assert_custom_unchanged(project, before)


@app.command()
def generate(
    project: ProjectOption = Path("."),
    validate: Annotated[bool, typer.Option("--validate/--no-validate")] = True,
    language: Annotated[
        str,
        typer.Option(
            "--language",
            help="python, typescript, or both. One reviewed scenario, either runner.",
        ),
    ] = "python",
) -> None:
    """Generate deterministic Playwright tests from approved YAML."""
    before = custom_tree_digest(project)
    languages = ("python", "typescript") if language == "both" else (language,)
    if any(item not in {"python", "typescript"} for item in languages):
        raise typer.BadParameter("--language must be python, typescript, or both")
    try:
        config = load_config(project)
        repository = SQLiteRepository(project / config.database_path)
        repository.initialize()
        paths: tuple[Path, ...] = ()
        for item in languages:
            paths += generate_approved(
                project, validate=validate, repository=repository, language=item
            )
        approved_sources = tuple(
            sorted((project / "scenarios" / "generated" / "approved").glob("*.yaml"))
        )
        approved_scenarios = tuple(read_scenario(path) for path in approved_sources)
        repository.replace_manifested_set(
            "generated-approved-scenarios",
            "scenario",
            ((scenario.id, scenario) for scenario in approved_scenarios),
        )
        assert_custom_unchanged(project, before)
    except (ValueError, RuntimeError, SyntaxError) as error:
        typer.echo(f"Generation failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Generated and validated {len(paths)} tests.")


@app.command("test")
def test_command(
    project: ProjectOption = Path("."),
    pytest_arg: Annotated[list[str] | None, typer.Option("--pytest-arg")] = None,
    shard: Annotated[
        str | None,
        typer.Option(
            "--shard",
            help="Run one deterministic slice as INDEX/TOTAL, for CI fan-out.",
        ),
    ] = None,
) -> None:
    """Execute generated and custom tests with failure evidence."""
    before = custom_tree_digest(project)
    try:
        config = load_config(project)
        repository = SQLiteRepository(project / config.database_path)
        repository.initialize()
        result = execute_tests(
            project,
            tuple(pytest_arg or ()),
            repository=repository,
            shard=_parse_shard(shard),
        )
        assert_custom_unchanged(project, before)
    except (ValueError, RuntimeError) as error:
        typer.echo(f"Execution failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Run {result.run_id} {result.status}; result: {result.result_path}; "
        f"evidence: {len(result.evidence_paths)}."
    )
    if result.exit_code:
        raise typer.Exit(code=result.exit_code)


@app.command()
def ingest(
    manifest: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="External run manifest, e.g. from @qualityproof/playwright.",
        ),
    ],
    project: ProjectOption = Path("."),
) -> None:
    """Merge evidence from another runner into this project's ledger."""
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    try:
        # Containment applies to foreign evidence too: an external manifest is
        # untrusted input and reaches the same trust rules as local source.
        loaded = read_manifest(manifest, project)
        entries = ingest_manifest(loaded, project, repository)
    except (ValueError, OSError) as error:
        typer.echo(f"Ingest refused: {error}", err=True)
        raise typer.Exit(code=1) from error
    counts = Counter(entry.status.value for entry in entries)
    typer.echo(
        f"Ingested {len(entries)} {loaded.framework.value} tests from run {loaded.run_id}: "
        f"{counts['VERIFIED']} verified, {counts['PARTIAL']} partial, {counts['UNKNOWN']} unknown."
    )


requirements_app = typer.Typer(help="Manage the authoritative requirement registry.")
app.add_typer(requirements_app, name="requirements")


@requirements_app.command("import")
def requirements_import(
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            resolve_path=True,
            help="YAML/JSON requirements or seed manifest, or a Markdown heading list.",
        ),
    ],
    project: ProjectOption = Path("."),
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help=(
                "Registry partition to replace. Re-importing a scope replaces only "
                "its own entries."
            ),
        ),
    ] = "imported-requirements",
) -> None:
    """Register requirements so coverage and provenance have an authority.

    Without a registry, a test can cite any identifier it likes as long as some
    file it chose contains that identifier. The registry is what makes an
    unregistered identifier an orphan rather than silent coverage.
    """
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    try:
        requirements = load_requirements(source)
    except (ValueError, OSError) as error:
        typer.echo(f"Import refused: {error}", err=True)
        raise typer.Exit(code=1) from error
    if not requirements:
        typer.echo(f"No requirements found in {source}", err=True)
        raise typer.Exit(code=1)
    repository.replace_manifested_set(
        scope,
        "requirement",
        ((item.id, item) for item in requirements),
    )
    repository.append_event(
        AuditEvent(
            id=str(uuid4()),
            event_type="requirements_imported",
            details={"scope": scope, "count": len(requirements), "source": source.name},
        )
    )
    typer.echo(
        f"Registered {len(requirements)} requirements in scope '{scope}': "
        f"{', '.join(item.id for item in requirements[:5])}"
        f"{' ...' if len(requirements) > 5 else ''}"
    )


@requirements_app.command("list")
def requirements_list(project: ProjectOption = Path(".")) -> None:
    """Show the registered requirements."""
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    requirements = repository.list("requirement", Requirement)
    if not requirements:
        typer.echo("No requirements registered. Import some with `requirements import`.")
        return
    for item in requirements:
        typer.echo(f"{item.id}\t{item.title}")
    typer.echo(f"\n{len(requirements)} registered.")


@app.command()
def coverage(
    project: ProjectOption = Path("."),
    fail_under: Annotated[
        float,
        typer.Option(
            "--fail-under",
            min=0.0,
            max=1.0,
            help="Exit non-zero when the verified share of requirements falls below this.",
        ),
    ] = 0.0,
    fail_on_uncovered: Annotated[
        bool,
        typer.Option(
            "--fail-on-uncovered",
            help="Exit non-zero if any registered requirement has no test referencing it.",
        ),
    ] = False,
    fail_on_orphans: Annotated[
        bool,
        typer.Option(
            "--fail-on-orphans",
            help="Exit non-zero if any test cites a requirement the registry does not contain.",
        ),
    ] = False,
    require_priority: Annotated[
        list[str] | None,
        typer.Option(
            "--require-priority",
            help=(
                "Repeatable. Exit non-zero if any requirement at this priority is not "
                "verified. A percentage hides risk; this asks whether anything critical "
                "is unproven."
            ),
        ),
    ] = None,
) -> None:
    """Report which requirements are NOT proven, and gate CI on it.

    The ledger answers "what is covered". This answers the question a release
    review actually asks, which is the complement.
    """
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    requirements = repository.list("requirement", Requirement)
    if not requirements:
        typer.echo(
            "No requirements registered; coverage is undefined. "
            "Import a requirement source with `qualityproof requirements import`.",
            err=True,
        )
        raise typer.Exit(code=1)
    # Re-classify against the live registry rather than trusting stored statuses.
    # A ledger row records what was true when `audit` ran; editing a requirement or
    # importing a registry afterwards would otherwise leave a stale VERIFIED in
    # place and keep the gate green over evidence that no longer holds.
    stored = repository.list("ledger", LedgerEntry)
    reclassified = build_ledger(
        tuple(entry.test for entry in stored),
        project=project,
        repository=repository,
    )
    drifted = tuple(
        sorted(
            entry.id
            for entry, fresh in zip(stored, reclassified, strict=True)
            if entry.status is not fresh.status
        )
    )
    report = compute_coverage(requirements, reclassified)
    json_path, markdown_path = write_coverage_reports(
        report, project / config.report_directory
    )
    summary = report.summary()
    typer.echo(
        f"Requirements: {summary['requirements']}; verified {summary['verified']}; "
        f"partial {summary['partial']}; uncovered {summary['uncovered']}; "
        f"orphan links {summary['orphan_link_count']}; "
        f"untraced tests {summary['untraced_test_count']}."
    )
    for band, counts in report.by_priority().items():
        typer.echo(f"  {band}: {counts['verified']}/{counts['total']} verified")
    if report.uncovered:
        typer.echo(f"Uncovered: {', '.join(report.uncovered)}")
    if report.orphan_links:
        typer.echo(f"Orphan links: {', '.join(report.orphan_links)}")
    if drifted:
        typer.echo(
            f"{len(drifted)} ledger row(s) no longer classify as recorded; "
            f"re-run `audit` to refresh them: {', '.join(drifted[:3])}"
            f"{' ...' if len(drifted) > 3 else ''}"
        )
    typer.echo(f"Reports: {json_path.name}, {markdown_path.name}")

    failures: list[str] = []
    if report.verified_ratio < fail_under:
        failures.append(
            f"verified share {report.verified_ratio:.3f} is below the required {fail_under:.3f}"
        )
    if fail_on_uncovered and report.uncovered:
        failures.append(f"{len(report.uncovered)} requirement(s) have no referencing test")
    if fail_on_orphans and report.orphan_links:
        failures.append(f"{len(report.orphan_links)} orphan requirement link(s)")
    for raw in require_priority or ():
        try:
            band = RequirementPriority(raw.strip().upper())
        except ValueError as error:
            raise typer.BadParameter(
                f"--require-priority must be one of "
                f"{', '.join(item.value for item in RequirementPriority)}"
            ) from error
        unproven = report.unproven_at(band)
        if unproven:
            failures.append(
                f"{len(unproven)} {band.value} requirement(s) not verified: "
                f"{', '.join(unproven)}"
            )
    if failures:
        for failure in failures:
            typer.echo(f"Coverage gate failed: {failure}", err=True)
        raise typer.Exit(code=1)


@app.command()
def report(project: ProjectOption = Path(".")) -> None:
    """Produce static JSON and HTML traceability ledger reports."""
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    entries = repository.list("ledger", LedgerEntry)
    destination = project / config.report_directory
    json_path = write_json_report(entries, destination / "ledger.json")
    html_path = write_html_report(entries, destination / "ledger.html")
    repository.append_event(
        AuditEvent(
            id=str(uuid4()),
            event_type="report_generated",
            details={"entries": len(entries), "directory": str(destination)},
        )
    )
    typer.echo(f"Wrote {json_path}")
    typer.echo(f"Wrote {html_path}")


@app.command()
def doctor(project: ProjectOption = Path(".")) -> None:
    """Validate local configuration and storage access."""
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    typer.echo("Configuration is valid; SQLite storage is accessible.")


@jira_app.command("config")
def jira_config() -> None:
    """Show non-secret Jira configuration and credential environment names."""
    typer.echo("Default adapter: mock")
    typer.echo(f"Cloud API token environment: {API_TOKEN_ENV} together with {EMAIL_ENV}")
    typer.echo(f"Cloud OAuth bearer environment: {TOKEN_ENV}")
    typer.echo(
        "An Atlassian API token authenticates over HTTP Basic as email:token; a "
        "bearer token is an OAuth 3LO access token. Either is accepted, and the "
        "API token is preferred when both are present."
    )
    typer.echo("No Jira credentials are read from or written to qualityproof.toml.")


@jira_app.command("auth-url")
def jira_auth_url(
    client_id: Annotated[str, typer.Option(envvar="QUALITYPROOF_JIRA_CLIENT_ID")],
    redirect_uri: Annotated[str, typer.Option()],
    scope: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    """Create an Atlassian 3LO URL with fresh state and PKCE values."""
    verifier, challenge = create_pkce_pair()
    state = secrets.token_urlsafe(32)
    scopes = tuple(scope or ["read:jira-work", "write:jira-work", "offline_access"])
    typer.echo(
        json.dumps(
            {
                "authorization_url": authorization_url(
                    client_id,
                    redirect_uri,
                    scopes,
                    state=state,
                    code_challenge=challenge,
                ),
                "state": state,
                "code_verifier": verifier,
                "notice": (
                    "Keep state/verifier ephemeral; verify returned state; "
                    "do not commit them."
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )


@jira_app.command("sync")
def jira_sync(
    finding: Annotated[Path, typer.Argument(exists=True, dir_okay=False, resolve_path=True)],
    project_key: Annotated[str, typer.Option()],
    project: ProjectOption = Path("."),
    adapter: Annotated[str, typer.Option(help="mock or cloud")] = "mock",
    base_url: Annotated[
        str | None,
        typer.Option(help="Jira API base, e.g. api.atlassian.com/ex/jira/<cloud-id>."),
    ] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Perform the Jira write.")] = False,
) -> None:
    """Create/update one idempotent finding; prints a dry-run unless --apply."""
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    parsed = JiraFinding.model_validate_json(finding.read_text(encoding="utf-8"))
    port: JiraPort
    if adapter == "mock":
        port = LocalJSONJiraAdapter(project / ".qualityproof" / "jira" / "issues.json")
    elif adapter == "cloud":
        if base_url is None:
            raise typer.BadParameter("--base-url is required for cloud")
        port = JiraCloudAdapter(base_url)
    else:
        raise typer.BadParameter("adapter must be mock or cloud")
    result = sync_finding(parsed, project_key, port, repository, dry_run=not apply)
    typer.echo(result.model_dump_json(indent=2))


@healing_app.command("propose")
def healing_propose(
    evidence: Annotated[Path, typer.Argument(exists=True, dir_okay=False, resolve_path=True)],
    project: ProjectOption = Path("."),
    limit: Annotated[int, typer.Option(min=1, max=10)] = 3,
) -> None:
    """Rank bounded same-contract candidates and write immutable proposals."""
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise typer.BadParameter("evidence must be a JSON object")
    failed = FailedLocatorEvidence.model_validate(payload.get("failed"))
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise typer.BadParameter("candidates must be a JSON array")
    candidates = tuple(SemanticCandidate.model_validate(item) for item in raw_candidates)
    proposals = propose_locator_healing(failed, candidates, limit=limit)
    paths = write_proposals(project, proposals)
    typer.echo(f"Wrote {len(paths)} proposal(s); no tests were changed.")


@healing_app.command("review")
def healing_review(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False, resolve_path=True)],
    decision: Annotated[str, typer.Option(help="approve or reject")],
    reason: Annotated[str, typer.Option()],
    actor: Annotated[str, typer.Option()] = "human",
    project: ProjectOption = Path("."),
) -> None:
    """Record review; approval emits a patch artifact without applying it."""
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    review = review_proposal(project, repository, proposal, decision, actor, reason)
    typer.echo(review.model_dump_json(indent=2))


@snapshot_app.command("create")
def snapshot_create(
    name: Annotated[str, typer.Argument()],
    project: ProjectOption = Path("."),
    application: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Optional application JSON.")
    ] = None,
) -> None:
    """Capture an immutable normalized evidence/application snapshot."""
    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    app_data: dict[str, object] | None = None
    if application:
        raw = json.loads(application.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise typer.BadParameter("application JSON must be an object")
        app_data = dict(raw)
    _, path = capture_snapshot(name, project, repository, app_data)
    typer.echo(f"Wrote immutable snapshot {path}")


@snapshot_app.command("list")
def snapshot_list(project: ProjectOption = Path(".")) -> None:
    """List local immutable snapshot names."""
    directory = project / ".qualityproof" / "snapshots"
    for path in sorted(directory.glob("*.json")):
        typer.echo(path.stem)


@app.command("diff")
def diff_command(
    before: Annotated[str, typer.Argument(help="Snapshot name or absolute JSON path.")],
    after: Annotated[str, typer.Argument(help="Snapshot name or absolute JSON path.")],
    project: ProjectOption = Path("."),
    format_name: Annotated[str, typer.Option("--format", help="json or markdown")] = "json",
    output: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Compare requirements, application shape, evidence, verdicts, and coverage."""
    comparison = compare_snapshots(
        read_snapshot(project, before),
        read_snapshot(project, after),
    )
    extension = "md" if format_name == "markdown" else "json"
    filename = f"diff-{comparison.before}-{comparison.after}.{extension}"
    destination = output or (project / ".qualityproof" / "reports" / filename)
    path = write_diff_report(comparison, destination, format_name)
    typer.echo(f"Wrote {path}")


if __name__ == "__main__":
    app()
