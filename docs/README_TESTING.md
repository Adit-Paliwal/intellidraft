# Testing

Suites live in `Data_Ingestion/tests/`. Runner: **pytest 9** (pinned). Full plan: `tests/TEST_PLAN.md`.

```bash
cd Data_Ingestion
../env/Scripts/python.exe -m pytest tests -q     # unit + integration (integration auto-skips if no server)
```

## Suites

| File | Type | Needs server? | Needs LLM? | Covers |
|---|---|---|---|---|
| `test_unit.py` | Unit + edge cases | no | no | ontology matching/caps/unicode, persona-review validator, `_extract_json`, fingerprint, prompt integrity, rollup, perf sanity |
| `test_validation_agent.py` | Unit + sample evaluation | no | no (LLM judge optional) | the scoring engine itself; prints a sample report |
| `test_api_integration.py` | Integration + negative-path | **yes** (`INTELLIDRAFT_BASE`, default `http://127.0.0.1:7073`) | no | smoke, negative, unicode, 409, concurrency race, SSE, admin gate |
| `api_contract.py --compare` | Regression contract (62 steps) | yes | no | status + response shape vs `contract_golden.json` |
| `load_test.py` | Performance | partial | no | p95 latency / error rate under concurrency |

> The integration contract has a known baseline: some review steps 404 against a fresh DB because a
> hardcoded `KNOWN_JOB` fixture isn't present — that is **not** a regression (verified via real-job e2e).
> Run integration against a server on the configured port; **never run two servers on one SQLite DB**.

## The Validation Agent as a test oracle

`agents/validation_agent.py` doubles as a QA harness (`tests/validation_agent.py` is a re-export shim).
It scores generated output vs ground truth on a weighted 0–100 (correctness 40 / completeness 20 /
format 15 / edge 15 / robustness 10); **PASS iff ≥ 80 and no CRITICAL**. Also exposed at runtime via
`POST /api/generate/{job_id}/validate`. It runs deterministically offline; `use_llm=True` adds a Gemini
semantic judge. See [README_AI_PIPELINE.md](README_AI_PIPELINE.md#6-validation-agent).

## Edge-case matrix (from TEST_PLAN.md)

Empty inputs · null/None · very large inputs (1 MB glossary scan, >50 MB upload → 413, 8K context
truncation) · unicode (ΔT glossary key, Devanagari + emoji names, ₹) · duplicates (duplicate
`project_code` → 409, reviewer dedupe) · out-of-range (`page=-5`, clamped) · invalid formats (non-UUID
→ 400, malformed JSON → 400 not 422, bad ext → 415) · concurrency (20 parallel idempotent draft
creations; parallel notification reads) · timeouts (tenacity transient-only; bounded SSE) · partial
payloads (truncated JSON, persona-review retry contract).

## Exit criteria

1. `pytest tests -q` green (skips only for a down server).
2. `api_contract.py --compare` → contract parity.
3. Validation-agent sample evaluation ≥ 80, correctly flagging the planted missing/wrong/extra fields.
4. Load sanity: zero 5xx at concurrency 20 on hot endpoints.

## Gaps / notes

- **No mocking of the LLM** in unit tests — LLM-touching paths are covered only by the (skippable)
  integration suite and manual e2e. There is no CI-gated test that exercises `generate_section` with a
  stubbed provider, so prompt/response-shape regressions in generation aren't caught automatically.
- **No frontend tests** (frontend removed).
- The regression `contract_golden.json` was surgically pruned rather than re-recorded (to avoid baking
  in fresh-DB review 404s) — keep that in mind when regenerating it.
