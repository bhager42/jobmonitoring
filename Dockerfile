# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.14
ARG BASE_REGISTRY=docker.io/library
# true: include Playwright + headless Chromium for JavaScript-heavy careers pages (~+300 MB).
# false: the "-lite" image; those pages can't be read, everything else works.
ARG WITH_BROWSER=true

FROM ${BASE_REGISTRY}/python:${PYTHON_VERSION}-slim-bookworm AS build
ARG UV_VERSION=0.13.1
ARG WITH_BROWSER
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project $([ "$WITH_BROWSER" = "true" ] && echo --extra browser)
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable $([ "$WITH_BROWSER" = "true" ] && echo --extra browser)

FROM ${BASE_REGISTRY}/python:${PYTHON_VERSION}-slim-bookworm AS runtime
ARG WITH_BROWSER
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/bhager42/jobmonitoring"
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data \
    PORT=8080
COPY --from=build /app/.venv /app/.venv
# Headless shell only: the full Chromium build is not needed for headless use.
RUN if [ "$WITH_BROWSER" = "true" ]; then \
      playwright install --with-deps --only-shell chromium \
      && rm -rf /var/lib/apt/lists/* \
      && chmod -R a+rX /ms-playwright; \
    fi
# TrueNAS SCALE's default apps user and group are 568.
RUN groupadd -g 568 apps \
    && useradd -u 568 -g 568 -M -d /data -s /usr/sbin/nologin apps \
    && mkdir -p /data \
    && chown 568:568 /data
USER 568:568
WORKDIR /app
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8080')}/healthz\", timeout=4)"]
# The slim base already ships ca-certificates and tzdata. Run with `init: true` (Docker's built-in
# tini) so child processes such as Chromium are reaped; both compose files set it.
CMD ["jobmonitor", "serve"]
