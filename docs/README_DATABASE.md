# Database Design

All models live in **`Data_Ingestion/generation/db.py`** (SQLAlchemy 2.0 declarative). There are
**13 tables**. Backend is selected by `LOCAL_DB` / `DATABRICKS_MODE`:

- **`LOCAL_DB=true`** → SQLite (WAL mode), file auto-created (redirected out of OneDrive to
  `%LOCALAPPDATA%\Intellidraft` — WAL DBs must not live in cloud-synced folders).
- **`LOCAL_DB=false`** → `DATABASE_URL` (PostgreSQL/Azure SQL), OR Databricks SQL Warehouse via
  service-principal OAuth when `DATABRICKS_MODE=true` and no URL is set.

## ER diagram

```mermaid
erDiagram
    projects ||--o| derived_data : "1:1 (cascade)"
    projects ||--o{ generation_jobs : "1:N (project_id)"
    generation_jobs ||--o{ sections : "1:N (cascade)"
    sections ||--o{ section_versions : "1:N (cascade)"
    sections ||--o{ section_comments : "1:N (cascade)"
    generation_jobs ||--o{ document_snapshots : "1:N"
    generation_jobs ||--o{ review_requests : "1:N"
    review_requests ||--o{ review_assignments : "1:N (cascade)"
    review_requests ||--o{ review_comments : "1:N (cascade)"
    review_requests ||--o{ review_summaries : "1:N"
    review_comments ||--o{ review_comments : "parent_id (threads)"

    projects {
why: "intake form + lifecycle"
    }
    generation_jobs {
why: "one generate request; latest per (project,doc_type) = current doc"
    }
    sections { }
    section_versions { }
    section_comments { }
    document_snapshots { }
    templates { }
    chat_sessions { }
    users { }
    personas { }
    review_requests { }
    review_assignments { }
    review_comments { }
    review_summaries { }
    notifications { }
```

## Table reference

### Core generation
| Table | PK | Key columns | Notes |
|---|---|---|---|
| `generation_jobs` | `job_id` | `document_id`, `project_id`, `status` (pending/in_progress/completed/failed), `document_type`, `review_status` (draft/under_review/revision_requested/approved/rejected), `total_sections`, `completed_sections`, `user_inputs_json` | One per generate request. `completed_sections` is bumped with **atomic SQL** to avoid lost updates across wave workers. |
| `sections` | `section_id` | `job_id` FK, `section_key`, `section_title`, `order_index`, `status`, `current_version`, `version_hash` | `version_hash` caches MD5(section:version) for the preview. |
| `section_versions` | `version_id` | `section_id` FK, `version_number`, `content` (Markdown), `word_count`, `generation_prompt`, `generation_model`, `is_accepted`, `trigger_type` (ai_generation/ai_regeneration/manual_edit/review_comment/static), `edited_by` | **Immutable, append-only.** Full history; regeneration adds a new numbered version. |
| `section_comments` | `comment_id` | `section_id` FK, `version_number`, `comment_text`, `comment_type` (edit_request/approval/rejection/note), `status` | User edit requests that drive regeneration. |
| `document_snapshots` | `snapshot_id` | `job_id` FK, `label`, `trigger_type` (manual/review_agent/auto/manual_html), `section_refs` (JSON), `author`, `html_content` | Point-in-time checkpoints. `manual_html` snapshots store the raw edited HTML verbatim. |
| `templates` | `template_id` | `name`, `document_type`, `sections_config` (JSON), `system_instructions`, `is_system` | System templates auto-refreshed from `templates/*.json` at startup; user templates never overwritten. |

### Projects
| Table | PK | Notes |
|---|---|---|
| `projects` | `project_id` | The intake form. ~40 columns incl. the 12 Figma fields (pain_points, opportunities, business_justification, deadline, integration_requirement, assumptions, approval_matrix, future_roadmap, scalability_considerations, innovation_objectives, sustainability_esg, project_type). `status`: draft/ready/generating/completed. `job_id` = legacy "most recent job" alias. |
| `derived_data` | `project_id` (FK/PK) | 1:1 with project, cascade delete. 12 AI-derived fields (current_challenges, to_be_process, success_criteria, business/functional/non-functional requirements, industry_benchmarks, workflow, analytics_requirements, systems_involved, data_sources, constraints_dependencies). |

### Chat
| Table | PK | Notes |
|---|---|---|
| `chat_sessions` | `session_id` | `project_id`, `job_id`, `document_type`, `phase` (context/generating/review), `messages_json`, `pending_json` (op awaiting confirmation). |

### Review module
| Table | PK | Notes |
|---|---|---|
| `users` | `user_id` | email-unique; role Admin/Project Manager/Contributor/Viewer. Auto-registered when added as a reviewer. |
| `personas` | `persona_id` | 5 system personas seeded on first use + user custom (owner_email). |
| `review_requests` | `review_id` | One "share for review" on a job; status open/completed/cancelled. |
| `review_assignments` | `assignment_id` | One reviewer + per-reviewer status (shared→reviewing→accepted/rejected/revision_requested). |
| `review_comments` | `comment_id` | Human or AI (`source`), section-anchored, **threaded via `parent_id`**, `applied_section_comment_id` links to the regeneration it triggered. |
| `review_summaries` | `summary_id` | Cached persona-wise AI summary; `comments_fingerprint` gates staleness. |
| `notifications` | `notification_id` | In-app bell; types review_shared/renotified/responded/comment_added/comments_kept/comment_applied. Keyed by `recipient_email`. |

## Migrations — no Alembic

- `get_engine()` runs `Base.metadata.create_all()` (creates missing **tables**) then
  **`_migrate_sqlite_columns()`** — a SQLite-only auto-migrator that `PRAGMA table_info`s each table
  and issues `ALTER TABLE … ADD COLUMN` for any ORM column missing from the live table (identifier
  allow-listed against a strict regex to defend against DDL injection).
- **Consequence:** on SQLite, adding a *nullable* column to a model is safe with zero migration work.
  **On production (Databricks SQL / PostgreSQL) this does not run** — schema changes there require a
  manual `ALTER TABLE` (memory notes several: `document_snapshots.author`, `.html_content`).

## Concurrency & locking (SQLite dev only)

`db._make_engine()` is tuned for the wave-parallel writers:
- **QueuePool** (never StaticPool — StaticPool shares one connection across threads → corruption).
- `PRAGMA journal_mode=WAL`, `busy_timeout=30000`, `synchronous=NORMAL`, `foreign_keys=ON`.
- **`@retry_on_locked`** decorator re-runs a whole write transaction on a transient
  "database is locked" (applied to `start_job` and `_persist_section_version`). Never wraps the LLM call.
- **Golden rule:** never run two server processes against one SQLite file. Production (Databricks SQL)
  has no single-writer lock, so this entire class of issue is dev-only.

## Ownership

- A **project** owns its `derived_data` (cascade) and many `generation_jobs`.
- A **job** owns its `sections` → `section_versions` / `section_comments` (cascade), plus snapshots,
  reviews. Deleting a project does **not** cascade to jobs (jobs reference `project_id` but are not a
  declared cascade relationship) — see `DELETE /api/projects/{id}` for actual cleanup behavior.
