# Runtime and direct dependencies are version-pinned in remote/pyproject.toml.
# Keep this Python patch tag synchronized with the CI Python version.
FROM python:3.13.5-slim-bookworm AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY remote/pyproject.toml remote/pyproject.toml
COPY remote/src remote/src
RUN python -m pip install --prefix=/install ./remote

FROM python:3.13.5-slim-bookworm

ENV PYTHONPATH=/app/remote/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 10001 winnow \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin winnow

WORKDIR /app
COPY --from=build /install /usr/local
COPY --chown=winnow:winnow remote/src remote/src
# The remote publisher intentionally reuses the versioned portable compiler,
# runtime, and assets. No repository history, tests, or local probe code is
# included in the runtime image.
COPY --chown=winnow:winnow .agents/skills/winnow/assets .agents/skills/winnow/assets
COPY --chown=winnow:winnow .agents/skills/winnow/scripts .agents/skills/winnow/scripts

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"]

# Do not enable Uvicorn's proxy-header processing: TrustedProxyPolicy owns the
# one explicit forwarding-header trust decision. Scale with replicas against
# the shared Redis store rather than relying on process-local state.
CMD ["python", "-m", "uvicorn", "winnow_remote.runtime:create_application", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers"]
