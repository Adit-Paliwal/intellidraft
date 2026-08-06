"""
API Contract Test — regression safety net for the FastAPI server.
==================================================================
Runs a scripted flow against a live server and records, per step:
  - HTTP status code
  - response shape (sorted top-level JSON keys, or content-type for non-JSON)

Values (UUIDs, timestamps) are intentionally ignored — only the CONTRACT
(status + shape) must stay stable across changes. The golden was originally
frozen from the retired Flask server during the FastAPI migration and is now
the reference for ANY route change: run --compare before merging; re-record
(--record) only when a contract change is deliberate.

Usage:
  python tests/api_contract.py http://127.0.0.1:7073 --compare
  python tests/api_contract.py http://127.0.0.1:7073 --record     # re-freeze

Golden file: tests/contract_golden.json
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import requests

GOLDEN = Path(__file__).parent / "contract_golden.json"

AUTHOR   = {"X-User-Email": "author@test.com",    "X-User-Name": "Test Author"}
REVIEWER = {"X-User-Email": "reviewer1@test.com", "X-User-Name": "Reviewer One"}

# A generation job that exists in the local dev DB (RFP Proj). Steps that
# depend on it are skipped gracefully (recorded as SKIP) if it's absent.
KNOWN_JOB = "b969e97b-a4c7-47fc-b28f-c2821b17dd75"


def shape(resp) -> dict:
    """Reduce a response to its contract: status + shape."""
    out = {"status": resp.status_code}
    ctype = resp.headers.get("content-type", "").split(";")[0]
    if ctype == "application/json":
        try:
            body = resp.json()
        except Exception:
            out["shape"] = "INVALID_JSON"
            return out
        if isinstance(body, dict):
            out["shape"] = sorted(body.keys())
        elif isinstance(body, list):
            out["shape"] = f"list[{len(body) and 'items' or 'empty'}]"
        else:
            out["shape"] = type(body).__name__
    else:
        out["shape"] = ctype
    return out


def run_flow(base: str) -> dict:
    """Execute every step; return {step_name: {status, shape}}."""
    s = requests.Session()
    r: dict = {}

    def step(name, method, path, expect_skip=False, **kw):
        kw.setdefault("timeout", 60)
        try:
            resp = s.request(method, base + path, **kw)
            r[name] = shape(resp)
            return resp
        except Exception as e:
            r[name] = {"status": "ERROR", "shape": str(e)[:80]}
            return None

    # ── Core / ingestion ──────────────────────────────────────────────────────
    step("health",        "GET", "/api/health")
    step("form_fields",   "GET", "/api/form-fields")
    step("templates",     "GET", "/api/templates")
    step("doc_404",       "GET", "/api/document/no-such-doc")
    step("doc_status_404","GET", "/api/document/no-such-doc/status")
    step("upload_nofile", "POST", "/api/upload")                     # 400
    step("upload_badext", "POST", "/api/upload",
         files={"file": ("evil.exe", b"MZ", "application/octet-stream")})  # 415

    # ── Projects ──────────────────────────────────────────────────────────────
    step("projects_list",  "GET", "/api/projects")
    step("projects_filter","GET", "/api/projects?business_unit=Digital&status=draft&page=1&per_page=5")
    step("projects_stats", "GET", "/api/projects/stats")
    step("project_404",    "GET", "/api/projects/no-such-project")

    draft = step("draft_create", "POST", "/api/projects/draft", json={
        "project_name": "Contract Test", "business_unit": "Digital",
        "pain_points": "x", "deadline": "2026-12-31", "project_type": "internal",
    })
    pid = (draft.json().get("project_id") if draft is not None and draft.status_code == 201 else str(uuid.uuid4()))

    step("draft_idempotent", "POST", "/api/projects/draft", json={"project_id": pid})
    step("draft_bad_uuid",   "POST", "/api/projects/draft", json={"project_id": "not-a-uuid"})  # 400
    step("project_get",      "GET",  f"/api/projects/{pid}")
    step("project_patch",    "PATCH", f"/api/projects/{pid}", json={"problem_statement": "p"})
    step("project_patch_empty", "PATCH", f"/api/projects/{pid}", json={})                        # 400
    step("project_validate", "POST", f"/api/projects/{pid}/validate")
    step("project_data",     "GET",  f"/api/projects/{pid}/data")
    step("data_ingested_put","PUT",  f"/api/projects/{pid}/data/ingested", json={"constraints": "c"})
    step("data_derived_put", "PUT",  f"/api/projects/{pid}/data/derived", json={"workflow": "w"})
    step("project_documents","GET",  f"/api/projects/{pid}/documents")
    step("create_invalid",   "POST", "/api/projects", json={"project_name": "only-name"})        # 422

    # ── Generation error paths (no LLM calls) ────────────────────────────────
    step("gen_job_404",      "GET",  "/api/generate/no-such-job")                    # 404
    step("gen_section_404",  "GET",  "/api/generate/j/section/no-such-section")      # 404
    step("gen_patch_empty",  "PATCH","/api/generate/j/section/s", json={})           # 400
    step("gen_preview_404",  "GET",  "/api/generate/no-such-job/preview")            # 404
    step("gen_export_404",   "GET",  "/api/generate/no-such-job/export?format=docx") # 404
    step("gen_snapshots",    "GET",  "/api/generate/no-such-job/snapshots")

    # ── Users & personas ──────────────────────────────────────────────────────
    step("users_list",   "GET",  "/api/users")
    step("user_upsert",  "POST", "/api/users", json={"email": "contract@test.com", "name": "Contract"})
    step("user_no_email","POST", "/api/users", json={"name": "x"})                   # 400
    step("personas_list","GET",  "/api/personas", headers=AUTHOR)
    p = step("persona_create", "POST", "/api/personas",
             json={"name": "Contract Persona", "description": "d"}, headers=AUTHOR)
    persona_id = (p.json().get("persona_id") if p is not None and p.status_code == 201 else "missing")
    step("persona_update", "PUT",    f"/api/personas/{persona_id}", json={"description": "d2"})
    step("persona_delete", "DELETE", f"/api/personas/{persona_id}")
    step("persona_del_404","DELETE", "/api/personas/no-such-persona")                # 404

    # ── Notifications ─────────────────────────────────────────────────────────
    step("notif_list",      "GET",  "/api/notifications", headers=AUTHOR)
    step("notif_no_header", "GET",  "/api/notifications")                            # 400
    step("notif_read",      "POST", "/api/notifications/read", json={}, headers=AUTHOR)

    # ── Review module ─────────────────────────────────────────────────────────
    step("review_sent",      "GET", "/api/review/sent", headers=AUTHOR)
    step("review_received",  "GET", "/api/review/received", headers=REVIEWER)
    step("review_no_header", "GET", "/api/review/sent")                              # 400
    step("review_404",       "GET", "/api/review/no-such-review")                    # 404
    step("review_share_404", "POST", "/api/review/share",
         json={"job_id": "no-such-job", "reviewers": [{"email": "a@b.c"}]}, headers=AUTHOR)  # 404

    share = step("review_share", "POST", "/api/review/share",
                 json={"job_id": KNOWN_JOB, "reviewers": [{"email": "reviewer1@test.com"}],
                       "message": "contract test"}, headers=AUTHOR)
    rid = (share.json().get("review_id") if share is not None and share.status_code == 201 else "missing")
    step("review_workspace", "GET",  f"/api/review/{rid}", headers=REVIEWER)
    c = step("review_comment", "POST", f"/api/review/{rid}/comments",
             json={"text": "contract comment"}, headers=REVIEWER)
    cid = (c.json().get("comment_id") if c is not None and c.status_code == 201 else "missing")
    step("comment_resolve", "PATCH", f"/api/review/comments/{cid}", json={"resolved": True}, headers=REVIEWER)
    step("comment_delete",  "DELETE", f"/api/review/comments/{cid}", headers=REVIEWER)
    step("review_respond",  "POST", f"/api/review/{rid}/respond",
         json={"action": "accepted"}, headers=REVIEWER)
    step("review_respond_bad", "POST", f"/api/review/{rid}/respond",
         json={"action": "bogus"}, headers=REVIEWER)                                 # 400
    step("review_renotify",  "POST", f"/api/review/{rid}/renotify")
    step("review_summaries", "GET",  f"/api/review/{rid}/summaries")

    # ── Cleanup ───────────────────────────────────────────────────────────────
    step("project_delete",     "DELETE", f"/api/projects/{pid}")                     # 204
    step("project_delete_404", "DELETE", f"/api/projects/{pid}")                     # 404

    return r


def main():
    if len(sys.argv) < 3 or sys.argv[2] not in ("--record", "--compare"):
        print(__doc__)
        sys.exit(2)
    base, mode = sys.argv[1].rstrip("/"), sys.argv[2]

    results = run_flow(base)

    if mode == "--record":
        GOLDEN.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"Recorded {len(results)} steps -> {GOLDEN}")
        for k, v in results.items():
            print(f"  {v['status']:>4}  {k}")
        return

    golden = json.loads(GOLDEN.read_text(encoding="utf-8-sig"))   # tolerate BOM
    failures = []
    for name, want in golden.items():
        got = results.get(name)
        if got is None:
            failures.append(f"{name}: MISSING (not executed)")
        elif got["status"] != want["status"]:
            failures.append(f"{name}: status {got['status']} != {want['status']} (shape got={got['shape']})")
        elif got["shape"] != want["shape"]:
            failures.append(f"{name}: shape {got['shape']} != {want['shape']}")
    extra = set(results) - set(golden)
    if extra:
        print(f"note: {len(extra)} steps not in golden: {sorted(extra)}")

    if failures:
        print(f"CONTRACT FAILURES ({len(failures)}/{len(golden)}):")
        for f in failures:
            print("  FAIL", f)
        sys.exit(1)
    print(f"CONTRACT OK — {len(golden)} steps match (status + shape).")


if __name__ == "__main__":
    main()
