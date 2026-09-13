FROM python:3.12-slim AS runtime
ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision=$VCS_REF
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY src ./src
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir -c requirements.lock '.[llm]' && useradd --uid 10001 --create-home harness
USER 10001
FROM runtime AS api
EXPOSE 8000
CMD ["uvicorn", "credit_harness.production.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "35"]
FROM runtime AS worker
CMD ["python", "-m", "credit_harness.production.workers", "agent"]
