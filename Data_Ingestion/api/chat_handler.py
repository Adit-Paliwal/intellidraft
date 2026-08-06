"""
Document Chat Studio — Backend Handler
========================================
Routes user chat messages to the appropriate generation service actions.
Session state is persisted in the ChatSession DB table.

Intent pipeline:
  user message  →  classify intent  →  handler  →  structured response

Phases:
  context    → awaiting "generate" command
  generating → job running, polling active
  review     → document ready for section review / modification

This handler is a thin routing layer — it does not call any LLM directly.
All LLM work happens inside generation_service / generator.
For production: swap process_message() body to call the ADK DocumentGeneratorAgent.
"""

from __future__ import annotations
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Optional

from generation.db import ChatSession, Project, GenerationJob, get_session

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Document type aliases
# ─────────────────────────────────────────────────────────────────────────────

_DOC_ALIASES: dict[str, str] = {
    "brd":        "Business Requirements Document (BRD)",
    "rfp":        "Request for Proposal (RFP)",
    "sow":        "Statement of Work (SOW)",
    "proposal":   "Project Proposal",
    "techspec":   "Technical Specification",
    "tech spec":  "Technical Specification",
    "tech_spec":  "Technical Specification",
    "scope":      "Scope Document",
    "ndpr":       "Non-Detailed Project Report (NDPR)",
    "nfa":        "Note for Approval (NFA)",
    "nit":        "Notice Inviting Tender (NIT)",
    "boq":        "Bill of Quantities (BOQ)",
    "arb":        "Architecture Review Board (ARB) Submission",
}


def _resolve_doc_type(raw: str) -> str:
    if not raw:
        return "Business Requirements Document (BRD)"
    k = raw.lower().strip().replace("-", " ").replace("_", " ")
    return _DOC_ALIASES.get(k, raw)


# ─────────────────────────────────────────────────────────────────────────────
# Intent classification — keyword matching, zero LLM cost
# ─────────────────────────────────────────────────────────────────────────────

# "Strong" modify words — unambiguous modification intent, no overlap with generate
_STRONG_MODIFY  = {"modify", "change", "rewrite", "rephrase", "shorten", "shorter",
                   "longer", "simplify", "formal", "informal", "expand",
                   "summarize", "summarise", "revise", "edit", "fix", "adjust", "refine"}
# "Weak" generate words only — excludes "make" (ambiguous: "make it shorter" = modify)
_STRONG_GENERATE = {"generate", "create", "start", "begin", "proceed",
                    "draft", "write", "produce", "build"}
_ALL_GENERATE   = _STRONG_GENERATE | {"go", "ready", "make"}
_STATUS_WORDS   = {"status", "progress", "how", "done", "complete",
                   "pending", "running", "finished", "remaining"}
_EXPORT_WORDS   = {"export", "download", "word", "docx", "pdf",
                   "markdown", "save", "file"}
_SHOW_WORDS     = {"show", "view", "read", "see", "display", "open", "print"}
# Regenerate/redo words — only meaningful once a document already exists (review phase)
_REGEN_WORDS    = {"regenerate", "regen", "redo", "recreate", "remake", "refresh", "rebuild"}
_CONFIRM_WORDS  = {"yes", "confirm", "proceed", "ok", "okay", "sure", "yep",
                   "yeah", "apply", "do", "go", "correct", "right", "please"}
_CANCEL_WORDS   = {"no", "cancel", "stop", "abort", "nevermind", "nope",
                   "dont", "skip", "undo", "revert"}


def _classify(message: str, phase: str) -> str:
    words = set(re.findall(r"\w+", message.lower()[:20000]))

    # Modify always beats generate — handles "generate: can you modify..."
    if words & _STRONG_MODIFY:
        return "modify"

    # Show — works in all phases (section may be complete even during generation)
    if words & _SHOW_WORDS:
        return "show"

    # Regenerate / redo — only in review phase (document already exists). A bare
    # regenerate with no named section is handled as "already up to date".
    if phase == "review" and words & (_REGEN_WORDS | _STRONG_GENERATE):
        return "regenerate"

    # Generate — only make sense when there is no active document yet
    if phase == "context" and words & _ALL_GENERATE:
        return "generate"

    # Status/export/generic
    if words & _STATUS_WORDS:
        return "status"
    if words & _EXPORT_WORDS:
        return "export"

    # "make the document" style in context phase
    if phase == "context" and words & _STRONG_GENERATE:
        return "generate"

    return "general"


def _best_section(message: str, sections: list) -> Optional[dict]:
    """Return the section whose title best matches the user's message.

    Scoring:
      +2  exact word match (title word found verbatim in message words)
      +1  stem match (one word starts-with the other, handles plural/singular)
    Tie-break: section appearing later in the list does NOT win on equal score,
    so the first best is kept (stable ordering).
    """
    msg_words = set(re.findall(r"\w+", message.lower()[:20000]))
    best: Optional[dict] = None
    high = 0
    for sec in sections:
        title_words = [w for w in re.findall(r"\w+", sec["section_title"].lower()) if len(w) > 3]
        score = 0
        for tw in title_words:
            if tw in msg_words:
                score += 2  # exact match — strong signal
            elif any(mw.startswith(tw) or tw.startswith(mw) for mw in msg_words if len(mw) > 3):
                score += 1  # stem match — handles "requirement" vs "requirements"
        if score > high:
            high, best = score, sec
    return best if high > 0 else None


def _fmt_out(message: str) -> str:
    msg = message.lower()
    if "pdf"      in msg:             return "pdf"
    if "markdown" in msg or ".md" in msg: return "md"
    return "docx"


# ─────────────────────────────────────────────────────────────────────────────
# Session helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create(
    session_id: str,
    project_id: Optional[str],
    doc_type:   Optional[str],
    db,
) -> ChatSession:
    chat = db.get(ChatSession, session_id)
    if chat is None:
        chat = ChatSession(
            session_id    = session_id,
            project_id    = project_id,
            document_type = _resolve_doc_type(doc_type or ""),
            phase         = "context",
        )
        db.add(chat)
        db.flush()
    else:
        if project_id and not chat.project_id:
            chat.project_id = project_id
        if doc_type:
            resolved = _resolve_doc_type(doc_type)
            if resolved != chat.document_type:
                chat.document_type = resolved

    # ── Link the chat to the project's EXISTING generation job ───────────────
    # A document is usually generated via POST /generate/project/{id} (API/UI),
    # NOT through the chat. Without this, chat.job_id stays None and every
    # "modify/show/regenerate" returns "no document generated yet" — the reason
    # chatbot edits appear to do nothing. Resolve the project's latest job here
    # (preferring one that matches this chat's document type) so edits work no
    # matter where the document was generated. Runs each message, so a job
    # created AFTER the chat session is picked up on the next message.
    if not chat.job_id and chat.project_id:
        base = db.query(GenerationJob).filter(GenerationJob.project_id == chat.project_id)
        latest = (
            base.filter(GenerationJob.document_type == chat.document_type)
                .order_by(GenerationJob.created_at.desc()).first()
            or base.order_by(GenerationJob.created_at.desc()).first()
        )
        if latest is not None:
            chat.job_id = latest.job_id
            if latest.status == "completed" and chat.phase not in ("review",):
                chat.phase = "review"

    return chat


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def init_session(
    project_id:   str,
    doc_type:     str,
    project_name: str = "",
    session_id:   Optional[str] = None,
) -> dict:
    """
    Return an existing chat session (if one already exists for this project + doc_type)
    or create a fresh one.

    Lookup order:
      1. Exact session_id in body → use that session if it exists
      2. Most-recent session for (project_id, document_type) → resume it
      3. Neither found → create new session

    This prevents the frontend creating a new session_id on every tab open.
    """
    doc_full = _resolve_doc_type(doc_type)

    with get_session() as db:
        # 1. Caller supplied a specific session_id → honour it if it exists
        if session_id:
            existing = db.get(ChatSession, session_id)
            if existing:
                return _resume_dict(existing)

        # 2. Look up the most recent session for this project + doc type
        if project_id:
            existing = (
                db.query(ChatSession)
                .filter(
                    ChatSession.project_id    == project_id,
                    ChatSession.document_type == doc_full,
                )
                .order_by(ChatSession.created_at.desc())
                .first()
            )
            if existing:
                return _resume_dict(existing)

        # 3. Create a fresh session
        new_sid  = str(uuid.uuid4())
        greeting = (
            f"Context loaded for **{project_name or project_id}**. "
            f"Ready to generate your **{doc_full}**. "
            f"Say **'generate'** when you want to start, or ask me anything about the process."
        )
        chat = ChatSession(
            session_id    = new_sid,
            project_id    = project_id,
            document_type = doc_full,
            phase         = "context",
        )
        chat.add_message("assistant", greeting)
        db.add(chat)
        db.commit()

    return {
        "session_id":    new_sid,
        "project_id":    project_id,
        "document_type": doc_full,
        "phase":         "context",
        "action":        "init",
        "content":       greeting,
        "data":          {},
    }


def _resume_dict(chat: ChatSession) -> dict:
    """Return the standard response shape for a resumed (existing) session."""
    msgs    = chat.get_messages()
    last    = msgs[-1] if msgs else None
    content = last["content"] if last else f"Welcome back — continuing your {chat.document_type} session."
    return {
        "session_id":    chat.session_id,
        "project_id":    chat.project_id,
        "job_id":        chat.job_id,
        "document_type": chat.document_type,
        "phase":         chat.phase,
        "action":        "resumed",
        "content":       content,
        "data":          (last.get("data") or {}) if last else {},
        "history":       msgs[-30:],  # last 30 messages so the frontend can restore UI
    }


def process_message(
    session_id: str,
    message:    str,
    project_id: Optional[str] = None,
    doc_type:   Optional[str] = None,
) -> dict:
    """Route a user message → assistant response dict."""
    with get_session() as db:
        chat = _get_or_create(session_id, project_id, doc_type, db)
        chat.add_message("user", message)

        # ── Release the SQLite write lock BEFORE any slow work ───────────────
        # This commit is the fix for "database is locked" triggered from the
        # chatbot. Previously the user-message write stayed PENDING (uncommitted)
        # for the whole handler, so the session held SQLite's single write lock
        # across multi-second work below — the generation spawn in _do_generate
        # (which starts the wave-parallel section writers) and the synchronous
        # LLM regeneration in _execute_modify. Those writers then contended with
        # this held lock and surfaced OperationalError: database is locked.
        # Committing here keeps the transaction short: the routing work below now
        # runs with at most a WAL *read* lock (readers never block the writer).
        # The assistant reply + any chat state changes are persisted in the
        # second short commit at the end. DO NOT remove this commit or move the
        # slow sub-handlers above it. (Prod on Databricks SQL has no single-writer
        # lock, so this class of error is local-SQLite-dev only — see db.py.)
        db.commit()

        try:
            # ── Auto-advance phase when background generation completes ───────
            if chat.phase == "generating" and chat.job_id:
                try:
                    from generation.generation_service import get_job as _get_job
                    _j = _get_job(chat.job_id)
                    if _j.get("status") == "completed":
                        chat.phase = "review"
                except Exception:
                    pass  # non-fatal — phase will advance on next successful check
                # Flush the phase change immediately so it isn't held pending
                # across the slow routing below (same lock-hygiene as above).
                db.commit()

            # ── Pending confirmation check (always takes priority) ────────────
            pending = chat.get_pending()
            ptype   = pending.get("type") if pending else None
            if ptype in ("modify", "update_sections"):
                words = set(re.findall(r"\w+", message.lower()[:20000]))
                is_cancel  = bool(words & _CANCEL_WORDS and not words & _CONFIRM_WORDS)
                is_confirm = bool(words & _CONFIRM_WORDS)

                if ptype == "modify":
                    if is_cancel:
                        chat.clear_pending()
                        result = {
                            "action":  "cancelled",
                            "content": f"Modification cancelled — **{pending['section_title']}** remains unchanged.",
                            "data":    {},
                        }
                    elif is_confirm:
                        result = _execute_modify(pending)
                        chat.clear_pending()
                    else:
                        result = {
                            "action":  "confirm_modify",
                            "content": (
                                f"Still waiting for confirmation — shall I modify "
                                f"**{pending['section_title']}**?\n\n"
                                f"Say **yes** to apply or **no** to cancel."
                            ),
                            "data": pending,
                        }

                else:  # update_sections
                    n = len(pending.get("sections", []))
                    if is_cancel:
                        chat.clear_pending()
                        result = {
                            "action":  "cancelled",
                            "content": "Section update cancelled — no changes made.",
                            "data":    {},
                        }
                    elif is_confirm:
                        result = _execute_section_updates(pending)
                        chat.clear_pending()
                    else:
                        result = {
                            "action":  "confirm_update",
                            "content": (
                                f"Still waiting — shall I regenerate **{n} section{'s' if n != 1 else ''}** "
                                f"based on **{pending.get('filename', 'the new document')}**?\n\n"
                                "Say **yes** to proceed or **no** to cancel."
                            ),
                            "data": pending,
                        }
            else:
                # ── Normal intent routing ─────────────────────────────────────
                intent = _classify(message, chat.phase)
                if   intent == "generate" and chat.phase == "context":
                    result = _do_generate(chat, db)
                elif intent == "regenerate":
                    result = _do_regenerate(chat, message)
                elif intent == "modify":
                    result = _do_modify(chat, message)
                elif intent == "show":
                    result = _do_show(chat, message)
                elif intent == "status":
                    result = _do_status(chat)
                elif intent == "export":
                    result = _do_export(chat, message)
                else:
                    result = _do_general(chat)

        except Exception as exc:
            logger.exception("chat_handler error for session %s", session_id)
            result = {
                "action":  "error",
                "content": f"Something went wrong: {exc}",
                "data":    {},
            }

        # ── Second short write: persist the assistant reply + any chat state
        # changes (job_id / phase / pending set by the sub-handlers above). All
        # slow work is already done, so this commit holds the write lock only
        # briefly; SQLite's busy_timeout (30s, see db.py) absorbs any contention.
        chat.add_message("assistant", result["content"], result.get("data"))
        db.commit()

        return {
            "session_id":    chat.session_id,
            "project_id":    chat.project_id,
            "job_id":        chat.job_id,
            "phase":         chat.phase,
            "document_type": chat.document_type,
            **result,
        }


def get_history(session_id: str) -> dict:
    """Return full session with message history."""
    with get_session() as db:
        chat = db.get(ChatSession, session_id)
        if chat is None:
            raise ValueError(f"Session '{session_id}' not found")
        return chat.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# Intent handlers
# ─────────────────────────────────────────────────────────────────────────────

def _do_generate(chat: ChatSession, db) -> dict:
    from generation.generation_service import start_job_from_project, get_job

    if not chat.project_id:
        return {"action": "need_project",
                "content": "Please select a project before generating.",
                "data": {}}

    proj = db.get(Project, chat.project_id)
    if proj is None:
        return {"action": "error",
                "content": "Project not found in database.",
                "data": {}}

    # ── Idempotency: return the existing job if it's already complete ─────────
    # Check chat.job_id first, fall back to proj.job_id (set by start_job_from_project)
    existing_job_id = chat.job_id or getattr(proj, "job_id", None)
    if existing_job_id:
        try:
            existing = get_job(existing_job_id)
            if existing.get("status") == "completed":
                chat.job_id = existing_job_id
                chat.phase  = "review"
                n = existing.get("total_sections", 0)
                return {
                    "action":  "completed",
                    "content": (
                        f"Your **{chat.document_type}** is already complete! "
                        f"All **{n} sections** are ready. "
                        "To modify a section, describe what you'd like changed. "
                        "Say **'export as Word'** or **'export as PDF'** to download."
                    ),
                    "data": {
                        "job_id":         existing_job_id,
                        "status":         "completed",
                        "total_sections": n,
                        "sections": [
                            {
                                "section_id":    s.get("section_id"),
                                "section_title": s.get("section_title"),
                                "status":        s.get("status"),
                            }
                            for s in existing.get("sections", [])
                        ],
                    },
                }
        except Exception:
            pass  # job check failed — fall through to create a new job

    has_docs = bool(proj.document_ids)
    doc_type = chat.document_type or proj.document_type or "Business Requirements Document (BRD)"

    preamble = ""
    if not has_docs:
        preamble = (
            "No source documents attached — generating from project form data only. "
            "Results will be based entirely on the project details you entered.\n\n"
        )

    job    = start_job_from_project(chat.project_id, doc_type_override=doc_type, allow_no_docs=True)
    job_id = job["job_id"]

    chat.job_id = job_id
    chat.phase  = "generating"

    n = job.get("total_sections", 0)
    return {
        "action":  "generation_started",
        "content": (
            preamble
            + f"Generation started! **{n} sections** are queued for your **{doc_type}**. "
            + "I'll update you as each section completes. "
            + f"Job: `{job_id}`"
        ),
        "data": {
            "job_id":         job_id,
            "total_sections": n,
            "sections": [
                {
                    "section_id":    s["section_id"],
                    "section_title": s["section_title"],
                    "status":        s["status"],
                }
                for s in job.get("sections", [])
            ],
        },
    }


def _do_status(chat: ChatSession) -> dict:
    if not chat.job_id:
        return {"action": "no_job",
                "content": "No active generation job. Say **'generate'** to start one.",
                "data": {}}

    from generation.generation_service import get_job
    job      = get_job(chat.job_id)
    total    = job.get("total_sections", 0)
    done     = job.get("completed_sections", 0)
    status   = job.get("status", "unknown")
    sections = job.get("sections", [])

    if status == "completed":
        chat.phase = "review"
        content = (
            f"All **{total} sections** are complete! "
            "You can now review them in the document panel. "
            "Ask me to modify any section, or say **'export'** to download."
        )
        action = "completed"
    elif status in ("pending", "in_progress"):
        pct         = int(done / total * 100) if total else 0
        done_titles = [s["section_title"] for s in sections if s.get("status") == "completed"]
        content = f"In progress: **{done}/{total} sections** ({pct}%)"
        if done_titles:
            preview = ", ".join(done_titles[:3])
            if len(done_titles) > 3:
                preview += f" +{len(done_titles) - 3} more"
            content += f" — ✓ {preview}"
        action = "status"
    elif status == "failed":
        content = f"Generation failed: {job.get('error', 'Unknown error')}"
        action  = "error"
    else:
        content = f"Status: **{status}**"
        action  = "status"

    return {
        "action":  action,
        "content": content,
        "data": {
            "job_id":             chat.job_id,
            "status":             status,
            "total_sections":     total,
            "completed_sections": done,
            "sections": [
                {
                    "section_id":    s.get("section_id"),
                    "section_title": s.get("section_title"),
                    "status":        s.get("status"),
                }
                for s in sections
            ],
        },
    }


def _do_regenerate(chat: ChatSession, message: str) -> dict:
    """
    Handle 'regenerate' / 'redo' / 'refresh' requests in the review phase.

      - If the user named a specific section → treat it as a targeted regenerate
        (store a pending modify and ask for confirmation).
      - Otherwise (a bare 'regenerate' with no section and no change details) →
        the document is already up to date; just reload the preview instead of
        re-running the LLM.
    """
    if not chat.job_id:
        return {"action": "no_job",
                "content": "No document generated yet. Say **'generate'** to create it first.",
                "data": {}}

    from generation.generation_service import get_job
    job      = get_job(chat.job_id)
    total    = job.get("total_sections", 0)
    sections = [s for s in job.get("sections", []) if s.get("status") == "completed"]

    # Did the user reference a specific section? → confirm a targeted regenerate.
    sec = _best_section(message, sections)
    if sec:
        pending = {
            "type":          "modify",
            "section_id":    sec["section_id"],
            "section_title": sec["section_title"],
            "instruction":   message,
        }
        chat.set_pending(pending)
        return {
            "action":  "confirm_modify",
            "content": (
                f"Regenerate the **{sec['section_title']}** section?\n\n"
                f"Say **yes** to proceed, or tell me what to change."
            ),
            "data": pending,
        }

    # No section + no change details → nothing to regenerate; show the preview.
    return {
        "action":  "up_to_date",
        "content": (
            "Your content is already up to date — there are no pending changes to regenerate. "
            "Showing the latest preview.\n\n"
            "To revise something specific, tell me the section and what to adjust "
            "(e.g. *“make the Scope section shorter”* or "
            "*“regenerate the Objective with more detail”*)."
        ),
        "data": {
            "job_id":         chat.job_id,
            "status":         "completed",
            "show_preview":   True,
            "total_sections": total,
            "sections": [
                {"section_id": s.get("section_id"),
                 "section_title": s.get("section_title"),
                 "status": s.get("status")}
                for s in sections
            ],
        },
    }


def _do_modify(chat: ChatSession, message: str) -> dict:
    """Identify target section and ask the user to confirm before regenerating."""
    if not chat.job_id:
        return {
            "action":  "no_job",
            "content": "No document generated yet — there's nothing to modify. Say **'generate'** to create the document first.",
            "data":    {},
        }

    from generation.generation_service import get_job
    job      = get_job(chat.job_id)
    sections = [s for s in job.get("sections", []) if s.get("status") == "completed"]

    sec = _best_section(message, sections)
    if not sec:
        titles = [s["section_title"] for s in sections]
        return {
            "action":  "clarify_section",
            "content": (
                "Which section would you like to modify? "
                "Completed sections:\n"
                + "\n".join(f"• **{t}**" for t in titles)
            ),
            "data": {
                "sections": [
                    {"section_id": s.get("section_id"), "section_title": s.get("section_title")}
                    for s in sections
                ]
            },
        }

    # Store the pending operation and ask for confirmation
    pending = {
        "type":          "modify",
        "section_id":    sec["section_id"],
        "section_title": sec["section_title"],
        "instruction":   message,
    }
    chat.set_pending(pending)

    return {
        "action":  "confirm_modify",
        "content": (
            f"I'll modify the **{sec['section_title']}** section with your instruction:\n\n"
            f"> {message}\n\n"
            f"Say **yes** to apply the change, or **no** to cancel."
        ),
        "data": pending,
    }


def _execute_modify(pending: dict) -> dict:
    """Actually regenerate a section after the user has confirmed."""
    from generation.generation_service import add_comment, regenerate_section

    comment  = add_comment(pending["section_id"], pending["instruction"], "edit_request")
    new_ver  = regenerate_section(pending["section_id"], comment["comment_id"])

    return {
        "action":  "section_modified",
        "content": (
            f"Done! **{pending['section_title']}** has been regenerated "
            f"(version {new_ver['version_number']}). "
            "All other sections remain unchanged."
        ),
        "data": {
            "section_id":    pending["section_id"],
            "section_title": pending["section_title"],
            "version":       new_ver,
        },
    }


def _do_show(chat: ChatSession, message: str) -> dict:
    if not chat.job_id:
        return {
            "action":  "no_job",
            "content": "No document generated yet — nothing to show. Say **'generate'** to create the document first.",
            "data":    {},
        }

    from generation.generation_service import get_job, get_section
    job      = get_job(chat.job_id)
    sections = [s for s in job.get("sections", []) if s.get("status") == "completed"]

    sec = _best_section(message, sections)
    if not sec:
        titles = [s["section_title"] for s in sections]
        return {
            "action":  "clarify_section",
            "content": "Which section would you like to see?\n"
                       + "\n".join(f"• **{t}**" for t in titles),
            "data":    {
                "sections": [
                    {"section_id": s.get("section_id"), "section_title": s.get("section_title")}
                    for s in sections
                ]
            },
        }

    full     = get_section(sec["section_id"])
    versions = full.get("versions", [])
    latest   = max(versions, key=lambda v: v["version_number"]) if versions else None

    return {
        "action":  "show_section",
        "content": f"Here's the **{full['section_title']}** section:",
        "data": {
            "section_id":    full["section_id"],
            "section_title": full["section_title"],
            "content":       latest["content"] if latest else "(no content yet)",
            "word_count":    latest.get("word_count", 0) if latest else 0,
            "version":       latest["version_number"] if latest else 0,
        },
    }


def _do_export(chat: ChatSession, message: str) -> dict:
    if not chat.job_id:
        return {"action": "no_job", "content": "No active job to export.", "data": {}}
    fmt = _fmt_out(message)
    label_map = {"docx": "Word (.docx)", "pdf": "PDF", "md": "Markdown"}
    return {
        "action":  "export_ready",
        "content": (
            f"Your **{label_map.get(fmt, fmt.upper())}** is ready. "
            "Click the download button below to save it."
        ),
        "data": {
            "job_id": chat.job_id,
            "format": fmt,
        },
    }


def attach_document_to_session(session_id: str, doc_id: str, filename: str) -> dict:
    """
    Called after a file is uploaded via the chat UI.

    1. Attaches the doc to the project's document_ids_json.
    2. Patches the active job's user_inputs_json so future regenerate_section()
       calls load the new document content (fixes frozen snapshot bug).
    3. If a document is already generated (phase == review), runs LLM impact
       analysis to find which sections should be updated, then asks for confirmation.
    """
    import json as _json

    with get_session() as db:
        chat = db.get(ChatSession, session_id)
        if not chat:
            raise ValueError(f"Session '{session_id}' not found")

        # 1. Attach to project document list
        if chat.project_id:
            proj = db.get(Project, chat.project_id)
            if proj:
                ids = _json.loads(proj.document_ids_json or "[]")
                if doc_id not in ids:
                    ids.append(doc_id)
                    proj.document_ids_json = _json.dumps(ids)

        # 2. Patch active job so regenerate_section() sees the new document
        job_id = chat.job_id
        phase  = chat.phase
        if job_id:
            job = db.get(GenerationJob, job_id)
            if job:
                ui = _json.loads(job.user_inputs_json or "{}")
                existing_ids: list = ui.get("document_ids") or []
                if doc_id not in existing_ids:
                    existing_ids.append(doc_id)
                    ui["document_ids"]       = existing_ids
                    job.user_inputs_json     = _json.dumps(ui)

        db.commit()

    # 3. If document already generated → analyze which sections need updating
    affected: list[dict] = []
    if phase == "review" and job_id:
        affected = _analyze_doc_impact(job_id, doc_id, filename)

    if affected:
        n          = len(affected)
        titles_md  = "\n".join(
            f"• **{a['section_title']}** — {a.get('reason', 'new information found')}"
            for a in affected
        )
        content = (
            f"**{filename}** uploaded and parsed.\n\n"
            f"Based on the new content, **{n} section{'s' if n != 1 else ''}** "
            f"should be updated:\n\n{titles_md}\n\n"
            f"Say **yes** to regenerate {'these sections' if n > 1 else 'this section'}, "
            f"or **no** to skip."
        )
        pending = {
            "type":     "update_sections",
            "job_id":   job_id,
            "doc_id":   doc_id,
            "filename": filename,
            "sections": affected,
        }
        with get_session() as db:
            chat = db.get(ChatSession, session_id)
            if chat:
                chat.set_pending(pending)
                chat.add_message("assistant", content, pending)
                db.commit()

        return {
            "action":  "confirm_update",
            "content": content,
            "data":    pending,
        }

    # No job yet or no affected sections — plain confirmation
    phase_hint = (
        "Say **'generate'** to start document generation using this file."
        if phase == "context"
        else "The document context has been updated — future regenerations will include it."
    )
    content = (
        f"**{filename}** has been uploaded and parsed successfully. "
        f"Its content will be included in the generation context. {phase_hint}"
    )
    with get_session() as db:
        chat = db.get(ChatSession, session_id)
        if chat:
            chat.add_message("assistant", content, {"document_id": doc_id, "filename": filename})
            db.commit()

    return {
        "action":      "document_uploaded",
        "content":     content,
        "document_id": doc_id,
        "filename":    filename,
        "data":        {"document_id": doc_id, "filename": filename},
    }


def _analyze_doc_impact(job_id: str, doc_id: str, filename: str) -> list[dict]:
    """
    LLM call: compare newly uploaded document against existing section content
    and return the list of sections that should be regenerated.

    Returns list of {"section_id", "section_title", "reason"}.
    Returns [] on any error (non-fatal).
    """
    import json as _json
    from llm_provider import call_with_fallback
    from storage.gcs_storage import get_storage_service
    from models.meta_schema import ParsedDocument
    from generation.generation_service import get_job

    # Load new document text
    try:
        store      = get_storage_service()
        meta       = store.get_meta_json(doc_id)
        parsed     = ParsedDocument(**meta)
        new_doc_text = parsed.to_llm_context(max_chars=4_000)
    except Exception as e:
        logger.warning("[chat] Could not load new doc %s for impact analysis: %s", doc_id, e)
        return []

    # Load completed sections
    try:
        job = get_job(job_id)
    except Exception:
        return []

    sections = [s for s in job.get("sections", []) if s.get("status") == "completed"]
    if not sections:
        return []

    # Build compact summary: title + first 250 chars of current content
    section_lines = []
    for sec in sections:
        snippet = (sec.get("current_content") or "")[:250].replace("\n", " ").strip()
        section_lines.append(
            f"section_id: {sec['section_id']}\n"
            f"title: {sec['section_title']}\n"
            f"snippet: {snippet}"
        )
    sections_text = "\n\n---\n\n".join(section_lines)

    prompt = (
        f'A new document named "{filename}" was uploaded to an in-progress project.\n\n'
        f"NEW DOCUMENT EXCERPT:\n{new_doc_text}\n\n"
        f"EXISTING GENERATED SECTIONS:\n{sections_text}\n\n"
        "Task: Identify which existing sections should be regenerated to incorporate "
        "new or updated information from the new document. Only include sections that "
        "would materially benefit from the new content.\n\n"
        "Return ONLY a valid JSON array (no markdown, no preamble):\n"
        '[{"section_id":"<uuid>","section_title":"<title>","reason":"<one sentence>"}]\n\n'
        "If no sections need updating, return: []"
    )

    try:
        response, _ = call_with_fallback(
            messages   = [{"role": "user", "content": prompt}],
            max_tokens = 800,
            timeout    = 30,
            log_prefix = "[DocImpact]",
        )
        text  = response.strip()
        start = text.find("[")
        end   = text.rfind("]")
        if start == -1 or end == -1:
            return []
        affected = _json.loads(text[start:end + 1])
        if not isinstance(affected, list):
            return []
        # Only keep entries that match real section IDs
        valid_ids = {s["section_id"] for s in sections}
        return [
            a for a in affected
            if isinstance(a, dict) and a.get("section_id") in valid_ids
        ]
    except Exception as e:
        logger.warning("[chat] Impact analysis LLM call failed: %s", e)
        return []


def _execute_section_updates(pending: dict) -> dict:
    """
    Regenerate only the sections flagged by the impact analysis.
    Runs in a background thread — returns immediately so the user sees a response.
    Each section gets a new version (version history preserved).
    """
    import threading
    from generation.generation_service import add_comment, regenerate_section as _regen

    sections_to_update = pending.get("sections", [])
    filename = pending.get("filename", "new document")
    job_id   = pending.get("job_id", "")

    if not sections_to_update:
        return {"action": "info", "content": "No sections marked for update.", "data": {}}

    def _run():
        for sec in sections_to_update:
            try:
                comment = add_comment(
                    sec["section_id"],
                    f"Update this section to incorporate information from the newly uploaded document: {filename}",
                    "edit_request",
                )
                _regen(sec["section_id"], comment["comment_id"])
                logger.info("[chat] Updated section '%s' (new version saved)", sec["section_title"])
            except Exception as exc:
                logger.warning("[chat] Failed to update section '%s': %s", sec["section_title"], exc)

    t = threading.Thread(target=_run, daemon=True, name=f"doc-update-{job_id[:8]}")
    t.start()

    n      = len(sections_to_update)
    titles = ", ".join(f"**{s['section_title']}**" for s in sections_to_update[:3])
    if n > 3:
        titles += f" +{n - 3} more"

    return {
        "action":  "updating_sections",
        "content": (
            f"Updating {n} section{'s' if n != 1 else ''} in the background: {titles}. "
            "Each section gets a new version — all previous versions are saved. "
            "Watch the document panel refresh as they complete."
        ),
        "data": {
            "job_id":   job_id,
            "sections": [
                {"section_id": s["section_id"], "section_title": s["section_title"]}
                for s in sections_to_update
            ],
        },
    }


def _do_general(chat: ChatSession) -> dict:
    hints = {
        "context": (
            "I'm ready to help! Say **'generate'** to start document generation, "
            "or describe any changes you'd like to the project context first."
        ),
        "generating": (
            "Generation is in progress. Say **'status'** to check progress, "
            "or wait — I'll update you automatically when each section completes."
        ),
        "review": (
            "The document is ready! Here's what you can do:\n"
            "• Say **'show [section name]'** to view a section\n"
            "• Say **'make the [section] shorter/simpler/more formal'** to modify it\n"
            "• Say **'export as Word'** or **'download PDF'** to get the file"
        ),
    }
    return {
        "action":  "general",
        "content": hints.get(chat.phase, "How can I help?"),
        "data":    {},
    }
