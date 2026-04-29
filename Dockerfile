FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SKR_CRYPTO_HOME=/var/lib/skr-crypto

WORKDIR /app

# curl is needed for the HEALTHCHECK below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy the package + project metadata, install with the [server] extra
# so fastapi / tronpy / uvicorn land in site-packages.
COPY pyproject.toml README.md ./
COPY skr_crypto/ skr_crypto/

RUN pip install ".[server]"

# Capture the build sha so /api/v1/version reports it. Override at
# build time:  docker build --build-arg GIT_SHA=$(git rev-parse --short=12 HEAD)
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

# Run as a non-root user; the private key lives in memory only and we
# never write to /app at runtime. Persistent state goes to
# $SKR_CRYPTO_HOME (mount it as a volume).
RUN useradd --create-home --no-log-init --uid 1000 appuser \
 && mkdir -p "${SKR_CRYPTO_HOME}/data" \
 && chown -R appuser:appuser "${SKR_CRYPTO_HOME}"

USER appuser
WORKDIR ${SKR_CRYPTO_HOME}

EXPOSE 8000

# Liveness probe — hits the unauthenticated /health/live endpoint so
# we don't have to bake the API token into the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent --show-error \
        "http://127.0.0.1:${SERVER_PORT:-8000}/api/v1/health/live" || exit 1

# Run the server. The CLI is also installed (skr-crypto …) for ad-hoc
# use via `docker exec`.
ENTRYPOINT ["skr-crypto-server"]
