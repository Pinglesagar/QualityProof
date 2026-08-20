"""Assert that cross-language evidence really reached the ledger.

Two green CI jobs prove only that two runners ran. The integration claim is that
TypeScript evidence is judged by the Python engine's rules, so this checks the
outcome that claim predicts: an annotated TypeScript test reaches VERIFIED, and
an unannotated one stays UNKNOWN rather than being credited for having run.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from qualityproof.config import load_config
from qualityproof.models import LedgerEntry, LedgerStatus
from qualityproof.repository import SQLiteRepository

ROOT = Path(__file__).parents[1]
EXTERNAL_FRAMEWORKS = frozenset({"playwright-typescript", "playwright-python"})


def assert_interop_ledger(project: Path) -> dict[str, object]:
    repository = SQLiteRepository(project / load_config(project).database_path)
    repository.initialize()
    entries = repository.list("ledger", LedgerEntry)
    external = [entry for entry in entries if entry.test.framework in EXTERNAL_FRAMEWORKS]
    if not external:
        raise SystemExit("no external-runner evidence reached the ledger")
    counts = Counter(entry.status.value for entry in external)
    if not counts[LedgerStatus.VERIFIED.value]:
        raise SystemExit(
            "an annotated TypeScript test with resolvable provenance should reach VERIFIED"
        )
    if not counts[LedgerStatus.UNKNOWN.value]:
        raise SystemExit(
            "an unannotated TypeScript test should remain UNKNOWN; the zero-config "
            "default must not be relaxed for foreign runners"
        )
    return {
        "external_entries": len(external),
        "frameworks": sorted({entry.test.framework for entry in external}),
        "VERIFIED": counts[LedgerStatus.VERIFIED.value],
        "PARTIAL": counts[LedgerStatus.PARTIAL.value],
        "UNKNOWN": counts[LedgerStatus.UNKNOWN.value],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT / ".qualityproof" / "interop-demo")
    args = parser.parse_args()
    print(json.dumps(assert_interop_ledger(args.project), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
