# FAQ

**Q. What LLM does it use? Is there really no fallback?**
Gemini 2.5 Flash on Vertex AI, via litellm, through `llm_provider.call_with_fallback`. Despite the
name and the root README, there is **no Azure/OpenAI fallback in code** — a Gemini failure raises
`RuntimeError` (surfaced as HTTP 502). The Azure config in `.env.example` is unused.

**Q. Is there a frontend?**
Not served by the app. `/` returns API-info JSON. React (`frontend-react/`) and legacy HTML frontends
were removed (recoverable in git). Use Swagger (`/docs`) or the Postman collection.

**Q. How do I run it locally?**
`python Data_Ingestion/main.py` with a `key.json` and default `.env` → http://localhost:7071.
See [deployment](README_DEPLOYMENT.md).

**Q. Where is the data stored?**
Dev: SQLite (auto-redirected out of OneDrive to `%LOCALAPPDATA%\Intellidraft`) + local files under
`Data_Ingestion/local_storage/`. Prod: Databricks SQL Warehouse + Unity Catalog Volumes.

**Q. How do I change what a document section says?**
Edit that section in `templates/<doc>.json` (its `instructions`, `variables`, `scope_boundary`,
`format`, `depth`) and **restart** — system templates auto-refresh from JSON at startup. No code change.

**Q. What's the difference between "generate" and "static" sections?**
`mode:"generate"` → the LLM writes it. `mode:"static"` → its `static_content` is inserted verbatim
with `{{placeholders}}` filled from project fields (legal boilerplate / tender forms). Both become
normal versions.

**Q. How is generation made fast?**
Wave-parallel: `GENERATION_CONCURRENCY` (default 6) sections at once, each wave seeing 150-char
previews of earlier sections. ~concurrency-fold wall-clock reduction, quality-neutral.

**Q. My generated job is stuck / disappeared after a restart.**
Thread-backend jobs don't survive a restart; the startup sweep marks jobs older than 45 min failed.
Re-run, or use `GENERATION_BACKEND=celery` for durability.

**Q. Why "database is locked"?**
Two servers on one SQLite file, almost always. Run one. Prod (Databricks SQL) never has this.

**Q. How does the review workflow decide approved vs rejected?**
Worst-wins: any rejected → rejected; else any revision_requested → revision_requested; else all
accepted → approved. See [business logic](README_BUSINESS_LOGIC.md#6-review-lifecycle--status-rollup).

**Q. What's the ontology and why does it matter?**
Business-owned JSONs (`ontology/*.json`) with Adani entities, a 549-term glossary, ~130 systems,
regulations, and the doc chain. `ontology.py` injects **only matching entries** into each prompt so
output uses real Adani terminology and systems. Update = replace JSONs + restart.

**Q. Is the Databricks Vector Search / RAG live?**
No. `knowledge.py` is scaffolded, disabled by default, wired into nothing, and depends on a missing
`embed_text`. It's a planned feature, not a working one.

**Q. How is identity handled? Is it secure?**
Identity comes from `X-User-Email` / `X-User-Name` headers with **no backend validation** and no
authorization enforcement. Fine for internal trusted use; **not** safe for public exposure. See
[security](README_SECURITY.md).

**Q. What are the ADK agents for?**
An optional conversational surface (`adk web`, port 8000) — orchestrator + doc_parser /
context_collector / document_generator / reviewer sub-agents whose tools call the same services. The
FastAPI app does **not** route through them; in-app chat uses keyword classification.

**Q. How do I export a document?**
`GET /api/generate/{job_id}/export?format=docx|pdf|md`. PDF path is docx2pdf (local Windows) →
xhtml2pdf (pure-Python, Databricks) → DOCX fallback. LibreOffice is no longer used.

**Q. Can one project have multiple documents?**
Yes — many jobs per project; the latest completed job per (project, doc_type) is that document's
current state. Generation is idempotent per doc type.

**Q. How do I add a new document type?**
Add `templates/<type>.json`, register the type in `template_manager._DOC_TYPE_MAP`, and add chat
aliases in `chat_handler._DOC_ALIASES`. Grounding for BRD/NDPR/NFA/NIT/RFP is richer (chain-aware);
other types generate without chain semantics.

**Q. Where do I look when a section came out wrong?**
The exact prompt sent to Gemini is stored on each `SectionVersion.generation_prompt` — read it via
`GET /api/generate/{job_id}/section/{section_id}`.
