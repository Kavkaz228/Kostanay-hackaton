FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
COPY backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir pip==26.2.1 && pip install --no-cache-dir -r requirements.txt -c requirements.lock
COPY backend/app ./app

FROM base AS test
COPY backend/requirements-test.txt ./requirements-test.txt
RUN pip install --no-cache-dir -r requirements-test.txt
COPY backend/tests ./tests
CMD ["python", "-m", "pytest", "-q", "tests"]

FROM base AS runtime
LABEL org.opencontainers.image.title="Allur twin 2.0 API" org.opencontainers.image.version="2.0.0" org.opencontainers.image.description="Production model, telemetry, quality, SCADA emulation and local AI integration"
RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
