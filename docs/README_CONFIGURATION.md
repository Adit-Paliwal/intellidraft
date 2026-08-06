# Configuration Reference

All config is environment-driven (`.env` loaded by `python-dotenv` in `main.py`, `job_runner.py`,
`celery_app.py`, and the agent/extractor modules). Template: `Data_Ingestion/.env.example`.

## LLM

| Var | Default | Meaning |
|---|---|---|
| `GEMINI_VERTEX_MODEL` | `gemini-2.5-flash` | Gemini model id (litellm `vertex_ai/<id>`) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model id for the **ADK agents** (`agents/_model.py`) |
| `GOOGLE_KEY_JSON_PATH` | `Data_Ingestion/key.json` | GCP service-account JSON path. ⚠️ On Windows use forward slashes — dotenv expands `\a \b \n \t …` |
| `VERTEX_LOCATION` | `us-central1` | Vertex region |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | API-key alternative to Vertex (used by ADK agent-model detection) |
| `MODEL_PROVIDER` | `gemini` | **Legacy/ignored by `llm_provider`** — code path is Gemini-only regardless |
| `AZURE_GPT5_*`, `AZURE_OPENAI_*` | — | **Not used by any code path.** Present in `.env.example` only |

## Database

| Var | Default | Meaning |
|---|---|---|
| `LOCAL_DB` | `true` | `true`=SQLite; `false`=`DATABASE_URL` or Databricks OAuth |
| `DATABASE_URL` | — | Any SQLAlchemy URL (PostgreSQL/Azure SQL/PAT-based databricks://). Required when `LOCAL_DB=false` and not Databricks OAuth |
| `INTELLIDRAFT_DB_DIR` | auto | Override the SQLite dir. Auto-redirects out of OneDrive/Dropbox to `%LOCALAPPDATA%\Intellidraft` |
| `DATABRICKS_MODE` | `false` | `true` → SQL Warehouse via service-principal OAuth + Unity Volume storage |
| `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH` | — | Warehouse connection (set in Databricks Apps UI) |
| `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET` | injected | Auto-provided inside a Databricks App (OAuth M2M) |
| `DATABRICKS_CATALOG`, `DATABRICKS_SCHEMA` | `adani_ael_ailabs_catalog_dev` / `document-generator` | Unity Catalog target |

## Storage

| Var | Default | Meaning |
|---|---|---|
| `LOCAL_MODE` | `true` | `true`=local FS storage; `false`=GCS |
| `GCS_BUCKET_NAME` | — | Required when `LOCAL_MODE=false` and not Databricks |
| `DATABRICKS_VOLUME_PATH` | `/Volumes/intellidraft/files` | Unity Catalog Volume root (when `DATABRICKS_MODE=true`) |

## Generation engine

| Var | Default | Meaning |
|---|---|---|
| `ASYNC_GENERATION` | `true` | `true`=background; `false`=synchronous in-request |
| `GENERATION_BACKEND` | derived | `thread` (default) · `subprocess` · `celery` · `sync`. Blank → thread if async else sync |
| `GENERATION_CONCURRENCY` | `6` | Sections generated in parallel per wave (1 = sequential) |
| `THREADPOOL_TOKENS` | `80` | anyio threadpool size for sync routes (set in lifespan) |
| `STALE_JOB_MINUTES` | `45` | Age after which orphaned pending/in_progress jobs are swept to failed on startup |

## Celery (only when `GENERATION_BACKEND=celery`)

| Var | Default | Meaning |
|---|---|---|
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Broker |
| `CELERY_RESULT_BACKEND` | = broker | Result store |
| `CELERY_TASK_QUEUE` | `generation` | Queue name |
| `CELERY_VISIBILITY_TIMEOUT` | `3600` | Redis re-delivery window — must exceed slowest generation |

## Vision

| Var | Default | Meaning |
|---|---|---|
| `VISION_ENABLED` | `true` | Send extracted images to Gemini for description during parse |

## Knowledge base (RAG scaffold — inert)

| Var | Default | Meaning |
|---|---|---|
| `KB_RETRIEVAL_ENABLED` | `false` | Enable Databricks Vector Search grounding — **non-functional today** (see AI pipeline doc) |
| `KB_ENDPOINT_NAME`, `KB_INDEX_NAME`, `KB_RETRIEVAL_TOP_K`, `KB_CONFIDENTIALITY_DEFAULT` | — | Vector Search config |

## API / ops

| Var | Default | Meaning |
|---|---|---|
| `CORS_ALLOW_ORIGINS` | `*` | Restrict to your host in prod |
| `ENABLE_ADMIN_ENDPOINTS` | unset | Must be `true` to allow `POST /api/admin/reset-db` |
| `PORT` / `DATABRICKS_APP_PORT` | 7071 | Bind port (cloud injects it) |
| `LO_CONVERT_TIMEOUT` | 90 | **Legacy** — LibreOffice was removed; no effect |

## Mode cheat-sheet

| Environment | `LOCAL_DB` | `LOCAL_MODE` | `DATABRICKS_MODE` | DB | Files |
|---|---|---|---|---|---|
| Local dev | true | true | false | SQLite | local FS |
| Docker self-host | true | true | false | SQLite (volume) | volume |
| Databricks Apps (prod) | false | false | true | SQL Warehouse (OAuth) | Unity Volumes |
| Generic cloud + GCS | false | false | false | `DATABASE_URL` | GCS |
