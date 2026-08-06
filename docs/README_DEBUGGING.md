# Debugging Guide

Logs are the primary signal (stdout, INFO). LLM calls log model / elapsed / length / token usage.
Each module uses a `log_prefix` (`[Generator:<section>]`, `[Extractor]`, `[ReviewAgent:<persona>]`, …).

## Symptom → cause → fix

### "database is locked" (SQLite dev)
- **Cause:** two server processes on one SQLite file, or a held write lock across slow work.
- **Fix:** run exactly **one** server; kill stray test servers (commonly on 7073). The engine already
  sets WAL + `busy_timeout=30s` + `synchronous=NORMAL`, and writes use `@retry_on_locked`. In the chat
  handler, the user-message commit is released *before* slow work (`process_message`) — don't remove
  those `db.commit()`s. Production (Databricks SQL) doesn't have this class of bug.

### LLM call fails / HTTP 502
- **Cause:** `llm_provider` raises `RuntimeError` — usually missing/invalid `key.json`, Vertex quota
  (429s exhausted after 3 retries), or an empty response.
- **Fix:** confirm `Data_Ingestion/key.json` exists and is valid; check the `[LLM]` log line for the
  actual `type(exc).__name__: exc`. There is **no Azure fallback** — Gemini is the only provider.

### Chatbot edit "doesn't show in the doc HTML"
- **Cause (historical, fixed):** (1) `regenerate_section` didn't bust the preview cache; (2) chat
  wasn't linked to the project's job. Both fixed — `regenerate_section` calls
  `invalidate_preview_cache`, and `_get_or_create` resolves the project's latest job each message.
- **If it recurs:** verify the new `SectionVersion` was written (`GET …/section/{id}`) and that
  `GET …/preview/html` was re-fetched (the client must re-GET after a modify).

### Generation job stuck "pending"/"in_progress" forever
- **Cause:** thread backend + a server restart orphaned it.
- **Fix:** the startup sweep marks jobs older than `STALE_JOB_MINUTES` (45) as failed; re-run
  generation. For durability, switch to `GENERATION_BACKEND=celery`.

### A section is missing from the output
- **Cause:** it failed twice (main wave + retry pass). Check `[gen] Retry: '<key>' failed again`.
- **Fix:** `GET /api/generate/{job_id}/section/{id}` shows `status=failed` + `error`. Re-run
  `POST …/section/{id}/regenerate`.

### Table section comes back too short
- Built-in: a `table` section under 40% of `target_words` triggers **one** boosted retry. If still
  short, tune the section's `variables`/`instructions` in `templates/<doc>.json` and restart.

### Preview looks wrong / local ≠ prod
- Preview is pure-Python (`markdown2` + `doc_style.preview_css`) — identical everywhere. If it looks
  unstyled, check `preview_service` rendered `html` (not a fallback) and that section markers exist.
  LibreOffice is **not** used anymore.

### Wrong template / everything renders like a BRD
- **Cause (historical, fixed):** a stale/default system `template_id` mismatching the doc type.
  `get_sections_for_job` now ignores a mismatched **system** template id and uses the doc type's own
  template. If it recurs, confirm the project's `template_id` isn't pinned to `brd`.

### Ontology terms not expanding (e.g. AEML-D not defined)
- **Cause:** the term isn't in `ontology/terminology.json`, or the scan text didn't contain it (matching
  is word-boundary). Ontology is `@lru_cache`d — **restart** after editing the JSONs.

### Template edit not taking effect
- System templates auto-refresh from `templates/*.json` **at startup**. Restart the server (or
  `POST /api/templates/{id}/reseed`). User templates are never overwritten.

### Windows: litellm install fails / key.json path broken
- Enable Long Paths (see deployment doc). In `.env`, use **forward slashes** for
  `GOOGLE_KEY_JSON_PATH` — dotenv mangles `\a \b \n \t …`. The loader always falls back to the default
  `key.json` location anyway.

### ModuleNotFoundError on startup
- Wrong interpreter. `main.py`'s dependency guard prints the interpreter + missing packages — activate
  the venv and reinstall.

## Useful checks
```bash
curl http://localhost:7071/api/health                     # liveness
curl http://localhost:7071/api/generate/<job_id>          # job + section statuses
# Reset dev DB (only if ENABLE_ADMIN_ENDPOINTS=true):
curl -X POST http://localhost:7071/api/admin/reset-db
```
The stored `generation_prompt` on each `SectionVersion` is the exact prompt sent to Gemini — invaluable
for debugging why a section came out a certain way.
