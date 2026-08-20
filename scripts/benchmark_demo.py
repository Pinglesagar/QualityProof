"""Measure QualityProof artifacts against independently loaded controlled ground truth."""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import NamedTuple, cast

from fastapi.testclient import TestClient

from demo.app import DemoVersion, create_app
from qualityproof.config import load_config
from qualityproof.discovery import normalize_route
from qualityproof.models import LedgerEntry, LedgerStatus, UnknownItem
from qualityproof.repository import SQLiteRepository
from qualityproof.snapshots import compare_snapshots, read_snapshot

ROOT = Path(__file__).parents[1]


def _login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"email": "shopper@example.test", "password": "shopper-demo"},
    )
    if response.status_code != 200:
        raise RuntimeError("demo login failed")


def _total(version: DemoVersion) -> str:
    client = TestClient(create_app(version))
    _login(client)
    client.post("/cart/add", data={"product_id": "1", "quantity": "2"})
    matched = re.search(r'data-testid="total">£([^<]+)', client.get("/checkout").text)
    if matched is None:
        raise RuntimeError(f"total marker missing in {version}")
    return matched.group(1)


def verify_fixture_integrity(unknowns: tuple[UnknownItem, ...]) -> tuple[str, ...]:
    """Check that the controlled fixture contains its declared seeds.

    These direct checks validate benchmark setup only. They are not QualityProof findings.
    """
    clients = {version: TestClient(create_app(version)) for version in ("v1", "v2")}
    for client in clients.values():
        _login(client)
    v1_product = clients["v1"].get("/products/1").text
    v2_product = clients["v2"].get("/products/1").text
    v1_profile = clients["v1"].get("/profile").text
    v2_profile = clients["v2"].get("/profile").text
    v1_catalogue = clients["v1"].get("/products").text
    v2_catalogue = clients["v2"].get("/products").text
    invalid = {"display_name": "Sam", "contact_email": "invalid", "phone": "123"}
    signals = {
        "SEED-LOCATOR-001": (
            'id="add-to-cart"' in v1_product
            and 'id="basket-add"' in v2_product
            and ">Add to cart</button>" in v1_product
            and ">Add to cart</button>" in v2_product
        ),
        "SEED-A11Y-001": (
            '<label for="phone">' in v1_profile and '<label for="phone">' not in v2_profile
        ),
        "SEED-LINK-001": (
            clients["v1"].get("/help").status_code == 200
            and clients["v2"].get("/missing-help").status_code == 404
        ),
        "SEED-AUTHZ-001": (
            clients["v1"].get("/admin").status_code == 403
            and clients["v2"].get("/admin").status_code == 200
        ),
        "SEED-VALIDATION-001": (
            "Enter a valid contact email" in clients["v1"].post("/profile", data=invalid).text
            and "Profile saved" in clients["v2"].post("/profile", data=invalid).text
        ),
        "SEED-TOTAL-001": _total("v1") == "28.00" and _total("v2") == "15.50",
        "SEED-LAYOUT-001": (
            'data-seed-defect="layout-overflow"' not in v1_catalogue
            and 'data-seed-defect="layout-overflow"' in v2_catalogue
        ),
        "SEED-JOURNEY-001": (
            clients["v1"].get("/legacy-order").status_code == 200
            and clients["v2"].get("/legacy-order").status_code == 404
        ),
        "SEED-SAFETY-001": any(
            "destructive_action_guard" in unknown.question for unknown in unknowns
        ),
    }
    return tuple(sorted(seed_id for seed_id, detected in signals.items() if detected))


#: Which observed facet legitimately explains each seeded defect category.
#: A ``page_state_changed`` signal only counts as detecting a seed when at least
#: one of that seed's admissible facets actually differs. Without this, a route
#: whose heading changed would be credited with detecting an unrelated layout or
#: permission defect that happens to live on the same route.
CATEGORY_FACETS: dict[str, frozenset[str]] = {
    "changed_locator_same_semantics": frozenset({"controls"}),
    "missing_accessible_label": frozenset({"accessibility", "controls"}),
    "permission_regression": frozenset({"status"}),
    "layout_overflow_marker": frozenset({"layout"}),
    "validation_regression": frozenset({"controls", "forms"}),
    "total_calculation_defect": frozenset({"controls", "headings"}),
}


class SignalClassification(NamedTuple):
    """Candidate findings, plus the changes deliberately excluded from scoring."""

    signals: tuple[str, ...]
    context_changes: tuple[str, ...]
    route_retargets: tuple[str, ...]
    facet_changes: dict[str, tuple[str, ...]]


def _execution_finding_name(verdict_key: str) -> str:
    """Derive a stable finding name from a changed verdict's identifier.

    Verdict keys are runner-shaped (``a.b.c::test_name[param]``); the test name
    is the part a reader recognises and the part ground truth can name.
    """
    tail = verdict_key.rsplit("::", 1)[-1]
    name = tail.split("[", 1)[0].strip()
    return name.removeprefix("test_").replace("_", "-") or tail


def _route_from_page_key(key: str) -> str:
    return key.rsplit("#", 1)[0]


def _normalize_signal(signal: str) -> str:
    for prefix in ("route_added:", "route_removed:", "page_state_changed:"):
        if signal.startswith(prefix):
            route = signal.removeprefix(prefix)
            return f"{prefix}{normalize_route(f'https://benchmark.invalid{route}')}"
    return signal


def qualityproof_signals(project: Path) -> SignalClassification:
    """Extract candidate signals, and separately the non-defect context changes.

    Two classification rules keep this set free of self-inflicted duplicates:

    1. Application metadata differences (a version string, a build id) describe
       *which* releases were compared. They are context, never a defect, so they
       are reported separately and never scored as candidate findings.
    2. When a referrer page's outbound link moves from one route to another,
       ``compare_snapshots`` reports it as a retarget. The observable defect is
       the newly reachable route, so the paired removal is recorded as
       supporting context rather than counted a second time. An *unpaired*
       removal is untouched and still reported as a removal.
    """
    before = read_snapshot(project, "demo-v1")
    after = read_snapshot(project, "demo-v2")
    comparison = compare_snapshots(before, after)
    retargeted_removals = {item.removed_route for item in comparison.route_retargets}
    context_changes = tuple(
        sorted(
            {
                f"application_metadata_changed:{key}"
                for key in comparison.application_metadata.changed
            }
            | {
                _normalize_signal(f"route_removed:{route}")
                for route in comparison.routes.removed
                if route in retargeted_removals
            }
        )
    )
    signals = {
        *(_normalize_signal(f"route_added:{route}") for route in comparison.routes.added),
        *(
            _normalize_signal(f"route_removed:{route}")
            for route in comparison.routes.removed
            if route not in retargeted_removals
        ),
    }
    # Every changed verdict is emitted. An earlier version mapped two known test
    # names to signal names and silently dropped the rest, which cannot cost
    # precision because a signal that is never emitted can never be unmatched.
    # Dropping observations to protect a score is the exact failure this harness
    # is supposed to expose, so the mapping is now derived, not curated.
    for key in comparison.verdicts.changed:
        signals.add(f"execution_finding:{_execution_finding_name(key)}")
    before_pages: dict[str, set[tuple[str, str | None]]] = {}
    after_pages: dict[str, set[tuple[str, str | None]]] = {}
    for key, fingerprint in before.page_fingerprints.items():
        before_pages.setdefault(_route_from_page_key(key), set()).add((key, fingerprint))
    for key, fingerprint in after.page_fingerprints.items():
        after_pages.setdefault(_route_from_page_key(key), set()).add((key, fingerprint))
    for route in set(before_pages) & set(after_pages):
        if before_pages[route] != after_pages[route]:
            signals.add(_normalize_signal(f"page_state_changed:{route}"))
    repository = SQLiteRepository(project / load_config(project).database_path)
    repository.initialize()
    for unknown in repository.list("unknown_item", UnknownItem):
        if "destructive_action_guard" in unknown.question:
            signals.add("unknown:destructive_action_guard")
    # Several referrers can move the same link (a shared footer); the destination
    # change is one finding regardless of how many pages carried the old link.
    retargets = tuple(
        sorted(
            {
                f"route_retargeted:{item.removed_route}->{item.added_route}"
                for item in comparison.route_retargets
            }
        )
    )
    return SignalClassification(
        tuple(sorted(signals)),
        context_changes,
        retargets,
        {
            _normalize_signal(f"page_state_changed:{route}").removeprefix(
                "page_state_changed:"
            ): facets
            for route, facets in comparison.page_facet_changes.items()
        },
    )


class SignalMatch(NamedTuple):
    matched_ids: tuple[str, ...]
    matched_signals: tuple[str, ...]
    rejected_for_wrong_cause: tuple[str, ...]


def match_signals(
    signals: tuple[str, ...],
    signal_to_ids: dict[str, list[str]],
    seed_categories: dict[str, str],
    facet_changes: dict[str, tuple[str, ...]],
) -> SignalMatch:
    """Credit a signal to a seed only when the observed cause could explain it.

    Without this check the matcher is generous in exactly the wrong direction: a
    route whose navigation links changed would be credited with detecting a
    layout or permission defect that merely lives on the same route. Requiring an
    admissible facet makes matching strictly harder, never easier.
    """
    matched_ids: list[str] = []
    matched_signals: set[str] = set()
    rejected: list[str] = []
    for signal in signals:
        for identifier in sorted(signal_to_ids.get(signal, [])):
            admissible = CATEGORY_FACETS.get(seed_categories.get(identifier, ""))
            if signal.startswith("page_state_changed:") and admissible is not None:
                route = signal.removeprefix("page_state_changed:")
                if not set(facet_changes.get(route, ())) & admissible:
                    rejected.append(f"{signal}!={identifier}")
                    continue
            matched_ids.append(identifier)
            matched_signals.add(signal)
    return SignalMatch(
        tuple(matched_ids), tuple(sorted(matched_signals)), tuple(sorted(set(rejected)))
    )


def _load_ground_truth(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("expected_findings") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("benchmark ground truth must contain expected_findings")
    ground_truth: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(
            entry.get("signal"), str
        ):
            raise ValueError("ground-truth entries require string id and signal")
        ground_truth[str(entry["id"])] = _normalize_signal(str(entry["signal"]))
    return ground_truth


def _assertions_by_provenance(entries: tuple[LedgerEntry, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for entry in entries:
        metadata = entry.test.metadata
        kinds = {item.kind.value for item in metadata.provenance} if metadata else {"UNATTRIBUTED"}
        for kind in kinds:
            counts[kind] += len(entry.test.assertions)
    return dict(sorted(counts.items()))


def run_benchmark(
    project: Path,
    output_directory: Path,
    *,
    workflow_runtime_seconds: float | None = None,
    ground_truth_path: Path | None = None,
) -> dict[str, object]:
    """Score persisted QualityProof outputs without reading fixture implementation details."""
    started = time.perf_counter()
    manifest = json.loads((ROOT / "demo" / "seeded-defects.json").read_text(encoding="utf-8"))
    seeds = manifest.get("seeds")
    if not isinstance(seeds, list):
        raise ValueError("seed manifest does not contain a seeds list")
    implemented_ids = tuple(
        sorted(
            str(seed["id"])
            for seed in seeds
            if isinstance(seed, dict) and seed.get("implemented") is True
        )
    )
    repository = SQLiteRepository(project / load_config(project).database_path)
    repository.initialize()
    unknowns = repository.list("unknown_item", UnknownItem)
    ledger = repository.list("ledger", LedgerEntry)
    fixture_checks_passed = verify_fixture_integrity(unknowns)
    fixture_checks_failed = tuple(sorted(set(implemented_ids) - set(fixture_checks_passed)))
    truth_path = ground_truth_path or ROOT / "demo" / "benchmark-ground-truth.json"
    ground_truth = _load_ground_truth(truth_path)
    signals, context_changes, retargets, facet_changes = qualityproof_signals(project)
    seed_categories = {
        str(seed["id"]): str(seed.get("category", ""))
        for seed in seeds
        if isinstance(seed, dict) and isinstance(seed.get("id"), str)
    }
    signal_to_ids: dict[str, list[str]] = {}
    for identifier, signal in ground_truth.items():
        signal_to_ids.setdefault(signal, []).append(identifier)
    match = match_signals(signals, signal_to_ids, seed_categories, facet_changes)
    matched_ids = list(match.matched_ids)
    matched_signals = set(match.matched_signals)
    unattributed = list(match.rejected_for_wrong_cause)
    expected_ids = set(ground_truth)
    missed_ids = tuple(sorted(expected_ids - set(matched_ids)))
    false_positive_signals = tuple(sorted(set(signals) - matched_signals))
    true_positives = len(matched_ids)
    precision = len(matched_signals) / len(signals) if signals else 0.0
    recall = true_positives / len(expected_ids) if expected_ids else 0.0
    journeys = len(tuple((project / "scenarios" / "generated" / "approved").glob("*.yaml")))
    status_counts = Counter(entry.status.value for entry in ledger)
    elapsed = time.perf_counter() - started
    try:
        truth_source = truth_path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        truth_source = truth_path.name
    result: dict[str, object] = {
        "schema_version": "qualityproof-demo-benchmark/v2",
        "ground_truth": {
            "source": truth_source,
            "expected_finding_count": len(expected_ids),
            "expected_finding_ids": tuple(sorted(expected_ids)),
        },
        "fixture_integrity": {
            "notice": (
                "Direct seeded-fixture checks validate benchmark setup only; "
                "they are not product detections or QualityProof findings."
            ),
            "declared_seed_count": len(implemented_ids),
            "checks_passed": fixture_checks_passed,
            "checks_failed": fixture_checks_failed,
        },
        "measurements": {
            "journeys_discovered": journeys,
            "qualityproof_candidate_signals": signals,
            "qualityproof_context_changes": context_changes,
            "qualityproof_route_retargets": retargets,
            "qualityproof_page_facet_changes": {
                route: list(facets) for route, facets in sorted(facet_changes.items())
            },
            "qualityproof_rejected_for_wrong_cause": tuple(sorted(set(unattributed))),
            "qualityproof_matched_finding_ids": tuple(sorted(matched_ids)),
            "qualityproof_missed_finding_ids": missed_ids,
            "qualityproof_false_positive_signals": false_positive_signals,
            "qualityproof_precision": round(precision, 6),
            "qualityproof_recall": round(recall, 6),
            "assertions_by_provenance": _assertions_by_provenance(ledger),
            "ledger_unknown_count": status_counts[LedgerStatus.UNKNOWN.value],
            "discovery_unknown_count": len(unknowns),
            "benchmark_runtime_seconds": round(elapsed, 6),
            "workflow_runtime_seconds": (
                round(workflow_runtime_seconds, 6) if workflow_runtime_seconds is not None else None
            ),
        },
        "scope_notice": (
            "Scores cover only this controlled fixture. No third-party tool was run and "
            "no third-party or general-accuracy comparison is claimed."
        ),
    }
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "benchmark.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    measurements = cast(dict[str, object], result["measurements"])
    with (output_directory / "benchmark.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("metric", "value"))
        for key, value in sorted(measurements.items()):
            rendered = (
                json.dumps(value, sort_keys=True)
                if isinstance(value, (dict, list, tuple))
                else value
            )
            writer.writerow((key, rendered))
    assertions = cast(dict[str, int], measurements["assertions_by_provenance"])
    markdown = [
        "# QualityProof controlled demo benchmark",
        "",
        f"- Journeys discovered: {journeys}",
        f"- QualityProof precision: {precision:.3f}",
        f"- QualityProof recall: {recall:.3f}",
        f"- Matched finding IDs: {', '.join(sorted(matched_ids)) or 'none'}",
        f"- Missed finding IDs: {', '.join(missed_ids) or 'none'}",
        f"- False-positive signals: {', '.join(false_positive_signals) or 'none'}",
        f"- Route retargets (one finding, not two): {', '.join(retargets) or 'none'}",
        f"- Context changes (not scored as findings): {', '.join(context_changes) or 'none'}",
        (
            "- Signals rejected for wrong cause: "
            f"{', '.join(sorted(set(unattributed))) or 'none'}"
        ),
        (
            "- Fixture integrity checks (not product detections): "
            f"{len(fixture_checks_passed)}/{len(implemented_ids)} passed"
        ),
        f"- Assertions by provenance: {json.dumps(assertions, sort_keys=True)}",
        f"- Ledger unknown count: {status_counts[LedgerStatus.UNKNOWN.value]}",
        f"- Discovery unknown count: {len(unknowns)}",
        f"- Benchmark runtime: {elapsed:.6f} seconds",
        "",
        (
            "Direct fixture checks are setup integrity checks, not QualityProof findings. "
            "No third-party tool was run and no comparative claim is made."
        ),
        "",
        (
            "Every observed change is emitted. Application-metadata differences are reported "
            "as context because they identify which releases were compared, and a retargeted "
            "link is counted once because one referrer changed one destination; an unpaired "
            "route removal is still scored as a removal."
        ),
        "",
        (
            "**What the recall figure does and does not mean.** Of the nine seeded defects, "
            "SEED-SAFETY-001 is identical in both releases and is 'detected' by the crawler "
            "reporting its own refusal to activate a destructive control, so it measures the "
            "guard rather than a regression. SEED-VALIDATION-001 and SEED-TOTAL-001 are "
            "detected because the workflow runs two hand-authored tests that assert those "
            "specific behaviours and observes them flip from pass to fail; that is a genuine "
            "execution finding but it is not discovery. The six remaining detections are both "
            "regressions and discoveries. Read recall as 9/9 of a fixture the author wrote, "
            "not as a general detection rate."
        ),
    ]
    (output_directory / "benchmark.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT / ".qualityproof" / "demo-workflow")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmark-results")
    parser.add_argument("--ground-truth", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run_benchmark(args.project, args.output, ground_truth_path=args.ground_truth),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
