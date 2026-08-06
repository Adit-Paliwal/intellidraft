# Document Generation — Tuning Guide

**Single source of truth = the template JSON files.** There is **no Excel and no
compile step** anymore. To change what a document contains, how long each section
is, or what rules the AI follows, you **edit `templates/<doc>.json` directly and
restart the server**.

```
templates/brd.json  templates/ndpr.json  templates/nfa.json  templates/rfp.json  templates/nit.json  …
        │  (read at server startup — no build step)
        ▼
generation reads each section's fields → builds the prompt → LLM
```

---

## 1. The workflow

```
1. Edit templates/<doc>.json   (see the field guide below)
2. Restart the server          (templates are reseeded from JSON on startup)
```
That's it. No `compile_mapping`, no `.xlsx`. Validate the JSON stays well-formed
(a trailing comma will break it) — any editor with JSON linting helps.

> The cover/document-control metadata (title, template ID, classification,
> disclaimer) lives in `mapping_data/template_config.json` — edit that for cover
> changes.

---

## 2. Anatomy of a template

```jsonc
{
  "id": "brd",
  "name": "Business Requirements Document",
  "document_type": "Business Requirements Document (BRD)",
  "system_instructions": "…document-wide tone/style rules…",
  "sections": [
    {
      "key": "s7_business_requirements",     // stable id (don't rename casually)
      "title": "Business Requirements",       // the heading
      "order": 7,
      "render_hint": "table",                 // prose | list | table | mixed | form | header
      "target_words": 360,                    // approximate length nudge
      "instructions": "List 15-20 business requirements … columns: Req # | BRQ | …",  // WHAT to write
      "scope_boundary": "implementation details (belongs in FRs), NFRs …",            // what NOT to write
      "variables": "Requirement Number | Description | Rationale | Impacted Stakeholder | Remarks",  // exact table columns
      "format": "Table",                      // drives the format directive + short-table retry
      "depth": "Detailed",                    // Detailed | Short
      "remarks": "Requirement IDs like BRQ-001.",
      "source_fields": ["Functional requirement", "Workflow requirement"]             // project inputs to pull from
    }
  ]
}
```

### What each section field controls (all optional except key/title/instructions)
| Field | Effect in the prompt |
|---|---|
| **instructions** | The core "what to write" — becomes `SECTION INSTRUCTIONS:` in the user prompt. |
| **scope_boundary** | Rendered as **MUST NOT INCLUDE** — the biggest lever against sections overlapping. |
| **variables** | Exact table column names, enforced as the header row (for table sections). |
| **format** | `Text`/`List`/`Table`/`Form`/`Header` → the output-format directive; `Table` also enables the short-table retry. |
| **depth** | `Detailed`/`Short` → a depth directive. |
| **target_words** | "approximately N words" nudge + the token budget (`max_tokens`). |
| **remarks** | Extra generation notes (ID formats, tone). |
| **source_fields** | "Source fields to pull from" — which project/derived form fields feed the section. |
| **mode / static_content** | `"mode":"static"` inserts `static_content` verbatim (placeholders filled) — **no LLM**. |
| **composite / subsections** | Marks a bundled parent+children unit (one LLM call renders `## 4.1`, `## 4.2` …). |

> The `instructions` say the *what*; the other fields say the *how/boundaries*.
> `build_section_guidance` (in `generation/section_mapping.py`) turns those other
> fields into the `## ADANI TEMPLATE SPECIFICATION` block — no duplication.

---

## 3. Common tuning tasks

**Make a section longer / shorter** — set `target_words` (and/or reword the
`instructions` count, e.g. "List 15-20" → "List 30-40"). The model treats the
count in `instructions` as authoritative and `target_words` as a nudge.

**Add a rule / constraint** — put a *don't* rule in `scope_boundary`; put a *do*
rule in `instructions` or `remarks`. Document-wide rules go in the template's
top-level `system_instructions`, or (for ALL doc types) in
`generation/generator.py` → `_SYSTEM_TEMPLATE`.

**Change a table's columns** — edit `variables` (those names become the enforced
header row) and keep `format` = `Table`.

**Force prose vs table vs list** — set `format` to `Text` / `Table` / `List`.

**A table came out too short** — it auto-retries once when < 40% of `target_words`.
If still thin, raise the count in `instructions` and/or `target_words`.

**Add a whole new section** — add a new object to `sections` with a unique `key`,
an `order`, a `title`, and `instructions`. Restart.

**Cover / template ID / classification / disclaimer** — edit
`mapping_data/template_config.json`.

---

## 4. Composite sub-sections (fewer LLM calls)

A section with `"composite": true` bundles a parent + its sub-sections into ONE
call; its `instructions` already embed each sub-section (`## 4.1`, `## 4.2` …).
To split a bundle back into separate LLM calls, replace the one composite section
with individual sections (each its own object with its own `instructions`).

---

## 5. Speed / latency

| Env var | Default | Effect |
|---|---|---|
| `GENERATION_CONCURRENCY` | 6 | Sections generated in parallel per document. |
| `GENERATION_BACKEND` | thread | thread / subprocess / celery / sync (see `.env.example`). |

---

## 6. Verify a change

Restart, generate the doc, and check section lengths + the validation score
(`POST /api/generate/{job_id}/validate`, aim ≥ 90). To preview a section's
guidance block without a server:
```python
import json
from generation.section_mapping import build_section_guidance
sec = next(s for s in json.load(open("templates/brd.json"))["sections"]
           if s["title"] == "Business Requirements")
print(build_section_guidance(sec))
```

See `generation/GENERATION_INTERNALS.md` for the full context-build + LLM-call flow.
