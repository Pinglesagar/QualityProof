# Registry digest verified from MCR's Docker-Content-Digest header; update per pinning guide.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble@sha256:aa81288e738725378becba5b3e06cb0f3a7f012a610e87e8d767a090ea3f740d AS runtime

ARG UV_VERSION=0.8.22
ARG QUALITYPROOF_REVISION=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    QUALITYPROOF_REVISION="${QUALITYPROOF_REVISION}"

WORKDIR /app

# Patch the base image's OS packages. The upstream Playwright image is a full
# Ubuntu userland and accumulates fixable CVEs between releases; the image scan
# gates on CRITICAL and HIGH with unfixed findings already excluded, so anything
# it reports is something a patch layer can actually resolve. Applying the patch
# is the correct response -- loosening the gate to go green would be reporting a
# clean scan we had not earned.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY demo ./demo
COPY scripts ./scripts
COPY examples ./examples
COPY tests ./tests
RUN uv sync --frozen --no-dev && chown -R pwuser:pwuser /app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

FROM runtime AS control
USER pwuser
CMD ["uvicorn", "qualityproof.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM runtime AS browser-job
RUN uv sync --frozen && chown -R pwuser:pwuser /app
USER pwuser
HEALTHCHECK NONE
CMD ["python", "-m", "scripts.run_azure_job"]
