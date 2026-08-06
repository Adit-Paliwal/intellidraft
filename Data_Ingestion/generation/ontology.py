"""
Adani Business Ontology — selective prompt grounding
=====================================================
Loads the business-provided ontology pack (Data_Ingestion/ontology/*.json) and
builds SMALL, per-call-relevant context blocks for every LLM prompt site:

  generator.py        → for_generation()   company + doc-type/workflow + glossary
  api/extractor.py    → for_extraction()   company + glossary
  derive_fields.py    → for_derivation()   company + tech landscape + glossary
  review_service.py   → for_review()       doc-type expectations for AI reviewers

Design rules (token discipline):
  - NEVER dump whole files. terminology.json is 549 terms (~7K tokens) and
    technical_landscape.json is ~25K tokens — we inject only entries that
    MATCH the text of the current call (word-boundary scan), hard-capped.
  - Every block degrades to "" gracefully: missing files, unknown doc types,
    and empty matches must never break a prompt build.
  - Target additions per call: ≤ ~1,200 tokens.

Ontology files (owned by the business — drop-in replaceable):
  adani_description.json      Group / AESL / AEML entity descriptions
  document_descriptions.json  BRD/NIT/RFP/NDPR/NFA purpose + inputs + owner
  workflow.json               The BRD→NDPR→NFA→NIT→RFP document chain
  terminology.json            549-term acronym/domain glossary
  key_regulations.json        Regulatory framework names (MERC, BEE, DFPO, LIS)
  technical_landscape.json    ~130 tools across AESL-Grid / AESL-Retail estate
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR = Path(__file__).parent.parent / "ontology"

# Doc types covered by the ontology (acronym → matcher on the doc_type string)
_DOC_KEYS = ("BRD", "NDPR", "NFA", "NIT", "RFP")


# ─────────────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────────────

def _read(name: str) -> dict:
    try:
        return json.loads((_DIR / name).read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[ontology] Could not load %s: %s", name, e)
        return {}


@lru_cache(maxsize=1)
def _load() -> dict:
    data = {
        "entities":     _read("adani_description.json").get("entities", []),
        "documents":    _read("document_descriptions.json").get("documents", {}),
        "workflow":     {s.get("doc_type"): s for s in _read("workflow.json").get("steps", [])},
        "terms":        _read("terminology.json").get("terms", {}),
        "regulations":  [r.get("name") for r in _read("key_regulations.json").get("regulations", []) if r.get("name")],
        "technologies": _read("technical_landscape.json").get("technologies", []),
    }
    logger.info(
        "[ontology] Loaded: %d entities, %d doc types, %d workflow steps, %d terms, %d technologies",
        len(data["entities"]), len(data["documents"]), len(data["workflow"]),
        len(data["terms"]), len(data["technologies"]),
    )
    return data


def _norm_doc_type(document_type: str) -> str | None:
    """'Business Requirements Document (BRD)' / 'brd' / 'NFA' → ontology key."""
    up = (document_type or "").upper()[:200]
    for key in _DOC_KEYS:
        if re.search(rf"\b{key}\b", up):
            return key
    # Full-form fallback (e.g. template passes the long name without acronym)
    full_forms = {v.get("full_form", "").upper(): k for k, v in _load()["documents"].items()}
    for full, key in full_forms.items():
        if full and full in up:
            return key
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Block builders
# ─────────────────────────────────────────────────────────────────────────────

def company_context(scan_text: str = "") -> str:
    """Adani Group + the entity (AESL / AEML) matched from the call's text."""
    entities = _load()["entities"]
    if not entities:
        return ""
    text_up = (scan_text or "").upper()

    def _matched(e: dict) -> bool:
        name = e.get("entity", "")
        if "AEML" in name:
            return "AEML" in text_up or "MUMBAI" in text_up
        if "AESL" in name:
            return "AESL" in text_up or "ENERGY SOLUTION" in text_up or "TRANSMISSION" in text_up
        return False

    picked = [e for e in entities if _matched(e)]
    if not picked:                       # default: group + AESL (primary BU for this platform)
        picked = [e for e in entities if "AESL" in e.get("entity", "")]
    group = next((e for e in entities if e.get("entity") == "Adani Group"), None)

    lines = ["## ORGANISATION CONTEXT (authoritative — use this framing)"]
    if group:
        lines.append(f"- **Adani Group**: {group.get('area_of_work', '')}")
    for e in picked[:2]:
        lines.append(f"- **{e.get('entity')}**: {e.get('what_they_are', '')} {e.get('area_of_work', '')}")
    regs = _load()["regulations"]
    if regs:
        terms = _load()["terms"]
        expanded = [f"{r} ({terms[r]})" if r in terms else r for r in regs]
        lines.append(f"- Key regulatory frameworks to respect where relevant: {', '.join(expanded)}.")
    return "\n".join(lines)


def document_context(document_type: str) -> str:
    """What this document type IS at Adani: purpose, required inputs, place in
    the BRD→NDPR→NFA→NIT→RFP chain, owner/reviewers, and risk-if-weak."""
    key = _norm_doc_type(document_type)
    if not key:
        return ""
    d = _load()["documents"].get(key, {})
    w = _load()["workflow"].get(key, {})
    if not d and not w:
        return ""
    lines = [f"## DOCUMENT-TYPE GUIDANCE — {key} ({d.get('full_form', key)}) at Adani"]
    if d.get("purpose"):
        lines.append(f"- Purpose: {d['purpose']}")
    if w.get("when_starts"):
        lines.append(f"- When it is written: {w['when_starts']}")
    if w.get("predecessor"):
        lines.append(f"- Predecessor in the document chain: {w['predecessor']} "
                     f"(chain: BRD → NDPR → NFA → NIT → RFP)")
    if d.get("input_required") or w.get("inputs_needed"):
        lines.append(f"- Inputs it must cover: {d.get('input_required', '')} {w.get('inputs_needed', '')}".strip())
    if w.get("output"):
        lines.append(f"- Expected output: {w['output']}")
    if d.get("owner") or w.get("reviewers"):
        lines.append(f"- Owner: {d.get('owner') or w.get('owner', '')} · Reviewed by: {w.get('reviewers', 'management')}")
    if w.get("explanation"):
        lines.append(f"- In simple words: {w['explanation']}")
    if w.get("risk_if_not_done"):
        lines.append(f"- Risk if this document is weak: {w['risk_if_not_done']}")
    return "\n".join(lines)


_SCAN_CAP = 300_000   # chars — matching beyond this adds nothing but CPU

def glossary_block(scan_text: str, limit: int = 40) -> str:
    """ONLY the glossary terms that actually appear in the call's text.
    Longest keys first (most specific), case-sensitive word-boundary match.

    Performance: a raw-substring precheck (C-speed) rejects the ~95% of the
    549 keys that can't match before any regex runs — keeps a 60K-char scan
    at ~10ms and even a 1MB scan well under a second."""
    terms = _load()["terms"]
    if not terms or not scan_text:
        return ""
    scan = scan_text[:_SCAN_CAP]
    matched: list[tuple[str, str]] = []
    for key in sorted(terms.keys(), key=len, reverse=True):
        if len(key) < 2 or len(matched) >= limit:
            continue
        if key not in scan:          # fast reject before the boundary regex
            continue
        try:
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])", scan):
                matched.append((key, terms[key]))
        except re.error:
            continue
    if not matched:
        return ""
    lines = ["## ADANI TERMINOLOGY (expand acronyms correctly on first use)"]
    lines += [f"- {k}: {v}" for k, v in sorted(matched)]
    return "\n".join(lines)


def tech_landscape_block(scan_text: str = "", limit: int = 12, include_overview: bool = False) -> str:
    """Tools from the AESL estate that the call's text mentions (detailed lines),
    optionally preceded by a compact capability overview grouped by domain."""
    techs = _load()["technologies"]
    if not techs:
        return ""
    lines: list[str] = []

    if include_overview:
        by_domain: dict[str, list[str]] = {}
        for t in techs:
            tool = t.get("tool", "")
            if tool:
                by_domain.setdefault(t.get("domain", "Other"), []).append(tool)
        lines.append("## AESL TECHNICAL ESTATE (existing systems — prefer integrating with these over inventing new ones)")
        for domain, tools in list(by_domain.items())[:8]:
            uniq = list(dict.fromkeys(tools))
            shown = ", ".join(uniq[:14]) + (" …" if len(uniq) > 14 else "")
            lines.append(f"- {domain}: {shown}")

    if scan_text:
        text_up = scan_text.upper()
        matched = []
        seen = set()
        for t in techs:
            tool = t.get("tool", "")
            if len(tool) < 3 or tool in seen:
                continue
            probe = re.sub(r"\s*\(.*\)$", "", tool).strip()   # "DRSO / a-DRSO" etc. still fine
            if len(probe) >= 3 and probe.upper() in text_up:
                seen.add(tool)
                matched.append(t)
            if len(matched) >= limit:
                break
        if matched:
            lines.append("## SYSTEMS MENTIONED — authoritative descriptions from the AESL landscape")
            for t in matched:
                lines.append(f"- {t['tool']} [{t.get('capability', '')}, {t.get('landscape', '')}]: "
                             f"{t.get('purpose', t.get('description', ''))}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Per-call assemblies (the four prompt sites import exactly one of these)
# ─────────────────────────────────────────────────────────────────────────────

def for_generation(document_type: str, scan_text: str) -> str:
    """Section generation: org framing + doc-type semantics + matched glossary + matched systems."""
    parts = [
        company_context(scan_text),
        document_context(document_type),
        glossary_block(scan_text, limit=35),
        tech_landscape_block(scan_text, limit=10),
    ]
    return "\n\n".join(p for p in parts if p)


def for_extraction(scan_text: str) -> str:
    """Field extraction from uploaded docs: org framing + matched glossary
    (knowing AEML-D is a distribution division, ABT is a tariff, etc. directly
    improves entity and field recognition)."""
    parts = [company_context(scan_text), glossary_block(scan_text, limit=40)]
    return "\n\n".join(p for p in parts if p)


def for_derivation(scan_text: str) -> str:
    """Derived-field generation: org framing + FULL estate overview + matched
    systems + glossary. systems_involved / data_sources / benchmarks should be
    grounded in what AESL actually runs, not invented."""
    parts = [
        company_context(scan_text),
        tech_landscape_block(scan_text, limit=12, include_overview=True),
        glossary_block(scan_text, limit=30),
    ]
    return "\n\n".join(p for p in parts if p)


def for_review(document_type: str) -> str:
    """AI persona review: what this doc type must contain at Adani, so the
    reviewer judges against the org's actual expectations."""
    block = document_context(document_type)
    if not block:
        return ""
    return (block +
            "\nWhen reviewing, explicitly check the document against the 'Inputs it must cover' "
            "list above and flag anything missing or weak — that is what Adani reviewers check first.")
