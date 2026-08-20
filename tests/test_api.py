import json
from pathlib import Path

from fastapi.testclient import TestClient

from qualityproof.api import ServiceSettings, create_app

REPORT_TOKEN = "report-token-with-at-least-32-characters"
API_TOKEN = "correct-token-with-at-least-32-characters"


def _settings(tmp_path: Path, **overrides: object) -> ServiceSettings:
    values: dict[str, object] = {
        "project_directory": tmp_path / "project",
        "benchmark_directory": tmp_path / "benchmarks",
        "run_queue_directory": tmp_path / "queue",
    }
    values.update(overrides)
    return ServiceSettings.model_validate(values)


def test_health_and_version_disclose_no_configuration(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, revision="abc123")))

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/version").json() == {"version": "0.1.0", "revision": "abc123"}
    assert client.get("/openapi.json").status_code == 404


def test_latest_artifacts_are_read_only_and_fixed(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, report_access_enabled=True, report_token=REPORT_TOKEN
    )
    report = settings.project_directory / ".qualityproof" / "reports" / "ledger.json"
    benchmark = settings.benchmark_directory / "benchmark.json"
    report.parent.mkdir(parents=True)
    benchmark.parent.mkdir(parents=True)
    report.write_text('{"kind":"report"}\n', encoding="utf-8")
    benchmark.write_text('{"kind":"benchmark"}\n', encoding="utf-8")
    client = TestClient(create_app(settings))

    headers = {"Authorization": f"Bearer {REPORT_TOKEN}"}
    assert client.get("/reports/latest", headers=headers).json() == {"kind": "report"}
    assert client.get("/benchmarks/latest", headers=headers).json() == {"kind": "benchmark"}
    assert client.post("/reports/latest").status_code == 405


def test_missing_artifact_is_not_an_internal_error(tmp_path: Path) -> None:
    response = TestClient(
        create_app(
            _settings(tmp_path, report_access_enabled=True, report_token=REPORT_TOKEN)
        )
    ).get("/reports/latest", headers={"Authorization": f"Bearer {REPORT_TOKEN}"})

    assert response.status_code == 404
    assert response.json() == {"detail": "No report is available."}


def test_report_access_is_disabled_and_bearer_protected_by_default(tmp_path: Path) -> None:
    assert TestClient(create_app(_settings(tmp_path))).get("/reports/latest").status_code == 404
    client = TestClient(
        create_app(
            _settings(tmp_path, report_access_enabled=True, report_token=REPORT_TOKEN)
        )
    )
    assert client.get("/reports/latest").status_code == 401


def test_report_response_is_centrally_redacted(tmp_path: Path) -> None:
    sentinel = "QP_SENTINEL_REPORT_SECRET"
    settings = _settings(
        tmp_path, report_access_enabled=True, report_token=REPORT_TOKEN
    )
    report = settings.project_directory / ".qualityproof" / "reports" / "ledger.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps({"log": f"Bearer {sentinel}", "password": sentinel}),
        encoding="utf-8",
    )
    client = TestClient(create_app(settings))

    response = client.get(
        "/reports/latest", headers={"Authorization": f"Bearer {REPORT_TOKEN}"}
    )

    assert sentinel not in response.text
    assert response.json()["password"] == "<REDACTED>"


def test_run_submission_is_disabled_by_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    response = TestClient(create_app(settings)).post(
        "/runs",
        json={"requested_by": "operator", "reason": "release check"},
        headers={"Authorization": "Bearer guessed"},
    )

    assert response.status_code == 404
    assert not settings.run_queue_directory.exists()


def test_enabled_submission_requires_a_configured_valid_token(tmp_path: Path) -> None:
    unconfigured = TestClient(
        create_app(_settings(tmp_path, run_submission_enabled=True, api_token=None))
    )
    payload = {"requested_by": "operator", "reason": "release check"}

    assert unconfigured.post("/runs", json=payload).status_code == 503
    weak = TestClient(
        create_app(_settings(tmp_path, run_submission_enabled=True, api_token="short-token"))
    )
    assert (
        weak.post(
            "/runs",
            json=payload,
            headers={"Authorization": "Bearer short-token"},
        ).status_code
        == 503
    )

    configured = TestClient(
        create_app(_settings(tmp_path, run_submission_enabled=True, api_token=API_TOKEN))
    )
    unauthorized = configured.post(
        "/runs",
        json=payload,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"


def test_authenticated_submission_queues_only_fixed_workflow(tmp_path: Path) -> None:
    settings = _settings(tmp_path, run_submission_enabled=True, api_token=API_TOKEN)
    client = TestClient(create_app(settings))

    response = client.post(
        "/runs",
        json={"requested_by": "operator", "reason": "release check"},
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    )

    assert response.status_code == 202
    queued = tuple(settings.run_queue_directory.glob("*.json"))
    assert len(queued) == 1
    document = json.loads(queued[0].read_text(encoding="utf-8"))
    assert document["workflow"] == "controlled-demo"
    assert "command" not in document


def test_submission_rejects_arbitrary_command_fields(tmp_path: Path) -> None:
    settings = _settings(tmp_path, run_submission_enabled=True, api_token=API_TOKEN)
    response = TestClient(create_app(settings)).post(
        "/runs",
        json={"requested_by": "operator", "reason": "check", "command": "rm -rf /"},
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    )

    assert response.status_code == 422
