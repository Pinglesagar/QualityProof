from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from qualityproof.cli import app
from qualityproof.config import load_config
from qualityproof.models import ActionEdge, Requirement, Scenario, UnknownItem
from qualityproof.repository import SQLiteRepository
from qualityproof.schema import SCHEMA_MODELS, export_schemas


def test_models_validate_nested_actions_and_scenarios() -> None:
    scenario = Scenario(
        id="checkout",
        title="Checkout",
        steps=("Open basket",),
        assertions=(),
    )
    edge = ActionEdge(
        id="edge-1",
        source_state_id="basket",
        target_state_id="payment",
        action={"type": "click", "selector": "[data-test=checkout]"},
    )

    assert scenario.steps == ("Open basket",)
    assert edge.action.type == "click"


def test_unknown_resolution_must_match_state() -> None:
    with pytest.raises(ValidationError):
        UnknownItem(id="unknown-1", question="Which account?", resolved=True)


def test_repository_round_trip(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data" / "qualityproof.db")
    repository.initialize()
    requirement = Requirement(id="REQ-1", title="Login", description="Users can sign in.")

    repository.put("requirement", requirement.id, requirement)

    assert repository.get("requirement", requirement.id, Requirement) == requirement
    assert repository.get("requirement", "missing", Requirement) is None


def test_config_rejects_secret_values(tmp_path: Path) -> None:
    (tmp_path / "qualityproof.toml").write_text(
        '[project]\napi_key = "must-not-be-here"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="secret-like"):
        load_config(tmp_path)


def test_schema_export_is_versioned(tmp_path: Path) -> None:
    written = export_schemas(tmp_path)

    assert len(written) == len(SCHEMA_MODELS)
    assert all(path.parent.name == "v1" for path in written)
    assert all(path.read_text(encoding="utf-8").endswith("\n") for path in written)


def test_cli_init_creates_local_foundation(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["init", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / "qualityproof.toml").is_file()
    assert (tmp_path / ".qualityproof" / "qualityproof.db").is_file()
    assert (tmp_path / ".qualityproof" / "schemas" / "v1").is_dir()


def test_jira_command_exposes_governed_subcommands() -> None:
    result = CliRunner().invoke(app, ["jira", "--help"])

    assert result.exit_code == 0
    assert "sync" in result.output
    assert "auth-url" in result.output


def test_every_fixture_is_exported_for_star_import() -> None:
    """A fixture missing from __all__ is invisible to the generated conftest.

    The generated conftest re-exports this module with a star import, so an
    unexported fixture fails at collection time with a confusing "fixture not
    found" for every test in the suite.
    """
    from qualityproof import fixtures

    # Identify fixtures by pytest's own marker rather than by "looks wrapped":
    # a re-exported helper is also a wrapped callable, and treating it as a
    # fixture made this test fail for the wrong reason.
    defined = {
        name
        for name, value in vars(fixtures).items()
        if not name.startswith("_") and hasattr(value, "_fixture_function_marker")
    }
    assert defined, "expected to discover pytest fixtures in the module"
    assert defined <= set(fixtures.__all__), sorted(defined - set(fixtures.__all__))
