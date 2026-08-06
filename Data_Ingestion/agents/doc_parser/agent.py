"""
DocParserAgent — Agent 1
========================
Google ADK LlmAgent that parses uploaded documents, runs Vision AI on images,
and collects project context for downstream generation.

Supported formats:  PDF · DOCX · PPTX · XLSX

MODEL (controlled by agents/_model.py + Data_Ingestion/.env):
  Gemini 2.5 Flash (Google Vertex AI / Gemini API)

How to run:
  cd "…\\Intellidraft"
  adk web      ← runs with the full multi-agent orchestrator
  DO NOT run from inside Data_Ingestion\\ — doubled path breaks ADK discovery.
"""

from __future__ import annotations
import hashlib
import inspect
import logging
import os
import sys
from datetime import date
from pathlib import Path

# ── Bootstrap: add Data_Ingestion/ to sys.path ───────────────────────────────
# agents/doc_parser/agent.py is 3 levels deep inside Data_Ingestion/
_BASE = Path(__file__).parent.parent.parent.resolve()   # → …/Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from google.adk.agents  import LlmAgent            # noqa: E402
from google.genai       import types as genai_types # noqa: E402

from .._model import get_agent_model                # noqa: E402
from .tools import (                                # noqa: E402
    list_artifacts_tool,
    parse_document_tool,
    get_document_meta_tool,
    list_elements_tool,
    submit_user_inputs_tool,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# MIME type → file extension map
# ─────────────────────────────────────────────────────────────────────────────

_MIME_TO_EXT: dict[str, str] = {
    "application/pdf":          "pdf",
    "application/msword":       "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":   "docx",
    "application/vnd.ms-powerpoint":                                             "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-excel":                                                  "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":         "xlsx",
}


# ─────────────────────────────────────────────────────────────────────────────
# Before-model callback — intercept file uploads, save to artifact store,
# then strip raw bytes from messages so the LLM never sees binary data.
# ─────────────────────────────────────────────────────────────────────────────

async def _strip_file_content_callback(callback_context, llm_request):
    """
    Intercept every LLM call and handle inline_data / file_data Parts.

    WHY THIS EXISTS
    ---------------
    ADK 2.0 passes files uploaded via the web-UI paperclip as raw inline_data
    bytes inside the user message — it does NOT automatically save them to the
    ADK artifact service.  Two problems follow:

      1. litellm may try to upload inline_data bytes as a file without a MIME
         type, causing provider-side errors (400 Invalid file data).

      2. tool_context.load_artifact() / list_artifacts() return nothing
         because the artifact service was never populated.

    WHAT WE DO
    ----------
    For every inline_data Part in every user message:

      a. Hash the first 4 KB for a stable 12-char key.
         On the FIRST occurrence call callback_context.save_artifact() to
         write the Part into the ADK artifact service under the name:
           upload_<hash>.<ext>
         Record hash → filename in session state so subsequent LLM turns
         (which replay the full conversation history) skip the re-save.

      b. Replace the Part with a plain-text placeholder that tells the LLM
         the exact filename to pass to parse_document_tool().

    After this callback:
      • The LLM sees clean text-only content (no bytes).
      • list_artifacts_tool() returns the saved filename(s).
      • parse_document_tool(filename) can call load_artifact(filename).

    Returns None → ADK continues with the (now-modified) request.
    """
    if not hasattr(llm_request, "contents") or not llm_request.contents:
        return None

    try:
        artifact_map: dict[str, str] = dict(
            callback_context.state.get("_uploaded_files", {}) or {}
        )
    except Exception:
        artifact_map = {}

    map_updated = False

    for i, content in enumerate(llm_request.contents):
        if getattr(content, "role", None) != "user":
            continue
        parts = getattr(content, "parts", None)
        if not parts:
            continue

        new_parts: list = []
        changed   = False

        for part in parts:

            # ── inline_data  (file uploaded via the web-UI paperclip) ─────────
            if getattr(part, "inline_data", None) is not None:
                mime = getattr(part.inline_data, "mime_type", None) or "application/octet-stream"
                data = getattr(part.inline_data, "data", b"") or b""

                file_key = hashlib.sha256(data[:4096], usedforsecurity=False).hexdigest()[:12]

                if file_key in artifact_map:
                    filename = artifact_map[file_key]
                    logger.info("[Callback] File already in artifact store as '%s'", filename)
                else:
                    ext      = _MIME_TO_EXT.get(mime) or (mime.split("/")[-1][:8]) or "bin"
                    filename = f"upload_{file_key}.{ext}"

                    save_fn = getattr(callback_context, "save_artifact", None)
                    if save_fn and callable(save_fn):
                        try:
                            result = save_fn(filename, part)
                            if inspect.isawaitable(result):
                                await result
                            logger.info(
                                "[Callback] Saved inline_data as artifact '%s' (mime=%s)",
                                filename, mime,
                            )
                        except Exception as exc:
                            logger.warning(
                                "[Callback] save_artifact('%s') failed: %s", filename, exc
                            )
                    else:
                        logger.warning(
                            "[Callback] callback_context has no save_artifact — "
                            "tools will not be able to load '%s'", filename,
                        )

                    artifact_map[file_key] = filename
                    map_updated = True

                new_parts.append(genai_types.Part(
                    text=(
                        f"[Uploaded file saved as '{filename}' (MIME: {mime}). "
                        f"Call list_artifacts_tool() to confirm, then "
                        f"parse_document_tool('{filename}') to extract its contents.]"
                    )
                ))
                changed = True
                logger.info("[Callback] Stripped inline_data (mime=%s) from user message", mime)

            # ── file_data  (Google Files API reference — rare with ADK web) ───
            elif getattr(part, "file_data", None) is not None:
                uri  = getattr(part.file_data, "file_uri", "") or ""
                name = uri.rsplit("/", 1)[-1] if uri else "uploaded_file"
                new_parts.append(genai_types.Part(
                    text=(
                        f"[File '{name}' attached via Files API. "
                        f"Call parse_document_tool('{name}') to process it.]"
                    )
                ))
                changed = True
                logger.info("[Callback] Stripped file_data (uri=%s) from user message", uri)

            else:
                new_parts.append(part)

        if changed:
            try:
                content.parts = new_parts
            except Exception:
                try:
                    new_content = genai_types.Content(role=content.role, parts=new_parts)
                    llm_request.contents[i] = new_content
                except Exception as exc:
                    logger.warning("[Callback] Could not replace content parts: %s", exc)

    if map_updated:
        try:
            callback_context.state["_uploaded_files"] = artifact_map
        except Exception as exc:
            logger.warning("[Callback] Could not persist artifact map to state: %s", exc)

    return None


# Model is resolved once at startup via the shared utility.
# Model resolved at startup via the shared utility — see agents/_model.py.


# ─────────────────────────────────────────────────────────────────────────────
# Agent instruction
# ─────────────────────────────────────────────────────────────────────────────

_INSTRUCTION = f"""
You are the Intellidraft Document Processing Agent. Today: {date.today().isoformat()}.

Your role is to help users:
1. Upload source documents (PDF, DOCX, PPTX, XLSX) via the paperclip icon
2. Extract all content — text, images (with AI-generated descriptions for diagrams and
   workflows), and tables
3. Collect project context from the user
4. Prepare everything for document generation (BRD, RFP, SOW, Proposal, etc.)

IMPORTANT NOTES ABOUT FILE UPLOADS:
- The ADK web UI artifact panel may not visually refresh after upload — this is a UI
  display quirk, NOT an error. The file IS stored.
- Always call list_artifacts_tool at the start of a conversation to see what files
  are actually available.
- To upload MULTIPLE documents: upload one file, ask me to parse it, then upload the
  next file and ask me to parse it. Each file is processed independently.

WORKFLOW — follow this exact sequence:

STEP 0 — CHECK WHAT'S UPLOADED
  At the start of every conversation (or when the user says they uploaded something),
  call list_artifacts_tool first.
  Tell the user what files were found and ask which one to parse (or parse the obvious
  one if there is only one).

STEP 1 — PARSE
  Call parse_document_tool with the exact filename shown by list_artifacts_tool.
  Report what was found: text blocks, images (note diagrams/workflows/charts), tables.
  If there are multiple uploaded files, ask the user which to process or process each
  one in sequence.

STEP 2 — SHOW CONTENTS (if the user asks)
  Use list_elements_tool to preview specific element types.
  For images, always show the ai_description and image_type — this tells the user what
  diagrams were understood.

STEP 3 — COLLECT PROJECT CONTEXT
  Ask the user for:
    • Project name
    • Output document type (BRD, RFP, SOW, Proposal, Technical Specification, Scope Document)
    • Output format (Word / PDF / Markdown)
    • Stakeholders
    • Project description
    • Business problem this project solves
    • Any special instructions for the AI

STEP 4 — SAVE INPUTS
  Call submit_user_inputs_tool with everything collected.

STEP 5 — CONFIRM & NEXT STEPS
  Tell the user:
    • Their document_id (needed for the generation API)
    • What content was extracted (highlight any workflows/architecture diagrams found)
    • That they can now call POST /api/generate/start with the document_id to begin generation
"""


# ─────────────────────────────────────────────────────────────────────────────
# DocParserAgent definition
# ─────────────────────────────────────────────────────────────────────────────

doc_parser_agent = LlmAgent(
    name        = "doc_processor",
    model       = get_agent_model("DocParserAgent"),
    description = (
        "Parses uploaded documents (PDF, DOCX, PPTX, XLSX), extracts text/images/tables, "
        "runs Vision AI on diagrams and architecture charts, "
        "and collects user project context for AI document generation."
    ),
    instruction = _INSTRUCTION,
    tools       = [
        list_artifacts_tool,
        parse_document_tool,
        get_document_meta_tool,
        list_elements_tool,
        submit_user_inputs_tool,
    ],
    # Strip raw file bytes before the LLM call — prevents ADK from sending
    # inline_data bytes directly to the LLM (causes provider-side errors).
    # Our parse_document_tool handles the actual file reading locally.
    before_model_callback = _strip_file_content_callback,
)
