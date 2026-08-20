"""Immutable evidence snapshots and deterministic release comparison."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from qualityproof.discovery import normalize_route
from qualityproof.models import (
    CoverageCounts,
    EvidenceSnapshot,
    EvidenceSnapshotDiff,
    LedgerEntry,
    PageState,
    Requirement,
    RouteRetarget,
    ScenarioSpec,
    SnapshotSectionDiff,
    UnknownItem,
    Verdict,
)
from qualityproof.repository import SQLiteRepository


class _HasId(Protocol):
    @property
    def id(self) -> str: ...


def _ids(values: Iterable[_HasId]) -> tuple[str, ...]:
    return tuple(sorted(str(value.id) for value in values))


def capture_snapshot(
    name: str,
    project: Path,
    repository: SQLiteRepository,
    application: dict[str, object] | None = None,
) -> tuple[EvidenceSnapshot, Path]:
    requirements = repository.list("requirement", Requirement)
    pages = repository.list("page_state", PageState)
    scenarios = repository.list("scenario", ScenarioSpec)
    verdicts = repository.list("verdict", Verdict)
    unknowns = repository.list("unknown_item", UnknownItem)
    ledger = repository.list("ledger", LedgerEntry)
    requirement_ids = tuple(
        sorted({item.id for item in requirements} | {req for item in ledger for req in (
            item.test.metadata.requirement_ids if item.test.metadata else ()
        )})
    )
    routes = tuple(sorted({page.route for page in pages}))
    ordered_pages = sorted(pages, key=lambda item: (item.route, item.id))
    page_fingerprints = {f"{page.route}#{page.id}": page.fingerprint for page in ordered_pages}
    page_links = {
        f"{page.route}#{page.id}": _linked_routes(page) for page in ordered_pages
    }
    page_facets = {f"{page.route}#{page.id}": page.facet_digests() for page in ordered_pages}
    page_roles = {f"{page.route}#{page.id}": page.role or "default" for page in ordered_pages}
    scenario_ids = _ids(scenarios)
    verdict_map = {
        verdict.assertion_id: verdict.status.value
        for verdict in sorted(verdicts, key=lambda item: item.assertion_id)
    }
    unknown_ids = _ids(tuple(item for item in unknowns if not item.resolved))
    snapshot = EvidenceSnapshot(
        name=name,
        requirements=requirement_ids,
        routes=routes,
        page_fingerprints=page_fingerprints,
        page_links=page_links,
        page_facets=page_facets,
        page_roles=page_roles,
        scenarios=scenario_ids,
        verdicts=verdict_map,
        unknowns=unknown_ids,
        coverage=CoverageCounts(
            requirements=len(requirement_ids),
            routes=len(routes),
            pages=len(pages),
            scenarios=len(scenario_ids),
            verdicts=len(verdict_map),
            unknowns=len(unknown_ids),
        ),
        application=application or {},
    )
    path = project / ".qualityproof" / "snapshots" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError(f"snapshot name already exists and is immutable: {name}")
    path.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
    repository.put("snapshot", name, snapshot)
    return snapshot, path


def read_snapshot(project: Path, name_or_path: str) -> EvidenceSnapshot:
    candidate = Path(name_or_path)
    path = candidate if candidate.is_absolute() else project / ".qualityproof" / "snapshots" / (
        name_or_path if name_or_path.endswith(".json") else f"{name_or_path}.json"
    )
    return EvidenceSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def _linked_routes(page: PageState) -> tuple[str, ...]:
    """Normalize a page's outbound links to routes, dropping unparseable hrefs."""
    routes: set[str] = set()
    for link in page.links:
        try:
            routes.add(normalize_route(link))
        except ValueError:
            continue
    return tuple(sorted(routes))


def _route_retargets(
    before: EvidenceSnapshot,
    after: EvidenceSnapshot,
    routes: SnapshotSectionDiff,
) -> tuple[RouteRetarget, ...]:
    """Pair a removed route with an added route reached from the same referrer.

    A referrer whose outbound link moved from one route to another describes a
    single change, not two independent ones. The rule is deliberately narrow:
    both sides must be anchored to the *same* referrer page route, so an
    unrelated removal and addition elsewhere in the application stay separate.
    """
    if not routes.added or not routes.removed:
        return ()
    before_by_referrer: dict[str, set[str]] = {}
    after_by_referrer: dict[str, set[str]] = {}
    for snapshot, sink in ((before, before_by_referrer), (after, after_by_referrer)):
        for key, links in snapshot.page_links.items():
            sink.setdefault(key.rsplit("#", 1)[0], set()).update(links)
    # Built once. Rebuilding these inside the loop made the function quadratic in
    # route count, which measured as 99.96% of comparison time at 32,000 page
    # states while producing byte-identical output.
    removed_routes = set(routes.removed)
    added_routes = set(routes.added)
    retargets: list[RouteRetarget] = []
    for referrer in sorted(set(before_by_referrer) & set(after_by_referrer)):
        lost = sorted(
            (before_by_referrer[referrer] - after_by_referrer[referrer]) & removed_routes
        )
        gained = sorted(
            (after_by_referrer[referrer] - before_by_referrer[referrer]) & added_routes
        )
        if len(lost) == 1 and len(gained) == 1:
            retargets.append(
                RouteRetarget(
                    referrer=referrer, removed_route=lost[0], added_route=gained[0]
                )
            )
    return tuple(retargets)


def _facet_changes(
    before: EvidenceSnapshot, after: EvidenceSnapshot
) -> dict[str, tuple[str, ...]]:
    """Attribute each route's page-state change to the facets that differ.

    Routes are compared rather than page keys because a page's identity is
    intentionally stable across facet changes: the same route observed twice
    should read as one thing changing, not two unrelated states.
    """
    def by_route_and_role(
        snapshot: EvidenceSnapshot,
    ) -> dict[tuple[str, str], dict[str, tuple[str, ...]]]:
        """Collect every observed state's digest per facet, not just the last one.

        A normalized route routinely maps to several page states — ``/products/:int``
        is one route and many products — so an earlier version of this function
        merged them with ``dict.update()`` and silently kept only the
        last-sorted state's digests. Any facet regression on the other states was
        erased. Digests are collected as a sorted tuple instead, so a change to
        *any* state under a route is visible.
        """
        merged: dict[tuple[str, str], dict[str, list[str]]] = {}
        for key, facets in snapshot.page_facets.items():
            route = key.rsplit("#", 1)[0]
            role = snapshot.page_roles.get(key, "default")
            bucket = merged.setdefault((route, role), {})
            for facet, digest in facets.items():
                bucket.setdefault(facet, []).append(digest)
        return {
            identity: {facet: tuple(sorted(digests)) for facet, digests in facets.items()}
            for identity, facets in merged.items()
        }

    before_states = by_route_and_role(before)
    after_states = by_route_and_role(after)
    changes: dict[str, set[str]] = {}
    # Compare like with like: a route observed as one role is only meaningfully
    # compared against the same route observed as the same role.
    for identity in sorted(set(before_states) & set(after_states)):
        route, _ = identity
        differing = {
            facet
            for facet in set(before_states[identity]) | set(after_states[identity])
            if before_states[identity].get(facet, ()) != after_states[identity].get(facet, ())
        }
        if differing:
            changes.setdefault(route, set()).update(differing)
    return {route: tuple(sorted(facets)) for route, facets in sorted(changes.items())}


def _set_diff(before: tuple[str, ...], after: tuple[str, ...]) -> SnapshotSectionDiff:
    return SnapshotSectionDiff(
        added=tuple(sorted(set(after) - set(before))),
        removed=tuple(sorted(set(before) - set(after))),
    )


def _map_diff(
    before: dict[str, str | None] | dict[str, str],
    after: dict[str, str | None] | dict[str, str],
) -> SnapshotSectionDiff:
    shared = set(before) & set(after)
    return SnapshotSectionDiff(
        added=tuple(sorted(set(after) - set(before))),
        removed=tuple(sorted(set(before) - set(after))),
        changed=tuple(sorted(key for key in shared if before[key] != after[key])),
    )


def _object_map_diff(
    before: dict[str, object], after: dict[str, object]
) -> SnapshotSectionDiff:
    normalized_before = {
        key: json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        for key, value in before.items()
    }
    normalized_after = {
        key: json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        for key, value in after.items()
    }
    return _map_diff(normalized_before, normalized_after)


def compare_snapshots(before: EvidenceSnapshot, after: EvidenceSnapshot) -> EvidenceSnapshotDiff:
    before_counts = before.coverage.model_dump()
    after_counts = after.coverage.model_dump()
    routes = _set_diff(before.routes, after.routes)
    return EvidenceSnapshotDiff(
        before=before.name,
        after=after.name,
        requirements=_set_diff(before.requirements, after.requirements),
        routes=routes,
        page_fingerprints=_map_diff(before.page_fingerprints, after.page_fingerprints),
        scenarios=_set_diff(before.scenarios, after.scenarios),
        verdicts=_map_diff(before.verdicts, after.verdicts),
        unknowns=_set_diff(before.unknowns, after.unknowns),
        application_metadata=_object_map_diff(before.application, after.application),
        coverage_delta={
            key: int(after_counts[key]) - int(before_counts[key]) for key in sorted(before_counts)
        },
        route_retargets=_route_retargets(before, after, routes),
        page_facet_changes=_facet_changes(before, after),
    )


def write_diff_report(
    comparison: EvidenceSnapshotDiff, destination: Path, format_name: str
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "json":
        content = comparison.model_dump_json(indent=2) + "\n"
    elif format_name == "markdown":
        lines = [f"# Evidence diff: {comparison.before} → {comparison.after}", ""]
        for section in (
            "requirements",
            "routes",
            "page_fingerprints",
            "scenarios",
            "verdicts",
            "unknowns",
            "application_metadata",
        ):
            value = getattr(comparison, section)
            lines.extend(
                [
                    f"## {section.replace('_', ' ').title()}",
                    f"- Added: {', '.join(value.added) or 'none'}",
                    f"- Removed: {', '.join(value.removed) or 'none'}",
                    f"- Changed: {', '.join(value.changed) or 'none'}",
                    "",
                ]
            )
        retarget_lines = [
            f"- {item.referrer}: {item.removed_route} → {item.added_route}"
            for item in comparison.route_retargets
        ] or ["- none"]
        lines.extend(["## Route retargets", *retarget_lines, ""])
        coverage_lines = [
            f"- {key}: {comparison.coverage_delta[key]:+d}"
            for key in sorted(comparison.coverage_delta)
        ]
        lines.extend(["## Coverage delta", *coverage_lines])
        content = "\n".join(lines) + "\n"
    else:
        raise ValueError("format must be json or markdown")
    destination.write_text(content, encoding="utf-8")
    return destination
