# New Developer Guide

## 1. Executive summary

IntelliDraft turns uploaded source documents + a project intake form into formal Adani governance
documents (BRD, RFP, NFA, NIT, …), section-by-section, using **Gemini 2.5 Flash**, grounded in an
**Adani business ontology**, and wraps them in a **review / comment / AI-review / validation** workflow.

It is **one FastAPI app** (`Data_Ingestion/main.py`, ~64 routes) over a service layer, a SQLAlchemy
data layer (SQLite dev / Databricks SQL prod), and object storage (local / GCS / Unity Volumes).
Generation runs **wave-parallel** on in-process threads by default (opt-in Celery for durability).
The product is **API-only** today. An optional Google-ADK multi-agent chat surface exists (`adk web`)
but is off the request path.

## 2. Architecture overview
See [README_ARCHITECTURE.md](README_ARCHITECTURE.md). One-liner: **Client → FastAPI routes → services
(generation / review / chat / extract) → {Gemini, DB, storage}, with ontology grounding on every prompt.**

## 3. Repository structure

```
Intellidraft/
├── Data_Ingestion/               # THE application
│   ├── main.py                   # FastAPI — all ~64 routes, CORS, lifespan
│   ├── llm_provider.py           # Gemini/Vertex access (the only LLM choke point)
│   ├── app.yaml                  # Databricks Apps config
│   ├── requirements.txt          # pinned deps (with CVE notes)
│   ├── api/                      # extractor, derive-less chat_handler, user_input_schema
│   ├── generation/              # engine, db(ORM), templates, ontology, review, export, preview
│   ├── agents/                  # Google ADK orchestrator + 4 sub-agents + validation_agent
│   ├── parsers/                 # pdf/docx/pptx/xlsx + vision_analyzer
│   ├── models/                  # Pydantic schemas (meta_schema = parsing contract)
│   ├── storage/                 # local/GCS + Databricks Volume
│   ├── ontology/                # business grounding JSONs (business-owned)
│   ├── templates/               # 11 document-type templates (JSON)
│   └── tests/                   # pytest suites + contract + load + validation agent
├── Dockerfile / docker-compose.yml
├── scripts/deploy.{ps1,sh}
├── docs/                        # ← this knowledge base
└── DATABRICKS_DEPLOY_GUIDE.md / README.md / SETUP.md  (⚠️ partly stale)
```

## 4. End-to-end flow
Upload → parse (+vision) → extract intake fields → save draft project → derive extended fields →
generate (wave-parallel) → validate → review → export. Full sequence:
[README_DATA_FLOW.md](README_DATA_FLOW.md).

## 5–10. AI pipeline, data flow, database, API, business logic, integrations
See the dedicated docs: [AI](README_AI_PIPELINE.md) · [Data flow](README_DATA_FLOW.md) ·
[Database](README_DATABASE.md) · [API](README_API.md) · [Business logic](README_BUSINESS_LOGIC.md).
**External integrations:** Google Vertex AI (Gemini — required); Databricks SQL Warehouse + Unity
Volumes (prod); GCS (optional); Redis (optional, Celery). No email/SSO integration is wired server-side.

## 11. Important classes / functions

| Symbol | File | Why it matters |
|---|---|---|
| `call_with_fallback` | `llm_provider.py` | Every LLM call goes through here (retry/backoff) |
| `start_job_from_project` | `generation/generation_service.py` | Single entry to generate from a project (used by REST, chat, ADK) |
| `_run_generation_job` | `generation/generation_service.py` | The wave-parallel body (also run by subprocess/Celery) |
| `generate_section` / `_build_system_prompt` | `generation/generator.py` | The section prompt assembly |
| `for_generation` (+ siblings) | `generation/ontology.py` | Selective grounding blocks |
| `ai_persona_review` / `summarize_for_author` | `generation/review_service.py` | Review AI |
| `ValidationAgent.evaluate` | `agents/validation_agent.py` | QA scoring + provenance |
| `process_message` | `api/chat_handler.py` | Chat routing + lock hygiene |
| `get_session` / `retry_on_locked` / `_make_engine` | `generation/db.py` | DB access + concurrency |
| `parse_document` / `ParsedDocument.to_llm_context` | `parsers/` + `models/meta_schema.py` | Parsing contract |

## 12. Important files to read first
`main.py` (routes) → `generation/db.py` (data model) → `generation/generation_service.py` (engine) →
`generation/generator.py` + `generation/ontology.py` (prompts) → `llm_provider.py`.

## 13. Configuration
See [README_CONFIGURATION.md](README_CONFIGURATION.md). Minimum to run locally: a valid
`Data_Ingestion/key.json` and the defaults in `.env.example`.

## 14. Common development tasks

| Task | How |
|---|---|
| Add/adjust a section | Edit `templates/<doc>.json` (instructions, `variables`, `scope_boundary`, `mode`) → restart |
| Add a new document type | Add `templates/<type>.json`, register it in `template_manager._DOC_TYPE_MAP` (+ chat aliases in `chat_handler._DOC_ALIASES`) |
| Update business grounding | Replace `ontology/*.json` → restart (they're `@lru_cache`d) |
| Add a DB column | Add it to the model in `db.py` (nullable). SQLite auto-migrates; **Databricks needs a manual ALTER** |
| Add an LLM call that returns JSON | Use `call_with_fallback(..., json_mode=True)` and tolerant parsing |
| Change branding/fonts | Edit `generation/doc_style.py` (single source for preview + .docx) |
| Make generation durable | `GENERATION_BACKEND=celery` + run a worker (docker `--profile celery`) |
| Add a route | Add a `def` handler in `main.py`; update `tests/api_contract.py` + Postman |

## 15. Deployment / 16. Debugging
See [README_DEPLOYMENT.md](README_DEPLOYMENT.md) and [README_DEBUGGING.md](README_DEBUGGING.md).

## 17. Known technical debt

1. **Doc drift** — root README/SETUP describe Azure fallback + React frontend that don't exist.
2. **`knowledge.py` (RAG)** — scaffolded, imports a missing `embed_text`, wired into nothing. Either
   finish it (add `embed_text`, call it from the four sites) or delete it.
3. **No backend auth** — identity trusted from headers; roles unenforced (see security doc).
4. **No LLM mocking in CI** — generation prompt/shape regressions aren't auto-caught.
5. **Stale comments** — Dockerfile mentions LibreOffice; `.env.example` has `LO_CONVERT_TIMEOUT` and
   Azure blocks; several module docstrings mention "Azure GPT fallback".
6. **No Alembic** — prod schema changes are manual ALTERs.
7. **Chat intent classifier** is keyword-based — some natural phrasings ("make it concise") misclassify.
8. **Working tree is heavily modified/uncommitted** — many new modules (`celery_app`, `job_runner`,
   `knowledge`, `doc_meta`, `doc_style`) are untracked; commit hygiene needed.

## 18. Risks
- **Vendor lock-in to Gemini** with no working fallback — a Vertex outage stops all generation.
- **Cost/quota** — large jobs fan out many Gemini calls; no rate limiting.
- **Data residency** — parsed content (possibly sensitive) is sent to Vertex `us-central1` by default.
- **SQLite in dev** is fragile under multi-process; only Databricks SQL is safe for concurrency at scale.

## 19. Future improvements
- Finish the Databricks Vector Search RAG (`knowledge.py`) for precedent grounding.
- Server-side Entra token validation + role enforcement.
- Email/push notifications (hook point already exists: `review_service._notify`).
- Per-route Pydantic request models; WebSocket notifications; async LLM routes (only once services
  are non-blocking).
- CI with a stubbed LLM provider for generation regression tests.

## 20. FAQ
See [README_FAQ.md](README_FAQ.md).

---

## Confidence levels by area

| Area | Confidence | Basis |
|---|---|---|
| Data model / database | **High** | Read `db.py` in full (all 13 models) |
| Generation engine & backends | **High** | Read `generation_service.py`, `job_runner.py`, `celery_app.py`, `tasks.py` in full |
| AI pipeline / prompts / ontology | **High** | Read `generator.py`, `ontology.py`, `extractor.py`, `derive_fields.py`, `llm_provider.py`, `knowledge.py`, `validation_agent.py` |
| Review workflow | **High** | Read `review_service.py` in full |
| Chat studio | **High** | Read `chat_handler.py` in full |
| Parsing / storage | **High** | Read `meta_schema.py`, `parser_factory.py`, `gcs_storage.py`, `databricks_volume_storage.py` |
| Deployment / config | **High** | Read Dockerfile, docker-compose, app.yaml, `.env.example`, requirements |
| API surface | **Medium-High** | Enumerated all routes + read many handlers; a few handler bodies (mid-`main.py`) inferred from names/services, not line-by-line |
| Export / .docx formatting internals | **Medium** | Read `doc_style.py`, `doc_meta.py`, `doc_writer.py` head + `brd_formatter` via memory; did not read every formatter branch |
| ADK agents (sub-agent tools) | **Medium** | Read orchestrator + `_model.py`; sub-agent `tools.py` bodies skimmed, not fully read |
| Templates (all 11) | **Medium** | Read `brd.json` structure + template_manager; other templates inferred to share the schema |
| Frontend | **N/A** | Removed from the served app (recoverable in git) |
