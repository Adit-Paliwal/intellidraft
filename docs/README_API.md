# API Documentation

- **Base URL (local):** `http://localhost:7071/api`
- **Interactive docs:** `/docs` (Swagger, auto-generated). `/api/docs` → 307 redirect to `/docs`.
- **Root `/`** → `{"service":"Intellidraft API","docs":"/docs","health":"/api/health"}` (no frontend).
- **Auth:** none enforced. Identity, where used (review/notifications), is read from
  `X-User-Email` / `X-User-Name` request headers. See [README_SECURITY.md](README_SECURITY.md).
- **Error contract:** `{"error": "..."}` with a meaningful status; malformed JSON bodies → **400**
  (not FastAPI's default 422 — overridden in `main.py`).
- **Reference:** all routes are `def` handlers in `Data_Ingestion/main.py`; a Postman collection
  (`IntelliDraft_API.postman_collection.json`) and `IntelliDraft_API_Endpoints.xlsx` ship at repo root.

> **DB-first rule:** POST/PATCH responses return only ids/counts. Read business data back with GET.

## Endpoint catalogue (~64 routes, grouped)

### System / admin
| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Service info JSON |
| GET | `/api/health` | Liveness probe (used by Docker HEALTHCHECK) |
| GET | `/api/docs` | Redirect → Swagger |
| POST | `/api/admin/reset-db` | **Dev only** — wipe+recreate SQLite. Gated by `ENABLE_ADMIN_ENDPOINTS=true` (else 403) |
| GET | `/api/form-fields` | Intake form field definitions |

### Ingestion
| Method | Route | Purpose |
|---|---|---|
| POST | `/api/upload` | Upload + parse a file (multipart `file`). ≤50 MB; ext must be pdf/doc(x)/ppt(x)/xls(x) else 415. Returns `document_id` + parse summary |
| GET | `/api/document/{doc_id}` | Full parsed `meta.json` |
| GET | `/api/document/{doc_id}/status` | Lightweight index record |

### Extraction / derivation
| Method | Route | Purpose |
|---|---|---|
| POST | `/api/extract-project-data` | LLM-extract 12 intake fields from `document_ids`; returns `missing_required[]` |
| POST | `/api/projects/{id}/derive-fields` | LLM-derive the 12 extended `derived_data` fields |
| POST | `/api/projects/{id}/validate` | Validate project readiness |

### Projects
| Method | Route | Purpose |
|---|---|---|
| POST | `/api/projects/draft` | Persist extracted fields immediately (no validation) → `draft` |
| POST | `/api/projects` | Create a project in one shot (all required fields) |
| GET | `/api/projects` | List projects (filters: business_unit, review_status; paginated) |
| GET | `/api/projects/stats` | Dashboard KPIs: total / under_draft / under_review / approved |
| GET | `/api/projects/{id}` | Full project (lifecycle meta) |
| PATCH | `/api/projects/{id}` | Partial update (set fields, document_type) |
| DELETE | `/api/projects/{id}` | Delete a project |
| GET | `/api/projects/{id}/data` | Ingested + derived fields (form read-back) |
| PUT | `/api/projects/{id}/data/ingested` | Overwrite ingested fields |
| PUT | `/api/projects/{id}/data/derived` | Overwrite derived fields (manual edit) |
| GET | `/api/projects/{id}/documents` | One entry per doc type generated (latest job per type) |

### Generation
| Method | Route | Purpose |
|---|---|---|
| POST | `/api/generate/project/{project_id}` | **Start generation** from a saved project. Idempotent per (project, doc_type) |
| GET | `/api/generate/{job_id}` | Poll job state (sections + current content). Uses `selectinload` (3 queries) |
| GET | `/api/generate/{job_id}/stream` | **SSE** progress stream (the only async route) |
| GET | `/api/generate/{job_id}/section/{section_id}` | One section, all versions + comments |
| PATCH | `/api/generate/{job_id}/section/{section_id}` | Manual edit → new `manual_edit` version |
| POST | `/api/generate/{job_id}/section/{section_id}/comment` | Add an edit-request comment |
| POST | `/api/generate/{job_id}/section/{section_id}/regenerate` | Regenerate a section (optional comment) → new version; busts preview cache |
| POST | `/api/generate/{job_id}/section/{section_id}/accept` | Mark a version accepted |
| GET | `/api/generate/{job_id}/preview` | Markdown preview (reads DB directly) |
| GET | `/api/generate/{job_id}/preview/html` | Styled HTML preview (markdown2 + `doc_style` CSS) |
| POST | `/api/generate/{job_id}/preview/save` | Save whole-document edited HTML → `manual_html` snapshot |
| POST | `/api/generate/{job_id}/snapshot` | Create a version checkpoint |
| GET | `/api/generate/{job_id}/snapshots` | List snapshots |
| GET | `/api/generate/{job_id}/snapshot/{snapshot_id}` | One snapshot incl. `html_content` |
| POST | `/api/generate/{job_id}/snapshot/{snapshot_id}/restore` | Restore a snapshot |
| POST | `/api/generate/{job_id}/validate` | Run the validation agent (409 if job not completed) |
| GET | `/api/generate/{job_id}/export?format=docx\|pdf\|md` | Download the assembled document |

### Templates
| Method | Route | Purpose |
|---|---|---|
| GET | `/api/templates` | List templates (filter by document_type) |
| POST | `/api/templates` | Create a user template |
| POST | `/api/templates/{template_id}/reseed` | Re-seed a system template from its JSON file |

### Chat studio
| Method | Route | Purpose |
|---|---|---|
| POST | `/api/chat/init` | Get-or-create a chat session for (project, doc_type) |
| POST | `/api/chat/message` | Send a message (keyword-routed; two-step confirm for modify) |
| GET | `/api/chat/{session_id}/history` | Full message history |
| POST | `/api/chat/{session_id}/upload` | Upload a file into a chat; may trigger impact analysis |

### Users & personas
| Method | Route | Purpose |
|---|---|---|
| GET / POST | `/api/users` · `/api/users` | List / upsert user |
| DELETE | `/api/users/{user_id}` | Delete user |
| GET / POST | `/api/personas` · `/api/personas` | List (system + own) / create custom persona |
| PUT / DELETE | `/api/personas/{persona_id}` | Edit / delete custom persona (system personas protected) |

### Review workflow
| Method | Route | Purpose |
|---|---|---|
| POST | `/api/review/share` | Share a job for review with named reviewers → marks doc `under_review` |
| GET | `/api/review/sent` | Reviews I requested (identity via header) |
| GET | `/api/review/received` | Reviews shared with me |
| GET | `/api/review/{review_id}` | Review workspace (sections + threaded comments + AI summaries) |
| POST | `/api/review/{review_id}/comments` | Add a comment (section-anchored / threaded) |
| PATCH / DELETE | `/api/review/comments/{comment_id}` | Edit / delete (author only) |
| POST | `/api/review/comments/{comment_id}/apply` | Apply a comment → regenerate that section |
| POST | `/api/review/{review_id}/respond` | Reviewer verdict (accepted/rejected/revision_requested) → rollup |
| POST | `/api/review/{review_id}/renotify` | Nudge pending reviewers |
| POST | `/api/review/{review_id}/ai-review` | AI persona review (not persisted until kept) |
| POST | `/api/review/{review_id}/ai-review/keep` | Persist selected AI comments |
| POST | `/api/review/{review_id}/summarize` | Persona-wise AI summaries (fingerprint-cached) |
| GET | `/api/review/{review_id}/summaries` | Cached summaries |

### Notifications
| Method | Route | Purpose |
|---|---|---|
| GET | `/api/notifications` | My notifications + unread count (identity via header) |
| POST | `/api/notifications/read` | Mark specific / all as read |

## Typical usage flow

```
1. POST /api/upload                       → document_id
2. POST /api/extract-project-data         → auto-fill intake
3. POST /api/projects/draft               → persist (draft)
4. PATCH /api/projects/{id}               → complete fields + set document_type
5. POST /api/projects/{id}/derive-fields  → extended analysis
6. POST /api/generate/project/{id}        → start job
7. GET  /api/generate/{job_id}            → poll until "completed"
8. POST /api/generate/{job_id}/validate   → QA score (optional)
9. POST /api/review/share                 → send for review (optional)
10. GET /api/generate/{job_id}/export?format=docx → download
```

## Execution path of a representative endpoint

`POST /api/generate/project/{id}` → `generation_service.start_job_from_project()` →
loads `Project` + `DerivedData` → idempotency check (return existing completed job) →
builds `user_inputs` → `start_job()` (insert `GenerationJob` + `Section` rows,
`@retry_on_locked`) → `_dispatch_generation()` (thread/subprocess/celery/sync) →
`_run_generation_job()` (wave-parallel `generate_section` → Gemini → `SectionVersion`) →
job `completed` + pre-warm preview. Client polls `GET /api/generate/{job_id}`.
