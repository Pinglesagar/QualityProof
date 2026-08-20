import json
import sqlite3
from pathlib import Path

import pytest

from qualityproof.models import (
    AuditedTest,
    AuditEvent,
    LedgerEntry,
    LedgerStatus,
    Requirement,
)
from qualityproof.reporting import PROVENANCE_NOTICE, write_html_report, write_json_report
from qualityproof.repository import SQLiteRepository


def test_repository_lists_queries_and_preserves_event_order(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    for identifier in ("REQ-2", "REQ-1", "OTHER-1"):
        repository.put(
            "requirement",
            identifier,
            Requirement(id=identifier, title=identifier, description="Description"),
        )
    first = AuditEvent(id="event-1", event_type="audit", details={"tests": 2})
    second = AuditEvent(id="event-2", event_type="report")
    repository.append_event(first)
    repository.append_event(second)

    assert [item.id for item in repository.list("requirement", Requirement)] == [
        "OTHER-1",
        "REQ-1",
        "REQ-2",
    ]
    assert [item.id for item in repository.query(
        "requirement", Requirement, record_id_prefix="REQ-"
    )] == ["REQ-1", "REQ-2"]
    assert repository.list_events() == (first, second)
    assert repository.list_events("audit") == (first,)

    repository.clear_kind("requirement")

    assert repository.list("requirement", Requirement) == ()
    assert repository.list_events() == (first, second)


def test_audit_events_are_append_only(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "qualityproof.db")
    repository.initialize()
    event = AuditEvent(id="event-1", event_type="audit")
    repository.append_event(event)

    with pytest.raises(sqlite3.IntegrityError):
        repository.append_event(event)


def test_json_and_html_reports_include_notice_and_ledger(tmp_path: Path) -> None:
    entry = LedgerEntry(
        id="test_sample.py::test_plain",
        status=LedgerStatus.UNKNOWN,
        reason="No metadata.",
        test=AuditedTest(
            id="test_sample.py::test_plain",
            path="test_sample.py",
            name="test_plain",
            line=1,
            framework="pytest",
        ),
    )

    json_path = write_json_report((entry,), tmp_path / "ledger.json")
    html_path = write_html_report((entry,), tmp_path / "ledger.html")
    document = json.loads(json_path.read_text(encoding="utf-8"))

    assert document["schema_version"] == "qualityproof-report/v1"
    assert document["summary"]["UNKNOWN"] == 1
    assert document["provenance_notice"] == PROVENANCE_NOTICE
    assert PROVENANCE_NOTICE in html_path.read_text(encoding="utf-8")
