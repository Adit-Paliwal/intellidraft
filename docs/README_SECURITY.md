# Security Review

> Scope: a code-level audit of the current backend. IntelliDraft is an internal Adani tool; several
> controls are intentionally light for internal use but are called out as risks for any wider exposure.

## 1. Authentication & authorization — ⚠️ the biggest gap

- **No backend authentication.** Routes are open. Identity is read from **`X-User-Email` /
  `X-User-Name`** request headers (see `main.py` CORS allow-headers, `review_service`, notifications).
  Entra ID SSO is described as a **frontend** concern; the backend performs **no token validation**.
- **No authorization checks** — roles exist in the `users` table (Admin/PM/Contributor/Viewer) but are
  not enforced. Any caller can act as any user by setting the header.
- Comment edit/delete does check `author_email == editor_email`, but that email is also caller-supplied.

**Risk:** anyone who can reach the API can read/modify any project, impersonate any reviewer, and
approve documents. **Do not expose this API publicly without adding real auth (validate the Entra
token server-side and derive identity from it).**

## 2. Secrets management

- **GCP key:** `Data_Ingestion/key.json` (gitignored). Mounted read-only in Docker; supplied as
  `GOOGLE_APPLICATION_CREDENTIALS_JSON` in Databricks. Loaded by `llm_provider._load_gemini_credentials`.
- **Databricks:** OAuth M2M inside Apps (no PAT); `DATABRICKS_CLIENT_ID/SECRET` injected by the platform.
- **`.env`** is gitignored; `.env.example` carries only placeholders. `app.yaml` explicitly says
  sensitive vars go in the Apps UI/secrets scope, never committed.
- ✅ No hardcoded credentials found in tracked source.

## 3. Injection surfaces

- **SQL:** SQLAlchemy ORM throughout — parameterized, no string-built SQL in the query path. The one
  dynamic DDL (`_migrate_sqlite_columns`) validates every table/column name against
  `^[A-Za-z_][A-Za-z0-9_]*$` before interpolation (defense against CWE-89). ✅
- **Prompt injection (LLM):** source-document text and user fields are inserted into prompts. A
  malicious uploaded document could attempt to steer generation ("ignore instructions…"). There are
  **no explicit prompt-injection guardrails**; mitigations are indirect (structured output +
  `json_mode` for JSON tasks, `_validate_persona_review` shape validation, section-scoped tasks). The
  generated content is a *draft for human review*, which limits blast radius, but treat generated
  text as untrusted. **Recommendation:** add input framing/*delimiters* and an output policy check.
- **Path traversal:** `parser_factory.parse_document` calls `Path(...).resolve()` and `is_file()`
  before access (CWE-22 hardening). Uploads use `werkzeug.secure_filename`. ✅
- **XSS:** the app is API-only (no server-rendered UI), but `preview/html` and `preview/save` store
  and return **raw HTML** (`manual_html` snapshots). Any client that renders that HTML must sanitize
  it — stored HTML is verbatim and untrusted.

## 4. File uploads

- Extension allow-list (pdf/doc(x)/ppt(x)/xls(x)) → 415 otherwise; **50 MB** cap → 413;
  `secure_filename`; parsed in a temp file that is unlinked in a `finally`. ✅
- No content-type sniffing beyond extension, and no AV scan — acceptable for internal use, note for
  wider exposure.

## 5. PII / data handling

- Reviewer emails/names are stored (users, assignments, comments, notifications). No special-category
  PII by design. Emails are normalized to lowercase.
- Parsed document content (including any PII inside uploads) is stored in `meta.json` and sent to
  **Gemini (Vertex AI)** — ensure the Vertex project/region satisfies data-residency requirements
  (`VERTEX_LOCATION=us-central1` by default — review for India-residency needs).
- Vision analysis sends image bytes to Gemini when `VISION_ENABLED=true`.

## 6. Rate limiting & DoS

- **No application-level rate limiting.** Generation is expensive (many Gemini calls). An unauthenticated
  caller can trigger large jobs. The threadpool cap (`THREADPOOL_TOKENS`) and Vertex quota are the only
  natural limits. Add a gateway/rate-limit in front for any exposed deployment.

## 7. CORS & admin surface

- CORS defaults to `*` (fine for same-origin internal use; set `CORS_ALLOW_ORIGINS` in prod).
- The destructive `POST /api/admin/reset-db` is hard-gated by `ENABLE_ADMIN_ENDPOINTS=true` (403 otherwise).

## 8. Dependency hygiene — ✅ strong

`requirements.txt` is pinned and carries **explicit CVE-remediation notes**: `python-dotenv 1.2.2`,
`aiohttp 3.14.3`, `starlette 1.0.1`, `PyJWT 2.13.0`, `joserfc 1.6.8`, `nltk 3.10.0`, `litellm 1.93.0`.
A vulnerability-remediation spreadsheet (`IntelliDraft_Vulnerability_Remediation_*.xlsx`) is tracked.
The Dockerfile runs `apt-get upgrade` and purges the build toolchain to shrink the CVE surface.

## Priority recommendations
1. **Add real backend auth** (validate Entra tokens; derive identity server-side) before any exposure.
2. **Sanitize stored/returned HTML** in the preview save/serve path on the consuming client.
3. **Add prompt-injection framing** and treat generated + parsed text as untrusted.
4. **Add rate limiting** in front of generation.
5. Confirm **Vertex AI data residency** matches Adani policy.
