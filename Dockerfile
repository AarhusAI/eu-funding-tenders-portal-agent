FROM python:3.12-slim AS base

ARG APP_UID=1000
ARG APP_GID=1000

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN addgroup --system --gid ${APP_GID} appuser \
 && adduser --system --no-create-home --uid ${APP_UID} --ingroup appuser appuser

WORKDIR /app
COPY pyproject.toml ./


FROM base AS dev
RUN pip install --no-cache-dir ".[dev]"
USER appuser
COPY app/ app/
EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1
# --reload restarts the process on every code change, dropping the corpus when
# CACHE_BACKEND=memory (the default), so the first request after a save re-pays
# the ~11 s cold fetch. `task up:redis` avoids that — the corpus outlives the
# reload. See app/services/cache.py.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM base AS prod
RUN pip install --no-cache-dir .
USER appuser
COPY app/ app/
EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1
# Single-process because CACHE_BACKEND defaults to "memory", which keeps the
# topic corpus in this process's heap: --workers would give each worker its own
# copy (~4.2 MB resident each) and make each pay its own ~11 s cold fetch.
#
# This is a property of the default backend, not a permanent constraint. Set
# CACHE_BACKEND=redis and the corpus is shared, at which point --workers (and
# multiple replicas) are fine. Do not add workers without doing that first.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
