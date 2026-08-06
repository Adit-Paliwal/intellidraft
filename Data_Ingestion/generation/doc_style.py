"""
Document Style — the SINGLE source of formatting truth
=======================================================
Both the Word download (brd_formatter.py) and the on-screen HTML preview
(preview_service.py) read the brand palette + fonts from here, so the preview
looks like the downloaded document. Change a colour/size once, here.

Why this exists: the preview used to be rendered by LibreOffice (it converted
the actual styled .docx to HTML), which meant it matched the download but ONLY
where LibreOffice was installed — locally. On Databricks (no LibreOffice) the
preview fell back to a plainly-styled markdown2 render, so local ≠ dev. We
removed LibreOffice from the preview path entirely and instead style the HTML
preview to match the .docx using these shared constants — same output
everywhere.
"""
from __future__ import annotations

# ── Adani brand palette (hex, no leading #) — mirrors the Word template ───────
DARK_BLUE  = "1F3763"   # H3 text + table-header fill
MID_BLUE   = "2E75B6"   # header rule line
LIGHT_BLUE = "D6E4F7"   # table alt-row (light)
GRAY       = "595959"   # meta / footer text
WHITE      = "FFFFFF"
BLACK      = "000000"

# ── Fonts + sizes (points — identical to the .docx styles) ────────────────────
FONT        = "Calibri"
SIZE_BODY   = 11
SIZE_H1     = 16   # black, bold
SIZE_H2     = 13   # black, bold
SIZE_H3     = 12   # dark-blue, bold
SIZE_TITLE  = 20   # cover title
SIZE_SUB    = 14   # cover subtitle
SIZE_META   = 10   # gray meta line
SIZE_TABLE  = 10.5


def _hx(hex6: str) -> str:
    return "#" + hex6


def preview_css() -> str:
    """CSS for the HTML preview, matching the Word download's styling.
    Uses pt sizes so on-screen sizing tracks the .docx."""
    return (
        f"body{{font-family:{FONT},'Segoe UI',Arial,sans-serif;font-size:{SIZE_BODY}pt;"
        f"color:{_hx(BLACK)};line-height:1.42;max-width:900px;margin:32px auto;padding:0 34px;}}"
        # cover / header block
        f".id-header{{border-bottom:2px solid {_hx(MID_BLUE)};margin:0 0 18pt;padding-bottom:8pt;}}"
        f".id-title{{font-size:{SIZE_TITLE}pt;font-weight:bold;text-align:center;color:{_hx(BLACK)};margin:4pt 0;}}"
        f".id-sub{{font-size:{SIZE_SUB}pt;font-weight:bold;text-align:center;color:{_hx(BLACK)};margin:2pt 0;}}"
        f".id-meta{{text-align:center;color:{_hx(GRAY)};font-size:{SIZE_META}pt;margin:2pt 0;}}"
        # headings (match .docx: H1/H2 black, H3 dark-blue)
        f"h1{{font-size:{SIZE_H1}pt;font-weight:bold;color:{_hx(BLACK)};margin:14pt 0 6pt;}}"
        f"h2{{font-size:{SIZE_H2}pt;font-weight:bold;color:{_hx(BLACK)};margin:10pt 0 4pt;}}"
        f"h3{{font-size:{SIZE_H3}pt;font-weight:bold;color:{_hx(DARK_BLUE)};margin:8pt 0 4pt;}}"
        f"h4{{font-size:{SIZE_BODY}pt;font-weight:bold;color:{_hx(DARK_BLUE)};margin:6pt 0 3pt;}}"
        f"p{{margin:5pt 0;}}"
        # tables (match .docx: dark-blue header, white text, light alt rows)
        f"table{{border-collapse:collapse;width:100%;margin:10pt 0;font-size:{SIZE_TABLE}pt;}}"
        f"th,td{{border:1px solid #b9c4d6;padding:5pt 8pt;text-align:left;vertical-align:top;}}"
        f"th{{background:{_hx(DARK_BLUE)};color:{_hx(WHITE)};font-weight:bold;}}"
        f"tr:nth-child(even) td{{background:#f4f7fc;}}"
        # inline / misc
        f"code{{font-family:Consolas,'Courier New',monospace;font-size:10pt;background:#f4f4f4;padding:1px 4px;border-radius:3px;}}"
        f"pre{{background:#f5f5f5;padding:10pt;overflow-x:auto;font-size:10pt;}}"
        f"ul,ol{{padding-left:22pt;}}li{{margin:2pt 0;}}"
        f"blockquote{{border-left:3px solid {_hx(MID_BLUE)};margin:0;padding:2pt 14pt;color:{_hx(GRAY)};}}"
        f"hr{{border:none;border-top:1px solid #d9d9d9;margin:14pt 0;}}"
        f"section[data-section-id]{{scroll-margin-top:60px;}}"
    )


def pdf_css() -> str:
    """CSS for the xhtml2pdf PDF export — same brand palette as the preview/.docx,
    but restricted to what xhtml2pdf (reportlab) supports (no nth-child/flex; uses
    @page + Helvetica since Calibri isn't embedded)."""
    return (
        "@page{size:A4;margin:2cm;}"
        f"body{{font-family:Helvetica,Arial,sans-serif;font-size:{SIZE_BODY}pt;color:{_hx(BLACK)};line-height:1.4;}}"
        f".id-title{{font-size:{SIZE_TITLE}pt;font-weight:bold;text-align:center;color:{_hx(BLACK)};}}"
        f".id-meta{{text-align:center;color:{_hx(GRAY)};font-size:{SIZE_META}pt;}}"
        f".id-rule{{border-bottom:2px solid {_hx(MID_BLUE)};margin-bottom:10pt;}}"
        f"h1{{font-size:{SIZE_H1}pt;font-weight:bold;color:{_hx(BLACK)};}}"
        f"h2{{font-size:{SIZE_H2}pt;font-weight:bold;color:{_hx(BLACK)};}}"
        f"h3{{font-size:{SIZE_H3}pt;font-weight:bold;color:{_hx(DARK_BLUE)};}}"
        f"table{{border-collapse:collapse;width:100%;margin:8pt 0;}}"
        f"th,td{{border:1px solid #b9c4d6;padding:4pt 6pt;text-align:left;}}"
        f"th{{background:{_hx(DARK_BLUE)};color:{_hx(WHITE)};font-weight:bold;}}"
        f"code{{font-family:Courier;font-size:10pt;background:#f4f4f4;}}"
        f"hr{{border-top:1px solid #d9d9d9;}}"
    )
