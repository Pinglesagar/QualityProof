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

# Runs before `uv sync`, deliberately. ENV PATH puts /app/.venv/bin first, and a
# uv-created virtual environment ships no pip, so once the venv exists
# `python -m pip` resolves to an interpreter that cannot satisfy it.
#
# setuptools and msgpack are upgraded here rather than because the application
# needs them: they arrive with the base image and its build tooling, and the image
# scan reported both as HIGH with fixes available -- setuptools CVE-2025-47273
# (path traversal) and msgpack GHSA-6v7p-g79w-8964 (out-of-bounds read). Neither
# is in uv.lock.
RUN python -m pip install --no-cache-dir \
        "uv==${UV_VERSION}" \
        "setuptools>=78.1.1" \
        "msgpack>=1.2.1"
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY demo ./demo
COPY scripts ./scripts
COPY examples ./examples
COPY tests ./tests
RUN uv sync --frozen --no-dev && chown -R pwuser:pwuser /app

# Build caches are inputs, not runtime files. Shipping them enlarges the image
# and, more to the point, leaves vulnerable wheels inside the published artifact
# even though nothing imports them -- which is exactly how the scan found a
# vulnerable setuptools that no lockfile mentions.
RUN rm -rf /root/.cache/uv /root/.cache/virtualenv /root/.cache/pip \
    && rm -rf /tmp/* /var/tmp/* || true

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

FROM runtime AS control
# Remove the build-time Python tooling. It carries the last two scan findings and
# nothing at runtime uses it: the control image runs uvicorn from /app/.venv and
# the job image runs python from the same venv.
#
# The findings survived upgrading the installed packages because the vulnerable
# copies are not the installed ones -- virtualenv embeds a setuptools seed wheel
# (CVE-2025-47273) and pip vendors msgpack via cachecontrol
# (GHSA-6v7p-g79w-8964). Upgrading dist-packages cannot reach a wheel bundled
# inside another package, so the code is removed instead.
#
# Deliberately done per final stage rather than in `runtime`: the browser-job
# stage runs `uv sync` again and needs the tooling until it has.
RUN rm -rf \
        /usr/local/lib/python3.12/dist-packages/pip \
        /usr/local/lib/python3.12/dist-packages/pip-*.dist-info \
        /usr/local/lib/python3.12/dist-packages/virtualenv \
        /usr/local/lib/python3.12/dist-packages/virtualenv-*.dist-info \
        /usr/local/lib/python3.12/dist-packages/virtualenv.py \
        /usr/local/lib/python3.12/dist-packages/python_discovery* \
        /usr/local/lib/python3.12/dist-packages/distlib* \
        /usr/local/lib/python3.12/dist-packages/filelock* \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
        /usr/local/bin/virtualenv \
    && rm -rf /root/.cache /tmp/* /var/tmp/* || true
USER pwuser
CMD ["uvicorn", "qualityproof.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM runtime AS browser-job
RUN uv sync --frozen && chown -R pwuser:pwuser /app
# Remove the build-time Python tooling. It carries the last two scan findings and
# nothing at runtime uses it: the control image runs uvicorn from /app/.venv and
# the job image runs python from the same venv.
#
# The findings survived upgrading the installed packages because the vulnerable
# copies are not the installed ones -- virtualenv embeds a setuptools seed wheel
# (CVE-2025-47273) and pip vendors msgpack via cachecontrol
# (GHSA-6v7p-g79w-8964). Upgrading dist-packages cannot reach a wheel bundled
# inside another package, so the code is removed instead.
#
# Deliberately done per final stage rather than in `runtime`: the browser-job
# stage runs `uv sync` again and needs the tooling until it has.
RUN rm -rf \
        /usr/local/lib/python3.12/dist-packages/pip \
        /usr/local/lib/python3.12/dist-packages/pip-*.dist-info \
        /usr/local/lib/python3.12/dist-packages/virtualenv \
        /usr/local/lib/python3.12/dist-packages/virtualenv-*.dist-info \
        /usr/local/lib/python3.12/dist-packages/virtualenv.py \
        /usr/local/lib/python3.12/dist-packages/python_discovery* \
        /usr/local/lib/python3.12/dist-packages/distlib* \
        /usr/local/lib/python3.12/dist-packages/filelock* \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
        /usr/local/bin/virtualenv \
    && rm -rf /root/.cache /tmp/* /var/tmp/* || true
USER pwuser
HEALTHCHECK NONE
CMD ["python", "-m", "scripts.run_azure_job"]
