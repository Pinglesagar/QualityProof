"""Non-secret project configuration."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from qualityproof.security import reject_custom_path

CONFIG_FILENAME = "qualityproof.toml"
SECRET_MARKERS = ("secret", "token", "password", "api_key", "apikey", "credential")


class ProjectConfig(BaseModel):
    """Safe-to-store settings. Credentials remain in environment variables."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database_path: Path = Path(".qualityproof/qualityproof.db")
    schema_directory: Path = Path(".qualityproof/schemas")
    report_directory: Path = Path(".qualityproof/reports")
    environment_variable_prefix: str = Field(default="QUALITYPROOF_", min_length=1)

    #: Origin the generated suite runs against. Bound at run time to the origin
    #: already recorded in each approved scenario, so configuration can select an
    #: environment but can never redirect a test at a different host.
    base_url: str | None = None
    #: Web-first assertion timeout. Pinned in configuration rather than left to
    #: the Playwright default so a slow environment is retuned in one place
    #: instead of by scattering explicit waits through generated tests.
    expect_timeout_ms: int = Field(default=5_000, ge=250, le=120_000)
    action_timeout_ms: int = Field(default=10_000, ge=250, le=120_000)
    navigation_timeout_ms: int = Field(default=30_000, ge=250, le=300_000)
    #: Viewports measured for layout overflow during discovery.
    viewports: tuple[str, ...] = ("375x812", "768x1024", "1280x800")
    #: Worker count for test execution; "auto" defers to the host CPU count.
    #: Defaults to serial because xdist only pays off above roughly 0.04 s per
    #: test — every worker re-collects, so on a fast suite "auto" measured about
    #: three times slower than one process. Raise it for browser suites, where
    #: per-test cost is far above the break-even.
    workers: str = Field(default="1", min_length=1)
    #: Reruns granted to a failing test. A test that only passes on a rerun is
    #: recorded FLAKY, never PASS, so retries buy signal instead of hiding it.
    retries: int = Field(default=0, ge=0, le=5)
    locale: str = Field(default="en-GB", min_length=2)
    timezone_id: str = Field(default="Europe/London", min_length=3)


def _reject_secret_keys(value: object, path: tuple[str, ...] = ()) -> None:
    if not isinstance(value, dict):
        return
    for raw_key, nested in value.items():
        key = str(raw_key).lower()
        qualified = ".".join((*path, str(raw_key)))
        if any(marker in key for marker in SECRET_MARKERS):
            raise ValueError(f"secret-like configuration key is not allowed: {qualified}")
        _reject_secret_keys(nested, (*path, str(raw_key)))


def load_config(project_directory: Path, environ: dict[str, str] | None = None) -> ProjectConfig:
    """Load project settings and non-secret path overrides."""
    source = project_directory / CONFIG_FILENAME
    data: dict[str, Any] = {}
    if source.exists():
        parsed = tomllib.loads(source.read_text(encoding="utf-8"))
        _reject_secret_keys(parsed)
        project_section = parsed.get("project", {})
        if not isinstance(project_section, dict):
            raise ValueError("[project] must be a TOML table")
        data.update(project_section)

    env = os.environ if environ is None else environ
    if database_path := env.get("QUALITYPROOF_DATABASE_PATH"):
        data["database_path"] = Path(database_path)
    if schema_directory := env.get("QUALITYPROOF_SCHEMA_DIRECTORY"):
        data["schema_directory"] = Path(schema_directory)
    if report_directory := env.get("QUALITYPROOF_REPORT_DIRECTORY"):
        data["report_directory"] = Path(report_directory)
    if base_url := env.get("QUALITYPROOF_BASE_URL"):
        data["base_url"] = base_url
    if workers := env.get("QUALITYPROOF_WORKERS"):
        data["workers"] = workers
    if retries := env.get("QUALITYPROOF_RETRIES"):
        data["retries"] = retries
    config = ProjectConfig.model_validate(data)
    reject_custom_path(project_directory, config.database_path, "database")
    reject_custom_path(project_directory, config.schema_directory, "schema output")
    reject_custom_path(project_directory, config.report_directory, "report output")
    return config


def write_default_config(project_directory: Path) -> Path:
    """Create a configuration file containing no credential values."""
    path = project_directory / CONFIG_FILENAME
    if path.exists():
        raise FileExistsError(f"configuration already exists: {path}")
    path.write_text(
        '[project]\n'
        'database_path = ".qualityproof/qualityproof.db"\n'
        'schema_directory = ".qualityproof/schemas"\n'
        'report_directory = ".qualityproof/reports"\n'
        'environment_variable_prefix = "QUALITYPROOF_"\n'
        'expect_timeout_ms = 5000\n'
        'action_timeout_ms = 10000\n'
        'navigation_timeout_ms = 30000\n'
        'viewports = ["375x812", "768x1024", "1280x800"]\n'
        'workers = "auto"\n'
        'retries = 0\n'
        'locale = "en-GB"\n'
        'timezone_id = "Europe/London"\n',
        encoding="utf-8",
    )
    return path
