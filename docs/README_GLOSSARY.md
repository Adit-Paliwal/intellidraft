# Glossary

## Product / domain

| Term | Meaning |
|---|---|
| **IntelliDraft** | The AI document-generation platform documented here |
| **AESL** | Adani Energy Solutions Limited — the primary business unit this platform serves |
| **AEML** | Adani Electricity Mumbai Limited — a related entity (default NIT purchaser) |
| **BRD** | Business Requirements Document |
| **NDPR** | Non-Detailed Project Report |
| **NFA** | Note for Approval |
| **NIT** | Notice Inviting Tender |
| **RFP** | Request for Proposal |
| **SOW** | Statement of Work |
| **BOQ** | Bill of Quantities |
| **ARB** | Architecture Review Board (submission) |
| **Document chain** | BRD → NDPR → NFA → NIT → RFP → SOW — the Adani governance sequence encoded in `ontology/workflow.json` |
| **Ontology pack** | Business-owned JSONs (`ontology/*.json`) grounding prompts: entities, glossary (549 terms), systems (~130), regulations, doc semantics, workflow |
| **Persona** | An AI reviewer lens (PM / Technical / Business Analyst / Compliance / Financial + custom) |
| **Provenance / grounding** | Validation-agent tracing of generated content back to the source document that supports it |

## Application concepts

| Term | Meaning |
|---|---|
| **Project** | The intake form + lifecycle unit; owns derived data and many generation jobs |
| **DerivedData** | 12 AI-derived extended fields (requirements, NFRs, workflow, systems…) per project |
| **Generation job** | One "generate document" request; owns sections |
| **Section / SectionVersion** | A document section; versions are immutable, numbered, append-only |
| **Snapshot** | A point-in-time checkpoint of section versions (or raw edited HTML for `manual_html`) |
| **Static vs generate section** | A template section either LLM-generates or inserts `static_content` verbatim with `{{placeholders}}` filled |
| **Composite section** | A parent section that generates itself + all sub-sections in one block |
| **trigger_type** | Origin of a version: ai_generation / ai_regeneration / manual_edit / review_comment / static / manual_html |
| **review_status** | A job's review state: draft / under_review / revision_requested / approved / rejected |
| **Wave** | A batch of `GENERATION_CONCURRENCY` sections generated in parallel |

## Technical

| Term | Meaning |
|---|---|
| **litellm** | The library that calls Gemini via `vertex_ai/<model>` |
| **Vertex AI** | Google Cloud's managed model platform hosting Gemini |
| **ADK** | Google Agent Development Kit — the optional `adk web` multi-agent surface |
| **Wave-parallel generation** | Generating sections `GENERATION_CONCURRENCY` at a time, each wave seeing 150-char previews of prior sections |
| **`@retry_on_locked`** | Decorator re-running a write transaction on a transient SQLite lock |
| **QueuePool** | The SQLAlchemy pool used for SQLite (never StaticPool) |
| **WAL** | SQLite write-ahead logging — readers don't block the single writer |
| **`to_llm_context`** | `ParsedDocument` method that flattens parsed elements into capped LLM text |
| **`json_mode`** | Vertex `response_format=json_object`, forcing valid-JSON replies |
| **Unity Catalog Volume** | Databricks managed file storage used in production |
| **Service-principal OAuth (M2M)** | Databricks App auth — no PAT; creds injected as `DATABRICKS_CLIENT_ID/SECRET` |
| **Selective injection** | Injecting only ontology entries that match a call's text (token discipline) |
| **Preview cache** | In-process HTML preview cache in `preview_service`, invalidated on any content change |

## Regulatory (from `ontology/key_regulations.json`)

| Term | Meaning |
|---|---|
| **MERC** | Maharashtra Electricity Regulatory Commission |
| **BEE** | Bureau of Energy Efficiency |
| **DFPO / LIS** | Other regulatory frameworks referenced in the ontology pack |
