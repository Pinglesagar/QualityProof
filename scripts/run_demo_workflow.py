"""Run the complete local deterministic QualityProof workflow against both demo versions."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import httpx

from scripts.benchmark_demo import run_benchmark

ROOT = Path(__file__).parents[1]


def _run(
    arguments: Sequence[str],
    *,
    project: Path,
    environment: dict[str, str],
    log: list[str],
    allowed_exit_codes: tuple[int, ...] = (0,),
) -> None:
    command = [sys.executable, "-m", "qualityproof.cli", *arguments]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    rendered = f"$ {' '.join(command)}\n{completed.stdout}{completed.stderr}".rstrip()
    log.append(rendered)
    if completed.returncode not in allowed_exit_codes:
        raise RuntimeError(f"workflow command failed ({completed.returncode}):\n{rendered}")


@contextmanager
def _server(version: str, port: int, environment: dict[str, str]) -> Iterator[str]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "demo.app",
            "--version",
            version,
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise RuntimeError(f"demo {version} failed to start:\n{output}")
        try:
            if httpx.get(f"{base_url}/products", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        process.terminate()
        raise RuntimeError(f"demo {version} did not become ready")
    try:
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_workflow(project: Path, output: Path, port: int = 8765) -> dict[str, object]:
    """Reset artifacts and execute init through benchmark with no network dependency."""
    started = time.perf_counter()
    if project.exists():
        shutil.rmtree(project)
    project.mkdir(parents=True)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    environment = dict(os.environ)
    # Two identities are crawled deliberately. A permission regression is only
    # observable from outside the privilege boundary: an administrator sees an
    # admin route return 200 both before and after the guard is removed, so a
    # single privileged crawl cannot record the defect at all.
    environment["QUALITYPROOF_SHOPPER_USERNAME"] = "shopper@example.test"
    environment["QUALITYPROOF_SHOPPER_PASSWORD"] = "shopper-demo"
    environment["QUALITYPROOF_ADMIN_USERNAME"] = "admin@example.test"
    environment["QUALITYPROOF_ADMIN_PASSWORD"] = "admin-demo"
    environment["QUALITYPROOF_USERNAME"] = "admin@example.test"
    environment["QUALITYPROOF_PASSWORD"] = "admin-demo"
    environment["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{environment.get('PATH', '')}"
    log: list[str] = []
    custom = project / "scenarios" / "custom"
    custom.mkdir(parents=True)
    (custom / "test_version_contracts.py").write_text(
        """import os
import re

import httpx


def _client():
    client = httpx.Client(base_url=os.environ["QUALITYPROOF_DEMO_BASE_URL"],
                          follow_redirects=True)
    response = client.post("/login", data={
        "email": "shopper@example.test", "password": "shopper-demo"})
    assert response.status_code == 200
    return client


def test_profile_validation():
    client = _client()
    response = client.post("/profile", data={
        "display_name": "Sam", "contact_email": "invalid", "phone": "123"})
    assert "Enter a valid contact email" in response.text


def test_checkout_total():
    client = _client()
    client.post("/cart/add", data={"product_id": "1", "quantity": "2"})
    matched = re.search(r'data-testid="total">£([^<]+)', client.get("/checkout").text)
    assert matched is not None
    assert matched.group(1) == "28.00"
""",
        encoding="utf-8",
    )

    _run(["init", "--project", str(project)], project=project, environment=environment, log=log)
    application_path = project / "application.json"

    with _server("v1", port, environment) as base_url:
        discovery = [
            "discover",
            f"{base_url}/products",
            "--project",
            str(project),
            "--login-url",
            f"{base_url}/login",
            "--username-selector",
            "#email",
            "--password-selector",
            "#password",
            "--submit-selector",
            "button[type=submit]",
            "--login-submit-method",
            "POST",
            "--login-submit-path",
            "/login",
            "--role",
            "shopper:QUALITYPROOF_SHOPPER_USERNAME:QUALITYPROOF_SHOPPER_PASSWORD",
            "--save-storage-state",
            str(project / ".qualityproof" / "auth" / "role.json"),
            "--role",
            "admin:QUALITYPROOF_ADMIN_USERNAME:QUALITYPROOF_ADMIN_PASSWORD",
            "--allowed-domain",
            "127.0.0.1",
            "--max-pages",
            "30",
            "--max-depth",
            "4",
            "--max-actions",
            "60",
            "--max-runtime",
            "30",
        ]
        _run(discovery, project=project, environment=environment, log=log)
        _run(
            [
                "plan",
                "--project",
                str(project),
                "--scenario-role",
                "shopper",
                "--requirements",
                str(ROOT / "examples" / "demo-requirements.yaml"),
            ],
            project=project,
            environment=environment,
            log=log,
        )
        _run(
            [
                "review",
                "--project",
                str(project),
                "--decision",
                "approve",
                "--actor",
                "demo-reviewer",
                "--reason",
                "Controlled local benchmark fixture",
            ],
            project=project,
            environment=environment,
            log=log,
        )
        _run(
            ["generate", "--project", str(project)],
            project=project,
            environment=environment,
            log=log,
        )
        environment["QUALITYPROOF_DEMO_BASE_URL"] = base_url
        environment["QUALITYPROOF_BASE_URL"] = base_url
        # Generated tests reuse the crawl's authenticated session rather than
        # logging in per test: one login, no inter-test coupling.
        environment["QUALITYPROOF_STORAGE_STATE"] = str(
            project / ".qualityproof" / "auth" / "shopper.json"
        )
        _run(["test", "--project", str(project)], project=project, environment=environment, log=log)
        # Provenance sources must resolve *inside* the project, and requirement
        # identifiers must exist in the registry. Both were previously satisfied by
        # accident: sources resolved through a working-directory fallback and no
        # registry gate applied, so the demo's VERIFIED rows were not earned.
        seed_destination = project / "demo"
        seed_destination.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "demo" / "seeded-defects.json", seed_destination)
        _run(
            [
                "requirements",
                "import",
                str(project / "demo" / "seeded-defects.json"),
                "--project",
                str(project),
                "--scope",
                "demo-seed-manifest",
            ],
            project=project,
            environment=environment,
            log=log,
        )
        _run(
            ["audit", str(ROOT / "tests" / "test_demo_app.py"), "--project", str(project)],
            project=project,
            environment=environment,
            log=log,
        )
        # Audit the generated suite too. Without this the generate-to-audit edge of
        # the pipeline carries nothing: generated tests would never appear in the
        # ledger, and the traceability metadata they now emit would go unread.
        # `audit` scopes its ledger set per path, so this merges rather than
        # replacing the hand-written audit above.
        _run(
            [
                "audit",
                str(project / ".qualityproof" / "generated"),
                "--project",
                str(project),
            ],
            project=project,
            environment=environment,
            log=log,
        )
        _run(
            ["coverage", "--project", str(project), "--fail-on-orphans"],
            project=project,
            environment=environment,
            log=log,
        )
        _run(
            ["report", "--project", str(project)], project=project, environment=environment, log=log
        )
        application_path.write_text('{"demo_version":"v1"}\n', encoding="utf-8")
        _run(
            [
                "snapshot",
                "create",
                "demo-v1",
                "--project",
                str(project),
                "--application",
                str(application_path),
            ],
            project=project,
            environment=environment,
            log=log,
        )

    with _server("v2", port, environment) as base_url:
        discovery[1] = f"{base_url}/products"
        login_index = discovery.index("--login-url") + 1
        discovery[login_index] = f"{base_url}/login"
        _run(discovery, project=project, environment=environment, log=log)
        environment["QUALITYPROOF_DEMO_BASE_URL"] = base_url
        environment["QUALITYPROOF_BASE_URL"] = base_url
        # Generated tests reuse the crawl's authenticated session rather than
        # logging in per test: one login, no inter-test coupling.
        environment["QUALITYPROOF_STORAGE_STATE"] = str(
            project / ".qualityproof" / "auth" / "shopper.json"
        )
        _run(
            ["test", "--project", str(project)],
            project=project,
            environment=environment,
            log=log,
            allowed_exit_codes=(1,),
        )
        application_path.write_text('{"demo_version":"v2"}\n', encoding="utf-8")
        _run(
            [
                "snapshot",
                "create",
                "demo-v2",
                "--project",
                str(project),
                "--application",
                str(application_path),
            ],
            project=project,
            environment=environment,
            log=log,
        )
    _run(
        [
            "diff",
            "demo-v1",
            "demo-v2",
            "--project",
            str(project),
            "--format",
            "markdown",
        ],
        project=project,
        environment=environment,
        log=log,
    )
    (output / "workflow.log").write_text("\n\n".join(log) + "\n", encoding="utf-8")
    elapsed = time.perf_counter() - started
    benchmark = run_benchmark(project, output, workflow_runtime_seconds=elapsed)
    summary = {
        "schema_version": "qualityproof-demo-workflow/v1",
        "status": "passed",
        "project": str(project),
        "output": str(output),
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "commands_run": len(log),
        "benchmark": benchmark,
    }
    (output / "workflow-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT / ".qualityproof" / "demo-workflow")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmark-results")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(json.dumps(run_workflow(args.project, args.output, args.port), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
