import json
from pathlib import Path

from qualityproof.models import PageState, UnknownItem, Verdict, VerdictStatus
from qualityproof.repository import SQLiteRepository
from qualityproof.snapshots import capture_snapshot
from scripts.benchmark_demo import match_signals, qualityproof_signals, run_benchmark


def test_benchmark_scores_only_persisted_outputs_against_independent_truth(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    output = tmp_path / "output"
    repository = SQLiteRepository(project / ".qualityproof" / "qualityproof.db")
    repository.initialize()
    repository.put(
        "page_state",
        "before",
        PageState(
            id="before",
            url="https://example.test/products/1",
            route="/products/{id}",
            fingerprint="old",
        ),
    )
    repository.put(
        "verdict",
        "checkout",
        Verdict(
            assertion_id="custom::test_checkout_total",
            status=VerdictStatus.PASS,
            rationale="v1 execution",
        ),
    )
    capture_snapshot("demo-v1", project, repository)
    repository.replace_sets(
        {
            "page_state": (
                (
                    "after",
                    PageState(
                        id="after",
                        url="https://example.test/products/1",
                        route="/products/{id}",
                        fingerprint="new",
                    ),
                ),
            ),
            "unknown_item": (
                (
                    "guard",
                    UnknownItem(
                        id="guard",
                        question="destructive_action_guard: refused",
                    ),
                ),
            ),
            "verdict": (
                (
                    "checkout",
                    Verdict(
                        assertion_id="custom::test_checkout_total",
                        status=VerdictStatus.FAIL,
                        rationale="v2 execution",
                    ),
                ),
            ),
        }
    )
    capture_snapshot("demo-v2", project, repository)
    truth = tmp_path / "hidden-ground-truth.json"
    truth.write_text(
        json.dumps(
            {
                "expected_findings": [
                    {
                        "id": "MATCHED",
                        "signal": "page_state_changed:/products/{id}",
                    },
                    {
                        "id": "MISSED",
                        "signal": "execution_finding:not-produced",
                    },
                    {
                        "id": "EXECUTION",
                        "signal": "execution_finding:checkout-total",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_benchmark(project, output, ground_truth_path=truth)
    measurements = result["measurements"]

    assert qualityproof_signals(project).signals == (
        "execution_finding:checkout-total",
        "page_state_changed:/products/{id}",
        "unknown:destructive_action_guard",
    )
    assert isinstance(measurements, dict)
    assert measurements["qualityproof_matched_finding_ids"] == ("EXECUTION", "MATCHED")
    assert measurements["qualityproof_missed_finding_ids"] == ("MISSED",)
    assert measurements["qualityproof_precision"] == 0.666667
    assert measurements["qualityproof_recall"] == 0.666667
    assert "product detections" in str(result["fixture_integrity"])
    assert "third-party" in str(result["scope_notice"])


def test_page_state_signal_is_refused_when_the_cause_cannot_explain_the_seed() -> None:
    """A layout defect is not detected by a navigation change on the same route.

    This is the guard against a benchmark flattering itself: both seeds live on
    /products, and string matching alone would credit the layout seed with a
    signal caused entirely by a removed link.
    """
    result = match_signals(
        signals=("page_state_changed:/products",),
        signal_to_ids={"page_state_changed:/products": ["SEED-LAYOUT-001"]},
        seed_categories={"SEED-LAYOUT-001": "layout_overflow_marker"},
        facet_changes={"/products": ("controls",)},
    )

    assert result.matched_ids == ()
    assert result.rejected_for_wrong_cause == (
        "page_state_changed:/products!=SEED-LAYOUT-001",
    )


def test_page_state_signal_is_credited_when_the_layout_facet_actually_moved() -> None:
    result = match_signals(
        signals=("page_state_changed:/products",),
        signal_to_ids={"page_state_changed:/products": ["SEED-LAYOUT-001"]},
        seed_categories={"SEED-LAYOUT-001": "layout_overflow_marker"},
        facet_changes={"/products": ("controls", "layout")},
    )

    assert result.matched_ids == ("SEED-LAYOUT-001",)
    assert result.rejected_for_wrong_cause == ()


def test_signals_without_a_facet_rule_are_matched_on_identity_alone() -> None:
    """Route and execution signals name their own cause, so no facet is required."""
    result = match_signals(
        signals=("route_removed:/legacy-order",),
        signal_to_ids={"route_removed:/legacy-order": ["SEED-JOURNEY-001"]},
        seed_categories={"SEED-JOURNEY-001": "removed_journey"},
        facet_changes={},
    )

    assert result.matched_ids == ("SEED-JOURNEY-001",)


def test_every_changed_verdict_becomes_a_signal(tmp_path: Path) -> None:
    """No observation may be silently withheld from scoring.

    A curated name map once converted two known test names into signals and
    dropped the rest. Because precision is matched-signals over emitted-signals,
    a dropped observation cannot cost anything — so dropping is indistinguishable
    from perfect accuracy. This guards the derivation against regressing to that.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "qualityproof.toml").write_text(
        '[project]\ndatabase_path = ".qualityproof/qualityproof.db"\n', encoding="utf-8"
    )
    repository = SQLiteRepository(project / ".qualityproof" / "qualityproof.db")
    repository.initialize()

    changed = {
        "a.b::test_profile_validation": VerdictStatus.PASS,
        "a.b::test_checkout_total": VerdictStatus.PASS,
        "a.b::test_journey_deadbeef[chromium]": VerdictStatus.PASS,
        "a.b::test_journey_cafef00d[chromium]": VerdictStatus.PASS,
    }
    for assertion_id, status in changed.items():
        repository.put(
            "verdict",
            assertion_id,
            Verdict(assertion_id=assertion_id, status=status, rationale="before"),
        )
    capture_snapshot("demo-v1", project, repository)
    for assertion_id in changed:
        repository.put(
            "verdict",
            assertion_id,
            Verdict(assertion_id=assertion_id, status=VerdictStatus.FAIL, rationale="after"),
        )
    capture_snapshot("demo-v2", project, repository)

    signals = qualityproof_signals(project).signals
    execution_signals = {item for item in signals if item.startswith("execution_finding:")}

    assert len(execution_signals) == len(changed), sorted(execution_signals)
    assert "execution_finding:profile-validation" in execution_signals
    assert "execution_finding:journey-deadbeef" in execution_signals
