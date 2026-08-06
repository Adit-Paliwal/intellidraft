"""
Intellidraft — FastAPI API Server (the ONLY API server; Flask was removed)
===========================================================================
API contract locked by tests/api_contract.py (62 steps) + tests/test_*.py.

HOW TO RUN locally (from the Intellidraft/ directory):
    env\\Scripts\\python.exe Data_Ingestion\\main.py
    # or: env\\Scripts\\uvicorn --app-dir Data_Ingestion main:app --host 0.0.0.0 --port 7071

Production (Databricks Apps — see Data_Ingestion/app.yaml):
    gunicorn -k uvicorn.workers.UvicornWorker --bind :$DATABRICKS_APP_PORT --workers 4 main:app

Design notes:
  - Endpoints are plain `def` (sync) — FastAPI runs them in a threadpool, so
    blocking services (SQLAlchemy, litellm) never block the event loop.
    Do NOT convert a route to `async def` unless every call inside it is
    non-blocking (the SSE stream endpoint is the intentional exception).
  - JSON bodies arrive as raw dicts (Body); Pydantic request models can be
    introduced route-by-route later.
  - Error contract: {"error": "..."} with meaningful status codes.
  - Interactive OpenAPI docs: /docs
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

# ── Venv / dependency guard ───────────────────────────────────────────────────
_REQUIRED = {
    "docx":      "python-docx",
    "fastapi":   "fastapi",
    "multipart": "python-multipart",
    "dotenv":    "python-dotenv",
}
_missing = []
for _mod, _pkg in _REQUIRED.items():
    try:
        __import__(_mod)
    except ModuleNotFoundError:
        _missing.append(_pkg)
if _missing:
    print("\n" + "=" * 62)
    print("  ERROR — Missing packages. Wrong Python interpreter?")
    print("  Running with:", sys.executable)
    print("  Missing:     ", ", ".join(_missing))
    print("  Fix: env\\Scripts\\python.exe -m pip install " + " ".join(_missing))
    print("=" * 62 + "\n")
    sys.exit(1)

# ── Bootstrap: add Data_Ingestion/ to sys.path ───────────────────────────────
_BASE = Path(__file__).parent.resolve()   # …/Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# ── Load .env ─────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(dotenv_path=_BASE / ".env", override=False)

# ── FastAPI ───────────────────────────────────────────────────────────────────
from fastapi import Body, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from werkzeug.utils import secure_filename   # kept: battle-tested filename sanitiser

# ── Logging ────────────────────────────────────────────────────────────────────
# Force UTF-8 on the console so log lines containing non-ASCII (→, ✓, ₹, em-dash)
# don't raise UnicodeEncodeError under the Windows cp1252 console. No-op on Linux.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB

# ── Lifespan ───────────────────────────────────────────────────────────────────
# Startup:  size the sync-endpoint threadpool, sweep orphaned generation jobs.
# Shutdown: dispose the DB engine (flushes SQLite WAL; was atexit under Flask).
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # 1. Threadpool capacity for sync `def` endpoints. anyio's default of 40
    #    is easily exhausted by long LLM routes; raise it (I/O-bound work).
    try:
        from anyio import to_thread
        to_thread.current_default_thread_limiter().total_tokens = int(
            os.getenv("THREADPOOL_TOKENS", "80")
        )
    except Exception:
        logger.warning("Could not resize threadpool — using anyio default")

    # 2. Orphaned-job sweep: generation runs on in-process threads, so any job
    #    still pending/in_progress long after creation was killed by a restart
    #    and will never finish. The 45-min age guard makes this safe under
    #    multi-worker gunicorn (a worker recycling must not kill a live job
    #    owned by a sibling process).
    try:
        from datetime import timedelta
        from generation.db import GenerationJob as _GJ, get_session as _gs
        cutoff = datetime.utcnow() - timedelta(minutes=int(os.getenv("STALE_JOB_MINUTES", "45")))
        with _gs() as s:
            stale = (
                s.query(_GJ)
                .filter(_GJ.status.in_(("pending", "in_progress")), _GJ.created_at < cutoff)
                .all()
            )
            for j in stale:
                j.status = "failed"
                j.error  = "Orphaned by a server restart — generation thread did not survive. Re-run generation."
            if stale:
                s.commit()
                logger.warning("Startup sweep: marked %d orphaned generation job(s) failed", len(stale))
    except Exception:
        logger.exception("Orphaned-job sweep failed (non-fatal)")

    yield

    try:
        from generation import db as _db
        if _db._engine is not None:
            _db._engine.dispose()
            print("  [DB] connections closed — WAL flushed.")
    except Exception:
        pass   # best-effort; never break the shutdown


app = FastAPI(title="Intellidraft API", version="1.0", lifespan=_lifespan)

# Lazy singletons
_store = None

def _get_store():
    global _store
    if _store is None:
        from storage.gcs_storage import get_storage_service
        _store = get_storage_service()
    return _store

# ── CORS + request logging middleware ─────────────────────────────────────────
# Parity with Flask: CORS headers stamped on EVERY response (not only when an
# Origin header is present), plus a 204 short-circuit for /api/* preflights.

# Restrict origins in production: CORS_ALLOW_ORIGINS="https://app.adani.com"
# ("*" default keeps local dev friction-free; the SPA is same-origin anyway).
CORS_HEADERS = {
    "Access-Control-Allow-Origin":  os.getenv("CORS_ALLOW_ORIGINS", "*"),
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, x-functions-key, X-User-Email, X-User-Name",
}

@app.middleware("http")
async def cors_and_log(request: Request, call_next):
    if request.method == "OPTIONS" and request.url.path.startswith("/api/"):
        return Response(status_code=204, headers=CORS_HEADERS)
    response = await call_next(request)
    for k, v in CORS_HEADERS.items():
        response.headers[k] = v
    if request.method not in ("OPTIONS", "HEAD"):
        logger.info("%-6s %-45s -> %s", request.method, request.url.path, response.status_code)
    return response


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    # Flask's get_json(silent=True) tolerated malformed bodies; keep errors in
    # the same {"error": ...} shape with a 400 (not FastAPI's default 422).
    return JSONResponse({"error": f"Invalid request: {exc.errors()[:3]}"}, status_code=400,
                        headers=CORS_HEADERS)


# ── Helpers ───────────────────────────────────────────────────────────────────

def J(data: dict, status: int = 200) -> JSONResponse:
    """json_resp equivalent — CORS is added by the middleware."""
    return JSONResponse(content=data, status_code=status)


def _qint(request: Request, name: str, default: int) -> int:
    """Flask's request.args.get(name, default, type=int): silent fallback."""
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


# ═════════════════════════════════════════════════════════════════════════════
# API-ONLY server — no frontend is served.
#
# The React SPA and legacy HTML pages were removed (2026-07-16): the app is
# tested and used purely through the REST API (Postman) + Swagger at /docs.
# Root "/" points at the interactive API docs so a browser hit isn't a dead end.
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/")
def _root():
    # Send browsers to the interactive API reference; API clients use /api/*.
    return JSONResponse(
        {"service": "Intellidraft API", "status": "ok",
         "docs": "/docs", "health": "/api/health"},
    )


# ═════════════════════════════════════════════════════════════════════════════
# INGESTION ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

# 0a. GET /api/health — lightweight liveness probe (used by Docker HEALTHCHECK)
@app.get("/api/health")
def health():
    return J({"status": "ok", "version": "1.0"})


# 0b. GET /api/docs — the hand-maintained api-docs.html was retired in favour
# of the auto-generated OpenAPI reference. Old bookmarks land on Swagger.
@app.get("/api/docs")
def api_docs():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs", status_code=307)


# 0c. POST /api/admin/reset-db — DEV ONLY: wipe and recreate the SQLite database
# Hard-gated: refuses unless ENABLE_ADMIN_ENDPOINTS=true (never set in prod).
@app.post("/api/admin/reset-db")
def admin_reset_db():
    if os.getenv("ENABLE_ADMIN_ENDPOINTS", "").lower() != "true":
        return J({"error": "Admin endpoints are disabled. Set ENABLE_ADMIN_ENDPOINTS=true (dev only)."}, 403)
    try:
        import generation.db as _db
        from pathlib import Path as _P

        if _db._engine is not None:
            _db._engine.dispose()
            _db._engine = None

        db_url = _db.DATABASE_URL
        deleted = []
        if db_url and db_url.startswith("sqlite:///"):
            db_path = _P(db_url.replace("sqlite:///", ""))
            for suffix in ("", "-wal", "-shm"):
                f = _P(str(db_path) + suffix) if suffix else db_path
                if f.exists():
                    f.unlink()
                    deleted.append(f.name)

        _db.get_engine()
        logger.info("DB reset: deleted %s, tables recreated.", deleted)
        return J({"status": "ok", "deleted": deleted,
                  "message": "Database wiped and recreated."})
    except Exception as e:
        logger.exception("admin_reset_db failed")
        return J({"error": str(e)}, 500)


# 1. GET /api/form-fields
@app.get("/api/form-fields")
def get_form_fields():
    from api.user_input_schema import DOCUMENT_FORM_FIELDS
    return J({"fields": [f.model_dump() for f in DOCUMENT_FORM_FIELDS]})


def _handle_upload(file_data: Optional[UploadFile]):
    """Shared upload logic for /api/upload and /api/chat/{sid}/upload."""
    from parsers.parser_factory import parse_document, SUPPORTED_EXTENSIONS

    if file_data is None or not (file_data.filename or "").strip():
        return None, J({"error": "No file provided. Send as multipart field 'file'."}, 400)

    filename = secure_filename(file_data.filename or "upload") or "upload"
    ext      = Path(filename).suffix.lower()

    if ext not in SUPPORTED_EXTENSIONS:
        return None, J({"error": f"Unsupported file type '{ext}'.",
                        "supported": SUPPORTED_EXTENSIONS}, 415)

    raw = file_data.file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return None, J({"error": "File too large. Max 50 MB."}, 413)

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    try:
        parsed_doc = parse_document(tmp_path)
        parsed_doc.source_filename = filename
        parsed_doc = _get_store().persist_all(parsed_doc, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return (parsed_doc, filename), None


# 2. POST /api/upload
@app.post("/api/upload")
def upload_document(file: UploadFile = File(default=None)):
    try:
        result, err = _handle_upload(file)
        if err is not None:
            return err
        parsed_doc, filename = result
        return J({
            "document_id": parsed_doc.document_id,
            "filename":    filename,
            "file_type":   parsed_doc.file_type,
            "blob_base":   parsed_doc.blob_base_path,
            "summary":     parsed_doc.summary.model_dump(),
            "message": (
                f"Document parsed. "
                f"{parsed_doc.summary.total_text_elements} text blocks, "
                f"{parsed_doc.summary.total_images} images, "
                f"{parsed_doc.summary.total_tables} tables."
            ),
        }, 201)
    except ValueError as e:
        return J({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("upload failed")
        return J({"error": f"Parsing failed: {e}"}, 500)


# 4./5. GET /api/document/{doc_id} + /status
@app.get("/api/document/{doc_id}")
def get_document(doc_id: str):
    try:
        return J(_get_store().get_meta_json(doc_id))
    except FileNotFoundError:
        return J({"error": f"Document '{doc_id}' not found."}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


@app.get("/api/document/{doc_id}/status")
def get_document_status(doc_id: str):
    try:
        return J(_get_store().get_document_index(doc_id))
    except FileNotFoundError:
        return J({"error": f"Document '{doc_id}' not found."}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# GENERATION ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

# 19. POST /api/generate/project/{project_id}  (registered before /{job_id} routes)
@app.post("/api/generate/project/{project_id}")
def generate_from_project(project_id: str, body: Optional[dict] = Body(default=None)):
    try:
        from generation.generation_service import start_job_from_project
        body = body or {}
        job = start_job_from_project(project_id, allow_no_docs=True,
                                     doc_type_override=body.get("document_type"))

        sections = [
            {
                "section_id":    s["section_id"],
                "section_title": s["section_title"],
                "status":        s["status"],
            }
            for s in job.get("sections", [])
        ]

        if job.get("already_complete"):
            return J({
                "job_id":           job["job_id"],
                "status":           "completed",
                "already_complete": True,
                "sections":         sections,
                "total_sections":   job.get("total_sections", len(sections)),
                "message":          "Document is already up to date. No new generation needed.",
            }, 200)

        return J({
            "job_id":         job["job_id"],
            "status":         job["status"],
            "review_status":  job.get("review_status", "draft"),
            "already_complete": False,
            "sections":       sections,
            "total_sections": job.get("total_sections", len(sections)),
            "message":        f"Generation started. {job.get('total_sections', 0)} sections queued.",
        }, 201)
    except FileNotFoundError as e:
        return J({"error": str(e)}, 404)
    except ValueError as e:
        return J({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("generate_from_project failed")
        return J({"error": str(e)}, 500)


# 7. GET /api/generate/{job_id}
@app.get("/api/generate/{job_id}")
def generate_get_job(job_id: str):
    try:
        from generation.generation_service import get_job
        return J(get_job(job_id))
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_get_job failed")
        return J({"error": str(e)}, 500)


# 8. GET /api/generate/{job_id}/section/{section_id}
@app.get("/api/generate/{job_id}/section/{section_id}")
def generate_get_section(job_id: str, section_id: str):
    try:
        from generation.generation_service import get_section
        return J(get_section(section_id))
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# 8b. PATCH /api/generate/{job_id}/section/{section_id} — manual content override
@app.patch("/api/generate/{job_id}/section/{section_id}")
def generate_update_section(job_id: str, section_id: str, request: Request,
                            body: Optional[dict] = Body(default=None)):
    try:
        content = ((body or {}).get("content") or "").strip()
        if not content:
            return J({"error": "content is required"}, 400)
        from generation.generation_service import update_section_content
        editor = (request.headers.get("X-User-Email") or "").strip()
        new_version = update_section_content(section_id, content, edited_by=editor)
        return J({
            "version": new_version,
            "message": f"Section updated — version {new_version['version_number']}.",
        })
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_update_section failed")
        return J({"error": str(e)}, 500)


# 9. POST /api/generate/{job_id}/section/{section_id}/comment
@app.post("/api/generate/{job_id}/section/{section_id}/comment")
def generate_add_comment(job_id: str, section_id: str,
                         body: Optional[dict] = Body(default=None)):
    try:
        from generation.generation_service import add_comment
        body         = body or {}
        comment_text = (body.get("comment_text") or "").strip()
        comment_type = body.get("comment_type", "edit_request")
        if not comment_text:
            return J({"error": "comment_text is required"}, 400)
        comment = add_comment(section_id, comment_text, comment_type)
        return J({"comment": comment, "message": "Comment saved."}, 201)
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# 10. POST /api/generate/{job_id}/section/{section_id}/regenerate
@app.post("/api/generate/{job_id}/section/{section_id}/regenerate")
def generate_regenerate(job_id: str, section_id: str,
                        body: Optional[dict] = Body(default=None)):
    try:
        from generation.generation_service import add_comment, regenerate_section
        body       = body or {}
        comment_id = body.get("comment_id")
        # Single-call section update: accept the edit instruction INLINE.
        # If the caller passes `instruction` (or `comment_text`) and no
        # comment_id, we create the edit_request comment here and feed it into
        # the regeneration — so the section is revised IN LIGHT OF the user's
        # input (revision mode: keeps the existing content, applies the change).
        # Precedence: explicit comment_id > inline instruction > neither.
        # With NEITHER, the section is regenerated from scratch (no edit) —
        # that is the from-blank behaviour and is usually NOT what "update this
        # section" wants, so the frontend should always send `instruction`.
        if not comment_id:
            instruction = (body.get("instruction") or body.get("comment_text") or "").strip()
            if instruction:
                comment_id = add_comment(section_id, instruction, "edit_request")["comment_id"]
        new_version = regenerate_section(section_id, comment_id)
        return J({"new_version": new_version,
                  "message": f"Regenerated — version {new_version['version_number']}."})
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_regenerate failed")
        return J({"error": str(e)}, 500)


# 11. POST /api/generate/{job_id}/section/{section_id}/accept
@app.post("/api/generate/{job_id}/section/{section_id}/accept")
def generate_accept(job_id: str, section_id: str,
                    body: Optional[dict] = Body(default=None)):
    try:
        from generation.generation_service import accept_version
        version_number = (body or {}).get("version_number")
        if version_number is None:
            return J({"error": "version_number is required"}, 400)
        accepted = accept_version(section_id, int(version_number))
        return J({"accepted_version": accepted,
                  "message": f"Version {version_number} accepted."})
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# 12. GET /api/generate/{job_id}/preview — markdown for on-screen rendering
@app.get("/api/generate/{job_id}/preview")
def generate_preview(job_id: str):
    try:
        from generation.doc_writer import assemble_preview
        return J(assemble_preview(job_id))
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_preview failed")
        return J({"error": str(e)}, 500)


# 12c. GET /api/generate/{job_id}/preview/html — LibreOffice HTML preview (async)
@app.get("/api/generate/{job_id}/preview/html")
def generate_preview_html(job_id: str):
    try:
        from generation.preview_service import get_or_submit_preview
        result = get_or_submit_preview(job_id)
        status_code = 200 if result.get("status") == "ready" else (
            202 if result.get("status") == "pending" else 500
        )
        return J(result, status_code)
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_preview_html failed")
        return J({"error": str(e)}, 500)


# 12c-save. POST /api/generate/{job_id}/preview/save — manual whole-document HTML edit
# The frontend sends ONLY the edited HTML of the preview. The backend stores it
# verbatim as a new version (DocumentSnapshot, trigger_type=manual_html) attributed
# to X-User-Name, reports which sections changed, and makes it the live preview.
@app.post("/api/generate/{job_id}/preview/save")
def generate_preview_save(job_id: str, request: Request,
                          body: Optional[dict] = Body(default=None)):
    try:
        body = body or {}
        html = body.get("html")
        if not html or not str(html).strip():
            return J({"error": "html is required"}, 400)
        from generation.generation_service import save_edited_html
        name  = (request.headers.get("X-User-Name")  or "").strip() or None
        email = (request.headers.get("X-User-Email") or "").strip() or None
        result = save_edited_html(job_id, html, author_name=name,
                                  author_email=email, label=body.get("label"))
        return J({
            "snapshot":         result,
            "changed_sections": result.get("changed_sections", []),
            "message": f"Saved as version by {name or email or 'user'}.",
        }, 201)
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_preview_save failed")
        return J({"error": str(e)}, 500)


# 12e-sse. GET /api/generate/{job_id}/stream — Server-Sent Events
# ASYNC generator: while idle (the vast majority of each 1s tick) it holds NO
# threadpool slot — only the brief DB read hops to a worker thread. A sync
# generator here would pin one of the ~80 threadpool slots per open stream.
@app.get("/api/generate/{job_id}/stream")
async def generate_stream_events(job_id: str):
    import asyncio
    from fastapi.concurrency import run_in_threadpool

    async def _sse():
        from generation.generation_service import get_job
        seen = set()
        tick = 0
        while True:
            try:
                job = await run_in_threadpool(get_job, job_id)
            except Exception:
                yield f"data: {json.dumps({'event': 'error', 'message': 'job not found'})}\n\n"
                return

            for sec in job.get("sections", []):
                if sec.get("status") == "completed" and sec["section_id"] not in seen:
                    seen.add(sec["section_id"])
                    payload = json.dumps({
                        "event":         "section_complete",
                        "section_id":    sec["section_id"],
                        "section_title": sec["section_title"],
                        "done":          len(seen),
                        "total":         job.get("total_sections", 0),
                    })
                    yield f"data: {payload}\n\n"

            if job.get("status") == "completed":
                yield f"data: {json.dumps({'event': 'all_complete', 'job_id': job_id, 'total_sections': job.get('total_sections', 0)})}\n\n"
                return

            if job.get("status") == "failed":
                yield f"data: {json.dumps({'event': 'failed', 'error': job.get('error', 'Unknown')})}\n\n"
                return

            tick += 1
            if tick % 5 == 0:
                yield "data: {\"event\": \"heartbeat\"}\n\n"

            await asyncio.sleep(1)

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )


# 12e. POST /api/generate/{job_id}/snapshot — create a version checkpoint
@app.post("/api/generate/{job_id}/snapshot")
def generate_create_snapshot(job_id: str, body: Optional[dict] = Body(default=None)):
    body         = body or {}
    label        = (body.get("label") or "").strip()
    trigger_type = (body.get("trigger_type") or "manual").strip()
    try:
        from generation.generation_service import create_snapshot
        snap = create_snapshot(job_id, label, trigger_type)
        return J(snap, 201)
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("create_snapshot failed")
        return J({"error": str(e)}, 500)


# 12f. GET /api/generate/{job_id}/snapshots — list version history
@app.get("/api/generate/{job_id}/snapshots")
def generate_list_snapshots(job_id: str):
    try:
        from generation.generation_service import list_snapshots
        return J({"snapshots": list_snapshots(job_id)})
    except Exception as e:
        logger.exception("list_snapshots failed")
        return J({"error": str(e)}, 500)


# 12f-get. GET /api/generate/{job_id}/snapshot/{snapshot_id} — one version WITH its
# html_content, so the frontend can load an old edited-HTML version to view/restore.
@app.get("/api/generate/{job_id}/snapshot/{snapshot_id}")
def generate_get_snapshot(job_id: str, snapshot_id: str):
    try:
        from generation.generation_service import get_snapshot
        return J(get_snapshot(job_id, snapshot_id))
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("get_snapshot failed")
        return J({"error": str(e)}, 500)


# 12g. POST /api/generate/{job_id}/snapshot/{snapshot_id}/restore
@app.post("/api/generate/{job_id}/snapshot/{snapshot_id}/restore")
def generate_restore_snapshot(job_id: str, snapshot_id: str):
    try:
        from generation.generation_service import restore_snapshot
        return J(restore_snapshot(job_id, snapshot_id))
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("restore_snapshot failed")
        return J({"error": str(e)}, 500)


# 12h. POST /api/generate/{job_id}/validate — quality + source-document provenance
# Runs the Validation Agent over the generated document: every section is
# attributed to the attached source document (name + storage PATH) that
# supports it; untraceable sections are flagged. No LLM call — deterministic.
@app.post("/api/generate/{job_id}/validate")
def generate_validate(job_id: str):
    try:
        from generation.generation_service import get_job
        from agents.validation_agent import EdgeCheck, SourceDoc, ValidationAgent

        job = get_job(job_id)
        if job.get("status") != "completed":
            return J({"error": f"Job is '{job.get('status')}' — validate after generation completes."}, 409)

        generated = {
            s["section_title"]: (s.get("current_content") or "")
            for s in job.get("sections", [])
        }

        # Attached source documents: name + storage path + parsed text
        sources = []
        from generation.db import GenerationJob as _GJ, get_session as _gs
        with _gs() as s:
            row = s.get(_GJ, job_id)
            user_inputs = json.loads(row.user_inputs_json or "{}") if row else {}
        doc_ids = user_inputs.get("document_ids") or [job.get("document_id")]
        from models.meta_schema import ParsedDocument
        store = _get_store()
        for doc_id in [d for d in doc_ids if d]:
            try:
                meta = store.get_meta_json(doc_id)
                parsed = ParsedDocument(**meta)
                sources.append(SourceDoc(
                    name=parsed.source_filename or doc_id,
                    path=parsed.blob_base_path or f"documents/{doc_id}",
                    content=parsed.to_llm_context(max_chars=60_000),
                ))
            except Exception:
                logger.warning("validate: could not load source document %s", doc_id)

        # Structural robustness checks: completed sections, no placeholder junk
        placeholders = [t for t, c in generated.items()
                        if re.search(r"\bTBD\b|\[insert |lorem ipsum", c, re.IGNORECASE)]
        empty = [t for t, c in generated.items() if len(c.strip()) < 40]
        checks = [
            EdgeCheck("all sections have substantive content", not empty,
                      f"thin/empty: {', '.join(empty[:5])}" if empty else ""),
            EdgeCheck("no placeholder text (TBD / [insert] / lorem ipsum)", not placeholders,
                      f"placeholders in: {', '.join(placeholders[:5])}" if placeholders else ""),
        ]

        report = ValidationAgent().evaluate(
            generated,
            ground_truth=None,
            source_documents=sources or None,
            robustness_checks=checks,
        )
        return J({
            "job_id": job_id,
            "document_type": job.get("document_type"),
            "source_documents": [{"name": s.name, "path": s.path} for s in sources],
            **report.to_dict(),
        })
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_validate failed")
        return J({"error": str(e)}, 500)


# 12b. GET /api/generate/{job_id}/export
@app.get("/api/generate/{job_id}/export")
def generate_export(job_id: str, request: Request):
    fmt = request.query_params.get("format", "")
    _fmt_map = {"docx": "Word (.docx)", "pdf": "PDF",
                "md": "Markdown", "markdown": "Markdown"}
    output_format = _fmt_map.get(fmt.lower()) if fmt else None
    try:
        from generation.doc_writer import export_job, upload_output_to_blob
        file_path, mime_type = export_job(job_id, output_format)
        blob_url = upload_output_to_blob(job_id, file_path, mime_type)
        if blob_url:
            return J({"job_id": job_id, "blob_url": blob_url,
                      "filename": file_path.name, "mime_type": mime_type})
        return FileResponse(
            str(file_path),
            media_type = mime_type,
            filename   = file_path.name,
        )
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("export failed")
        return J({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# TEMPLATE ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

# 13. GET /api/templates
@app.get("/api/templates")
def get_templates(request: Request):
    try:
        from generation.template_manager import list_templates, ensure_seeded
        ensure_seeded()
        doc_type = request.query_params.get("document_type")
        return J({"templates": list_templates(doc_type)})
    except Exception as e:
        logger.exception("get_templates failed")
        return J({"error": str(e)}, 500)


# 14. POST /api/templates
@app.post("/api/templates")
def create_template(body: Optional[dict] = Body(default=None)):
    try:
        from generation.template_manager import save_user_template
        body          = body or {}
        name          = (body.get("name") or "").strip()
        document_type = (body.get("document_type") or "").strip()
        sections      = body.get("sections") or []
        if not name or not document_type or not sections:
            return J({"error": "name, document_type and sections are required"}, 400)
        tmpl = save_user_template(
            name=name, document_type=document_type, sections=sections,
            system_instructions=body.get("system_instructions"),
            description=body.get("description"),
        )
        return J({"template": tmpl.to_dict(), "message": "Template saved."}, 201)
    except Exception as e:
        logger.exception("create_template failed")
        return J({"error": str(e)}, 500)


@app.post("/api/templates/{template_id}/reseed")
def reseed_template_route(template_id: str):
    try:
        from generation.template_manager import reseed_template
        ok = reseed_template(template_id)
        if not ok:
            return J({"error": f"Template JSON not found: {template_id}.json"}, 404)
        return J({"status": "reseeded", "template_id": template_id})
    except Exception as e:
        logger.exception("reseed_template failed")
        return J({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# PROJECT ENDPOINTS  (DB-backed via SQLAlchemy)
# ═════════════════════════════════════════════════════════════════════════════

def _get_proj_or_404(session, project_id: str):
    from generation.db import Project as _P
    p = session.get(_P, project_id)
    if p is None:
        raise FileNotFoundError(f"Project '{project_id}' not found")
    return p


def _apply_fields(proj, body: dict) -> None:
    """Write body keys onto the ORM project object. Safe for partial updates."""
    scalar = [
        "project_code", "project_name", "business_unit", "business_priority",
        "problem_statement", "project_objective", "as_is_processes",
        "proposed_solution", "technical_landscape", "constraints", "risks",
        "estimated_cost_crores", "start_date", "end_date",
        "document_type", "output_format", "additional_instructions",
        "template_id", "status",
        # Figma create-form fields (5-step wizard)
        "pain_points", "opportunities", "business_justification", "deadline",
        "integration_requirement", "assumptions", "approval_matrix",
        "future_roadmap", "scalability_considerations", "innovation_objectives",
        "sustainability_esg", "project_type",
    ]
    for f in scalar:
        if f in body:
            setattr(proj, f, body[f])
    if "stakeholders" in body:
        v = body["stakeholders"]
        proj.stakeholders_json = json.dumps(v) if isinstance(v, list) else v
    if "document_ids" in body:
        v = body["document_ids"]
        proj.document_ids_json = json.dumps(v) if isinstance(v, list) else v


def _project_code_conflict(session, code: str, exclude_id: str = None):
    """Return a 409 response if project_code is used by another project, else None."""
    if not code or not str(code).strip():
        return None
    from generation.db import Project as _P
    q = session.query(_P).filter(_P.project_code == str(code).strip())
    if exclude_id:
        q = q.filter(_P.project_id != exclude_id)
    existing = q.first()
    if existing:
        name = existing.project_name or existing.project_id
        return J({
            "error": f"Project code '{code}' is already in use by project '{name}'. "
                     "Please choose a different project code.",
            "conflict_project_id": existing.project_id,
        }, 409)
    return None


# 15. POST /api/extract-project-data
@app.post("/api/extract-project-data")
def extract_project_data(body: Optional[dict] = Body(default=None)):
    try:
        from api.extractor import extract_project_data as _extract
        document_ids = (body or {}).get("document_ids") or []
        if not document_ids:
            return J({"error": "document_ids array is required"}, 400)
        result  = _extract(document_ids)
        filled  = result.get("filled_count", 0)
        total   = result.get("total_fields", 15)
        missing = len(result.get("missing_required", []))
        msg = (f"All fields populated from {len(document_ids)} document(s)."
               if missing == 0 else
               f"Extracted {filled}/{total} fields. {missing} required field(s) still needed.")
        return J({**result, "document_count": len(document_ids), "message": msg})
    except FileNotFoundError as e:
        return J({"error": str(e)}, 404)
    except RuntimeError as e:
        logger.error("extract-project-data LLM error: %s", e)
        return J({"error": f"LLM extraction failed: {e}. Check MODEL_PROVIDER and API keys in .env."}, 502)
    except Exception as e:
        logger.exception("extract-project-data failed")
        return J({"error": str(e)}, 500)


# 15b. POST /api/projects/draft — create a draft project (idempotent)
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)

@app.post("/api/projects/draft")
def create_draft_project(body: Optional[dict] = Body(default=None)):
    try:
        from generation.db import Project as _P, DerivedData as _D, get_session
        body = body or {}

        client_pid = (body.get("project_id") or "").strip()
        if client_pid:
            if not _UUID_RE.match(client_pid):
                return J({"error": "project_id must be a valid UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)"}, 400)
            with get_session() as s:
                existing = s.get(_P, client_pid)
                if existing:
                    return J({"project_id": client_pid, "status": existing.status,
                              "message": "Project already exists."}, 200)
            pid = client_pid
        else:
            pid = str(uuid4())

        proj = _P(project_id=pid)
        proj.status = "draft"
        _apply_fields(proj, body)
        from sqlalchemy.exc import IntegrityError
        try:
            with get_session() as s:
                if body.get("project_code"):
                    conflict = _project_code_conflict(s, body["project_code"])
                    if conflict:
                        return conflict
                s.add(proj)
                s.add(_D(project_id=pid))
                s.commit()
        except IntegrityError:
            # Concurrent create with the same client-supplied UUID: the
            # check-then-insert above raced another request. Idempotent
            # semantics — return the row the winner created.
            with get_session() as s:
                existing = s.get(_P, pid)
                if existing:
                    return J({"project_id": pid, "status": existing.status,
                              "message": "Project already exists."}, 200)
            raise
        return J({"project_id": pid, "status": "draft",
                  "message": "Draft project created."}, 201)
    except Exception as e:
        logger.exception("create_draft_project failed")
        return J({"error": str(e)}, 500)


# 17b. GET /api/projects/stats — registered BEFORE /api/projects/{project_id}
@app.get("/api/projects/stats")
def project_stats():
    try:
        from generation.generation_service import get_project_stats
        return J(get_project_stats())
    except Exception as e:
        logger.exception("project_stats failed")
        return J({"error": str(e)}, 500)


# 16. POST /api/projects — create project (full validation, status → "ready")
@app.post("/api/projects")
def create_project(body: Optional[dict] = Body(default=None)):
    try:
        from generation.db import Project as _P, DerivedData as _D, get_session
        from models.project_schema import ProjectFormData
        from pydantic import ValidationError
        body = body or {}
        try:
            form = ProjectFormData(**body)
        except ValidationError as ve:
            errors = [
                {"field": e["loc"][0] if e["loc"] else "unknown", "message": e["msg"]}
                for e in ve.errors()
            ]
            return J({"error": "Validation failed", "fields": errors}, 422)
        pid  = str(uuid4())
        proj = _P(project_id=pid)
        proj.status = "ready"
        _apply_fields(proj, form.model_dump())
        with get_session() as s:
            if form.project_code:
                conflict = _project_code_conflict(s, form.project_code)
                if conflict:
                    return conflict
            s.add(proj)
            s.add(_D(project_id=pid))
            s.commit()
            saved_status = proj.status
        return J({"project_id": pid, "status": saved_status,
                  "message": f"Project '{form.project_name}' saved."}, 201)
    except Exception as e:
        logger.exception("create_project failed")
        return J({"error": str(e)}, 500)


# 17. GET /api/projects — list with pagination + filters + review rollup
@app.get("/api/projects")
def list_projects(request: Request):
    try:
        from generation.db import Project as _P, GenerationJob as _J, get_session
        from generation.generation_service import project_review_rollup
        from sqlalchemy import or_, func as sqlfunc
        qp       = request.query_params
        q        = (qp.get("q")        or "").strip().lower()[:100]
        status   = (qp.get("status")   or "").strip()
        code     = (qp.get("code")     or "").strip()
        bunit    = (qp.get("business_unit") or "").strip()
        review_f = (qp.get("review_status") or "").strip()
        page     = max(1, _qint(request, "page", 1))
        per_page = min(100, max(1, _qint(request, "per_page", 50)))

        with get_session() as s:
            qry = s.query(_P)
            if code:
                qry = qry.filter(_P.project_code == code)
            elif q:
                qry = qry.filter(or_(
                    sqlfunc.lower(_P.project_name).contains(q),
                    sqlfunc.lower(_P.project_code).contains(q),
                ))
            if status:
                qry = qry.filter(_P.status == status)
            if bunit:
                qry = qry.filter(sqlfunc.lower(_P.business_unit) == bunit.lower())

            total = qry.count()
            rows = qry.order_by(_P.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

            pids = [p.project_id for p in rows]
            legacy_job_ids = [p.job_id for p in rows if p.job_id]
            jobs = []
            if pids:
                conds = [_J.project_id.in_(pids)]
                if legacy_job_ids:
                    conds.append(_J.job_id.in_(legacy_job_ids))
                jobs = s.query(_J).filter(or_(*conds)).all()
            jobs_by_pid: dict[str, list] = {}
            legacy_by_id = {}
            for j in jobs:
                if j.project_id:
                    jobs_by_pid.setdefault(j.project_id, []).append(j)
                legacy_by_id[j.job_id] = j

            summaries = []
            for p in rows:
                d = p.to_summary_dict()
                p_jobs = list(jobs_by_pid.get(p.project_id, []))
                if not p_jobs and p.job_id and p.job_id in legacy_by_id:
                    p_jobs = [legacy_by_id[p.job_id]]
                d["review_status"]  = project_review_rollup(p_jobs)
                d["document_count"] = len({(j.document_type or "").upper() for j in p_jobs})
                summaries.append(d)

            if review_f:
                summaries = [d for d in summaries if d["review_status"] == review_f]

            pages = (total + per_page - 1) // per_page
        return J({
            "projects": summaries,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "count": len(summaries),
        })
    except Exception as e:
        logger.exception("list_projects failed")
        return J({"error": str(e)}, 500)


# 17c. GET /api/projects/{project_id}/documents — multi-document view
@app.get("/api/projects/{project_id}/documents")
def project_documents(project_id: str):
    try:
        from generation.generation_service import list_project_documents
        return J({"project_id": project_id,
                  "documents": list_project_documents(project_id)})
    except FileNotFoundError:
        return J({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        logger.exception("project_documents failed")
        return J({"error": str(e)}, 500)


# 18e. GET /api/projects/{project_id}/data — ingested + derived combined
@app.get("/api/projects/{project_id}/data")
def get_project_data(project_id: str):
    try:
        from generation.db import DerivedData as _D, get_session
        with get_session() as s:
            proj     = _get_proj_or_404(s, project_id)
            ingested = proj.to_ingested_dict()
            meta = {
                "document_type":           proj.document_type or "BRD",
                "output_format":           proj.output_format or "Word (.docx)",
                "template_id":             proj.template_id or "",
                "additional_instructions": proj.additional_instructions or "",
                "status":                  proj.status,
            }
            for k in ("project_id","status","job_id","created_at","updated_at",
                      "document_type","output_format","additional_instructions",
                      "document_ids","template_id"):
                ingested.pop(k, None)
            row = s.get(_D, project_id)
            if row:
                derived      = row.to_dict()
                generated_at = derived.pop("generated_at", None)
                derived.pop("project_id", None); derived.pop("updated_at", None)
            else:
                derived = {k: "" for k in [
                    "current_challenges","to_be_process","success_criteria",
                    "business_requirements","functional_requirements",
                    "non_functional_requirements","industry_benchmarks","workflow",
                    "analytics_requirements","systems_involved",
                    "data_sources","constraints_dependencies",
                ]}
                generated_at = None
        return J({"project_id": project_id, **meta, "ingested": ingested,
                  "derived": derived, "derived_generated_at": generated_at})
    except FileNotFoundError:
        return J({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# 18f. PUT /api/projects/{project_id}/data/ingested — save ingested edits
@app.put("/api/projects/{project_id}/data/ingested")
def update_ingested_data(project_id: str, body: Optional[dict] = Body(default=None)):
    try:
        from generation.db import get_session
        body = body or {}
        if not body:
            return J({"error": "Request body is empty."}, 400)
        non_ing = {"status","job_id","document_type","output_format",
                   "additional_instructions","document_ids","template_id"}
        body = {k: v for k, v in body.items() if k not in non_ing}
        now = datetime.utcnow()
        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            if body.get("project_code"):
                conflict = _project_code_conflict(s, body["project_code"], exclude_id=project_id)
                if conflict:
                    return conflict
            _apply_fields(proj, body)
            proj.updated_at = now
            s.commit()
        return J({"project_id": project_id, "updated_at": now.isoformat()})
    except FileNotFoundError:
        return J({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# 18g. PUT /api/projects/{project_id}/data/derived — save derived edits
@app.put("/api/projects/{project_id}/data/derived")
def update_derived_data(project_id: str, body: Optional[dict] = Body(default=None)):
    try:
        from generation.db import DerivedData as _D, get_session
        body = body or {}
        if not body:
            return J({"error": "Request body is empty."}, 400)
        DER_FIELDS = [
            "current_challenges","to_be_process","success_criteria",
            "business_requirements","functional_requirements",
            "non_functional_requirements","industry_benchmarks","workflow",
            "analytics_requirements","systems_involved",
            "data_sources","constraints_dependencies",
        ]
        mark_gen = bool(body.get("mark_as_generated", False))
        now = datetime.utcnow()
        with get_session() as s:
            _get_proj_or_404(s, project_id)
            row = s.get(_D, project_id)
            if row is None:
                row = _D(project_id=project_id); s.add(row)
            for f in DER_FIELDS:
                if f in body:
                    setattr(row, f, body[f])
            row.updated_at = now
            if mark_gen:
                row.generated_at = now
            s.commit()
        return J({"project_id": project_id, "updated_at": now.isoformat()})
    except FileNotFoundError:
        return J({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# 18h. POST /api/projects/{project_id}/derive-fields — AI-derive 12 fields
@app.post("/api/projects/{project_id}/derive-fields")
def derive_project_fields(project_id: str):
    try:
        from generation.db import DerivedData as _D, get_session
        from generation.derive_fields import derive_project_fields as _derive

        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            project_data = proj.to_ingested_dict()
            doc_ids = json.loads(proj.document_ids_json or "[]")

        derived = _derive(project_data, doc_ids)
        now     = datetime.utcnow()

        with get_session() as s:
            _get_proj_or_404(s, project_id)
            row = s.get(_D, project_id)
            if row is None:
                row = _D(project_id=project_id); s.add(row)
            for k, v in derived.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            row.generated_at = now
            row.updated_at   = now
            s.commit()

        populated = sum(1 for v in derived.values() if v)
        return J({"project_id": project_id, "status": "ok",
                  "fields_populated": populated, "updated_at": now.isoformat(),
                  "message": f"{populated} fields derived by AI."})
    except FileNotFoundError:
        return J({"error": f"Project '{project_id}' not found."}, 404)
    except RuntimeError as e:
        logger.error("derive-fields LLM error for project %s: %s", project_id, e)
        return J({"error": f"AI derivation failed: {e}"}, 502)
    except Exception as e:
        logger.exception("derive-fields failed for project %s", project_id)
        return J({"error": str(e)}, 500)


# 18i. POST /api/projects/{project_id}/validate — pre-flight check before Generate
@app.post("/api/projects/{project_id}/validate")
def validate_project(project_id: str):
    try:
        from generation.db import get_session
        REQUIRED = [
            "project_name", "problem_statement", "project_objective",
            "as_is_processes", "proposed_solution", "technical_landscape",
        ]
        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            data = proj.to_ingested_dict()
        missing = [f for f in REQUIRED if not (data.get(f) or "").strip()]
        ready   = len(missing) == 0
        return J({
            "project_id":        project_id,
            "valid":             ready,
            "ready_to_generate": ready,
            "missing_required":  missing,
            "message": "Ready to generate." if ready else
                       f"{len(missing)} required field(s) missing: {', '.join(missing)}",
        })
    except FileNotFoundError:
        return J({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# 18a. GET /api/projects/{project_id} — full project
@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    try:
        from generation.db import get_session
        with get_session() as s:
            return J(_get_proj_or_404(s, project_id).to_full_dict())
    except FileNotFoundError:
        return J({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# 18c. PATCH /api/projects/{project_id} — partial update / autosave
@app.patch("/api/projects/{project_id}")
def patch_project(project_id: str, body: Optional[dict] = Body(default=None)):
    try:
        from generation.db import get_session
        body = body or {}
        if not body:
            return J({"error": "Request body is empty."}, 400)
        now = datetime.utcnow()
        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            if body.get("project_code"):
                conflict = _project_code_conflict(s, body["project_code"], exclude_id=project_id)
                if conflict:
                    return conflict
            _apply_fields(proj, body)
            proj.updated_at = now
            s.commit()
        return J({"project_id": project_id, "updated_at": now.isoformat()})
    except FileNotFoundError:
        return J({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# 18d. DELETE /api/projects/{project_id}
@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    try:
        from generation.db import get_session
        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            s.delete(proj)
            s.commit()
        # media_type matches Flask's default text/html on empty 204 (contract parity)
        return Response(status_code=204, media_type="text/html")
    except FileNotFoundError:
        return J({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return J({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# CHAT STUDIO ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/api/chat/init")
def chat_init(body: Optional[dict] = Body(default=None)):
    try:
        body         = body or {}
        project_id   = (body.get("project_id") or "").strip()
        doc_type     = (body.get("document_type") or "brd").strip()
        project_name = (body.get("project_name") or "").strip()
        client_sid   = (body.get("session_id") or "").strip() or None
        auto_created = False

        if not project_id:
            from generation.db import Project as _P, DerivedData as _D, get_session
            project_id = str(uuid4())
            proj = _P(project_id=project_id, status="draft")
            if project_name:
                proj.project_name = project_name
            if doc_type:
                proj.document_type = doc_type
            with get_session() as s:
                s.add(proj)
                s.add(_D(project_id=project_id))
                s.commit()
            auto_created = True
            logger.info("chat/init: auto-created draft project %s", project_id)

        from api.chat_handler import init_session
        result = init_session(project_id, doc_type, project_name, session_id=client_sid)
        if auto_created:
            result["auto_created_project_id"] = project_id
            result["note"] = "A new draft project was created for this chat session. Use auto_created_project_id for subsequent project API calls."
        return J(result, 201)
    except Exception as e:
        logger.exception("chat/init failed")
        return J({"error": str(e)}, 500)


@app.post("/api/chat/message")
def chat_message(body: Optional[dict] = Body(default=None)):
    try:
        body       = body or {}
        session_id = (body.get("session_id") or "").strip()
        message    = (body.get("message") or "").strip()
        project_id = body.get("project_id")
        doc_type   = body.get("document_type")
        if not session_id:
            return J({"error": "session_id is required"}, 400)
        if not message:
            return J({"error": "message is required"}, 400)
        from api.chat_handler import process_message
        return J(process_message(session_id, message, project_id, doc_type))
    except Exception as e:
        logger.exception("chat/message failed")
        return J({"error": str(e)}, 500)


@app.get("/api/chat/{session_id}/history")
def chat_history(session_id: str):
    try:
        from api.chat_handler import get_history
        return J(get_history(session_id))
    except ValueError as e:
        return J({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("chat_history failed")
        return J({"error": str(e)}, 500)


@app.post("/api/chat/{session_id}/upload")
def chat_upload(session_id: str, file: UploadFile = File(default=None)):
    try:
        from api.chat_handler import attach_document_to_session
        result, err = _handle_upload(file)
        if err is not None:
            return err
        parsed_doc, filename = result
        out = attach_document_to_session(session_id, parsed_doc.document_id, filename)
        return J(out, 201)
    except ValueError as e:
        return J({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("chat_upload failed")
        return J({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# REVIEW MODULE — users, personas, review workflow
# Identity: X-User-Email / X-User-Name headers (Entra ID SSO on the frontend);
# body fields "email"/"name" are accepted as a fallback for API testing.
# ═════════════════════════════════════════════════════════════════════════════

def _caller_identity(request: Request, body: Optional[dict] = None) -> dict:
    body = body or {}
    email = (request.headers.get("X-User-Email") or body.get("email") or "").strip().lower()
    name  = (request.headers.get("X-User-Name")  or body.get("name")  or "").strip()
    return {"email": email, "name": name or (email.split("@")[0] if email else "")}


def _review_error(e: Exception):
    """Map service exceptions to HTTP codes."""
    if isinstance(e, FileNotFoundError):
        return J({"error": str(e)}, 404)
    if isinstance(e, PermissionError):
        return J({"error": str(e)}, 403)
    if isinstance(e, ValueError):
        return J({"error": str(e)}, 400)
    logger.exception("review route failed")
    return J({"error": str(e)}, 500)


# ── Users ─────────────────────────────────────────────────────────────────────

@app.get("/api/users")
def users_list():
    try:
        from generation.review_service import list_users
        return J({"users": list_users()})
    except Exception as e:
        return _review_error(e)


@app.post("/api/users")
def users_upsert(body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import upsert_user
        body = body or {}
        if not body.get("email"):
            return J({"error": "email is required"}, 400)
        return J(upsert_user(body["email"].strip().lower(),
                             body.get("name", ""), body.get("role", "Contributor")), 201)
    except Exception as e:
        return _review_error(e)


@app.delete("/api/users/{user_id}")
def users_delete(user_id: str):
    try:
        from generation.review_service import delete_user
        delete_user(user_id)
        return J({"status": "deleted", "user_id": user_id})
    except Exception as e:
        return _review_error(e)


# ── Personas ──────────────────────────────────────────────────────────────────

@app.get("/api/personas")
def personas_list(request: Request):
    try:
        from generation.review_service import list_personas
        me = _caller_identity(request)
        return J({"personas": list_personas(me["email"] or None)})
    except Exception as e:
        return _review_error(e)


@app.post("/api/personas")
def personas_create(request: Request, body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import create_persona
        body = body or {}
        if not body.get("name"):
            return J({"error": "name is required"}, 400)
        me = _caller_identity(request, body)
        return J(create_persona(body["name"], body.get("description", ""), me["email"] or None), 201)
    except Exception as e:
        return _review_error(e)


@app.put("/api/personas/{persona_id}")
def personas_update(persona_id: str, body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import update_persona
        body = body or {}
        return J(update_persona(persona_id, body.get("name"), body.get("description")))
    except Exception as e:
        return _review_error(e)


@app.delete("/api/personas/{persona_id}")
def personas_delete(persona_id: str):
    try:
        from generation.review_service import delete_persona
        delete_persona(persona_id)
        return J({"status": "deleted", "persona_id": persona_id})
    except Exception as e:
        return _review_error(e)


# ── Review workflow ───────────────────────────────────────────────────────────
# NOTE: static paths (share/sent/received/comments) registered BEFORE {review_id}.

@app.post("/api/review/share")
def review_share(request: Request, body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import share_for_review
        body = body or {}
        me   = _caller_identity(request, body)
        if not me["email"]:
            return J({"error": "Caller identity required (X-User-Email header or body.email)"}, 400)
        if not body.get("job_id"):
            return J({"error": "job_id is required"}, 400)
        result = share_for_review(body["job_id"], me, body.get("reviewers") or [], body.get("message"))
        return J(result, 201)
    except Exception as e:
        return _review_error(e)


@app.get("/api/review/sent")
def review_sent(request: Request):
    try:
        from generation.review_service import list_sent
        me = _caller_identity(request)
        if not me["email"]:
            return J({"error": "X-User-Email header required"}, 400)
        return J({"reviews": list_sent(me["email"])})
    except Exception as e:
        return _review_error(e)


@app.get("/api/review/received")
def review_received(request: Request):
    try:
        from generation.review_service import list_received
        me = _caller_identity(request)
        if not me["email"]:
            return J({"error": "X-User-Email header required"}, 400)
        return J({"reviews": list_received(me["email"])})
    except Exception as e:
        return _review_error(e)


@app.patch("/api/review/comments/{comment_id}")
def review_edit_comment(comment_id: str, request: Request,
                        body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import update_review_comment, resolve_review_comment
        body = body or {}
        me   = _caller_identity(request, body)
        result = None
        if body.get("text") is not None:
            result = update_review_comment(comment_id, me["email"], body["text"])
        if body.get("resolved") is not None:
            result = resolve_review_comment(comment_id, bool(body["resolved"]))
        if result is None:
            return J({"error": "Provide 'text' and/or 'resolved'"}, 400)
        return J(result)
    except Exception as e:
        return _review_error(e)


@app.delete("/api/review/comments/{comment_id}")
def review_delete_comment(comment_id: str, request: Request):
    try:
        from generation.review_service import delete_review_comment
        me = _caller_identity(request)
        delete_review_comment(comment_id, me["email"])
        return J({"status": "deleted", "comment_id": comment_id})
    except Exception as e:
        return _review_error(e)


@app.post("/api/review/comments/{comment_id}/apply")
def review_apply_comment(comment_id: str, body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import apply_comment_to_section
        return J(apply_comment_to_section(comment_id, (body or {}).get("section_id")))
    except Exception as e:
        return _review_error(e)


@app.get("/api/review/{review_id}")
def review_workspace(review_id: str, request: Request):
    try:
        from generation.review_service import get_review_workspace
        me = _caller_identity(request)
        return J(get_review_workspace(review_id, me["email"] or None))
    except Exception as e:
        return _review_error(e)


@app.post("/api/review/{review_id}/comments")
def review_add_comment(review_id: str, request: Request,
                       body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import add_review_comment
        body = body or {}
        me   = _caller_identity(request, body)
        if not me["email"]:
            return J({"error": "Caller identity required"}, 400)
        return J(add_review_comment(
            review_id, me, body.get("text", ""),
            section_id=body.get("section_id"), parent_id=body.get("parent_id"),
        ), 201)
    except Exception as e:
        return _review_error(e)


@app.post("/api/review/{review_id}/respond")
def review_respond(review_id: str, request: Request,
                   body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import respond
        body = body or {}
        me   = _caller_identity(request, body)
        if not me["email"]:
            return J({"error": "Caller identity required"}, 400)
        return J(respond(review_id, me["email"], body.get("action", "")))
    except Exception as e:
        return _review_error(e)


@app.post("/api/review/{review_id}/renotify")
def review_renotify(review_id: str):
    try:
        from generation.review_service import renotify
        return J(renotify(review_id))
    except Exception as e:
        return _review_error(e)


@app.post("/api/review/{review_id}/ai-review")
def review_ai_review(review_id: str, body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import ai_persona_review
        body    = body or {}
        persona = body.get("persona") or "Project Manager"
        return J(ai_persona_review(review_id, persona, body.get("instructions", "")))
    except Exception as e:
        return _review_error(e)


@app.post("/api/review/{review_id}/ai-review/keep")
def review_ai_keep(review_id: str, request: Request,
                   body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import keep_ai_comments
        body = body or {}
        me   = _caller_identity(request, body)
        if not me["email"]:
            return J({"error": "Caller identity required"}, 400)
        kept = keep_ai_comments(review_id, me, body.get("persona", ""), body.get("comments") or [])
        return J({"kept": kept, "count": len(kept)}, 201)
    except Exception as e:
        return _review_error(e)


@app.post("/api/review/{review_id}/summarize")
def review_summarize(review_id: str, body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import summarize_for_author
        body = body or {}
        return J({"summaries": summarize_for_author(
            review_id, body.get("personas"), force=bool(body.get("force")))})
    except Exception as e:
        return _review_error(e)


@app.get("/api/review/{review_id}/summaries")
def review_summaries(review_id: str):
    try:
        from generation.review_service import get_summaries
        return J({"summaries": get_summaries(review_id)})
    except Exception as e:
        return _review_error(e)


# ── In-app notifications ──────────────────────────────────────────────────────

@app.get("/api/notifications")
def notifications_list(request: Request):
    try:
        from generation.review_service import list_notifications
        email = (request.headers.get("X-User-Email") or request.query_params.get("email") or "").strip().lower()
        if not email:
            return J({"error": "X-User-Email header required"}, 400)
        unread_only = (request.query_params.get("unread_only", "").lower() == "true")
        limit       = _qint(request, "limit", 50)
        return J(list_notifications(email, unread_only=unread_only, limit=limit))
    except Exception as e:
        return _review_error(e)


@app.post("/api/notifications/read")
def notifications_mark_read(request: Request, body: Optional[dict] = Body(default=None)):
    try:
        from generation.review_service import mark_notifications_read
        body  = body or {}
        email = (request.headers.get("X-User-Email") or body.get("email") or "").strip().lower()
        if not email:
            return J({"error": "X-User-Email header required"}, 400)
        return J(mark_notifications_read(email, body.get("ids")))
    except Exception as e:
        return _review_error(e)


# No SPA fallback — this is an API-only server. Unknown non-/api paths return
# FastAPI's default 404 JSON.


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "7071"))
    print()
    print("  Intellidraft API Server (FastAPI)")
    print("  ----------------------------------")
    print(f"  API     ->  http://localhost:{port}/api")
    print(f"  Swagger ->  http://localhost:{port}/docs")
    print(f"  Health  ->  http://localhost:{port}/api/health")
    print("  Stop    ->  Ctrl+C")
    print()
    uvicorn.run(app, host="0.0.0.0", port=port)
