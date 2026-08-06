"""
Knowledge Base — RAG grounding via Databricks Vector Search
============================================================
Sibling of `generation/ontology.py`. Where ontology.py injects a static,
business-supplied glossary/systems block, THIS module injects a dynamic
`{knowledge_block}` retrieved from historical documents indexed in Databricks
Vector Search.

It is wired into the SAME four prompt sites as ontology.py, one assembly
function each:

  generator.py        → for_generation()   prior similar-document sections
  api/extractor.py    → for_extraction()   prior docs of the same project/BU
  derive_fields.py    → for_derivation()   systems/benchmarks used before
  review_service.py   → for_review()       approved exemplars for this doc type

Design rules (identical contract to ontology.py — do NOT break these):
  - EVERY function degrades to "" gracefully. Missing config, disabled flag,
    empty index, network error, or a bad response must NEVER break a prompt
    build. Generation must work exactly as today when the KB is off.
  - Token discipline: cap the injected block (~1,500 tokens). Retrieval is
    capped at KB_RETRIEVAL_TOP_K chunks post-rerank.
  - Every injected chunk is CITED (filename · project · date) so output is
    auditable and feeds the validation_agent provenance mechanism.
  - Access control happens IN the query (metadata predicate), never at display.

Enable with env flags (both default OFF — app behaves as today until set):
  KB_RETRIEVAL_ENABLED=true
  KB_ENDPOINT_NAME=intellidraft-vector-endpoint
  KB_INDEX_NAME=adani_ael_ailabs_catalog_dev.knowledge_base_v1.chunks_index
  KB_RETRIEVAL_TOP_K=5
  KB_CONFIDENTIALITY_DEFAULT=internal
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config (read once; all optional — absence just disables the KB)
# ─────────────────────────────────────────────────────────────────────────────

def _enabled() -> bool:
    return os.getenv("KB_RETRIEVAL_ENABLED", "false").lower() == "true"

_ENDPOINT   = os.getenv("KB_ENDPOINT_NAME", "intellidraft-vector-endpoint")
_INDEX      = os.getenv("KB_INDEX_NAME", "")
_TOP_K      = int(os.getenv("KB_RETRIEVAL_TOP_K", "5"))
_CONF_LEVEL = os.getenv("KB_CONFIDENTIALITY_DEFAULT", "internal")

# Token budget for the whole injected block. ~4 chars/token ⇒ ~1,500 tokens.
_BLOCK_CHAR_CAP = 6_000
# Per-chunk char cap so one long chunk can't eat the whole budget.
_CHUNK_CHAR_CAP = 1_200

# Confidentiality ordering — a query at level N may retrieve docs at level ≤ N.
_CONF_ORDER = {"public": 0, "internal": 1, "confidential": 2, "secret": 3}

# The document-chain relationships (mirrors ontology.workflow: BRD→NDPR→NFA→NIT→RFP).
# When generating doc X, precedent from these related types is most useful.
_RELATED_DOC_TYPES = {
    "BRD":  ["BRD", "NDPR"],
    "NDPR": ["NDPR", "BRD", "NFA"],
    "NFA":  ["NFA", "NDPR", "NIT"],
    "NIT":  ["NIT", "NFA", "RFP"],
    "RFP":  ["RFP", "NIT", "SOW"],
    "SOW":  ["SOW", "RFP", "Proposal"],
}


def _norm_doc_key(document_type: str) -> str | None:
    up = (document_type or "").upper()
    for key in ("NDPR", "BRD", "NFA", "NIT", "RFP", "SOW", "BOQ", "ARB"):
        if re.search(rf"\b{key}\b", up):
            return key
    return None


def _related_doc_types(document_type: str) -> list[str]:
    key = _norm_doc_key(document_type)
    return _RELATED_DOC_TYPES.get(key, [key] if key else [])


# ─────────────────────────────────────────────────────────────────────────────
# Vector Search client (lazy singleton; any import/auth failure ⇒ KB disabled)
# ─────────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _index():
    """Return the Databricks Vector Search index handle, or None if unavailable.

    Cached so we build the client once. Any failure (SDK missing, no creds,
    endpoint down) logs a warning and returns None → all callers degrade to "".
    """
    if not _enabled() or not _INDEX:
        return None
    try:
        from databricks.vector_search.client import VectorSearchClient
        client = VectorSearchClient(disable_notice=True)
        idx = client.get_index(endpoint_name=_ENDPOINT, index_name=_INDEX)
        logger.info("[knowledge] Vector Search index ready: %s", _INDEX)
        return idx
    except Exception as e:
        logger.warning("[knowledge] Vector Search unavailable — KB disabled: %s", e)
        return None


def _embed(text: str) -> list[float] | None:
    """Embed a query string. Uses the embedding model configured for the KB.

    Returns None on any failure so the caller degrades to "". We keep this
    separate from llm_provider (which is for chat/vision) because embeddings
    are a different model family.
    """
    if not text:
        return None
    try:
        from llm_provider import embed_text  # thin wrapper added alongside call_with_fallback
        return embed_text(text)
    except Exception as e:
        logger.warning("[knowledge] Embedding failed — skipping retrieval: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval core
# ─────────────────────────────────────────────────────────────────────────────

def _build_filters(
    business_unit: str | None,
    document_types: list[str] | None,
    project_id: str | None,
    confidentiality: str | None,
) -> dict:
    """Metadata predicates applied INSIDE the vector query (Databricks filter dict).

    Access control lives here: a user never receives grounding from a document
    above their clearance because the query itself excludes it.
    """
    f: dict = {}
    if business_unit:
        f["business_unit"] = business_unit
    if document_types:
        f["document_type"] = document_types          # Databricks treats a list as IN(...)
    if project_id:
        # Not an equality filter — we WANT cross-project precedent, so we do not
        # pin project_id here. Kept as a param for callers that want same-project only.
        pass
    # Confidentiality: retrieve only docs at or below the caller's level.
    max_level = _CONF_ORDER.get((confidentiality or _CONF_LEVEL).lower(), 1)
    allowed = [name for name, lvl in _CONF_ORDER.items() if lvl <= max_level]
    f["confidentiality_level"] = allowed
    return f


def hybrid_search(
    query: str,
    business_unit: str | None = None,
    document_types: list[str] | None = None,
    project_id: str | None = None,
    confidentiality: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Retrieve the most relevant chunks. Returns [] on any problem.

    Databricks Vector Search with query_type="HYBRID" performs vector + keyword
    (BM25) fusion server-side, so we get hybrid ranking without a separate BM25
    store. Re-ranking (cross-encoder) can be layered later (Phase 12).
    """
    idx = _index()
    if idx is None:
        return []
    vector = _embed(query)
    if vector is None:
        return []
    try:
        res = idx.similarity_search(
            query_vector=vector,
            query_text=query,                      # enables HYBRID fusion
            columns=[
                "chunk_id", "document_id", "chunk_text", "section_title",
                "document_type", "project_name", "source_document_path",
                "created_date", "business_unit",
            ],
            filters=_build_filters(business_unit, document_types, project_id, confidentiality),
            num_results=limit or _TOP_K,
            query_type="HYBRID",
        )
    except Exception as e:
        logger.warning("[knowledge] similarity_search failed — degrading to no-KB: %s", e)
        return []

    # Response shape: {"result": {"data_array": [[col1, col2, ...], ...]}, "manifest": {...}}
    try:
        cols = [c["name"] for c in res["manifest"]["columns"]]
        rows = res["result"]["data_array"]
    except Exception:
        return []
    return [dict(zip(cols, row)) for row in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Block rendering (the injectable, CITED text)
# ─────────────────────────────────────────────────────────────────────────────

def _cite(chunk: dict) -> str:
    fname = chunk.get("source_document_path", "") or ""
    fname = fname.rsplit("/", 1)[-1] or "unknown"
    proj = chunk.get("project_name") or "—"
    date = (chunk.get("created_date") or "")
    date = str(date)[:10]
    return f"{fname} · {proj} · {date}".strip(" ·")


def _render_block(chunks: list[dict], heading: str) -> str:
    if not chunks:
        return ""
    lines = [f"## {heading}",
             "Draw on the following precedent from prior Adani documents. Reuse real "
             "figures, system names, and clause language where relevant — and KEEP the "
             "[source: …] citation for anything you lift from here."]
    used = 0
    for c in chunks:
        text = (c.get("chunk_text") or "").strip()
        if not text:
            continue
        if len(text) > _CHUNK_CHAR_CAP:
            text = text[:_CHUNK_CHAR_CAP] + " …"
        sect = c.get("section_title") or ""
        entry = f"\n### {sect}  [source: {_cite(c)}]\n{text}\n" if sect \
                else f"\n[source: {_cite(c)}]\n{text}\n"
        if used + len(entry) > _BLOCK_CHAR_CAP:
            break
        lines.append(entry)
        used += len(entry)
    return "\n".join(lines) if len(lines) > 2 else ""


# ─────────────────────────────────────────────────────────────────────────────
# Per-call assemblies (the four prompt sites import exactly one of these)
# ─────────────────────────────────────────────────────────────────────────────

def for_generation(
    document_type: str,
    section_title: str,
    user_inputs: dict,
    scan_text: str = "",
) -> str:
    """Section generation grounding: precedent chunks from prior similar documents,
    matched to THIS section. Query is section-aware — generating 'Risk Assessment'
    of an NFA retrieves risk sections from prior NFAs."""
    if not _enabled():
        return ""
    project_name = user_inputs.get("project_name", "")
    problem = (user_inputs.get("business_problem") or "")[:400]
    query = f"{document_type} — {section_title}: {project_name} {problem}".strip()
    try:
        chunks = hybrid_search(
            query=query,
            business_unit=user_inputs.get("business_unit"),
            document_types=_related_doc_types(document_type),
            confidentiality=user_inputs.get("confidentiality_level"),
        )
        return _render_block(chunks, f"PRIOR PRECEDENT — {section_title}")
    except Exception:
        logger.exception("[knowledge] for_generation failed — continuing without KB")
        return ""


def for_extraction(scan_text: str, business_unit: str | None = None) -> str:
    """Field extraction grounding: prior docs from the same project/BU so entity
    and field recognition matches what we called things before."""
    if not _enabled():
        return ""
    try:
        chunks = hybrid_search(query=scan_text[:600], business_unit=business_unit)
        return _render_block(chunks, "PRIOR RELATED DOCUMENTS (for consistent naming)")
    except Exception:
        logger.exception("[knowledge] for_extraction failed — continuing without KB")
        return ""


def for_derivation(scan_text: str, business_unit: str | None = None) -> str:
    """Derived-field grounding: chunks describing systems, benchmarks, roadmaps
    used before, so systems_involved / benchmarks are real, not invented."""
    if not _enabled():
        return ""
    try:
        chunks = hybrid_search(query=scan_text[:600], business_unit=business_unit)
        return _render_block(chunks, "SYSTEMS & BENCHMARKS USED IN PRIOR WORK")
    except Exception:
        logger.exception("[knowledge] for_derivation failed — continuing without KB")
        return ""


def for_review(document_type: str, section_title: str = "") -> str:
    """AI-persona review grounding: approved exemplars for this doc type, so the
    reviewer checks the draft against real approved precedent."""
    if not _enabled():
        return ""
    try:
        query = f"approved {document_type} {section_title}".strip()
        chunks = hybrid_search(query=query, document_types=_related_doc_types(document_type))
        return _render_block(chunks, f"APPROVED {document_type} PRECEDENT (review against this)")
    except Exception:
        logger.exception("[knowledge] for_review failed — continuing without KB")
        return ""
