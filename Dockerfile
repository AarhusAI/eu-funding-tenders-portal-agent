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
# --reload drops the in-process topic corpus on every code change, so the first
# request after a save re-pays the cold fetch (see app/services/corpus.py).
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM base AS prod
RUN pip install --no-cache-dir .
USER appuser
COPY app/ app/
EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1
# Deliberately single-process: the topic corpus is cached in the process heap
# (app/services/corpus.py), so adding --workers would give each worker its own
# copy — N times the memory and N cold-start fetches of ~20 MB each.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
