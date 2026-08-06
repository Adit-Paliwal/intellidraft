# IntelliDraft — Project Knowledge Base

> Reverse-engineered onboarding documentation for the IntelliDraft AI document-generation
> platform. Every claim here was verified against the source under `Data_Ingestion/`
> as of the exploration date. Where the codebase and older docs disagree, this
> knowledge base follows the **code**.

## What is IntelliDraft?

IntelliDraft is a **GenAI-enabled project-lifecycle documentation platform** built for
**Adani Energy Solutions Limited (AESL)**. A user uploads source documents (PDF / DOCX /
PPTX / XLSX) and fills a project intake form; the system generates formal enterprise
documents — **BRD, RFP, SOW, Project Proposal, Technical Specification, Scope Document,
NDPR, NFA, NIT, BOQ, ARB** (11 types) — section-by-section using Google **Gemini 2.5 Flash**,
grounded in an Adani business ontology, then runs them through a full **review, comment,
AI-persona-review, and validation** workflow.

The product is **API-only** today (FastAPI + Swagger + Postman). A React SPA and legacy
HTML frontends existed but were removed from the served app (still recoverable in git).

## Document index

| Doc | Covers | Phases |
|---|---|---|
| [README_ARCHITECTURE.md](README_ARCHITECTURE.md) | System / backend / AI architecture, components, request lifecycle | 2, 9, 10 |
| [README_AI_PIPELINE.md](README_AI_PIPELINE.md) | LLM provider, prompts, ontology grounding, RAG scaffold, review AI, validation agent, ADK agents | 6 |
| [README_DATA_FLOW.md](README_DATA_FLOW.md) | End-to-end data flow, DTOs, schemas, transformations | 5 |
| [README_DATABASE.md](README_DATABASE.md) | All 13 tables, relationships, ER diagram, migrations | 7 |
| [README_API.md](README_API.md) | All ~64 REST endpoints — method, route, purpose | 8 |
| [README_BUSINESS_LOGIC.md](README_BUSINESS_LOGIC.md) | Domain, entities, business rules, hidden logic, doc-type chain | 4 |
| [README_CONFIGURATION.md](README_CONFIGURATION.md) | Every environment variable, defaults, modes | 1, 13 |
| [README_DEPLOYMENT.md](README_DEPLOYMENT.md) | Local, Docker, Databricks Apps, generation backends, startup sequence | 14 |
| [README_SECURITY.md](README_SECURITY.md) | Auth, secrets, injection surfaces, PII, uploads, LLM security | 12 |
| [README_TESTING.md](README_TESTING.md) | Test suites, contract, load, validation-agent evaluation | 13 |
| [README_DEBUGGING.md](README_DEBUGGING.md) | Common failures and how to diagnose them | 16 |
| [README_NEW_DEVELOPER_GUIDE.md](README_NEW_DEVELOPER_GUIDE.md) | Executive summary, repo tour, common tasks, important files | 15 |
| [README_GLOSSARY.md](README_GLOSSARY.md) | Domain + technical glossary | 15 |
| [README_FAQ.md](README_FAQ.md) | Frequently asked questions | 15 |

## Tech stack at a glance (verified in `Data_Ingestion/requirements.txt`)

- **Language / runtime:** Python 3.11+
- **Web framework:** FastAPI 0.136 + Uvicorn/Gunicorn (ASGI). **Flask is fully removed.**
- **LLM:** Google **Gemini 2.5 Flash** on **Vertex AI**, called via **litellm** (`vertex_ai/…`).
- **ORM / DB:** SQLAlchemy 2.0 → **SQLite** (dev) or **Databricks SQL Warehouse** (prod).
- **AI agents:** Google **ADK 2.2** (`adk web` — an optional multi-agent chat surface).
- **Parsing:** PyMuPDF (PDF), python-docx, python-pptx, openpyxl (XLSX), Gemini vision for images.
- **Export:** python-docx (.docx), markdown2 (HTML preview), xhtml2pdf / docx2pdf (PDF).
- **Optional durable queue:** Celery 5.4 + Redis (opt-in, `GENERATION_BACKEND=celery`).
- **Storage:** local filesystem (dev) / GCS (legacy) / **Databricks Unity Catalog Volumes** (prod).

## Confidence levels

See the "Confidence" section at the bottom of the [New Developer Guide](README_NEW_DEVELOPER_GUIDE.md#confidence-levels-by-area)
for a per-area High/Medium/Low breakdown of how thoroughly each area was verified.

## ⚠️ Documentation drift (found during reverse-engineering)

The root `README.md`, `SETUP.md`, and several module docstrings are **stale**. Do not trust them
where they conflict with this knowledge base:

1. **Azure GPT-5 fallback does not exist in code.** `llm_provider.call_with_fallback()` calls
   **only** Gemini Vertex AI and raises `RuntimeError` on failure — there is no Azure path,
   despite the root README and several docstrings describing "Gemini primary / Azure GPT-5 fallback".
2. **`generation/knowledge.py` (Vector Search RAG) is scaffolded but not wired in.** No prompt site
   imports it; only `ontology.py` is used. It also depends on `llm_provider.embed_text`, which does
   not exist. It is inert dead code today.
3. **No frontend is served.** `main.py` returns API-info JSON at `/`. React/HTML frontends were removed.
4. **LibreOffice is gone.** The Dockerfile comment still mentions it, but it is not installed and the
   preview path is pure-Python (markdown2 + shared CSS).
5. **Celery/Redis is back** as an *opt-in* durable backend (was previously removed).
