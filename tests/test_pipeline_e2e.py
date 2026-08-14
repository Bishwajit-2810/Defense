"""End-to-end pipeline check — DESTRUCTIVE, and therefore opt-in.

This test shells out to ``run_all.py --reset``, which is a Redis ``FLUSHALL``
plus a truncate of Postgres and ClickHouse, and then starts the full Docker +
Ollama stack. Running it wipes whatever is in the developer's live dev
environment.

It used to run on a bare ``pytest``. That made a plain test run destructive to
any machine with a live stack, and — because it also needs a full stack it
usually cannot get — it failed after burning ~130 s, which was 85% of the
suite's total wall clock. AUDIT_PASS7 records it as "not run here because
--reset is destructive to a running environment"; this is that decision
expressed in the code instead of in prose, so nobody has to know the prose.

Run it deliberately:

    RUN_DESTRUCTIVE_E2E=1 pytest tests/test_pipeline_e2e.py
"""

import os
import select
import subprocess
import sys
import time

import pytest

#: Opt-in switch. Absent = skip, because the default has to be the safe one.
_OPT_IN = "RUN_DESTRUCTIVE_E2E"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.destructive,
    pytest.mark.skipif(
        os.environ.get(_OPT_IN, "").strip().lower() not in ("1", "true", "yes"),
        reason=(
            f"destructive: runs `run_all.py --reset` (Redis FLUSHALL + Postgres/"
            f"ClickHouse truncate) and needs a full Docker + Ollama stack. "
            f"Set {_OPT_IN}=1 to run it."
        ),
    ),
]


@pytest.mark.timeout(300)
def test_pipeline_e2e_run_all():
    """Posts flow through every worker and reach analysis_results.

    Catches the 'analysis_results=0 loop' — the pipeline appearing healthy while
    nothing is actually landing in the results table.
    """
    repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    proc = subprocess.Popen(
        # The interpreter running the tests, not whatever `python` resolves to
        # on PATH — the venv is where this project's deps live.
        [sys.executable, "run_all.py", "--reset", "--no-dashboard"],
        cwd=repo_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    success = False
    output = ""
    start_time = time.time()

    try:
        while time.time() - start_time < 240:
            reads, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not reads:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            output += line
            if "all 50 posts analysed" in output:
                success = True
                break
            if "fewer than 50 results so far" in line:
                success = False
                break
    finally:
        # `start_new_session=True` puts run_all.py in its own process group, and
        # it spawns workers. Terminating only the parent orphans them, which is
        # how a failed run left workers holding Redis consumer groups.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    if not success:
        print(f"OUTPUT:\n{output}")
        pytest.fail("Pipeline failed to process all 50 posts (analysis_results loop bug)")
