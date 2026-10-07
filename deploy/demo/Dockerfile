# Allur twin 2.0 — one-container online demo (Render or any Docker host).
# Build from the repository root:
#   docker build -f deploy/demo/Dockerfile -t allur-twin-demo .
#   docker run -p 7860:7860 -e JURY_PASSWORD="your passphrase of 15+ characters" allur-twin-demo
# Required: JURY_PASSWORD. Optional: JURY_USERNAME (default jury), JURY_ROLE (operator or viewer), PUBLIC_ORIGIN.
# The platform's PORT variable overrides the default port 7860.
FROM node:22.16.0-alpine AS web
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=7860 STATIC_DIR=/app/static
WORKDIR /app
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd --create-home --uid 1000 user
COPY backend/app /app/app
COPY deploy/demo/serve.py /app/serve.py
COPY --from=web /build/dist /app/static
USER user
EXPOSE 7860
CMD ["python", "serve.py"]
