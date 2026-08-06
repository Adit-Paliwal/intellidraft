"""
Section Generator
==================
Core LLM call for generating a single document section.

Uses llm_provider.call_with_fallback() — Gemini 2.5 Flash (Vertex AI).
Same provider as extractor and derive_fields.
Prompt strategy:
  - System prompt: role, document type, full document context, user inputs
  - User prompt: section-specific instructions + any edit comment
  - Previous sections (already generated) are appended to system prompt
    for coherence — the LLM knows what it already wrote

Supports:
  - First-time generation of a section
  - Re-generation with an edit comment (revision mode)
"""

from __future__ import annotations
import logging
import sys
from pathlib import Path
from typing import Optional

# Ensure Data_Ingestion/ is on sys.path so llm_provider can be imported
_BASE = Path(__file__).parent.parent.resolve()
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = """You are a professional business analyst and technical writer.
You are generating the "{document_type}" document for the project described below.

## PROJECT INFORMATION
- **Project Name:** {project_name}
- **Document Type:** {document_type}
- **Target Audience:** {target_audience}
- **Stakeholders:** {stakeholders}
- **Language:** {language}

{ontology_block}

## BUSINESS CONTEXT
{business_problem}

## PROJECT DESCRIPTION
{project_description}

{additional_instructions_block}

## SOURCE DOCUMENT CONTENT
The following content was extracted from the uploaded source document(s).
Use this as your primary factual reference — extract specific details, numbers,
processes, system names, and data from here rather than inventing them.

{llm_context}

{previous_sections_block}

---
FORMATTING RULES (follow exactly):
- Write in Markdown. Use ## and ### only for sub-headings *within* this section.
- Do NOT repeat the section title as a top-level heading — start directly with the content.
- TABLES: whenever you present tabular data you MUST use a Markdown pipe table, and the
  FIRST row MUST be a header row naming every column, immediately followed by a separator
  row. Example:
      | Sr. | Requirement | Priority |
      | --- | --- | --- |
      | 1 | ... | Must Have |
  Never output a table without its header row, and never leave a column unnamed.
- Lists: use "- " bullets or "1." numbered lists as appropriate.
- Use fenced code blocks (```) only for schemas, configs, or technical specifications.
- Target length: approximately {target_words} words.
- Language: {language}

CONTENT QUALITY RULES:
- Be specific and concrete. Pull real names, numbers, systems, dates, and processes from the
  SOURCE DOCUMENT CONTENT above — do NOT invent generic filler.
- Use Adani terminology exactly as defined in the ADANI TERMINOLOGY glossary (when present):
  expand each acronym on first use, then use the acronym. Never redefine an acronym differently.
- When naming systems, prefer the AESL technical estate entries provided above over
  hypothetical or generic system names.
- Respect the DOCUMENT-TYPE GUIDANCE (when present): cover its required inputs, honour its
  position in the BRD → NDPR → NFA → NIT → RFP chain, and write for its stated reviewers.
- Never emit placeholders such as "TBD", "Lorem ipsum", "[insert ...]", "XYZ", or "example.com".
  If a detail is genuinely unavailable, state a reasonable, professional assumption instead.
- Maintain a formal, precise, enterprise-grade tone. No marketing fluff, no aspirational vagueness.
- Output ONLY the section content — no preamble ("Here is the section..."), no meta commentary.
"""

_GENERATION_PROMPT = """Generate the **{section_title}** section.

SECTION INSTRUCTIONS:
{section_instructions}

{mapping_guidance_block}Write this section now. Start directly with the content — no preamble, no "Here is the section:" opener."""

_REVISION_PROMPT = """The user has reviewed the **{section_title}** section and requested changes.

ORIGINAL SECTION CONTENT:
{previous_content}

USER EDIT REQUEST:
{edit_comment}

SECTION INSTRUCTIONS (for reference):
{section_instructions}

{mapping_guidance_block}Rewrite the **{section_title}** section incorporating the user's requested changes.
Preserve any parts the user did not ask to change. Start directly with the revised content."""


# ─────────────────────────────────────────────────────────────────────────────
# Public function
# ─────────────────────────────────────────────────────────────────────────────

def generate_section(
    section_key:          str,
    section_title:        str,
    section_instructions: str,
    document_type:        str,
    system_instructions:  str,
    llm_context:          str,
    user_inputs:          dict,
    previous_sections:    list[dict],
    target_words:         int = 300,
    edit_comment:         Optional[str] = None,
    previous_content:     Optional[str] = None,
    section_spec:         Optional[dict] = None,
) -> tuple[str, str, str]:
    """
    Generate (or regenerate) a single section using the LLM.

    Args:
        section_key:          Internal key, e.g. "executive_summary"
        section_title:        Display title, e.g. "Executive Summary"
        section_instructions: Section-specific prompt instructions
        document_type:        e.g. "Business Requirements Document (BRD)"
        system_instructions:  Template-level instructions (tone, style, etc.)
        llm_context:          Full document context from ParsedDocument.to_llm_context()
        user_inputs:          Dict of user-provided metadata (project_name, etc.)
        previous_sections:    List of {"title": ..., "content": ...} already generated
        target_words:         Approximate word count target
        edit_comment:         If set → revision mode (user requested changes)
        previous_content:     The existing section content to revise (required if edit_comment set)

    Returns:
        (content_markdown, full_prompt_used, model_identifier)
    """
    # Cap llm_context to keep prompt size manageable.
    # Gemini 2.5 Flash has a 1M context window but we cap here to keep latency predictable.
    # 8 000 chars ≈ ~2 000 tokens, sufficient for rich document context per section.
    _MAX_CONTEXT_CHARS = 8_000
    if len(llm_context) > _MAX_CONTEXT_CHARS:
        llm_context = llm_context[:_MAX_CONTEXT_CHARS] + "\n\n[... document truncated for brevity ...]"

    system_prompt = _build_system_prompt(
        document_type        = document_type,
        system_instructions  = system_instructions,
        llm_context          = llm_context,
        user_inputs          = user_inputs,
        previous_sections    = previous_sections,
        target_words         = target_words,
    )

    # Enrich the section prompt with per-section guidance from the Adani mapping document.
    # Falls back gracefully (empty string) if the section or doc type isn't in the mapping.
    # The section's spec (scope boundary / columns / depth / source fields) now
    # travels inside the template section (single source of truth). The caller
    # passes it as section_spec; build_section_guidance turns it into the block.
    from generation.section_mapping import build_section_guidance
    mapping_guidance = build_section_guidance(section_spec)
    mapping_guidance_block = f"{mapping_guidance}\n\n" if mapping_guidance else ""

    if edit_comment and previous_content:
        user_prompt = _REVISION_PROMPT.format(
            section_title          = section_title,
            previous_content       = previous_content,
            edit_comment           = edit_comment,
            section_instructions   = section_instructions,
            mapping_guidance_block = mapping_guidance_block,
        )
    else:
        user_prompt = _GENERATION_PROMPT.format(
            section_title        = section_title,
            section_instructions = section_instructions,
            mapping_guidance_block = mapping_guidance_block,
        )

    full_prompt_for_log = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"

    combined_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
    max_tok = _estimate_max_tokens(target_words)

    logger.info(
        "[Generator] Section '%s' — prompt %d chars, max_tokens=%d",
        section_key, len(combined_prompt), max_tok,
    )

    from llm_provider import call_with_fallback
    try:
        content, provider = call_with_fallback(
            messages   = [{"role": "user", "content": combined_prompt}],
            max_tokens = max_tok,
            timeout    = 180,
            log_prefix = f"[Generator:{section_key}]",
        )
    except RuntimeError as e:
        logger.exception("[Generator] LLM call failed for section '%s'", section_key)
        raise RuntimeError(f"LLM generation failed for '{section_title}': {e}") from e

    logger.info(
        "[Generator] Section '%s' done via %s — %d words",
        section_key, provider, len(content.split()),
    )
    return content, full_prompt_for_log, provider


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_system_prompt(
    document_type:       str,
    system_instructions: str,
    llm_context:         str,
    user_inputs:         dict,
    previous_sections:   list[dict],
    target_words:        int,
) -> str:
    # Previous sections block — helps LLM maintain coherence and avoid repetition
    if previous_sections:
        prev_block_lines = [
            "\n## SECTIONS ALREADY WRITTEN\n"
            "Do NOT repeat information already covered in these sections:\n"
        ]
        for s in previous_sections:
            # Include only first 150 chars — enough for coherence, keeps prompt lean
            # as more sections accumulate (section 12 would otherwise carry ~4 400 chars)
            preview = s["content"][:150].strip()
            if len(s["content"]) > 150:
                preview += "…"
            prev_block_lines.append(f"### {s['title']}\n{preview}\n")
        previous_sections_block = "\n".join(prev_block_lines)
    else:
        previous_sections_block = ""

    additional = user_inputs.get("additional_instructions", "")
    if additional:
        additional_instructions_block = f"## ADDITIONAL INSTRUCTIONS FROM USER\n{additional}\n"
    else:
        additional_instructions_block = ""

    # Business ontology block — org framing, doc-type semantics, matched glossary
    # and matched AESL systems. Scans everything the model will see so only
    # RELEVANT ontology entries are injected (token discipline lives in ontology.py).
    from generation.ontology import for_generation as _ontology_for_generation
    _scan = " ".join(filter(None, (
        user_inputs.get("project_name", ""),
        user_inputs.get("business_problem") or "",
        user_inputs.get("project_description") or "",
        additional or "",
        user_inputs.get("derived_context") or "",
        llm_context or "",
    )))
    try:
        ontology_block = _ontology_for_generation(document_type, _scan)
    except Exception:
        logger.exception("[Generator] Ontology block build failed — continuing without it")
        ontology_block = ""

    # Also prepend template system instructions
    if system_instructions:
        additional_instructions_block = (
            f"## DOCUMENT STYLE INSTRUCTIONS\n{system_instructions}\n\n"
            + additional_instructions_block
        )

    return _SYSTEM_TEMPLATE.format(
        document_type                = document_type,
        project_name                 = user_inputs.get("project_name", "Unnamed Project"),
        target_audience              = user_inputs.get("target_audience") or "Business and technical stakeholders",
        stakeholders                 = user_inputs.get("stakeholders") or "Not specified",
        language                     = user_inputs.get("language", "English"),
        business_problem             = user_inputs.get("business_problem") or "Not provided",
        project_description          = user_inputs.get("project_description") or "Not provided",
        additional_instructions_block= additional_instructions_block,
        ontology_block               = ontology_block,
        llm_context                  = llm_context or "(No source document provided)",
        previous_sections_block      = previous_sections_block,
        target_words                 = target_words,
    )


def _estimate_max_tokens(target_words: int) -> int:
    """target_words × 6, floor 5 000, ceiling 16 000."""
    return max(5000, min(int(target_words * 6), 16000))


