"""
Celery tasks — document generation
===================================
Thin task wrappers around the existing generation service. The heavy lifting
still lives in `generation_service._run_generation_job`; Celery only changes
WHERE and HOW durably it runs. Enabled by `GENERATION_BACKEND=celery`.

The task body is intentionally tiny and imports the service lazily so that
importing this module (which the worker does at startup) never triggers a
circular import with generation_service.
"""

from __future__ import annotations

import logging

from generation.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="generation.generate_document",
    acks_late=True,
    # _run_generation_job handles its own per-section failures and marks the job
    # failed on unrecoverable errors, so it rarely raises. If it DOES raise
    # (e.g. an infra blip), let Celery retry a couple of times — the job body is
    # idempotent (completed sections are skipped on re-run).
    autoretry_for=(Exception,),
    max_retries=2,
    retry_backoff=10,        # 10s, 20s
    retry_backoff_max=60,
    retry_jitter=True,
)
def generate_document_task(self, job_id: str) -> dict:
    """Run one generation job to completion on a Celery worker.

    Idempotent: on a redelivery/retry, `_run_generation_job` resets any
    stuck "generating" sections to pending and skips already-completed ones,
    so re-running never double-writes finished sections.
    """
    from generation.generation_service import _run_generation_job

    logger.info("[celery] Generating job %s (attempt %d)", job_id, self.request.retries + 1)
    _run_generation_job(job_id)
    logger.info("[celery] Job %s finished", job_id)
    return {"job_id": job_id, "status": "done"}
