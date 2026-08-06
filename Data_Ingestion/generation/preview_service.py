"""
preview_service.py
==================
Document preview for IntelliDraft — pure-Python HTML, NO LibreOffice.

How it works (synchronous, single-renderer — LibreOffice was removed
2026-07-17 so local and Databricks produce identical output)
------------------------------------------------------------------
1. Client calls GET /api/generate/{job_id}/preview/html
2. If a whole-document manual HTML edit exists (and is newer than every section),
   it is served as-is (see _latest_manual_html).
3. Otherwise the document is rendered to HTML by render_document_html() — markdown2
   per section, styled to MATCH the Word download via generation/doc_style.py, with
   <section data-section-id> markers for inline/whole-document editing.
4. Any section edit/regenerate calls invalidate_preview_cache(job_id).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# ── In-process deduplication state ───────────────────────────────────────────
# Prevents multiple simultaneous conversions for the same job regardless of
# how many API calls arrive concurrently (polling, React auto-trigger, etc.).
#
# _preview_cache : job_id → HTML string
# _inflight      : job_id → "sync" sentinel while a conversion is running
# _inflight_lock : guards both dicts
_preview_cache:  dict[str, str]  = {}
_inflight:       dict[str, str]  = {}   # value = task_id or "sync"
_inflight_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Public API — called from the FastAPI routes (main.py)
# ─────────────────────────────────────────────────────────────────────────────

def pregenerate_preview(job_id: str) -> None:
    """
    Called by the generation service when a job finishes.
    Kicks off the DOCX→HTML conversion in a daemon thread so the result
    is cached before the React client ever calls the preview endpoint.
    Safe to call multiple times — the in-flight guard prevents duplicate runs.
    """
    def _warmup() -> None:
        try:
            result = get_or_submit_preview(job_id)
            logger.info("[preview] Pre-warmup job %s → status=%s", job_id, result.get("status"))
        except Exception as exc:
            logger.warning("[preview] Pre-warmup failed (non-fatal) for job %s: %s", job_id, exc)

    t = threading.Thread(target=_warmup, daemon=True, name=f"preview-warmup-{job_id[:8]}")
    t.start()


def _latest_manual_html(job_id: str) -> "str | None":
    """Return the newest manual_html snapshot's HTML — but only if no section has
    been regenerated/edited AFTER it was saved. Once a section changes, the
    manual HTML is stale and we fall back to re-rendering from the section
    content. Returns None when there is no current manual HTML."""
    from generation.db import DocumentSnapshot, Section, get_session as _gs
    from sqlalchemy import func
    try:
        with _gs() as s:
            snap = (
                s.query(DocumentSnapshot)
                .filter(
                    DocumentSnapshot.job_id == job_id,
                    DocumentSnapshot.trigger_type == "manual_html",
                    DocumentSnapshot.html_content.isnot(None),
                )
                .order_by(DocumentSnapshot.created_at.desc())
                .first()
            )
            if not snap:
                return None
            latest_sec = (
                s.query(func.max(Section.updated_at))
                .filter(Section.job_id == job_id)
                .scalar()
            )
            if latest_sec and snap.created_at and latest_sec > snap.created_at:
                return None   # a section changed after this edit → stale
            return snap.html_content
    except Exception:
        return None


def get_or_submit_preview(job_id: str) -> dict:
    """
    Returns one of:
      {"status": "ready",   "html": "<html>…", "cached": bool}
      {"status": "pending", "task_id": "…",    "poll_url": "…"}
      {"status": "error",   "error": "…"}

    A whole-document MANUAL HTML edit (saved via /preview/save) takes precedence
    and is served as-is, until a section is AI-regenerated after it.

    NOTE: LibreOffice was removed from the preview path (2026-07-17). The preview
    is ALWAYS rendered by render_document_html() — styled to match the .docx via
    generation/doc_style.py — so local and Databricks produce identical output.
    """
    manual = _latest_manual_html(job_id)
    if manual is not None:
        return {"status": "ready", "html": manual, "cached": False, "renderer": "manual_html"}
    try:
        html = render_document_html(job_id)
        html = _inject_section_handlers(html, job_id)
        return {"status": "ready", "html": html, "cached": False, "renderer": "html"}
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.exception("[preview] render failed for job %s", job_id)
        return {"status": "error", "error": str(e)}


def poll_preview_status(job_id: str, task_id: str) -> dict:
    """
    LEGACY endpoint shim — the Celery worker path was retired. Serves the
    in-memory cache when the conversion already finished; otherwise tells the
    caller to poll the (synchronous) /preview/html endpoint instead.
    """
    with _inflight_lock:
        cached = _preview_cache.get(job_id)
        still_running = _inflight.get(job_id) == "sync"
    if cached:
        return {"status": "ready", "html": cached, "cached": True}
    if still_running:
        return {"status": "pending", "state": "CONVERTING",
                "poll_url": f"/api/generate/{job_id}/preview/html"}
    return {"status": "pending", "state": "NOT_STARTED",
            "poll_url": f"/api/generate/{job_id}/preview/html"}


def invalidate_preview_cache(job_id: str) -> None:
    """
    Delete the cached preview for this job so the next request re-converts.
    Call this after any section is patched (manual edit) or regenerated.
    """
    with _inflight_lock:
        _preview_cache.pop(job_id, None)
        _inflight.pop(job_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

# CSS comes from the shared style module so the preview matches the Word download.
def _preview_css() -> str:
    from generation.doc_style import preview_css
    return preview_css()


def _current_sections(job_id: str) -> list[dict]:
    """Return [{section_id, title, markdown}] for the job's CURRENT section
    versions, in document order. Empty list if the job/sections are missing."""
    from generation.db import GenerationJob, get_session as _gs
    out: list[dict] = []
    with _gs() as s:
        job = s.get(GenerationJob, job_id)
        if not job:
            return out
        for sec in sorted(job.sections, key=lambda x: x.order_index):
            latest = sec.latest_version()
            out.append({
                "section_id": sec.section_id,
                "title":      sec.section_title,
                "markdown":   (latest.content if latest else "") or "",
            })
    return out


def _render_section_body(md: str) -> str:
    """Markdown → HTML for a single section's body (no wrapper)."""
    import markdown2
    return markdown2.markdown(
        md or "", extras=["tables", "fenced-code-blocks", "break-on-newline", "strike"],
    )


def render_document_html(job_id: str) -> str:
    """Build the full preview HTML — styled to MATCH the Word download
    (generation/doc_style.py) — with each section wrapped in a
    <section data-section-id data-section-title> marker so the whole-document
    HTML editor can split it back into sections on save.

    Sections are numbered "N. Title" at the top level to mirror the .docx.
    This is the ONLY preview renderer (no LibreOffice) so local == Databricks.
    """
    from datetime import datetime
    from generation.db import GenerationJob, get_session as _gs
    from generation.doc_writer import _extract_project_name

    with _gs() as s:
        job = s.get(GenerationJob, job_id)
        doc_type = (job.document_type if job else "") or "Document"
        project  = _extract_project_name(job.user_inputs_json if job else None)

    # Cover / header block — mirrors the .docx cover + document-control meta line.
    # Control metadata (template ID / status / classification / confidentiality)
    # comes from the SAME shared loader the .docx cover uses, so they never drift.
    from generation.doc_meta import get_doc_meta
    meta = get_doc_meta(doc_type)
    ctrl = " &nbsp;·&nbsp; ".join(
        f"<strong>{label}:</strong> {meta[k]}"
        for label, k in (("Template ID", "template_id"),
                         ("Status", "document_status"),
                         ("Classification", "classification"))
        if meta.get(k)
    )
    parts = [
        '<div class="id-header">',
        f'<div class="id-title">{doc_type}</div>',
        f'<div class="id-meta"><strong>Project:</strong> {project} &nbsp;·&nbsp; '
        f'<strong>Generated:</strong> {datetime.utcnow():%Y-%m-%d %H:%M} UTC</div>',
    ]
    if ctrl:
        parts.append(f'<div class="id-meta">{ctrl}</div>')
    if meta.get("confidentiality_text"):
        parts.append(f'<div class="id-meta" style="font-style:italic">{meta["confidentiality_text"]}</div>')
    parts.append('</div>')
    for i, sec in enumerate(_current_sections(job_id), start=1):
        body = _render_section_body(sec["markdown"])
        title_attr = sec["title"].replace('"', "&quot;")
        parts.append(
            f'<section data-section-id="{sec["section_id"]}" '
            f'data-section-title="{title_attr}">\n'
            f'<h2>{i}. {sec["title"]}</h2>\n{body}\n</section>'
        )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_preview_css()}</style></head><body>"
        + "\n".join(parts)
        + "</body></html>"
    )


def split_sections_from_html(html: str) -> dict:
    """Parse edited HTML → {section_id: normalized_inner_html} using the
    <section data-section-id> markers. Returns {} if no markers are present."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return {}
    out: dict[str, str] = {}
    soup = BeautifulSoup(html or "", "html.parser")
    for sec in soup.find_all("section", attrs={"data-section-id": True}):
        sid = sec.get("data-section-id")
        # normalize whitespace so trivial reflow isn't counted as an edit
        inner = "".join(str(c) for c in sec.contents)
        out[sid] = " ".join(inner.split())
    return out


def detect_changed_sections(job_id: str, submitted_html: str) -> list[dict]:
    """Compare the submitted (edited) HTML against the freshly-rendered current
    HTML, per section marker, and return [{section_id, title}] for the sections
    whose content differs. Best-effort: [] if markers are absent on either side."""
    current = split_sections_from_html(render_document_html(job_id))
    edited  = split_sections_from_html(submitted_html)
    if not current or not edited:
        return []
    titles = {s["section_id"]: s["title"] for s in _current_sections(job_id)}
    changed = []
    for sid, edited_inner in edited.items():
        if sid in current and current[sid] != edited_inner:
            changed.append({"section_id": sid, "section_title": titles.get(sid, "")})
    return changed


# ─────────────────────────────────────────────────────────────────────────────
# Section click-handler injection
# ─────────────────────────────────────────────────────────────────────────────

def _inject_section_handlers(html: str, job_id: str) -> str:
    """
    Post-process LibreOffice HTML to enable true inline editing.

    Injects a script that:
    - Makes section headings clickable (hover highlight + pointer cursor)
    - On click: signals parent to fetch section content, then shows
      a contenteditable div directly in the document (replacing the
      LibreOffice-rendered paragraphs for that section)
    - Sticky amber toolbar appears with Save / Cancel buttons
    - On Save: sends content back to parent via postMessage → parent
      calls PATCH /section/{id} and reloads the preview

    Bidirectional postMessage protocol:
      iframe → parent : { type: 'intellidraft_section_click', section_id, title }
      parent → iframe : { type: 'intellidraft_section_content', section_id, content }
      iframe → parent : { type: 'intellidraft_save_section', section_id, content }
      parent → iframe : { type: 'intellidraft_save_complete' }
      parent → iframe : { type: 'intellidraft_save_error', error }

    Safe to call on error (returns html unchanged).
    """
    import json as json_mod
    from generation.db import GenerationJob, get_session as _gs

    try:
        with _gs() as session:
            job = session.get(GenerationJob, job_id)
            if not job or not job.sections:
                return html
            section_map = {
                sec.section_title: sec.section_id
                for sec in job.sections
            }
    except Exception:
        return html

    map_json = json_mod.dumps(section_map)

    script = (
        "<script>\n"
        "(function(){\n"
        "var SM=" + map_json + ";\n"          # section_title → section_id
        "var _sid=null,_hel=null,_orig=[];\n"  # active section state

        # ── sticky edit toolbar ──────────────────────────────────────────
        "function _bar(){return document.getElementById('_id_bar');}\n"
        "function _showBar(title){\n"
        "  var b=_bar();\n"
        "  if(!b){\n"
        "    b=document.createElement('div');\n"
        "    b.id='_id_bar';\n"
        "    b.style.cssText='position:sticky;top:0;z-index:9999;background:#fff3cd;"
        "border-bottom:2px solid #f0ad4e;padding:8px 16px;display:flex;"
        "align-items:center;gap:10px;font-family:sans-serif;box-shadow:0 2px 6px rgba(0,0,0,.12);';\n"
        "    b.innerHTML='<span style=\"font-size:13px;font-weight:600;color:#856404\">"
        "&#9998; Editing: <em id=\"_id_bar_t\"></em></span>'\n"
        "      +'<button id=\"_id_sav\" style=\"margin-left:auto;background:#5c6bc0;"
        "color:#fff;border:none;border-radius:4px;padding:6px 16px;cursor:pointer;"
        "font-size:13px;font-weight:600;\">&#128190; Save</button>'\n"
        "      +'<button id=\"_id_can\" style=\"background:#fff;color:#5c6bc0;"
        "border:1px solid #c5cae9;border-radius:4px;padding:6px 12px;"
        "cursor:pointer;font-size:13px;\">Cancel</button>';\n"
        "    document.body.prepend(b);\n"
        "    document.getElementById('_id_sav').addEventListener('click',_doSave);\n"
        "    document.getElementById('_id_can').addEventListener('click',_doCancel);\n"
        "  }\n"
        "  document.getElementById('_id_bar_t').textContent=title;\n"
        "  b.style.display='flex';\n"
        "}\n"
        "function _hideBar(){var b=_bar();if(b)b.style.display='none';}\n"

        # ── inline editor ────────────────────────────────────────────────
        "function _showEditor(sid,content){\n"
        "  if(!_hel)return;\n"
        "  var ed=document.getElementById('_id_ed');if(ed)ed.remove();\n"
        # hide original content between this heading and the next
        "  var el=_hel.nextElementSibling;\n"
        "  _orig=[];\n"
        "  while(el&&!/^H[1-4]$/.test(el.tagName)){\n"
        "    _orig.push({el:el,d:el.style.display});\n"
        "    el.style.display='none';\n"
        "    el=el.nextElementSibling;\n"
        "  }\n"
        # insert a contenteditable div with the Markdown content
        "  ed=document.createElement('div');\n"
        "  ed.id='_id_ed';\n"
        "  ed.contentEditable='true';\n"
        "  ed.style.cssText='border:2px solid #5c6bc0;border-radius:6px;padding:16px 20px;"
        "min-height:120px;outline:none;font-family:Consolas,\"Courier New\",monospace;"
        "font-size:13px;line-height:1.75;background:#f8f9ff;color:#1a1a2e;"
        "white-space:pre-wrap;word-wrap:break-word;margin:10px 0 16px 0;';\n"
        "  ed.textContent=content;\n"
        "  _hel.insertAdjacentElement('afterend',ed);\n"
        "  ed.focus();\n"
        "  var r=document.createRange(),s=window.getSelection();\n"
        "  r.selectNodeContents(ed);r.collapse(false);\n"
        "  s.removeAllRanges();s.addRange(r);\n"
        "}\n"

        # ── save / cancel ────────────────────────────────────────────────
        "function _doSave(){\n"
        "  var ed=document.getElementById('_id_ed');\n"
        "  if(!ed||!_sid)return;\n"
        "  var btn=document.getElementById('_id_sav');\n"
        "  btn.textContent='Saving…';btn.disabled=true;\n"
        "  window.parent.postMessage({type:'intellidraft_save_section',"
        "section_id:_sid,content:ed.innerText},'*');\n"
        "}\n"
        "function _doCancel(){\n"
        "  _orig.forEach(function(o){o.el.style.display=o.d;});\n"
        "  _orig=[];\n"
        "  var ed=document.getElementById('_id_ed');if(ed)ed.remove();\n"
        "  _hideBar();_sid=null;_hel=null;\n"
        "}\n"

        # ── listen for messages from parent ──────────────────────────────
        "window.addEventListener('message',function(e){\n"
        "  if(!e.data)return;\n"
        "  if(e.data.type==='intellidraft_section_content')_showEditor(e.data.section_id,e.data.content);\n"
        "  if(e.data.type==='intellidraft_save_complete')_doCancel();\n"
        "  if(e.data.type==='intellidraft_save_error'){\n"
        "    var btn=document.getElementById('_id_sav');\n"
        "    if(btn){btn.textContent='&#128190; Save';btn.disabled=false;}\n"
        "    alert('Save failed: '+(e.data.error||'unknown error'));\n"
        "  }\n"
        "});\n"

        # ── attach hover + click to headings ─────────────────────────────
        "function _attach(){\n"
        "  document.querySelectorAll('h1,h2,h3,h4').forEach(function(el){\n"
        "    var title=el.textContent.trim();\n"
        "    var secId=SM[title];\n"
        "    if(!secId)return;\n"
        "    el.style.cursor='pointer';\n"
        "    el.setAttribute('title','Click to edit this section');\n"
        "    el.style.transition='background .15s,border-left .15s';\n"
        "    el.style.paddingLeft='6px';\n"
        "    el.addEventListener('mouseenter',function(){\n"
        "      if(_sid!==secId){this.style.background='rgba(92,107,192,.1)';"
        "this.style.borderLeft='3px solid #5c6bc0';this.style.borderRadius='3px';}\n"
        "    });\n"
        "    el.addEventListener('mouseleave',function(){\n"
        "      if(_sid!==secId){this.style.background='';this.style.borderLeft='';}\n"
        "    });\n"
        "    el.addEventListener('click',function(){\n"
        "      if(_sid===secId)return;\n"   # already editing this section
        "      if(_sid)_doCancel();\n"       # cancel any previous edit
        "      _sid=secId;_hel=el;\n"
        "      _showBar(title);\n"
        "      window.parent.postMessage({type:'intellidraft_section_click',"
        "section_id:secId,title:title},'*');\n"
        "    });\n"
        "  });\n"
        "}\n"
        "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_attach);\n"
        "else _attach();\n"
        "})();\n"
        "</script>\n"
    )

    if "</body>" in html:
        return html.replace("</body>", script + "</body>", 1)
    return html + script
