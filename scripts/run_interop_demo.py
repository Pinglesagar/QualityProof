"""Prove the cross-language evidence path end to end, locally and offline.

One demo application, two runners, one ledger. The TypeScript suite executes
under Playwright's own runner and reports through the QualityProof reporter; the
Python engine then applies exactly the trust rules it applies to its own tests.
The point of the demo is that nothing is relaxed at the boundary: an annotated
TypeScript test can reach VERIFIED, an under-attributed one is PARTIAL, and an
unannotated one is UNKNOWN.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn

from demo.app import DemoVersion, create_app
from qualityproof.config import load_config
from qualityproof.external import ingest_manifest, read_manifest
from qualityproof.repository import SQLiteRepository

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "packages" / "qualityproof-playwright"


@contextmanager
def _serve(version: DemoVersion, port: int) -> Iterator[str]:
    config = uvicorn.Config(create_app(version), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/products", timeout=1.0)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        raise RuntimeError("demo application did not start")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def run_interop(project: Path, port: int = 8765) -> dict[str, object]:
    if shutil.which("npx") is None:
        raise RuntimeError("npx is required; install Node.js 20 or newer")
    if not (PACKAGE / "dist" / "reporter.js").is_file():
        raise RuntimeError(
            "build the adapter first: npm --prefix packages/qualityproof-playwright run build"
        )
    if project.exists():
        shutil.rmtree(project)
    project.mkdir(parents=True)
    subprocess.run(
        [sys.executable, "-m", "qualityproof.cli", "init", "--project", str(project)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    # The engine resolves provenance sources relative to the project, so the
    # requirements the TypeScript suite cites must be present there.
    destination = project / "example"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy(PACKAGE / "example" / "requirements.yaml", destination / "requirements.yaml")

    manifest_path = project / ".qualityproof" / "external" / "playwright-run.json"
    with _serve("v1", port) as base_url:
        completed = subprocess.run(
            [
                "npx",
                "playwright",
                "test",
                "-c",
                "example/playwright.config.ts",
                "--project=chromium",
            ],
            cwd=PACKAGE,
            env={
                **os.environ,
                "QUALITYPROOF_BASE_URL": base_url,
                "QUALITYPROOF_EXTERNAL_OUTPUT": str(manifest_path),
            },
            capture_output=True,
            text=True,
            check=False,
        )
    if not manifest_path.is_file():
        raise RuntimeError(
            f"reporter wrote no manifest (exit {completed.returncode}):\n"
            f"{completed.stdout}{completed.stderr}"
        )

    config = load_config(project)
    repository = SQLiteRepository(project / config.database_path)
    repository.initialize()
    manifest = read_manifest(manifest_path, project)
    entries = ingest_manifest(manifest, project, repository)
    counts = Counter(entry.status.value for entry in entries)
    return {
        "schema_version": "qualityproof-interop-demo/v1",
        "playwright_exit_code": completed.returncode,
        "manifest": str(manifest_path.relative_to(project)),
        "framework": manifest.framework.value,
        "artifact_policy": manifest.artifact_policy,
        "tests_ingested": len(entries),
        "ledger": {
            "VERIFIED": counts["VERIFIED"],
            "PARTIAL": counts["PARTIAL"],
            "UNKNOWN": counts["UNKNOWN"],
        },
        "entries": [
            {"status": entry.status.value, "name": entry.test.name, "reason": entry.reason}
            for entry in sorted(entries, key=lambda item: item.id)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT / ".qualityproof" / "interop-demo")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(json.dumps(run_interop(args.project, args.port), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
