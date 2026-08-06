"""
Job runner — standalone entry point that generates ONE document job.
====================================================================
This is the SAME body executed by:
  - the local `GENERATION_BACKEND=subprocess` backend (Approach B mirrored on your
    machine: each job runs in its own OS process), and
  - a Databricks Job Python task (Approach B in production).

Section-level parallelism (up to GENERATION_CONCURRENCY sections at once) happens
INSIDE _run_generation_job, so it works identically here — in a subprocess, in a
Databricks Job, or in an in-process thread. This process is just a thin wrapper
that loads config and calls it.

Usage:
    python -m generation.job_runner <job_id>          # local subprocess backend
    # Databricks Job (python task): entry point = generation/job_runner.py,
    #   parameter = <job_id>
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ── Bootstrap: Data_Ingestion/ on sys.path so `generation.*` imports resolve ──
_BASE = Path(__file__).parent.parent.resolve()   # …/Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# Load .env so this standalone process gets the same config as the API.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_BASE / ".env", override=False)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("job_runner")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].strip():
        print("usage: python -m generation.job_runner <job_id>", file=sys.stderr)
        return 2

    job_id = argv[1].strip()
    logger.info("[job_runner] pid=%s starting job %s", os.getpid(), job_id)

    # Imported here (not at module top) so a bad-args invocation never pays the
    # cost of importing the DB / LLM stack.
    from generation.generation_service import _run_generation_job

    _run_generation_job(job_id)   # marks the job completed/failed internally
    logger.info("[job_runner] pid=%s finished job %s", os.getpid(), job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
