# Siloquy — Discord stats channels for Silo
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install pinned dependencies from prebuilt wheels only (no build scripts run).
COPY requirements.txt .
RUN pip install --only-binary=:all: --no-deps -r requirements.txt \
    && pip uninstall -y pip setuptools wheel || true

COPY siloquy ./siloquy

# Unprivileged user with no shell and no home directory.
RUN groupadd --system --gid 10001 siloquy \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin siloquy \
    && mkdir -p /data && chown 10001:10001 /data && chmod 700 /data

USER 10001:10001
VOLUME ["/data"]

# Healthy while the update loop has ticked within the last 5 minutes.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/siloquy-heartbeat')<300 else 1)"]

ENTRYPOINT ["python", "-m", "siloquy"]
