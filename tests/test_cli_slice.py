import json
from pathlib import Path

from typer.testing import CliRunner

from qualityproof.cli import app
from qualityproof.models import LedgerEntry, LedgerStatus
from qualityproof.repository import SQLiteRepository


def test_cli_audit_and_report_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "requirements.md").write_text(
        "# REQ-1\n\nCheckout must be available.\n",
        encoding="utf-8",
    )
    tests_directory = tmp_path / "tests"
    tests_directory.mkdir()
    (tests_directory / "test_checkout.py").write_text(
        """
from qualityproof import qualityproof

@qualityproof(
    requirements=["REQ-1"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "requirements.md",
        "locator": "#REQ-1",
    }],
)
def test_checkout():
    assert True

def test_unclassified():
    assert True
""",
        encoding="utf-8",
    )
    runner = CliRunner()

    audit_result = runner.invoke(
        app,
        ["audit", str(tests_directory), "--project", str(tmp_path)],
    )
    report_result = runner.invoke(app, ["report", "--project", str(tmp_path)])

    assert audit_result.exit_code == 0, audit_result.output
    assert "1 verified, 0 partial, 1 unknown" in audit_result.output
    assert report_result.exit_code == 0, report_result.output
    report = json.loads(
        (tmp_path / ".qualityproof" / "reports" / "ledger.json").read_text(encoding="utf-8")
    )
    assert report["summary"] == {"PARTIAL": 0, "UNKNOWN": 1, "VERIFIED": 1, "total": 2}
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    entries = repository.list("ledger", LedgerEntry)
    assert {entry.status for entry in entries} == {LedgerStatus.VERIFIED, LedgerStatus.UNKNOWN}
    assert [event.event_type for event in repository.list_events()] == [
        "source_audit_completed",
        "report_generated",
    ]


def test_cli_audit_reports_syntax_errors(tmp_path: Path) -> None:
    source = tmp_path / "test_broken.py"
    source.write_text("def test_broken(:\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["audit", str(source), "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "Unable to parse" in result.output


def test_repeated_audit_reconciles_deleted_tests_atomically(tmp_path: Path) -> None:
    tests_directory = tmp_path / "tests"
    tests_directory.mkdir()
    source = tests_directory / "test_removed.py"
    source.write_text("def test_removed():\n    assert True\n", encoding="utf-8")
    runner = CliRunner()
    first = runner.invoke(app, ["audit", str(tests_directory), "--project", str(tmp_path)])
    assert first.exit_code == 0, first.output

    source.unlink()
    replacement = tests_directory / "test_current.py"
    replacement.write_text("def test_current():\n    assert True\n", encoding="utf-8")
    second = runner.invoke(app, ["audit", str(tests_directory), "--project", str(tmp_path)])
    repository = SQLiteRepository(tmp_path / ".qualityproof" / "qualityproof.db")
    entries = repository.list("ledger", LedgerEntry)

    assert second.exit_code == 0, second.output
    assert [entry.test.name for entry in entries] == ["test_current"]
