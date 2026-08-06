"""
Section guidance — the per-section spec block injected into the generation prompt.
================================================================================
SINGLE SOURCE OF TRUTH: each section's spec now lives directly inside its template
file (templates/<doc>.json). There is NO Excel and NO compile step. To tune the
system, edit the section in templates/<doc>.json and restart the server.

This module turns a section's spec fields into the
'## ADANI TEMPLATE SPECIFICATION' block that is appended to the section's user
prompt (see generation/generator.py -> generate_section).

A template section may carry any of these OPTIONAL fields (all tunable by hand):
  scope_boundary  what NOT to include (rendered as "MUST NOT INCLUDE")
  variables       exact table column names (enforced as the table header row)
  format          Text | List | Table | Form | Header
  depth           Detailed | Short
  remarks         extra generation notes
  source_fields   project/derived form fields that feed this section
  annexure        {"reference_text": "..."}  (e.g. NDPR regulatory references)

A section that carries none of these (its `instructions` already say everything)
simply produces no extra block — build_section_guidance returns "".
"""

from __future__ import annotations

import re
from typing import Optional


def build_section_guidance(section: Optional[dict]) -> str:
    """Build the complementary spec block from a template section dict.

    `section` is the section config as stored in templates/<doc>.json. Returns an
    empty string when the section carries no spec fields.
    """
    if not section:
        return ""

    lines = ["## ADANI TEMPLATE SPECIFICATION (authoritative — follow exactly)"]

    # MUST NOT INCLUDE — the scope boundary keeps sections from bleeding into each
    # other. Rendered first so the model reads the boundary before writing.
    sb = (section.get("scope_boundary") or "").strip()
    if sb:
        sb = re.sub(r"^\s*EXCLUDE\s*:\s*", "", sb, flags=re.IGNORECASE).strip()
        lines.append(f"**MUST NOT INCLUDE (scope boundary):** {sb}")

    if section.get("variables"):
        lines.append(f"**Required columns / variables:** {section['variables']}")

    fmt = (section.get("format") or "").lower()
    if fmt:
        if "table" in fmt:
            cols = section.get("variables", "")
            header_rule = (
                f" The FIRST row MUST be a header row with exactly these column names: {cols}."
                if cols else
                " The FIRST row MUST be a header row naming every column."
            )
            lines.append(
                "**Output format:** Table — output a Markdown pipe table." + header_rule +
                " Immediately follow the header with a separator row (| --- | --- |), then the "
                "data rows. Every data row must fill every column."
            )
        elif "form" in fmt:
            lines.append("**Output format:** Structured form — reproduce the form fields and labels exactly.")
        elif "list" in fmt:
            lines.append("**Output format:** Bulleted list (- items).")
        elif "header" in fmt:
            lines.append("**Output format:** Section header — a brief 1-2 line framing paragraph only.")
        elif "text" in fmt:
            lines.append("**Output format:** Prose paragraphs.")

    depth = section.get("depth")
    if depth == "Detailed":
        lines.append("**Depth:** Detailed — cover thoroughly with specifics, metrics and named systems.")
    elif depth == "Short":
        lines.append("**Depth:** Short — concise; a few tight paragraphs or bullets, no padding.")

    if section.get("remarks"):
        lines.append(f"**Generation notes:** {section['remarks']}")

    src = section.get("source_fields")
    if src:
        src_str = ", ".join(src) if isinstance(src, (list, tuple)) else str(src)
        lines.append(f"**Source fields to pull from (project inputs):** {src_str}")

    anx = section.get("annexure") or {}
    ref = (anx.get("reference_text") or "").strip() if isinstance(anx, dict) else ""
    if ref:
        lines.append(f"**Regulatory / annexure reference (weave in where appropriate):** {ref}")

    return "\n".join(lines) if len(lines) > 1 else ""
