"""
Celery application — durable background generation
===================================================
Optional, opt-in task queue for document generation. Enabled by setting
`GENERATION_BACKEND=celery` (see generation_service). When unset, the app keeps
using in-process daemon threads exactly as before, so this module is inert until
you deploy a broker + worker.

Why a queue at all
------------------
`_run_generation_job` used to run only on an in-process daemon thread: a worker
restart/crash killed any in-flight job (main.py's startup sweep just marks those
`failed`). Routing the job through Celery makes it DURABLE — the job stays on the
broker until a worker acknowledges completion, and `task_acks_late` means a
crash mid-generation re-queues the job instead of losing it. `_run_generation_job`
is already idempotent (it resets stuck "generating" sections to pending and skips
already-completed ones), so at-least-once redelivery is safe.

Run a worker
------------
    # from the Data_Ingestion/ directory, same env as the API (DB, LLM creds):
    celery -A generation.celery_app worker --loglevel=INFO --concurrency=2

`--concurrency` = how many DOCUMENTS a worker generates at once. Each document
internally fans its sections out across GENERATION_CONCURRENCY threads, so keep
worker concurrency modest (2–4) to avoid oversubscribing the LLM quota.

Configuration (env)
-------------------
    CELERY_BROKER_URL      broker (default redis://localhost:6379/0)
    CELERY_RESULT_BACKEND  result store (default = broker URL)
    CELERY_TASK_QUEUE      queue name (default "generation")
    CELERY_VISIBILITY_TIMEOUT   Redis re-delivery window, seconds (default 3600)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Bootstrap: add Data_Ingestion/ to sys.path so `generation.*` imports resolve
# when the worker is launched as `celery -A generation.celery_app` ───────────────
_BASE = Path(__file__).parent.parent.resolve()   # …/Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# Load .env so a standalone worker process gets the same config as the API.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_BASE / ".env", override=False)
except Exception:
    pass

from celery import Celery

_BROKER  = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", _BROKER)
_QUEUE   = os.environ.get("CELERY_TASK_QUEUE", "generation")
# Redis visibility timeout MUST exceed the longest possible generation, or Redis
# re-delivers the task to a second worker while the first is still running it.
_VISIBILITY = int(os.environ.get("CELERY_VISIBILITY_TIMEOUT", "3600"))

celery_app = Celery(
    "intellidraft",
    broker=_BROKER,
    backend=_BACKEND,
    include=["generation.tasks"],   # explicit — task module is imported by the worker
)

celery_app.conf.update(
    task_default_queue=_QUEUE,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # ── Durability ───────────────────────────────────────────────────────────
    # ack the task only AFTER _run_generation_job returns, and re-queue it if the
    # worker dies mid-task. Combined with the idempotent job body, this is the
    # whole point of moving off in-process threads.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # ── Fairness / stability for long LLM tasks ──────────────────────────────
    worker_prefetch_multiplier=1,          # don't hoard tasks; one long job at a time
    task_track_started=True,
    worker_max_tasks_per_child=50,         # recycle workers to bound memory growth
    result_expires=24 * 3600,              # keep results 1 day
    broker_connection_retry_on_startup=True,
    # Redis-specific: re-delivery window must cover the slowest generation.
    broker_transport_options={"visibility_timeout": _VISIBILITY},
    result_backend_transport_options={"visibility_timeout": _VISIBILITY},
)
