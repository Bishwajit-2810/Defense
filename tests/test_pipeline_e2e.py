import os
import subprocess
import pytest

@pytest.mark.timeout(300)
def test_pipeline_e2e_run_all():
    """
    Pipeline integration test to ensure posts flow through all workers 
    and successfully reach the analysis_results table without getting stuck.
    This would catch the 'analysis_results=0 loop' instantly.
    """
    repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    
    # We run run_all.py with --reset and --no-dashboard
    # It will start docker containers, workers, and load the 50 sample posts.
    # It prints 'all 50 posts analysed' when they reach the DB.
    
    proc = subprocess.Popen(
        ["python", "run_all.py", "--reset", "--no-dashboard"],
        cwd=repo_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True
    )
    
    success = False
    output = ""
    import time
    start_time = time.time()
    
    import select
    while time.time() - start_time < 240:
        reads, _, _ = select.select([proc.stdout], [], [], 1.0)
        if reads:
            line = proc.stdout.readline()
            if not line:
                break
            output += line
            if "all 50 posts analysed" in output:
                success = True
                break
            elif "fewer than 50 results so far" in line:
                success = False
                break
                
    proc.terminate()
    proc.wait(timeout=10)
    
    if not success:
        print(f"OUTPUT:\n{output}")
        pytest.fail("Pipeline failed to process all 50 posts (analysis_results loop bug)")
