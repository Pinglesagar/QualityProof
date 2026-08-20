"""Run the fixed controlled workflow and publish evidence using managed identity."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.storage.queue import QueueClient

from scripts.run_demo_workflow import run_workflow


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _receive_request(queue_url: str | None, credential: DefaultAzureCredential) -> Any | None:
    if not queue_url:
        return None
    queue = QueueClient.from_queue_url(queue_url, credential=credential)
    message = next(iter(queue.receive_messages(messages_per_page=1, visibility_timeout=1800)), None)
    return (queue, message) if message is not None else None


def _upload(
    service: BlobServiceClient,
    container: str,
    source: Path,
    destination: str,
    *,
    immutable: bool,
) -> None:
    content_type = {
        ".csv": "text/csv",
        ".html": "text/html",
        ".json": "application/json",
        ".log": "text/plain",
        ".md": "text/markdown",
    }.get(source.suffix, "application/octet-stream")
    blob = service.get_blob_client(container, destination)
    with source.open("rb") as handle:
        if immutable:
            blob.upload_blob(
                handle,
                overwrite=False,
                content_settings=ContentSettings(content_type=content_type),
            )
            return
        try:
            etag = blob.get_blob_properties().etag
        except ResourceNotFoundError:
            blob.upload_blob(
                handle,
                overwrite=False,
                content_settings=ContentSettings(content_type=content_type),
            )
        else:
            blob.upload_blob(
                handle,
                overwrite=True,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                content_settings=ContentSettings(content_type=content_type),
            )


def _validated_run_id(value: object | None) -> str:
    if value is None:
        return f"run-{uuid4().hex}"
    candidate = str(value)
    if not re.fullmatch(r"run-[0-9a-f]{32}", candidate):
        raise ValueError("queued run_id is not a QualityProof UUID")
    return candidate


def main() -> None:
    account_url = _required_environment("QUALITYPROOF_STORAGE_ACCOUNT_URL")
    container = os.getenv("QUALITYPROOF_EVIDENCE_CONTAINER", "evidence")
    credential = DefaultAzureCredential()
    queued = _receive_request(os.getenv("QUALITYPROOF_RUN_QUEUE_URL"), credential)

    with tempfile.TemporaryDirectory(prefix="qualityproof-") as temporary:
        root = Path(temporary)
        project = root / "project"
        output = root / "results"
        summary = run_workflow(project, output)
        run_id = _validated_run_id(None)
        if queued is not None:
            _, message = queued
            request = json.loads(message.content)
            run_id = _validated_run_id(request.get("run_id"))

        service = BlobServiceClient(account_url, credential=credential)
        _upload(
            service,
            container,
            project / ".qualityproof" / "reports" / "ledger.json",
            f"runs/{run_id}/ledger.json",
            immutable=True,
        )
        _upload(
            service,
            container,
            project / ".qualityproof" / "reports" / "ledger.json",
            "reports/ledger.json",
            immutable=False,
        )
        for artifact in output.iterdir():
            if artifact.is_file():
                _upload(
                    service,
                    container,
                    artifact,
                    f"runs/{run_id}/{artifact.name}",
                    immutable=True,
                )
                _upload(
                    service,
                    container,
                    artifact,
                    f"benchmarks/{artifact.name}",
                    immutable=False,
                )
        summary_path = output / "azure-job-summary.json"
        summary_path.write_text(
            json.dumps({"run_id": run_id, "workflow": summary}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _upload(
            service,
            container,
            summary_path,
            f"runs/{run_id}/azure-job-summary.json",
            immutable=True,
        )

    if queued is not None:
        queue, message = queued
        queue.delete_message(message)


if __name__ == "__main__":
    main()
