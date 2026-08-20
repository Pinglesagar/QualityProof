"""Minimal read-only report API with an explicitly enabled run queue."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from qualityproof import __version__
from qualityproof.security import EvidenceRedactor

_MINIMUM_TOKEN_LENGTH = 32


def _is_strong_token(value: str | None) -> bool:
    return value is not None and len(value) >= _MINIMUM_TOKEN_LENGTH


class ServiceSettings(BaseModel):
    """Environment-derived service settings; secret values are never persisted."""

    model_config = ConfigDict(frozen=True)

    project_directory: Path = Path(".")
    benchmark_directory: Path = Path("benchmark-results")
    run_queue_directory: Path = Path(".qualityproof/run-queue")
    run_submission_enabled: bool = False
    report_access_enabled: bool = False
    api_token: str | None = None
    report_token: str | None = None
    revision: str = "local"
    storage_account_url: str | None = None
    evidence_container: str = "evidence"
    run_queue_url: str | None = None

    @classmethod
    def from_environment(cls) -> ServiceSettings:
        enabled = os.getenv("QUALITYPROOF_RUN_SUBMISSION_ENABLED", "").lower() in {
            "1",
            "true",
            "yes",
        }
        report_enabled = os.getenv("QUALITYPROOF_REPORT_ACCESS_ENABLED", "").lower() in {
            "1",
            "true",
            "yes",
        }
        return cls(
            project_directory=Path(os.getenv("QUALITYPROOF_PROJECT_DIRECTORY", ".")),
            benchmark_directory=Path(
                os.getenv("QUALITYPROOF_BENCHMARK_DIRECTORY", "benchmark-results")
            ),
            run_queue_directory=Path(
                os.getenv("QUALITYPROOF_RUN_QUEUE_DIRECTORY", ".qualityproof/run-queue")
            ),
            run_submission_enabled=enabled,
            report_access_enabled=report_enabled,
            api_token=os.getenv("QUALITYPROOF_API_TOKEN"),
            report_token=os.getenv("QUALITYPROOF_REPORT_TOKEN"),
            revision=os.getenv("QUALITYPROOF_REVISION", "local"),
            storage_account_url=os.getenv("QUALITYPROOF_STORAGE_ACCOUNT_URL"),
            evidence_container=os.getenv("QUALITYPROOF_EVIDENCE_CONTAINER", "evidence"),
            run_queue_url=os.getenv("QUALITYPROOF_RUN_QUEUE_URL"),
        )


class RunSubmission(BaseModel):
    """Metadata for a fixed, controlled demo run; arbitrary commands are not accepted."""

    model_config = ConfigDict(extra="forbid")

    requested_by: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class RunReceipt(BaseModel):
    run_id: str
    status: str
    submitted_at: datetime


def _read_json(path: Path, description: str) -> JSONResponse:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {description} is available.",
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The latest {description} is unavailable.",
        ) from error
    return JSONResponse(content=EvidenceRedactor.from_environment().value(content))


def _read_blob(settings: ServiceSettings, name: str, description: str) -> JSONResponse:
    if settings.storage_account_url is None:
        raise RuntimeError("blob storage was not configured")
    try:
        credential = DefaultAzureCredential()
        service = BlobServiceClient(settings.storage_account_url, credential=credential)
        blob = service.get_blob_client(settings.evidence_container, name)
        payload = blob.download_blob().readall()
        content = json.loads(payload)
    except Exception as error:
        # Azure SDK exception details can contain infrastructure metadata; return a fixed message.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The latest {description} is unavailable.",
        ) from error
    return JSONResponse(content=EvidenceRedactor.from_environment().value(content))


def create_app(settings: ServiceSettings | None = None) -> FastAPI:
    """Build the service without requiring cloud credentials or SDKs."""

    configured = settings or ServiceSettings.from_environment()
    app = FastAPI(
        title="QualityProof control service",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/version")
    def version() -> dict[str, str]:
        return {"version": __version__, "revision": configured.revision}

    def authorize_report(authorization: Annotated[str | None, Header()] = None) -> None:
        if not configured.report_access_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Report access is disabled.",
            )
        report_token = configured.report_token
        if not _is_strong_token(report_token):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Report access is not securely configured.",
            )
        assert report_token is not None
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            supplied, report_token
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Valid bearer authentication is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get(
        "/reports/latest",
        response_class=JSONResponse,
        dependencies=[Depends(authorize_report)],
    )
    def latest_report() -> JSONResponse:
        if configured.storage_account_url:
            return _read_blob(configured, "reports/ledger.json", "report")
        return _read_json(
            configured.project_directory / ".qualityproof" / "reports" / "ledger.json",
            "report",
        )

    @app.get(
        "/benchmarks/latest",
        response_class=JSONResponse,
        dependencies=[Depends(authorize_report)],
    )
    def latest_benchmark() -> JSONResponse:
        if configured.storage_account_url:
            return _read_blob(configured, "benchmarks/benchmark.json", "benchmark")
        return _read_json(configured.benchmark_directory / "benchmark.json", "benchmark")

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if not configured.run_submission_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Run submission is disabled.",
            )
        api_token = configured.api_token
        if not _is_strong_token(api_token):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Run submission is not securely configured.",
            )
        assert api_token is not None
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Valid bearer authentication is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.post(
        "/runs",
        response_model=RunReceipt,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authorize)],
    )
    def submit_run(submission: RunSubmission) -> RunReceipt:
        submitted_at = datetime.now(UTC)
        receipt = RunReceipt(
            run_id=f"run-{uuid4().hex}",
            status="queued",
            submitted_at=submitted_at,
        )
        configured.run_queue_directory.mkdir(parents=True, exist_ok=True)
        destination = configured.run_queue_directory / f"{receipt.run_id}.json"
        document = {
            **receipt.model_dump(mode="json"),
            **submission.model_dump(mode="json"),
            "workflow": "controlled-demo",
        }
        if configured.run_queue_url:
            try:
                credential = DefaultAzureCredential()
                QueueClient.from_queue_url(
                    configured.run_queue_url,
                    credential=credential,
                ).send_message(json.dumps(document, sort_keys=True))
            except Exception as error:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="The run queue is unavailable.",
                ) from error
            return receipt
        destination.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return receipt

    return app


app = create_app()
