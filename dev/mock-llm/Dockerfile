FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

RUN addgroup --system --gid ${APP_GID} appuser \
 && adduser --system --no-create-home --uid ${APP_UID} --ingroup appuser appuser

WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115.0" "uvicorn[standard]>=0.30.0"

USER appuser

COPY main.py script.py ./

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
