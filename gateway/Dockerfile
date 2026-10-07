FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home gateway && mkdir /data && chown gateway:gateway /data
WORKDIR /app
COPY bridge.py ./bridge.py
USER gateway
CMD ["python", "bridge.py"]
