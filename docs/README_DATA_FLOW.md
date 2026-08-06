# Data Flow

## 1. End-to-end journey (upload → generated document)

```mermaid
sequenceDiagram
    participant U as Client
    participant API as FastAPI (main.py)
    participant PAR as parsers + vision
    participant STORE as Storage (FS/GCS/Volume)
    participant EX as extractor / derive_fields
    participant GEN as generation_service
    participant LLM as Gemini (Vertex)
    participant DB as DB

    U->>API: POST /api/upload (multipart file)
    API->>PAR: parse_document(tmp)
    PAR->>LLM: vision analyze images (if VISION_ENABLED)
    PAR->>STORE: persist_all → source/images/tables/meta.json/cosmos index
    API-->>U: {document_id, summary}

    U->>API: POST /api/extract-project-data {document_ids}
    API->>EX: extract_project_data
    EX->>STORE: get_meta_json → ParsedDocument.to_llm_context
    EX->>LLM: JSON extraction (12 intake fields) [json_mode]
    API-->>U: {extracted, missing_required[]}

    U->>API: POST /api/projects/draft (persist extracted)
    API->>DB: insert Project (status=draft)
    U->>API: POST /api/projects/{id}/derive-fields
    API->>EX: derive_fields → LLM → DerivedData row

    U->>API: POST /api/generate/project/{id}
    API->>GEN: start_job_from_project
    GEN->>DB: GenerationJob + Section rows (pending)
    GEN->>GEN: background thread: wave-parallel generation
    loop each wave (GENERATION_CONCURRENCY sections)
        GEN->>LLM: generate_section (prose Markdown)
        GEN->>DB: SectionVersion v1 + atomic completed_sections++
    end
    GEN->>DB: job status=completed; pre-warm HTML preview
    U->>API: GET /api/generate/{job_id} (poll) / .../stream (SSE)
    U->>API: GET /api/generate/{job_id}/export?format=docx
    API->>DB: assemble accepted/current versions
    API-->>U: file (docx/pdf/md)
```

## 2. Key data transformations & why they exist

| Stage | From → To | Where | Why |
|---|---|---|---|
| **Parse** | Raw file bytes → `ParsedDocument` (text/image/table elements with `ref` ids) | `parsers/*` → `models/meta_schema.py` | One canonical, citeable representation regardless of source format |
| **Vision** | Image bytes → `ai_description` + `image_type` + `key_elements` | `vision_analyzer` (during `persist_all`, before base64 is cleared) | Diagrams/screenshots become first-class *text* the LLM can read |
| **Context build** | `ParsedDocument` → flat LLM text (`to_llm_context`) | `meta_schema.ParsedDocument.to_llm_context` | Ordered, char-capped, image tags inlined as `[IMAGE [type]: …]` |
| **Extract** | LLM text → 12-field intake JSON | `api/extractor.py` | Auto-fill the intake form from documents |
| **Derive** | Intake fields → 12 extended analysis fields | `generation/derive_fields.py` | Pre-analysed context (requirements, NFRs, workflow, systems) that feeds generation |
| **Assemble user_inputs** | `Project` + `DerivedData` → `user_inputs` dict | `generation_service.start_job_from_project` | Single dict the generator consumes; merges form + derived + doc ids |
| **Generate** | `user_inputs` + doc context + ontology → Markdown per section | `generation/generator.py` | The actual document content |
| **Persist versions** | Markdown → `SectionVersion` rows (immutable, numbered) | `generation_service._persist_section_version` | Full version history + regeneration + snapshots |
| **Render preview** | Section Markdown → styled HTML | `preview_service` + `doc_style.preview_css` | On-screen preview identical local == Databricks (no LibreOffice) |
| **Export** | Section versions → `.docx` / `.pdf` / `.md` | `doc_writer` + `brd_formatter` | Final deliverable in Adani branding |

## 3. The DTOs / schemas (Phase 5)

- **`models/meta_schema.py`** (Pydantic) — the parsing contract:
  - `ParsedDocument` → `text_elements[]`, `image_elements[]`, `table_elements[]`, `summary`.
  - Element `ref` scheme: `text#txt_0001`, `image#img_0001`, `table#tbl_0001`.
  - `UserInputData` — the generation input contract (project_name, document_type, output_format,
    context fields, `sections_to_include`, `additional_instructions`, language).
  - `DocumentSummary` — counts + vision flags (`has_workflows`, `has_architecture`, `has_charts`).
- **`models/project_schema.py`** / **`api/user_input_schema.py`** — form-field definitions
  (drive `GET /api/form-fields`).
- **ORM `to_dict()` / `to_ingested_dict()` / `to_full_dict()`** in `db.py` — the API serialization
  layer. **POST/PATCH responses return only ids/counts; all business data is read back via GET**
  (the "DB-first" rule).

## 4. Storage layout (identical across backends)

`storage/get_storage_service()` returns one of three implementations by env
(`DATABRICKS_MODE` → Volumes, else `LOCAL_MODE` → local FS, else GCS). All mirror the same layout:

```
documents/{doc_id}/source/{filename}
documents/{doc_id}/images/{element_id}.{ext}
documents/{doc_id}/tables/{element_id}.csv
documents/{doc_id}/meta.json          ← the ParsedDocument
cosmos/{doc_id}.json                  ← lightweight index record
outputs/{job_id}/{filename}           ← exported documents
```

Locally, `meta.json` is the source of truth for a parsed document — `generation_service` and
`extractor` both re-hydrate a `ParsedDocument` from it via `store.get_meta_json(doc_id)`.

## 5. Multi-document context concatenation

`generation_service._load_job_context()` loads **all** attached `document_ids` (not just one),
concatenates their `to_llm_context` (20K chars each), appends the AI-derived analysis block, and
caps the total at **60,000 chars**. Per-section, `generator` re-caps to 8,000 chars. This two-level
cap keeps latency predictable while still letting a project draw on several sources.

## 6. Chat data flow (Document Chat Studio)

`api/chat_handler.py` persists everything in the `chat_sessions` table (`messages_json`,
`pending_json`, `phase`). A message is classified by **keyword matching (no LLM)** into
generate / modify / show / status / export / regenerate, then routed to the relevant service call.
The one LLM call the chat makes is **document-impact analysis** when a new file is uploaded mid-review
(`_analyze_doc_impact` → which sections to regenerate). Modify is a **two-step confirm** (instruction →
"yes" → regenerate). See [README_BUSINESS_LOGIC.md](README_BUSINESS_LOGIC.md#5-document-chat-studio).
