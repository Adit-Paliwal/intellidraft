"""
Generation Service
==================
Orchestrates the full document generation workflow:

  start_job()             → creates DB records, launches background thread
  add_comment()           → store a user comment on a section version
  regenerate_section()    → re-call LLM for one section (with optional comment)
  accept_version()        → mark a specific version as accepted by the user
  get_job()               → full job state with sections + versions + comments
  export_job()            → assemble final document from accepted/current versions

Background generation (dev mode):
  Sections are generated one-by-one in a daemon thread so the HTTP call
  that started the job returns immediately. The client polls GET /api/generate/{job_id}.

Production note (Databricks Apps):
  Generation runs on in-process daemon threads; main.py's startup sweep marks
  jobs orphaned by a restart as failed. When scale demands it, move
  `_run_generation_job` to a Databricks Job — it is the exact body the task
  handler would execute. Set ASYNC_GENERATION=false to generate synchronously
  inside the request (useful for testing or very small documents).
"""

from __future__ import annotations
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select

from generation.db import (
    GenerationJob, Section, SectionVersion, SectionComment, DocumentSnapshot,
    get_session, retry_on_locked,
)
from generation.template_manager import get_sections_for_job, get_template_by_id
from generation.generator import generate_section
from storage.gcs_storage import get_storage_service

logger = logging.getLogger(__name__)

# Set ASYNC_GENERATION=false to generate synchronously (blocking) inside the request.
# Default true = background thread, HTTP call returns immediately.
ASYNC_GENERATION = os.environ.get("ASYNC_GENERATION", "true").lower() == "true"

# GENERATION_BACKEND selects HOW _run_generation_job executes:
#   thread  — in-process daemon thread (default; historical behaviour, NOT durable:
#             a server restart orphans in-flight jobs → main.py startup sweep fails them)
#   celery  — enqueue to a Celery worker (DURABLE: the job survives an API restart,
#             and a worker crash re-queues it — task_acks_late in celery_app.py)
#   sync    — run inline inside the HTTP request (debugging / tiny documents)
# Back-compat: when GENERATION_BACKEND is unset we derive it from ASYNC_GENERATION
# (true→thread, false→sync), so existing deployments are byte-for-byte unchanged
# until they opt in to the queue.
_DEFAULT_BACKEND   = "thread" if ASYNC_GENERATION else "sync"
GENERATION_BACKEND = os.environ.get("GENERATION_BACKEND", _DEFAULT_BACKEND).strip().lower()

# How many sections to generate CONCURRENTLY within a wave. Sections only see a
# 150-char preview of prior sections (see generator._build_section_prompt), so
# wave-parallelism is quality-neutral while cutting wall-clock ~concurrency-fold.
# Default raised 4→6 to offset the richer, more granular section lists compiled
# from the expert mapping document. Tune up to 8 for very large docs (watch Vertex
# 429s — llm_provider retries them). Set to 1 to restore sequential generation.
GENERATION_CONCURRENCY = max(1, int(os.environ.get("GENERATION_CONCURRENCY", "6")))


# ─────────────────────────────────────────────────────────────────────────────
# Start a generation job
# ─────────────────────────────────────────────────────────────────────────────

@retry_on_locked()   # job+section INSERTs can contend with in-flight generation writes
def start_job(
    document_id:    str,
    user_inputs:    dict,
    template_id:    Optional[str] = None,
) -> dict:
    """
    Create a GenerationJob and all its Section rows in the DB.
    Start section generation (background thread or sync based on ASYNC_GENERATION).

    Args:
        document_id:  UUID of the parsed document (from the upload step)
        user_inputs:  Dict matching UserInputData fields
        template_id:  Optional template override

    Returns:
        Job dict (with sections in pending state)
    """
    import json as json_mod

    document_type = user_inputs.get("document_type", "Business Requirements Document (BRD)")
    sections_override = user_inputs.get("sections_to_include")

    section_configs = get_sections_for_job(document_type, template_id, sections_override)

    if not section_configs:
        raise ValueError(f"No sections resolved for document_type='{document_type}'")

    job_id = str(uuid.uuid4())

    with get_session() as session:
        job = GenerationJob(
            job_id           = job_id,
            document_id      = document_id,
            status           = "pending",
            document_type    = document_type,
            output_format    = user_inputs.get("output_format", "Word (.docx)"),
            template_id      = template_id,
            language         = user_inputs.get("language", "English"),
            user_inputs_json = json_mod.dumps(user_inputs),
            total_sections   = len(section_configs),
        )
        session.add(job)

        for cfg in section_configs:
            section = Section(
                section_id    = str(uuid.uuid4()),
                job_id        = job_id,
                section_key   = cfg["key"],
                section_title = cfg["title"],
                order_index   = cfg["order"],
                status        = "pending",
            )
            session.add(section)

        session.commit()
        job_dict = job.to_dict()

    logger.info("Created job %s with %d sections", job_id, len(section_configs))

    ran_sync = _dispatch_generation(job_id)
    if ran_sync:
        # Synchronous backend ran to completion inside this call — return fresh state.
        with get_session() as session:
            job_obj = session.get(GenerationJob, job_id)
            job_dict = job_obj.to_dict()

    return job_dict


def _dispatch_generation(job_id: str) -> bool:
    """Kick off generation for a freshly-created job using GENERATION_BACKEND.

    Returns True iff generation ran SYNCHRONOUSLY (inline) and the caller should
    re-read the job to get its final state; False for the async backends
    (celery / thread) where the job continues in the background.

    The job row is already committed as "pending" before this is called, so a
    dispatch failure never loses the job — main.py's startup sweep reaps any
    job left pending past STALE_JOB_MINUTES.
    """
    backend = GENERATION_BACKEND

    if backend == "celery":
        try:
            from generation.tasks import generate_document_task
            generate_document_task.delay(job_id)
            logger.info("[gen] Job %s enqueued to Celery (queue durable)", job_id)
            return False
        except Exception:
            # Broker unreachable / misconfigured. Don't strand the job — fall back
            # to an in-process thread so generation still proceeds (degraded: not
            # durable). Logged at CRITICAL so the broker outage is visible.
            logger.critical(
                "[gen] Celery enqueue FAILED for job %s — falling back to in-process "
                "thread (NOT durable). Check CELERY_BROKER_URL and broker health.",
                job_id, exc_info=True,
            )
            backend = "thread"

    if backend == "subprocess":
        # Run the job in its OWN OS process (mirrors a Databricks Job run locally).
        # Section-level parallelism still happens inside the child via the wave loop.
        _spawn_job_process(job_id)
        return False

    if backend == "sync":
        _run_generation_job(job_id)
        return True

    # Default: in-process daemon thread — returns immediately.
    t = threading.Thread(
        target=_run_generation_job,
        args=(job_id,),
        daemon=True,
        name=f"gen-{job_id[:8]}",
    )
    t.start()
    return False


def _spawn_job_process(job_id: str) -> None:
    """Launch `python -m generation.job_runner <job_id>` as a detached child
    process. This mirrors Approach B (Databricks Job) locally: the job body runs
    in a SEPARATE process, yet the wave loop inside _run_generation_job still
    generates GENERATION_CONCURRENCY (default 4) sections in parallel.

    A daemon reaper thread only waits on the child to log its exit and avoid
    zombies — it does NO generation work itself. If the spawn fails (e.g. a bad
    interpreter path), it falls back to running the job in-process so generation
    still proceeds.

    NOTE (local SQLite only): the child is a second process writing the SQLite
    file. That is WAL-safe here (WAL supports multi-process readers + one writer;
    db.py sets busy_timeout + synchronous=NORMAL and section writes use
    retry_on_locked), but it is a different concurrency profile than the thread
    backend. In production (Databricks SQL Warehouse) there is no such caveat.
    """
    import subprocess
    import sys
    from pathlib import Path

    app_dir = Path(__file__).parent.parent.resolve()   # …/Data_Ingestion (has the `generation` package)
    cmd = [sys.executable, "-m", "generation.job_runner", job_id]

    def _run() -> None:
        try:
            proc = subprocess.Popen(cmd, cwd=str(app_dir))
            logger.info("[gen] Job %s dispatched to subprocess pid=%s", job_id, proc.pid)
            rc = proc.wait()
            logger.info("[gen] Subprocess job %s (pid=%s) exited rc=%s", job_id, proc.pid, rc)
        except Exception:
            logger.critical(
                "[gen] Subprocess spawn FAILED for job %s — running in-process instead.",
                job_id, exc_info=True,
            )
            _run_generation_job(job_id)

    t = threading.Thread(target=_run, daemon=True, name=f"gen-proc-{job_id[:8]}")
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# Background generation loop
# ─────────────────────────────────────────────────────────────────────────────

def _fill_placeholders(text: str, user_inputs: dict) -> str:
    """Substitute {{placeholder}} tokens in static section content with the
    project's field values. Used for STATIC sections (NIT boilerplate / forms)
    so the reused text carries THIS project's name, NIT number, dates, contacts.
    Unknown/empty tokens become a visible fill-in blank so nothing looks broken.
    """
    import re
    if not text:
        return ""
    ctx = dict(user_inputs or {})
    # Sensible NIT defaults so common tokens resolve even if a field is unset.
    ctx.setdefault("client_name",  "Adani Electricity Mumbai Limited (AEML)")
    ctx.setdefault("organisation", "Adani Electricity Mumbai Limited (AEML)")
    ctx.setdefault("purchaser",    "Adani Electricity Mumbai Limited (AEML)")

    def _repl(m: "re.Match") -> str:
        key = m.group(1).strip()
        val = ctx.get(key)
        return str(val) if val not in (None, "") else "__________"

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", _repl, text)


@retry_on_locked()
def _persist_section_version(job_id: str, section_id: str, content: str,
                             prompt: str, model_id: str) -> None:
    """Write a completed section's v1 + atomically bump the job counter.
    Retries the whole transaction on a transient SQLite lock (wave workers write
    concurrently). Self-contained — no side effects outside the DB — so it is
    safe to re-run. The expensive LLM call happens in the caller, never here."""
    import hashlib as _hm
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            return
        session.add(SectionVersion(
            version_id        = str(uuid.uuid4()),
            section_id        = section_id,
            version_number    = 1,
            content           = content,
            word_count        = len(content.split()),
            generation_prompt = prompt,
            generation_model  = model_id,
            trigger_type      = "ai_generation",
        ))
        sec.status          = "completed"
        sec.current_version = 1
        sec.version_hash    = _hm.sha256(f"{section_id}:1".encode(), usedforsecurity=False).hexdigest()[:16]
        sec.updated_at      = datetime.utcnow()
        # Atomic increment — safe under concurrent wave workers
        session.query(GenerationJob).filter(GenerationJob.job_id == job_id).update(
            {GenerationJob.completed_sections: GenerationJob.completed_sections + 1},
            synchronize_session=False,
        )
        session.commit()

def _run_generation_job(job_id: str) -> None:
    """
    Generate all pending sections for a job, sequentially.
    Updates the DB after each section so the client can poll progress.
    This is the body that a production queue-triggered function would execute.
    """
    logger.info("[gen] Starting job %s", job_id)

    # Reset any sections left in "generating" state by a previous interrupted run
    with get_session() as session:
        stuck = (
            session.query(Section)
            .filter(Section.job_id == job_id, Section.status == "generating")
            .all()
        )
        for s in stuck:
            s.status = "pending"
        if stuck:
            session.commit()
            logger.info("[gen] Reset %d stuck-generating section(s) to pending", len(stuck))

    # Load document context once for the whole job
    try:
        llm_context, user_inputs, system_instructions = _load_job_context(job_id)
    except Exception as e:
        logger.exception("[gen] Failed to load context for job %s", job_id)
        _mark_job_failed(job_id, str(e))
        return

    # Mark job as in_progress
    with get_session() as session:
        job = session.get(GenerationJob, job_id)
        if not job:
            return
        job.status = "in_progress"
        session.commit()

    # Load section IDs in order
    with get_session() as session:
        sections = (
            session.query(Section)
            .filter(Section.job_id == job_id)
            .order_by(Section.order_index)
            .all()
        )
        section_rows = [(s.section_id, s.section_key, s.section_title, s.order_index) for s in sections]

    # Pull section configs from template for instructions
    document_type     = user_inputs.get("document_type", "")
    template_id       = user_inputs.get("template_id")
    section_configs   = get_sections_for_job(document_type, template_id, None)
    config_by_key     = {c["key"]: c for c in section_configs}

    previous_sections: list[dict] = []   # accumulated for coherence (150-char previews in prompt)

    def _generate_one(row: tuple, prev_snapshot: list[dict]) -> Optional[dict]:
        """
        Generate one section end-to-end. Runs on a wave worker thread —
        every DB touch uses its own session, and the job progress counter is
        incremented with an ATOMIC SQL expression (a plain `job.x += 1` here
        would be a read-modify-write race across concurrent workers).
        Returns {"title", "content", "order"} on success, None on skip/failure.
        """
        section_id, section_key, section_title, order_index = row
        cfg          = config_by_key.get(section_key, {})
        instructions = cfg.get("instructions", f"Generate the {section_title} section.")
        target_words = cfg.get("target_words", 300)

        # Mark as generating
        with get_session() as session:
            sec = session.get(Section, section_id)
            if not sec or sec.status not in ("pending",):
                return None   # already done or failed
            sec.status     = "generating"
            sec.updated_at = datetime.utcnow()
            session.commit()

        # ── STATIC sections: skip the LLM entirely ───────────────────────────
        # A section with mode="static" (e.g. NIT legal boilerplate / forms) is
        # NOT generated — its `static_content` is inserted verbatim, with
        # {{placeholders}} filled from the project fields. Deterministic, exact,
        # and legally safe. It still lands as a normal SectionVersion so preview /
        # download / manual + AI edit / versioning all work the same.
        if cfg.get("mode") == "static":
            content = _fill_placeholders(cfg.get("static_content", ""), user_inputs)
            _persist_section_version(job_id, section_id, content, "(static content)", "static")
            logger.info("[gen] Section '%s' inserted STATIC (%d words)", section_key, len(content.split()))
            return {"title": section_title, "content": content, "order": order_index}

        try:
            content, prompt, model_id = generate_section(
                section_key          = section_key,
                section_title        = section_title,
                section_instructions = instructions,
                document_type        = document_type,
                system_instructions  = system_instructions,
                llm_context          = llm_context,
                user_inputs          = user_inputs,
                previous_sections    = prev_snapshot,
                target_words         = target_words,
                section_spec         = cfg,
            )

            # Short-table guard: table sections sometimes come back far too brief
            # (the model stops after a few rows — e.g. an FR table with 3 of 40 rows).
            # Retry ONCE with a stronger nudge when a TABLE section is well under its
            # target. Bounded (one extra call), and only for real table sections with
            # a meaningful length target — composites ("mixed") never trigger this.
            w1 = len(content.split())
            _rh = (cfg.get("render_hint") or "").lower()
            _is_table = _rh == "table" or "table" in str(cfg.get("format", "")).lower()
            if _is_table and target_words >= 400 and w1 < 0.4 * target_words:
                try:
                    boosted = instructions + (
                        f"\n\nIMPORTANT: the previous draft was far too brief. Output the FULL "
                        f"table with ALL rows requested (aim for ~{target_words} words). Every row "
                        f"must fill every column. Do not summarise, truncate, or write 'see above'."
                    )
                    c2, p2, m2 = generate_section(
                        section_key=section_key, section_title=section_title,
                        section_instructions=boosted, document_type=document_type,
                        system_instructions=system_instructions, llm_context=llm_context,
                        user_inputs=user_inputs, previous_sections=prev_snapshot,
                        target_words=target_words, section_spec=cfg,
                    )
                    if len(c2.split()) > w1:
                        logger.info("[gen] Section '%s' short-table retry %d→%d words",
                                    section_key, w1, len(c2.split()))
                        content, prompt, model_id = c2, p2, m2
                except Exception:
                    logger.warning("[gen] Section '%s' short-table retry failed — keeping first draft", section_key)

            # Persist in a lock-retrying write (the LLM call above is NOT retried)
            _persist_section_version(job_id, section_id, content, prompt, model_id)

            logger.info("[gen] Section '%s' done (%d words)", section_key, len(content.split()))
            return {"title": section_title, "content": content, "order": order_index}

        except Exception as e:
            logger.exception("[gen] Section '%s' failed", section_key)
            with get_session() as session:
                sec = session.get(Section, section_id)
                if sec:
                    sec.status     = "failed"
                    sec.error      = str(e)
                    sec.updated_at = datetime.utcnow()
                session.query(GenerationJob).filter(GenerationJob.job_id == job_id).update(
                    {GenerationJob.completed_sections: GenerationJob.completed_sections + 1},
                    synchronize_session=False,
                )
                session.commit()
            return None

    # ── Wave-parallel execution ──────────────────────────────────────────────
    # Sections run GENERATION_CONCURRENCY at a time. Each wave sees the
    # completed output (truncated previews) of all previous waves — same
    # coherence signal as the old sequential loop, at a fraction of the
    # wall-clock time (25 sections: ~10 min sequential → ~2-3 min at c=4).
    from concurrent.futures import ThreadPoolExecutor

    for wave_start in range(0, len(section_rows), GENERATION_CONCURRENCY):
        wave = section_rows[wave_start:wave_start + GENERATION_CONCURRENCY]
        prev_snapshot = list(previous_sections)   # immutable view for this wave
        logger.info(
            "[gen] Job %s — wave %d: generating %d section(s) IN PARALLEL: %s",
            job_id, wave_start // GENERATION_CONCURRENCY + 1, len(wave),
            ", ".join(r[2] for r in wave),   # r[2] = section_title
        )
        if len(wave) == 1:
            results = [_generate_one(wave[0], prev_snapshot)]
        else:
            with ThreadPoolExecutor(max_workers=len(wave),
                                    thread_name_prefix=f"gen-{job_id[:8]}") as pool:
                results = list(pool.map(lambda r: _generate_one(r, prev_snapshot), wave))
        # Keep document order stable in the coherence context
        for res in sorted((r for r in results if r), key=lambda d: d["order"]):
            previous_sections.append({"title": res["title"], "content": res["content"]})

    # ── Retry pass: re-call generate_section for any section that failed ────────
    # Uses the same function and same context variables as the main loop above.
    with get_session() as session:
        retry_rows = [
            (s.section_id, s.section_key, s.section_title, s.order_index)
            for s in (
                session.query(Section)
                .filter(Section.job_id == job_id, Section.status == "failed")
                .order_by(Section.order_index)
                .all()
            )
        ]

    for section_id, section_key, section_title, order_index in retry_rows:
        cfg          = config_by_key.get(section_key, {})
        instructions = cfg.get("instructions", f"Generate the {section_title} section.")
        target_words = cfg.get("target_words", 300)
        try:
            content, prompt, model_id = generate_section(
                section_key          = section_key,
                section_title        = section_title,
                section_instructions = instructions,
                document_type        = document_type,
                system_instructions  = system_instructions,
                llm_context          = llm_context,
                user_inputs          = user_inputs,
                previous_sections    = previous_sections,
                target_words         = target_words,
                section_spec         = cfg,
            )
            import hashlib as _hm
            with get_session() as session:
                sec     = session.get(Section, section_id)
                new_ver = max((v.version_number for v in sec.versions), default=0) + 1
                session.add(SectionVersion(
                    version_id        = str(uuid.uuid4()),
                    section_id        = section_id,
                    version_number    = new_ver,
                    content           = content,
                    word_count        = len(content.split()),
                    generation_prompt = prompt,
                    generation_model  = model_id,
                    trigger_type      = "ai_generation",
                ))
                sec.status          = "completed"
                sec.current_version = new_ver
                sec.version_hash    = _hm.sha256(f"{section_id}:{new_ver}".encode(), usedforsecurity=False).hexdigest()[:16]
                sec.updated_at      = datetime.utcnow()
                # Do NOT increment completed_sections here — it was already counted
                # when the section failed in the main loop above.
                session.commit()
            previous_sections.append({"title": section_title, "content": content})
            logger.info("[gen] Retry: '%s' done (%d words)", section_key, len(content.split()))
        except Exception:
            logger.exception("[gen] Retry: '%s' failed again — will be omitted from output", section_key)

    # Final job status — use SQL aggregates to avoid lazy-load DetachedInstanceError
    with get_session() as session:
        total_sections = session.scalar(
            select(func.count()).where(Section.job_id == job_id)
        ) or 0
        failed_sections = session.scalar(
            select(func.count()).where(
                Section.job_id == job_id,
                Section.status == "failed",
            )
        ) or 0
        final_status = "failed" if (failed_sections > 0 and failed_sections == total_sections) else "completed"

        job = session.get(GenerationJob, job_id)
        if job:
            job.status       = final_status
            job.completed_at = datetime.utcnow()

            # Also flip the linked Project status so the dashboard reflects reality.
            # Project lifecycle: draft → ready → generating → completed | failed
            from generation.db import Project as _Project
            linked_proj = session.query(_Project).filter(
                _Project.job_id == job_id
            ).first()
            if linked_proj:
                linked_proj.status     = final_status   # "completed" or "failed"
                linked_proj.updated_at = datetime.utcnow()
                logger.info(
                    "[gen] Project %s status → %s",
                    linked_proj.project_id, final_status,
                )

            session.commit()

    logger.info("[gen] Job %s finished — status=%s (total=%d failed=%d)",
                job_id, final_status, total_sections, failed_sections)

    # Pre-warm the HTML preview so it's cached before the client polls for it.
    # Runs in a separate daemon thread — never blocks or fails the generation job.
    if final_status == "completed":
        try:
            from generation.preview_service import pregenerate_preview
            pregenerate_preview(job_id)
        except Exception as _e:
            logger.warning("[gen] Preview pre-warmup trigger failed (non-fatal): %s", _e)


def _load_job_context(job_id: str) -> tuple[str, dict, str]:
    """
    Load the document context and user inputs for a job.
    Returns (llm_context, user_inputs_dict, system_instructions).

    Multi-document support:
      If user_inputs contains "document_ids" (list), all documents are loaded
      and their contexts are concatenated (capped at 60 000 chars total).
      Falls back to the single job.document_id when "document_ids" is absent
      (e.g. jobs started via POST /api/generate/start with a single document_id).
    """
    import json as json_mod

    with get_session() as session:
        job = session.get(GenerationJob, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        document_id      = job.document_id
        user_inputs_json = job.user_inputs_json
        template_id      = job.template_id
        document_type    = job.document_type

    user_inputs = json_mod.loads(user_inputs_json or "{}")

    # ── Resolve document ID list ──────────────────────────────────────────────
    # generate_from_project passes all attached doc IDs via user_inputs["document_ids"].
    # generate_start passes a single document_id at the job level.
    all_doc_ids: list[str] = user_inputs.get("document_ids") or [document_id]

    # ── Load and concatenate document contexts ────────────────────────────────
    store = get_storage_service()
    from models.meta_schema import ParsedDocument

    contexts: list[str] = []
    for doc_id in all_doc_ids:
        try:
            meta      = store.get_meta_json(doc_id)
            parsed    = ParsedDocument(**meta)
            # Cap per-document to 20 000 chars; total capped below at 60 000.
            ctx       = parsed.to_llm_context(max_chars=20_000)
            header    = f"=== Source Document: {parsed.source_filename} ===" if len(all_doc_ids) > 1 else ""
            contexts.append(f"{header}\n{ctx}".strip())
            logger.info("[gen] Loaded doc %s (%d chars)", parsed.source_filename, len(ctx))
        except Exception as e:
            logger.warning("[gen] Could not load document %s: %s", doc_id, e)

    if not contexts:
        logger.warning(
            "[gen] No documents loaded for job %s — generating from form data only. "
            "Checked doc IDs: %s", job_id, all_doc_ids,
        )
    llm_context = "\n\n---\n\n".join(contexts) if contexts else ""

    # Append AI-derived project analysis (from DerivedData table) when available.
    # This gives the LLM pre-analysed context even when raw documents are absent.
    derived_ctx = user_inputs.get("derived_context", "")
    if derived_ctx:
        sep = "\n\n---\n\n" if llm_context else ""
        llm_context = llm_context + sep + derived_ctx

    llm_context = llm_context[:60_000]

    # ── Get template system instructions ─────────────────────────────────────
    # template_id is stored on the job record (set by start_job).
    # For generate_from_project it's also in user_inputs["template_id"] so
    # _run_generation_job can resolve sections correctly.
    system_instructions = ""
    if template_id:
        tmpl = get_template_by_id(template_id)
        if tmpl and tmpl.system_instructions:
            system_instructions = tmpl.system_instructions
    if not system_instructions:
        from generation.template_manager import get_template_for_doc_type
        tmpl = get_template_for_doc_type(document_type)
        if tmpl and tmpl.system_instructions:
            system_instructions = tmpl.system_instructions

    return llm_context, user_inputs, system_instructions


def _mark_job_failed(job_id: str, error: str) -> None:
    with get_session() as session:
        job = session.get(GenerationJob, job_id)
        if job:
            job.status       = "failed"
            job.error        = error
            job.completed_at = datetime.utcnow()
            session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# User actions (comments, regeneration, acceptance)
# ─────────────────────────────────────────────────────────────────────────────

def add_comment(
    section_id:   str,
    comment_text: str,
    comment_type: str = "edit_request",
) -> dict:
    """Add a user comment on a section. Returns the saved comment dict."""
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            raise ValueError(f"Section {section_id} not found")

        comment = SectionComment(
            comment_id     = str(uuid.uuid4()),
            section_id     = section_id,
            version_number = sec.current_version,
            comment_text   = comment_text,
            comment_type   = comment_type,
            status         = "pending",
        )
        session.add(comment)
        session.commit()
        session.refresh(comment)
        return comment.to_dict()


def regenerate_section(
    section_id:  str,
    comment_id:  Optional[str] = None,
) -> dict:
    """
    Regenerate a section. If comment_id is given, incorporates that comment.
    Creates a new SectionVersion with an incremented version_number.
    Returns the new version dict.
    """
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            raise ValueError(f"Section {section_id} not found")

        job = session.get(GenerationJob, sec.job_id)
        if not job:
            raise ValueError(f"Job for section {section_id} not found")

        # Get current content to revise
        current_ver = None
        if sec.versions:
            current_ver = max(sec.versions, key=lambda v: v.version_number)

        # Get the edit comment if specified
        edit_comment     = None
        trigger_cmt_id   = None
        if comment_id:
            cmt = session.get(SectionComment, comment_id)
            if cmt:
                edit_comment   = cmt.comment_text
                trigger_cmt_id = comment_id

        next_version = (sec.current_version or 0) + 1

        # Snapshot values before closing session
        section_key     = sec.section_key
        section_title   = sec.section_title
        job_id          = sec.job_id
        document_type   = job.document_type
        template_id     = job.template_id
        current_content = current_ver.content if current_ver else None
        user_inputs_json = job.user_inputs_json

        # Mark section as regenerating
        sec.status     = "generating"
        sec.updated_at = datetime.utcnow()
        session.commit()

    # Load full context
    import json as json_mod
    user_inputs = json_mod.loads(user_inputs_json or "{}")
    llm_context, _, system_instructions = _load_job_context(job_id)

    # Section-specific instructions
    section_configs = get_sections_for_job(document_type, template_id, None)
    config_by_key   = {c["key"]: c for c in section_configs}
    cfg             = config_by_key.get(section_key, {})
    instructions    = cfg.get("instructions", f"Generate the {section_title} section.")
    target_words    = cfg.get("target_words", 300)

    # Previously completed sections for coherence
    with get_session() as session:
        all_sections = (
            session.query(Section)
            .filter(Section.job_id == job_id, Section.status == "completed")
            .order_by(Section.order_index)
            .all()
        )
        prev_sections = []
        for s in all_sections:
            if s.section_id == section_id:
                continue
            latest = max(s.versions, key=lambda v: v.version_number) if s.versions else None
            if latest:
                prev_sections.append({"title": s.section_title, "content": latest.content})

    try:
        content, prompt, model_id = generate_section(
            section_key          = section_key,
            section_title        = section_title,
            section_instructions = instructions,
            document_type        = document_type,
            system_instructions  = system_instructions,
            llm_context          = llm_context,
            user_inputs          = user_inputs,
            previous_sections    = prev_sections,
            target_words         = target_words,
            section_spec         = cfg,
            edit_comment         = edit_comment,
            previous_content     = current_content,
        )

        with get_session() as session:
            ver = SectionVersion(
                version_id        = str(uuid.uuid4()),
                section_id        = section_id,
                version_number    = next_version,
                content           = content,
                word_count        = len(content.split()),
                generation_prompt = prompt,
                generation_model  = model_id,
                trigger_comment_id= trigger_cmt_id,
                trigger_type      = "ai_regeneration",
            )
            session.add(ver)

            sec = session.get(Section, section_id)
            sec.status          = "completed"
            sec.current_version = next_version
            import hashlib as _hm
            sec.version_hash    = _hm.sha256(f"{section_id}:{next_version}".encode(), usedforsecurity=False).hexdigest()[:16]
            sec.updated_at      = datetime.utcnow()

            # Mark the triggering comment as addressed
            if comment_id:
                cmt = session.get(SectionComment, comment_id)
                if cmt:
                    cmt.status = "addressed"

            session.commit()
            session.refresh(ver)
            _result = ver.to_dict()

        # Bust the LibreOffice HTML preview cache so the NEXT /preview/html
        # re-renders with the regenerated content. Without this the DB has the
        # new version but /preview/html keeps serving the OLD cached HTML — the
        # reason chatbot/AI edits "don't show up in the doc HTML". Manual edits
        # (update_section_content) already do this; regenerate must too.
        try:
            from generation.preview_service import invalidate_preview_cache
            invalidate_preview_cache(job_id)
        except Exception:
            pass  # non-fatal — preview will still re-render on a later cache miss
        return _result

    except Exception as e:
        with get_session() as session:
            sec = session.get(Section, section_id)
            if sec:
                sec.status     = "failed"
                sec.error      = str(e)
                sec.updated_at = datetime.utcnow()
                session.commit()
        raise


def accept_version(section_id: str, version_number: int) -> dict:
    """
    Mark a specific version as accepted by the user.
    Clears the is_accepted flag on all other versions of the same section.
    Updates section.current_version to the accepted version.
    """
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            raise ValueError(f"Section {section_id} not found")

        for ver in sec.versions:
            ver.is_accepted = (ver.version_number == version_number)

        sec.current_version = version_number
        sec.updated_at      = datetime.utcnow()
        session.commit()

        accepted = next((v for v in sec.versions if v.version_number == version_number), None)
        return accepted.to_dict() if accepted else {}


def update_section_content(section_id: str, content: str, edited_by: str = None) -> dict:
    """
    Directly overwrite a section with manually-edited content.
    Creates a new SectionVersion (incrementing from the latest) and marks it accepted.
    Used by the frontend preview panel's inline editor.
    edited_by: email of the editor (X-User-Email) — enables reviewer-edit attribution.
    """
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            raise ValueError(f"Section {section_id} not found")

        next_version = (
            max((v.version_number for v in sec.versions), default=0) + 1
        )

        # Clear accepted flag on existing versions
        for v in sec.versions:
            v.is_accepted = False

        ver = SectionVersion(
            version_id        = str(uuid.uuid4()),
            section_id        = section_id,
            version_number    = next_version,
            content           = content,
            word_count        = len(content.split()),
            generation_prompt = None,
            trigger_type      = "manual_edit",
            edited_by         = (edited_by or "").strip().lower() or None,
            is_accepted       = True,
        )
        session.add(ver)

        sec.current_version = next_version
        # Update version_hash for faster preview caching (avoids 50ms MD5 recalc)
        import hashlib as _hm
        sec.version_hash = _hm.sha256(f"{sec.section_id}:{next_version}".encode(), usedforsecurity=False).hexdigest()[:16]
        sec.updated_at      = datetime.utcnow()
        session.commit()

        # Bust the LibreOffice preview cache so next preview re-converts with new content
        try:
            from generation.preview_service import invalidate_preview_cache
            invalidate_preview_cache(sec.job_id)
        except Exception:
            pass  # non-fatal — preview will regenerate on next cache miss

        return ver.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# Read queries
# ─────────────────────────────────────────────────────────────────────────────

def get_job(job_id: str, include_all_versions: bool = False) -> dict:
    """
    Return job state with sections and versions.

    include_all_versions=False (default): Return only current version per section
      → Fast (45ms). Use for job list/polling.
    include_all_versions=True: Return all versions per section
      → Slow (358ms). Use only when explicitly requested.
    """
    from sqlalchemy.orm import selectinload

    with get_session() as session:
        # selectinload: 3 queries total instead of 1 + 2×N lazy loads.
        # This endpoint is polled every ~2.5s during generation — the N+1
        # was 51 queries per poll on a 25-section job.
        job = (
            session.query(GenerationJob)
            .options(
                selectinload(GenerationJob.sections).selectinload(Section.versions),
                selectinload(GenerationJob.sections).selectinload(Section.comments),
            )
            .filter(GenerationJob.job_id == job_id)
            .first()
        )
        if not job:
            raise ValueError(f"Job {job_id} not found")
        result = job.to_dict(include_sections=True)

        if not include_all_versions:
            # Strip all versions; include only current_content per section
            for sec in result.get("sections", []):
                versions = sec.get("versions", [])
                current_v = sec.get("current_version", 0)
                current = next((v for v in versions if v["version_number"] == current_v), None)
                sec["current_content"] = current["content"] if current else None
                sec["version_count"] = len(versions)
                sec.pop("versions", None)  # Don't send all versions

        return result


def get_section(section_id: str) -> dict:
    """Return a single section with all versions and comments."""
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            raise ValueError(f"Section {section_id} not found")
        return sec.to_dict(include_versions=True, include_comments=True)


def list_jobs(document_id: Optional[str] = None) -> list[dict]:
    """List all jobs, optionally filtered by document_id."""
    with get_session() as session:
        q = session.query(GenerationJob).order_by(GenerationJob.created_at.desc())
        if document_id:
            q = q.filter(GenerationJob.document_id == document_id)
        return [j.to_dict(include_sections=False) for j in q.all()]


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrapper — project-based generation (used by FastAPI + ADK agent)
# ─────────────────────────────────────────────────────────────────────────────

def start_job_from_project(
    project_id:       str,
    doc_type_override: Optional[str] = None,
    allow_no_docs:    bool = False,
) -> dict:
    """
    Start a generation job from a fully saved project.

    Assembles user_inputs from the Project + DerivedData records in the DB,
    calls start_job(), and flips project.status to 'generating'.

    This is the single source of truth for project-based generation.
    It is called by:
      - main.py (FastAPI)  →  POST /api/generate/project/<project_id>
      - agents/document_generator/tools.py  →  start_generation ADK tool
      - api/chat_handler.py  →  Document Chat Studio

    Args:
        project_id:        UUID of the saved project.
        doc_type_override: Override the project's document_type (e.g. from the chat tab).
        allow_no_docs:     If True, generate from form data only when no docs are attached.

    Returns:
        Same dict as start_job() — { job_id, status, sections, total_sections, ... }

    Raises:
        FileNotFoundError: project not found.
        ValueError:        no source documents attached (when allow_no_docs=False).
    """
    import json as json_mod

    from generation.db import Project, get_session as _get_session

    # ── Load project fields ───────────────────────────────────────────────────
    with _get_session() as session:
        proj = session.get(Project, project_id)
        if not proj:
            raise FileNotFoundError(f"Project '{project_id}' not found.")

        fd = proj.to_ingested_dict()
        effective_doc_type = doc_type_override or fd.get("document_type") or "BRD"

        # ── Idempotency — PER (project, document_type) ─────────────────────────
        # A project holds multiple documents (BRD, NFA, NIT, …). Re-requesting a
        # doc type that already completed returns that job instead of regenerating.
        # Covers every caller: REST endpoint, chat handler, ADK agent tool.
        existing_job = (
            session.query(GenerationJob)
            .filter(GenerationJob.project_id == project_id,
                    GenerationJob.document_type == effective_doc_type,
                    GenerationJob.status == "completed")
            .order_by(GenerationJob.created_at.desc())
            .first()
        )
        # Legacy fallback: pre-multi-doc jobs have no project_id — check proj.job_id,
        # but only honour it when the document type actually matches.
        if existing_job is None and proj.job_id:
            legacy = session.get(GenerationJob, proj.job_id)
            if (legacy and legacy.status == "completed"
                    and (legacy.document_type or "").lower() == effective_doc_type.lower()):
                existing_job = legacy
        if existing_job is not None:
            logger.info(
                "[gen] Project %s already has completed %s job %s — returning existing",
                project_id, effective_doc_type, existing_job.job_id,
            )
            existing = get_job(existing_job.job_id)
            existing["already_complete"] = True
            return existing

        doc_ids = json_mod.loads(proj.document_ids_json or "[]")
        sths    = json_mod.loads(proj.stakeholders_json  or "[]")

    if not doc_ids and not allow_no_docs:
        raise ValueError(
            "No source documents attached to this project. "
            "Please upload and parse a document first, "
            "then attach it to the project via the form."
        )

    # ── Build stakeholder string ──────────────────────────────────────────────
    sth_str = ", ".join(
        f"{s.get('name', '')} ({s.get('designation', '')})"
        for s in sths if s.get("name")
    ) or None

    # ── Build extra context from optional project fields ──────────────────────
    extra_parts: list[str] = []
    for label, key in [
        ("Business Unit",       "business_unit"),
        ("Project Code",        "project_code"),
        ("Constraints",         "constraints"),
        ("Risks",               "risks"),
        ("Technical Landscape", "technical_landscape"),
        ("Business Priority",   "business_priority"),
        ("Project Type",        "project_type"),
        ("Pain Points",         "pain_points"),
        ("Opportunities",       "opportunities"),
        ("Business Justification", "business_justification"),
        ("Deadline",            "deadline"),
        ("Integration Requirement", "integration_requirement"),
        ("Assumptions",         "assumptions"),
        ("Approval Matrix",     "approval_matrix"),
        ("Future Roadmap",      "future_roadmap"),
        ("Scalability Considerations", "scalability_considerations"),
        ("Innovation Objectives", "innovation_objectives"),
        ("Sustainability / ESG", "sustainability_esg"),
    ]:
        if fd.get(key):
            extra_parts.append(f"{label}: {fd[key]}")
    if fd.get("start_date") or fd.get("end_date"):
        extra_parts.append(
            f"Timeline: {fd.get('start_date', 'TBD')} to {fd.get('end_date', 'TBD')}"
        )
    if fd.get("estimated_cost_crores"):
        extra_parts.append(f"Estimated Cost: Rs.{fd['estimated_cost_crores']} Crores")

    # ── Load AI-derived fields from DerivedData (if the user ran derive-fields) ──
    from generation.db import DerivedData as _DerivedData
    with _get_session() as session:
        derived_row = session.get(_DerivedData, project_id)
        derived_ctx_parts: list[str] = []
        if derived_row:
            for label, key in [
                ("Current Challenges",           "current_challenges"),
                ("To-Be Process",                "to_be_process"),
                ("Success Criteria",             "success_criteria"),
                ("Business Requirements",        "business_requirements"),
                ("Functional Requirements",      "functional_requirements"),
                ("Non-Functional Requirements",  "non_functional_requirements"),
                ("Workflow",                     "workflow"),
                ("Analytics Requirements",       "analytics_requirements"),
                ("Systems Involved",             "systems_involved"),
                ("Data Sources",                 "data_sources"),
                ("Constraints & Dependencies",   "constraints_dependencies"),
            ]:
                val = getattr(derived_row, key, None)
                if val and val.strip():
                    derived_ctx_parts.append(f"### {label}\n{val.strip()}")
        derived_ctx = (
            "## AI-Derived Project Analysis\n\n" + "\n\n".join(derived_ctx_parts)
            if derived_ctx_parts else ""
        )

    # ── Assemble user_inputs for start_job ───────────────────────────────────
    # (effective_doc_type resolved above, before the idempotency check)
    user_inputs = {
        "project_name":            fd.get("project_name", ""),
        "document_type":           effective_doc_type,
        "output_format":           fd.get("output_format", "Word (.docx)"),
        "stakeholders":            sth_str,
        # Prefer proposed_solution (richer) over project_objective for description
        "project_description":     fd.get("proposed_solution") or fd.get("project_objective", ""),
        "business_problem":        fd.get("problem_statement"),
        "expected_outcome":        fd.get("project_objective"),
        "additional_instructions": "\n\n".join(extra_parts) if extra_parts else None,
        # Pass ALL attached doc IDs so _load_job_context loads every document
        "document_ids":            doc_ids,
        # Pass template_id so _run_generation_job resolves the correct section list
        "template_id":             fd.get("template_id"),
        # AI-derived extended context — appended to llm_context in _load_job_context
        "derived_context":         derived_ctx,
    }

    # ── Start the job ─────────────────────────────────────────────────────────
    # When no docs attached (allow_no_docs=True), use project_id as a placeholder
    # document_id — the generator will proceed with form data only (llm_context="").
    primary_doc_id = doc_ids[0] if doc_ids else project_id
    job = start_job(primary_doc_id, user_inputs, fd.get("template_id"))

    # ── Link job ↔ project, flip project status → 'generating' ────────────────
    with _get_session() as session:
        # Stamp project_id on the job (multi-document support: one job per doc type)
        job_row = session.get(GenerationJob, job["job_id"])
        if job_row:
            job_row.project_id = project_id
        proj2 = session.get(Project, project_id)
        if proj2:
            proj2.job_id     = job["job_id"]   # legacy alias: most recent job
            proj2.status     = "generating"
            proj2.updated_at = datetime.utcnow()
        session.commit()

    job["project_id"] = project_id
    logger.info(
        "[gen] start_job_from_project: project=%s job=%s doc_type=%s docs=%d",
        project_id, job["job_id"], effective_doc_type, len(doc_ids),
    )
    return job


# ─────────────────────────────────────────────────────────────────────────────
# Multi-document-per-project queries (Figma: BRD/NFA/NIT/… each with own state)
# ─────────────────────────────────────────────────────────────────────────────

def _project_jobs(session, project_id: str) -> list:
    """All jobs belonging to a project — project_id column plus the legacy
    Project.job_id pointer (pre-multi-doc jobs that were never stamped)."""
    from generation.db import Project as _P
    jobs = (
        session.query(GenerationJob)
        .filter(GenerationJob.project_id == project_id)
        .all()
    )
    seen = {j.job_id for j in jobs}
    proj = session.get(_P, project_id)
    if proj and proj.job_id and proj.job_id not in seen:
        legacy = session.get(GenerationJob, proj.job_id)
        if legacy:
            jobs.append(legacy)
    return jobs


def list_project_documents(project_id: str) -> list[dict]:
    """
    One entry per document type generated (or generating) for this project —
    the latest job per type wins. Powers GET /api/projects/{id}/documents
    (the Documents section of the Individual Project page).
    """
    from generation.db import Project as _P
    with get_session() as session:
        if not session.get(_P, project_id):
            raise FileNotFoundError(f"Project '{project_id}' not found")
        latest_by_type: dict[str, GenerationJob] = {}
        for j in _project_jobs(session, project_id):
            key = (j.document_type or "").upper()
            cur = latest_by_type.get(key)
            if cur is None or (j.created_at and cur.created_at and j.created_at > cur.created_at):
                latest_by_type[key] = j
        out = []
        for j in latest_by_type.values():
            out.append({
                "document_type":      j.document_type,
                "job_id":             j.job_id,
                "status":             j.status,
                "review_status":      j.review_status or "draft",
                "total_sections":     j.total_sections,
                "completed_sections": j.completed_sections,
                "created_at":         j.created_at.isoformat() if j.created_at else None,
                "completed_at":       j.completed_at.isoformat() if j.completed_at else None,
            })
        out.sort(key=lambda d: d["created_at"] or "", reverse=True)
        return out


def project_review_rollup(jobs: list) -> str:
    """
    Roll a project's document review states up to the dashboard KPI status:
      approved     — documents exist and ALL are approved
      under_review — any document under_review / revision_requested / rejected
      under_draft  — everything else (no jobs, or drafts only)
    """
    states = {(j.review_status or "draft") for j in jobs}
    if not states:
        return "under_draft"
    if states & {"under_review", "revision_requested", "rejected"}:
        return "under_review"
    if states == {"approved"}:
        return "approved"
    return "under_draft"


def get_project_stats() -> dict:
    """Dashboard KPI counts: Total / Under Draft / Under Review / Approved."""
    from generation.db import Project as _P
    with get_session() as session:
        projects = session.query(_P).all()
        # Batch: all project-linked jobs in one query, grouped in Python
        all_jobs = (
            session.query(GenerationJob)
            .filter(GenerationJob.project_id.isnot(None))
            .all()
        )
        by_project: dict[str, list] = {}
        for j in all_jobs:
            by_project.setdefault(j.project_id, []).append(j)

        counts = {"total": len(projects), "under_draft": 0, "under_review": 0, "approved": 0}
        for p in projects:
            jobs = by_project.get(p.project_id, [])
            if not jobs and p.job_id:   # legacy single-job projects
                legacy = session.get(GenerationJob, p.job_id)
                if legacy:
                    jobs = [legacy]
            counts[project_review_rollup(jobs)] += 1
        return counts


# ─────────────────────────────────────────────────────────────────────────────
# Document Snapshots — point-in-time version checkpoints
# ─────────────────────────────────────────────────────────────────────────────

def create_snapshot(job_id: str, label: str, trigger_type: str = "manual") -> dict:
    """
    Capture a DocumentSnapshot recording the current accepted (or latest) version
    of every section.  Returns the saved snapshot dict.

    trigger_type: "manual" (user-clicked) | "review_agent" | "auto"
    """
    import json as json_mod

    with get_session() as session:
        job = session.get(GenerationJob, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        refs = []
        for sec in sorted(job.sections, key=lambda s: s.order_index):
            # Prefer explicitly accepted version; fall back to highest version_number
            accepted = next((v for v in sec.versions if v.is_accepted), None)
            latest   = max(sec.versions, key=lambda v: v.version_number) if sec.versions else None
            chosen   = accepted or latest
            if chosen:
                refs.append({
                    "section_id":     sec.section_id,
                    "section_title":  sec.section_title,
                    "version_id":     chosen.version_id,
                    "version_number": chosen.version_number,
                })

        snap = DocumentSnapshot(
            snapshot_id  = str(uuid.uuid4()),
            job_id       = job_id,
            label        = label or datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            trigger_type = trigger_type,
            section_refs = json_mod.dumps(refs),
        )
        session.add(snap)
        session.commit()
        session.refresh(snap)
        return snap.to_dict()


def list_snapshots(job_id: str) -> list[dict]:
    """Return all snapshots for a job, most recent first."""
    with get_session() as session:
        snaps = (
            session.query(DocumentSnapshot)
            .filter(DocumentSnapshot.job_id == job_id)
            .order_by(DocumentSnapshot.created_at.desc())
            .all()
        )
        return [s.to_dict() for s in snaps]


def get_snapshot(job_id: str, snapshot_id: str) -> dict:
    """Return one snapshot INCLUDING its html_content — used to load an old
    edited-HTML version for viewing/restoring."""
    with get_session() as session:
        snap = session.get(DocumentSnapshot, snapshot_id)
        if not snap or snap.job_id != job_id:
            raise ValueError(f"Snapshot {snapshot_id} not found for job {job_id}")
        return snap.to_dict(include_html=True)


def save_edited_html(
    job_id:       str,
    html:         str,
    author_name:  Optional[str] = None,
    author_email: Optional[str] = None,
    label:        Optional[str] = None,
) -> dict:
    """
    Save a WHOLE-DOCUMENT manual HTML edit as a new version (DocumentSnapshot,
    trigger_type='manual_html'). Stores the edited HTML VERBATIM and attributes
    it to the author. The live preview (/preview/html) then serves this HTML
    until a section is AI-regenerated. Also reports which sections changed
    (best-effort, informational — the HTML is stored as one blob).
    """
    import json
    from generation.preview_service import detect_changed_sections, invalidate_preview_cache

    # Which sections did the editor touch? (compared via section markers)
    try:
        changed = detect_changed_sections(job_id, html)
    except Exception:
        changed = []

    with get_session() as session:
        job = session.get(GenerationJob, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        # Snapshot the current section versions too, so this version is a full
        # checkpoint (section_refs) alongside the raw edited HTML.
        refs = []
        for sec in sorted(job.sections, key=lambda x: x.order_index):
            lv = sec.latest_version()
            if lv:
                refs.append({
                    "section_id":     sec.section_id,
                    "version_id":     lv.version_id,
                    "version_number": lv.version_number,
                    "section_title":  sec.section_title,
                })

        default_label = f"Edited by {author_name or author_email or 'user'} — {datetime.utcnow():%Y-%m-%d %H:%M} UTC"
        snap = DocumentSnapshot(
            snapshot_id  = str(uuid.uuid4()),
            job_id       = job_id,
            label        = (label or "").strip() or default_label,
            trigger_type = "manual_html",
            author       = author_name or author_email,
            html_content = html,
            section_refs = json.dumps(refs),
        )
        session.add(snap)
        session.commit()
        session.refresh(snap)
        result = snap.to_dict()

    # New manual HTML is now the live preview; drop any stale rendered cache.
    try:
        invalidate_preview_cache(job_id)
    except Exception:
        pass

    result["changed_sections"] = changed
    return result


def restore_snapshot(job_id: str, snapshot_id: str) -> dict:
    """
    Restore a DocumentSnapshot: for each referenced section, mark the
    snapshotted version as is_accepted=True (clearing others) and update
    section.current_version.

    Invalidates the preview cache so the next preview re-converts.
    Returns {"restored_sections": [...section_ids...], "snapshot_id": "…"}.
    """
    # A manual_html version restores by making its saved HTML current again —
    # re-save it as a NEW manual_html snapshot so it becomes the newest (and thus
    # the live preview). Keeps full history rather than mutating timestamps.
    with get_session() as _s:
        _snap = _s.get(DocumentSnapshot, snapshot_id)
        if not _snap or _snap.job_id != job_id:
            raise ValueError(f"Snapshot {snapshot_id} not found for job {job_id}")
        _is_html = bool(_snap.trigger_type == "manual_html" and _snap.html_content)
        _html    = _snap.html_content if _is_html else None
        _author  = _snap.author if _is_html else None
        _label   = _snap.label if _is_html else None
    if _is_html:
        new_snap = save_edited_html(job_id, _html, author_name=_author,
                                    label=f"Restored: {_label}")
        return {"restored_html_snapshot": new_snap["snapshot_id"], "snapshot_id": snapshot_id}

    with get_session() as session:
        snap = session.get(DocumentSnapshot, snapshot_id)
        if not snap or snap.job_id != job_id:
            raise ValueError(f"Snapshot {snapshot_id} not found for job {job_id}")

        refs = snap.get_section_refs()
        restored: list[str] = []

        for ref in refs:
            sec = session.get(Section, ref["section_id"])
            if not sec:
                continue
            target_vid = ref["version_id"]
            for ver in sec.versions:
                ver.is_accepted = (ver.version_id == target_vid)
                if ver.is_accepted:
                    sec.current_version = ver.version_number
                    sec.updated_at      = datetime.utcnow()
                    restored.append(sec.section_id)

        session.commit()

    try:
        from generation.preview_service import invalidate_preview_cache
        invalidate_preview_cache(job_id)
    except Exception:
        pass

    return {"restored_sections": restored, "snapshot_id": snapshot_id}
