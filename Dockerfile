FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --uid 10001 --create-home exochain
COPY agents agents
COPY config config
COPY core core
COPY data_sources data_sources
COPY models models
COPY optimization optimization
COPY orchestrator orchestrator
COPY services services
COPY scripts scripts
COPY dashboard/backend dashboard/backend
RUN mkdir -p /app/data && chown 10001:10001 /app/data
USER 10001:10001
CMD ["python", "-m", "uvicorn", "dashboard.backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "100", "--timeout-graceful-shutdown", "180", "--no-access-log"]
