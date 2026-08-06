"""
doc_meta.py — per-document-type cover / control metadata
========================================================
ONE loader for the cover metadata compiled from the expert mapping
(template_config sheet → mapping_data/template_config.json). Used by BOTH the
Word cover (brd_formatter) and the HTML preview cover (preview_service), so the
two can never drift. Returns {} for an unknown type → callers fall back to their
built-in defaults.

Fields per doc type: title_prefix, full_name, template_id, document_status,
classification, confidentiality_text, disclaimer_text.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_CFG = Path(__file__).parent / "mapping_data" / "template_config.json"
_KEYS = ("BRD", "NDPR", "NFA", "NIT", "RFP")


@lru_cache(maxsize=1)
def _load() -> dict:
    try:
        return json.loads(_CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _doc_key(doc_type: str) -> "str | None":
    up = (doc_type or "").upper()[:200]
    for k in _KEYS:
        if re.search(rf"\b{k}\b", up):
            return k
    # full-name fallback (e.g. "Notice Inviting Tender" → NIT)
    for k, meta in _load().items():
        fn = (meta.get("full_name") or "").upper()
        if fn and fn in up:
            return k
    return None


def get_doc_meta(doc_type: str) -> dict:
    """Cover/control fields for a doc type. Empty dict if not in the mapping."""
    return dict(_load().get(_doc_key(doc_type) or "", {}))
