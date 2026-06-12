#!/usr/bin/env python3
"""run_all.py — one-command launcher for the Smart Layer.

Run it with uv (recommended):

    uv run run_all.py                 # core pipeline + dashboard, load the 50 posts
    uv run run_all.py --with-agents   # also start the agents + 3 MCP servers
    uv run run_all.py --reset         # wipe prior data, then load the 50 posts fresh
    uv run run_all.py --no-load       # don't push the sample posts
    uv run run_all.py --no-dashboard  # skip serving the web UI
    uv run run_all.py --down          # on exit, also `docker compose down`

It brings up the Docker datastores, waits for them, initialises ClickHouse,
checks Ollama, launches the 5 workers + API, serves the dashboard, and (by
default) pushes the 50 sample posts. Press Ctrl-C to stop everything it started.

Equivalent manual steps live in easy_run.md / run.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEPLOY = REPO / "deploy"
INFRA = ["postgres", "redis", "clickhouse", "minio"]
API_PORT = 8001
DASH_PORT = 8080
OLLAMA_URL = "http://localhost:11434"

_C = {"cyan": "\033[36m", "yellow": "\033[33m", "red": "\033[31m", "green": "\033[32m", "off": "\033[0m"}
_procs: list[tuple[str, subprocess.Popen]] = []
_started_ollama = False


def log(m): print(f"{_C['cyan']}[run_all]{_C['off']} {m}", flush=True)
def ok(m): print(f"{_C['green']}[run_all]{_C['off']} {m}", flush=True)
def warn(m): print(f"{_C['yellow']}[run_all] WARNING:{_C['off']} {m}", flush=True)
def die(m): print(f"{_C['red']}[run_all] ERROR:{_C['off']} {m}", file=sys.stderr, flush=True); sys.exit(1)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def docker_field(svc: str, fmt: str) -> str:
    r = subprocess.run(["docker", "inspect", "-f", fmt, f"deploy-{svc}-1"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def ipof(svc: str) -> str:
    ip = docker_field(svc, "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")
    if not ip:
        die(f"could not resolve container IP for deploy-{svc}-1 (is it running?)")
    return ip


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def pg_count() -> int:
    r = subprocess.run(
        ["docker", "exec", "deploy-postgres-1", "psql", "-U", "defense", "-d", "defense",
         "-tAc", "select count(*) from analysis_results"],
        capture_output=True, text=True)
    try:
        return int(r.stdout.strip() or "0")
    except ValueError:
        return 0


def reap_stale() -> None:
    """Kill leftover service processes from a previous run before starting.

    A run_all that was hard-killed (or whose children were orphaned) leaves the
    pipeline + API still running. The stale API keeps port 8001 bound and points
    at the *old* Postgres container IP, so docker recreating Postgres gives the
    container a new IP while the orphaned API still dials the dead one — every
    request 500s with "Connection refused". We can't rely on the previous run's
    own cleanup(), so reap here: every service we launch runs as the repo venv
    python (cmdline[0] == .venv/bin/python), which uniquely tags our processes.
    """
    marker = str(REPO / ".venv" / "bin" / "python")
    me = os.getpid()
    victims: list[int] = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        pid = int(d.name)
        if pid == me:
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # process gone or not ours to read
        if not argv or not argv[0].decode("utf-8", "replace").startswith(marker):
            continue
        cmdline = b" ".join(argv).decode("utf-8", "replace")
        # Skip run_all.py itself and editor tooling (e.g. the black LSP server)
        # that merely happen to share the venv interpreter.
        if "run_all.py" in cmdline or "lsp_server" in cmdline:
            continue
        victims.append(pid)
    if not victims:
        return
    log(f"reaping {len(victims)} leftover service process(es) from a previous run …")
    for pid in victims:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    time.sleep(2)
    for pid in victims:
        if not Path(f"/proc/{pid}").exists():
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def start(name: str, cmd: list[str], env: dict, cwd: Path | None = None) -> subprocess.Popen:
    logf = open(f"/tmp/{name}.log", "ab")
    p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=env,
                         stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    _procs.append((name, p))
    log(f"started {name} (pid {p.pid}) → /tmp/{name}.log")
    return p


def cleanup(down: bool = False) -> None:
    if _procs:
        log("stopping host services…")
    for name, p in reversed(_procs):
        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass
    t0 = time.time()
    for _, p in _procs:
        try:
            p.wait(timeout=max(0.1, 5 - (time.time() - t0)))
        except Exception:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
    if _started_ollama:
        subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
    if down:
        log("docker compose down (keeping volumes)…")
        subprocess.run(["docker", "compose", "down"], cwd=str(DEPLOY))
    ok("stopped. (Docker infra %s)" % ("removed" if down else "left running"))


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #
def ensure_deps() -> str:
    """uv sync (best-effort) and return the interpreter to launch services with."""
    if shutil.which("uv"):
        log("uv sync …")
        if subprocess.run(["uv", "sync"], cwd=str(REPO)).returncode != 0:
            warn("`uv sync` failed — continuing with whatever is installed")
    else:
        warn("`uv` not found on PATH — assuming dependencies are already installed")
    venv_py = REPO / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else sys.executable


def start_infra() -> None:
    if not shutil.which("docker"):
        die("docker not found on PATH")
    log("starting datastores: " + ", ".join(INFRA))
    if subprocess.run(["docker", "compose", "up", "-d", *INFRA], cwd=str(DEPLOY)).returncode != 0:
        die("`docker compose up` failed")

    log("waiting for Postgres + ClickHouse to be healthy …")
    for _ in range(40):
        pg, ch = docker_field("postgres", "{{.State.Health.Status}}"), docker_field("clickhouse", "{{.State.Health.Status}}")
        if pg == "healthy" and ch == "healthy":
            ok("datastores healthy")
            break
        time.sleep(3)
    else:
        die("datastores did not become healthy in time (check `docker compose ps`)")

    log("creating ClickHouse tables …")
    sql = (REPO / "services/workers/assembler/clickhouse_init.sql").read_bytes()
    subprocess.run(
        ["docker", "exec", "-i", "deploy-clickhouse-1", "clickhouse-client",
         "--user", "defense", "--password", "defense", "--database", "defense", "--multiquery"],
        input=sql)


def ensure_ollama() -> None:
    global _started_ollama
    if http_ok(f"{OLLAMA_URL}/v1/models"):
        ok("Ollama is up")
        return
    if not shutil.which("ollama"):
        warn("Ollama not running and not installed — Stage-2 LLM calls will fail. "
             "Install from https://ollama.com and `ollama pull qwen2.5:7b qwen3-vl:4b`.")
        return
    log("starting `ollama serve` …")
    logf = open("/tmp/ollama.log", "ab")
    subprocess.Popen(["ollama", "serve"], stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    _started_ollama = True
    for _ in range(20):
        if http_ok(f"{OLLAMA_URL}/v1/models"):
            ok("Ollama is up")
            return
        time.sleep(1)
    warn("Ollama did not come up in time — check /tmp/ollama.log")


def build_env() -> dict:
    env = os.environ.copy()
    env.update({
        "REPO": str(REPO),
        "DATABASE_URL": f"postgresql+asyncpg://defense:defense@{ipof('postgres')}:5432/defense",
        "REDIS_URL": f"redis://{ipof('redis')}:6379/0",
        "CLICKHOUSE_URL": f"clickhouse://defense:defense@{ipof('clickhouse')}:9000/defense",
        "MINIO_ENDPOINT": f"http://{ipof('minio')}:9000",
        "MINIO_ACCESS_KEY": "minioadmin",
        "MINIO_SECRET_KEY": "minioadmin",
        "MINIO_BUCKET": "defense",
        "LLM_BACKEND": "local",
        "LOCAL_LLM_BASE_URL": f"{OLLAMA_URL}/v1",
        "LOCAL_LLM_API_KEY": "ollama",
        "LLM_A_LOCAL_MODEL": "qwen2.5:7b",
        "LLM_B_LOCAL_MODEL": "qwen2.5:7b",
        "VLM_LOCAL_MODEL": "qwen3-vl:4b",
        "MODEL_STUB_MODE": "true",
        "JWT_SECRET": "demo",
        "LOG_LEVEL": "INFO",
        "PYTHONPATH": str(REPO),
        # The API proxies /v1/agents/* to the agents service; its default
        # (http://agents:8010) is the compose hostname, which doesn't resolve
        # in host mode — so the host URL must be in the API's env too.
        "AGENTS_SERVICE_URL": "http://127.0.0.1:8010",
    })
    return env


def start_pipeline(py: str, env: dict) -> None:
    start("stage1", [py, "-m", "services.workers.stage1_nlp"], env, REPO)
    start("router", [py, "-m", "services.workers.router"], env, REPO)
    start("stage2", [py, "-m", "services.workers.stage2_llm"], env, REPO)
    start("assembler", [py, "__main__.py"], env, REPO / "services/workers/assembler")
    start("ingestion", [py, "-m", "services.ingestion"], env, REPO)
    start("api", [py, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                  "--port", str(API_PORT), "--log-level", "warning"], env, REPO / "services/api")

    log("waiting for the API to answer …")
    for _ in range(30):
        if http_ok(f"http://127.0.0.1:{API_PORT}/v1/health"):
            ok(f"API healthy on :{API_PORT}")
            return
        time.sleep(1)
    warn("API health check didn't pass — check /tmp/api.log")


def start_agents(py: str, env: dict) -> None:
    a = env.copy()
    a.update({
        "ANALYTICS_MCP_URL": "http://127.0.0.1:8110",
        "RETRIEVAL_MCP_URL": "http://127.0.0.1:8101",
        "INGEST_MCP_URL": "http://127.0.0.1:8102",
        "AGENTS_SERVICE_URL": "http://127.0.0.1:8010",
        "CLICKHOUSE_HOST": ipof("clickhouse"), "CLICKHOUSE_PORT": "9000",
        "CLICKHOUSE_DB": "defense", "CLICKHOUSE_USER": "defense", "CLICKHOUSE_PASSWORD": "defense",
        "ANALYTICS_MCP_STUB": "true", "RETRIEVAL_MCP_STUB": "true",
    })
    uvi = [py, "-m", "uvicorn", "--host", "127.0.0.1", "--log-level", "warning"]
    # analytics_mcp + agents use package-relative imports → module path from repo
    # root; retrieval/ingest use cwd-relative imports → run from their own dir.
    start("analytics_mcp", uvi + ["mcp.analytics_mcp.server:app", "--port", "8110"], a, REPO)
    start("retrieval_mcp", uvi + ["server:app", "--port", "8101"], a, REPO / "mcp/retrieval_mcp")
    start("ingest_mcp", uvi + ["server:app", "--port", "8102"], a, REPO / "mcp/ingest_mcp")
    start("agents", uvi + ["services.agents.main:app", "--port", "8010"], a, REPO)
    ok("agent layer started (analytics :8110, retrieval :8101, ingest :8102, agents :8010)")


def serve_dashboard(py: str, env: dict) -> None:
    # The dashboard defaults to the dev API on :8001 (dashboard/app.js), so we
    # just serve the static files — no patching needed.
    start("dashboard", [py, "-m", "http.server", str(DASH_PORT)], env, REPO / "dashboard")


def reset_data() -> None:
    # Must run BEFORE the workers start: FLUSHALL destroys the Redis consumer
    # groups the workers create at startup (workers also self-heal on NOGROUP,
    # but a pre-start reset avoids the error churn entirely).
    log("resetting data (Redis FLUSHALL + truncate) …")
    subprocess.run(["docker", "exec", "deploy-redis-1", "redis-cli", "FLUSHALL"], capture_output=True)
    subprocess.run(["docker", "exec", "deploy-postgres-1", "psql", "-U", "defense", "-d", "defense",
                    "-c", "TRUNCATE analysis_results, posts, jobs CASCADE;"], capture_output=True)


def load_posts(skip_if_loaded: bool) -> None:
    if skip_if_loaded and pg_count() > 0:
        ok(f"posts already loaded ({pg_count()} results) — skipping upload (use --reset to redo)")
        return

    log("uploading the 50 sample posts …")
    posts = json.loads((REPO / "posts_with_details.json").read_text())
    body = json.dumps({"posts": posts}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{API_PORT}/v1/posts/upload", data=body,
                                 headers={"Content-Type": "application/json", "X-API-Key": "demo"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            log(f"upload accepted: HTTP {r.status}")
    except Exception as e:
        warn(f"upload failed: {e} — check /tmp/api.log and /tmp/ingestion.log")
        return

    log("waiting for posts to drain into Postgres …")
    for _ in range(40):
        n = pg_count()
        print(f"  analysis_results={n}", flush=True)
        if n >= 50:
            ok("all 50 posts analysed")
            return
        time.sleep(3)
    warn("fewer than 50 results so far — check the worker logs in /tmp/*.log")


def banner(with_agents: bool, dashboard: bool) -> None:
    print()
    ok("════════════════════════════════════════════════════════════")
    ok("  Smart Layer is up.")
    print(f"   • API        →  http://127.0.0.1:{API_PORT}/v1/health")
    if dashboard:
        print(f"   • Dashboard  →  http://127.0.0.1:{DASH_PORT}   (log in with API key: demo)")
    if with_agents:
        print("   • Agents     →  POST http://127.0.0.1:%d/v1/agents/query" % API_PORT)
    print("   • Logs       →  /tmp/<service>.log")
    ok("  Press Ctrl-C to stop everything this script started.")
    ok("════════════════════════════════════════════════════════════")
    print()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="One-command launcher for the Smart Layer.")
    ap.add_argument("--with-agents", action="store_true", help="also start agents + 3 MCP servers")
    ap.add_argument("--no-dashboard", action="store_true", help="don't serve the web UI")
    ap.add_argument("--no-load", action="store_true", help="don't push the 50 sample posts")
    ap.add_argument("--reset", action="store_true", help="wipe prior data before loading posts")
    ap.add_argument("--down", action="store_true", help="on exit, also `docker compose down`")
    args = ap.parse_args()

    py = ensure_deps()
    reap_stale()
    start_infra()
    ensure_ollama()
    env = build_env()
    if args.reset:
        reset_data()
    start_pipeline(py, env)
    if args.with_agents:
        start_agents(py, env)
    if not args.no_load:
        load_posts(skip_if_loaded=not args.reset)
    if not args.no_dashboard:
        serve_dashboard(py, env)

    banner(args.with_agents, not args.no_dashboard)

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    try:
        while not stop["flag"]:
            time.sleep(1)
    finally:
        print()
        cleanup(down=args.down)


if __name__ == "__main__":
    main()
