"""Measure how much of the trust logic is actually defended by tests.

Coverage answers "was this line executed"; mutation answers "would anything have
noticed if it were wrong". For a project whose whole claim is about assertion
strength, that is the honest self-measurement, and it is aimed at the modules that
decide what evidence *proves* rather than at the whole codebase.

Two deliberate behaviours:

* Outcomes are read from the runner's own per-mutant output, because
  ``mutmut results`` does not report killed mutants distinctly in this version and
  parsing it produced a fabricated 0%% score during development.
* A run in which nothing at all is killed is reported as an inert oracle rather
  than as a score of zero. A number is only published when it was measured.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[1]

#: The runner marks each finished mutant with an outcome glyph on its own line.
OUTCOME_LINE = re.compile(
    r"^(?P<glyph>\U0001F389|\U0001F641|\U0001F914|\u23f0)"
    r"\s+(?P<name>qualityproof\.[\w.\u01c1]+)"
)
GLYPH_OUTCOME = {
    "\U0001F389": "killed",
    "\U0001F641": "survived",
    "\U0001F914": "suspicious",
    "\u23f0": "timeout",
}

#: Modules whose logic decides what a test is entitled to claim. A surviving
#: mutant here means the engine could give a wrong verdict with no test objecting.
TRUST_MODULES = ("audit", "security", "snapshots", "generation")


def _module_of(name: str) -> str:
    parts = name.split(".")
    return parts[1] if len(parts) > 1 else "unknown"


def parse_outcomes(text: str) -> dict[str, dict[str, int]]:
    """Tally outcomes per module from the runner's per-mutant output."""
    tally: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for line in text.splitlines():
        matched = OUTCOME_LINE.match(line)
        if not matched:
            continue
        outcome = GLYPH_OUTCOME[matched.group("glyph")]
        tally[_module_of(matched.group("name"))][outcome] += 1
    return {module: dict(counts) for module, counts in sorted(tally.items())}


def _score(counts: dict[str, int]) -> tuple[float | None, int]:
    killed = counts.get("killed", 0)
    evaluated = killed + counts.get("survived", 0)
    if evaluated == 0:
        return None, 0
    if killed == 0:
        # Zero kills across many mutants is not 0%%; it means the mutated package
        # was never the one under test. Refuse to publish a number for it.
        return None, evaluated
    return round(killed / evaluated, 4), evaluated


def run_mutation(timeout: float, *, log: Path | None = None) -> dict[str, object]:
    """Run the trust-module mutants, or summarize an existing run log."""
    if log is not None:
        output = log.read_text(encoding="utf-8", errors="replace")
        exit_code: int | None = None
    else:
        patterns = [f"qualityproof.{module}.*" for module in TRUST_MODULES]
        completed = subprocess.run(
            [sys.executable, "-m", "mutmut", "run", *patterns],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        exit_code = completed.returncode

    per_module = parse_outcomes(output)
    combined: dict[str, int] = defaultdict(int)
    for counts in per_module.values():
        for outcome, count in counts.items():
            combined[outcome] += count
    overall_score, overall_evaluated = _score(dict(combined))
    inert = overall_evaluated > 0 and overall_score is None

    return {
        "schema_version": "qualityproof-mutation/v3",
        "exit_code": exit_code,
        "oracle_inert": inert,
        "trust_modules": list(TRUST_MODULES),
        "per_module": {
            module: {
                **counts,
                "evaluated": _score(counts)[1],
                "mutation_score": _score(counts)[0],
            }
            for module, counts in per_module.items()
        },
        "combined": {
            **dict(combined),
            "evaluated": overall_evaluated,
            "mutation_score": overall_score,
        },
        "oracle": (
            "Excludes the three test modules importing `scripts` or `demo`; the runner "
            "copies only the package, so those are unavailable. Neither covers a trust rule."
        ),
        "notice": (
            "No score claimed: nothing was killed, so the mutated package was not the one "
            "imported by the tests. Publishing 0.0 here would be a fabricated measurement."
            if inert
            else "Score covers trust-critical modules only; not comparable to a "
            "whole-repository figure. Surviving mutants that only alter a human-readable "
            "message are counted, so the raw score understates defended logic."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "benchmark-results" / "mutation.json")
    parser.add_argument("--timeout", type=float, default=3_600.0)
    parser.add_argument(
        "--from-log",
        type=Path,
        help="Summarize an existing run log instead of re-running the mutants.",
    )
    args = parser.parse_args()
    report = run_mutation(args.timeout, log=args.from_log)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["oracle_inert"]:
        raise SystemExit("mutation oracle is inert; no score reported")


if __name__ == "__main__":
    main()
