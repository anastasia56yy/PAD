# syntax=docker/dockerfile:1
# Libraria notification-service: notifications
# Build from the repository root:
#   docker build -f lab3/docker/notification-service/Dockerfile -t libraria/notification-service:1.0 .
# The service code itself lives in lab2/services/notification-service (lab2); this lab only packages and deploys it.

# ---- stage 1: dependencies (cached until requirements.txt changes) ----
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY lab2/common/requirements.txt /tmp/requirements.txt
RUN pip install --prefix=/install -r /tmp/requirements.txt

# ---- stage 2: runtime (no pip cache, no build files) ----
FROM python:3.12-slim
LABEL org.opencontainers.image.title="libraria-notification-service" \
      org.opencontainers.image.description="notifications" \
      org.opencontainers.image.source="https://github.com/anastasia56yy/PAD"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    SERVICE_NAME=notification-service
WORKDIR /app

COPY --from=deps /install /usr/local
# shared library first, service code last: a code change rebuilds only the last layer
COPY lab2/common/utm_common ./utm_common
COPY lab2/services/notification-service/app ./app

# never run as root
RUN useradd --create-home --uid 10001 appuser
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status == 200 else 1)"]
# configuration and secrets come from the environment (ConfigMap / Secret in Kubernetes), never from the image
ENTRYPOINT ["uvicorn", "app.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
