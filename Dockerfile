# ══════════════════════════════════════════════════════════════════════════════
# IntelliDraft — API-only Docker Image (FastAPI, single container)
#
# Entry point : Data_Ingestion/main.py (FastAPI via gunicorn + uvicorn workers)
# Port        : 7071 local · $PORT in cloud
# Python      : 3.11-slim
#
# What this image contains:
#   - FastAPI REST API server (main.py) — the only server. No frontend is served;
#     the app is used/tested purely via the API (Postman) + Swagger at /docs.
#   - LibreOffice Writer for the DOCX→HTML preview endpoint (markdown2 fallback)
#   - Google ADK multi-agent system + parsers + document generation
#
# Build:  docker build -t intellidraft .
# Run:    docker compose up -d      (see docker-compose.yml)
#         docker run -p 7071:7071 --env-file Data_Ingestion/.env intellidraft
#
# The preview stack is fully synchronous — no Celery, no Redis. Generation runs
# wave-parallel inside the app by default (GENERATION_CONCURRENCY, default 4).
# Optional durable queue: set GENERATION_BACKEND=celery and run a worker from this
# SAME image — `celery -A generation.celery_app worker` (see docker-compose.yml).
# ══════════════════════════════════════════════════════════════════════════════

# ── Python runtime ───────────────────────────────────────────────────────────
FROM python:3.11-slim

# System libs. `apt-get upgrade` patches OS-package CVEs (libexpat, curl, perl-base,
# libc, …) flagged by container scanning. gcc/g++ are BUILD-ONLY (to compile wheels)
# and are purged after pip install below — this drops the binutils / linux-libc-dev
# toolchain from the shipped image (the bulk of the container-scan findings).
# Runtime libs (libglib2.0-0, libgl1, libgomp1 for PyMuPDF/Pillow/NumPy, curl for the
# healthcheck) are installed explicitly so `apt-get autoremove` never removes them.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        gcc g++ \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies (pywin32 is Windows-only — strip before installing on Linux).
# After the wheels are built, purge the compiler toolchain so it does not ship.
COPY Data_Ingestion/requirements.txt /tmp/requirements.txt
RUN grep -v "pywin32" /tmp/requirements.txt > /tmp/req_linux.txt \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/req_linux.txt \
    && apt-get purge -y --auto-remove gcc g++ \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip /tmp/req_linux.txt /tmp/requirements.txt

# Application code
COPY Data_Ingestion/ /app/Data_Ingestion/

# Pre-create local storage so it exists even before a volume is mounted
RUN mkdir -p /app/Data_Ingestion/local_storage

WORKDIR /app/Data_Ingestion

# PORT: cloud platforms inject $PORT; local Docker falls back to 7071
EXPOSE 7071

# Health check — lightweight /api/health (zero DB cost)
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD curl -sf http://localhost:${PORT:-7071}/api/health > /dev/null || exit 1

# ── Start: gunicorn + uvicorn ASGI workers (FastAPI) ─────────────────────────
# --worker-class uvicorn.workers.UvicornWorker : ASGI worker for main:app.
#   Sync `def` endpoints run in each worker's threadpool, so blocking
#   SQLAlchemy/litellm calls never block the event loop.
# --timeout 200 : derive-fields uses a 180s LLM timeout; 200s gives headroom.
CMD exec gunicorn \
    --workers=2 \
    --worker-class=uvicorn.workers.UvicornWorker \
    --bind=0.0.0.0:${PORT:-7071} \
    --timeout=200 \
    --access-logfile=- \
    main:app
