"""Deterministic JSON and standalone HTML ledger reports."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from qualityproof.models import LedgerEntry

REPORT_SCHEMA_VERSION = "qualityproof-report/v1"
PROVENANCE_NOTICE = (
    "Runtime reports are observations only and never establish requirement provenance "
    "by themselves."
)


def report_document(entries: tuple[LedgerEntry, ...]) -> dict[str, object]:
    counts = Counter(entry.status.value for entry in entries)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "provenance_notice": PROVENANCE_NOTICE,
        "summary": {
            "total": len(entries),
            "VERIFIED": counts["VERIFIED"],
            "PARTIAL": counts["PARTIAL"],
            "UNKNOWN": counts["UNKNOWN"],
        },
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }


def write_json_report(entries: tuple[LedgerEntry, ...], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report_document(entries), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def write_html_report(entries: tuple[LedgerEntry, ...], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = report_document(entries)
    summary = document["summary"]
    assert isinstance(summary, dict)
    rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(entry.id)}</code></td>"
        f'<td class="{entry.status.value.lower()}">{entry.status.value}</td>'
        f"<td>{html.escape(entry.reason)}</td>"
        f"<td>{len(entry.test.assertions)}</td>"
        "</tr>"
        for entry in entries
    )
    destination.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QualityProof traceability ledger</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; margin: 2rem; color: #17202a; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #d5d8dc; padding: .6rem; text-align: left; }}
.verified {{ color: #147a3d; }} .partial {{ color: #9a6700; }} .unknown {{ color: #b42318; }}
.notice {{ background: #fff8c5; padding: 1rem; border-left: 4px solid #d4a72c; }}
code {{ overflow-wrap: anywhere; }}
</style>
</head>
<body>
<h1>QualityProof traceability ledger</h1>
<p class="notice">{html.escape(PROVENANCE_NOTICE)}</p>
<p>Total: {summary["total"]} · Verified: {summary["VERIFIED"]} ·
Partial: {summary["PARTIAL"]} · Unknown: {summary["UNKNOWN"]}</p>
<table>
<thead><tr><th>Test</th><th>State</th><th>Reason</th><th>Assertions</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body>
</html>
""",
        encoding="utf-8",
    )
    return destination
