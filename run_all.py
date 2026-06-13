#!/usr/bin/env python3
"""run_all.py — one-command launcher for the Smart Layer.

Run it with uv (recommended):

    uv run run_all.py                 # core pipeline + dashboard, load the 50 posts
    uv run run_all.py --with-agents   # also start the agents + 3 MCP servers
    uv run run_all.py --reset         # wipe prior data, then load the 50 posts fresh
    uv run run_all.py --fast          # run LLM work on Groq Cloud (needs GROQ_API_KEY) — much faster
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
import socket
import subprocess
import sys
import threading
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

# Live log streaming to the console (enabled by --logs). When on, each service's
# stdout/stderr is teed to /tmp/<name>.log AND printed here with a coloured,
# service-prefixed line so the whole pipeline can be watched in one terminal.
_STREAM = False
_LOG_LEVEL = "INFO"
# Only print lines matching this substring filter when set (e.g. "llm" to watch
# just LLM activity). None = print every line.
_LOG_FILTER: str | None = None
_STREAM_COLORS = ["\033[36m", "\033[32m", "\033[33m", "\033[35m", "\033[34m",
                  "\033[91m", "\033[92m", "\033[93m", "\033[95m", "\033[96m"]
_color_idx = 0


def _uvicorn_level() -> str:
    """uvicorn's own log level — verbose when streaming, quiet otherwise."""
    return _LOG_LEVEL.lower() if _STREAM else "warning"


def _pump(name: str, stream, logf, color: str) -> None:
    """Read a child's combined stdout/stderr line-by-line: tee to its log file
    and echo to the console with a coloured ``<service> |`` prefix."""
    prefix = f"{color}{name:>13}{_C['off']} | "
    try:
        for raw in iter(stream.readline, b""):
            try:
                logf.write(raw)
                logf.flush()
            except Exception:
                pass
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if _LOG_FILTER and _LOG_FILTER.lower() not in line.lower():
                continue
            sys.stdout.write(prefix + line + "\n")
            sys.stdout.flush()
    except Exception:
        pass
    finally:
        try:
            logf.close()
        except Exception:
            pass


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
    global _color_idx
    logf = open(f"/tmp/{name}.log", "ab")
    if _STREAM:
        # Pipe the child's output through a reader thread that tees it to the log
        # file and the console. The thread owns/closes logf when the pipe ends.
        p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True, bufsize=1)
        color = _STREAM_COLORS[_color_idx % len(_STREAM_COLORS)]
        _color_idx += 1
        threading.Thread(target=_pump, args=(name, p.stdout, logf, color), daemon=True).start()
    else:
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
        # Stage 1 (Fast NLP) and Stage 2 run on DIFFERENT Ollama models:
        #   stage1 = gemma3:4b (fast/light) carries the high-volume per-post +
        #            per-comment NLP; stage2 = qwen2.5:7b (quality) does the
        #            selective summary/insight + context-aware comment stance.
        "STAGE1_LOCAL_MODEL": os.environ.get("STAGE1_LOCAL_MODEL", "gemma3:4b"),
        "STAGE2_LOCAL_MODEL": os.environ.get("STAGE2_LOCAL_MODEL", "qwen2.5:7b"),
        # STAGE1_LLM=true → Stage-1 NLP runs on the stage1 LLM (embeddings stay
        # stubbed under MODEL_STUB_MODE; any LLM failure falls back to the stub).
        "STAGE1_LLM": os.environ.get("STAGE1_LLM", "true"),
        "STAGE1_LLM_COMMENT_MAX": os.environ.get("STAGE1_LLM_COMMENT_MAX", "60"),
        # llm_a / llm_b = the agents + report roles; kept on the quality model.
        "LLM_A_LOCAL_MODEL": "qwen2.5:7b",
        "LLM_B_LOCAL_MODEL": os.environ.get("LLM_B_LOCAL_MODEL", "qwen2.5:7b"),
        "VLM_LOCAL_MODEL": "qwen3-vl:4b",
        # Per-post context-aware comment labelling. Every comment is ALWAYS
        # analysed by the instant Stage-1 heuristic (full coverage); this only
        # bounds the slow premium LLM pass to the top-N most-liked comments so a
        # post with thousands of comments can't stall Stage-2. Set 0 to LLM-label
        # EVERY comment (only practical on Groq / a GPU — slow on local CPU).
        "COMMENT_STANCE_MAX_PER_POST": os.environ.get("COMMENT_STANCE_MAX_PER_POST", "40"),
        "COMMENT_STANCE_BATCH": os.environ.get("COMMENT_STANCE_BATCH", "40"),
        "MODEL_STUB_MODE": "true",
        "JWT_SECRET": "demo",
        "LOG_LEVEL": _LOG_LEVEL,
        "PYTHONPATH": str(REPO),
        # The API proxies /v1/agents/* to the agents service; its default
        # (http://agents:8010) is the compose hostname, which doesn't resolve
        # in host mode — so the host URL must be in the API's env too.
        "AGENTS_SERVICE_URL": "http://127.0.0.1:8010",
    })
    return env


def _dotenv_value(key: str) -> str:
    """Read a single KEY=value from the repo-root .env (best-effort).

    The LLM client reads GROQ_API_KEY straight from os.environ, but .env is only
    loaded by the services' settings layer — so for --fast we pull the key here
    and inject it into the child env explicitly.
    """
    f = REPO / ".env"
    if not f.exists():
        return ""
    for line in f.read_text().splitlines():
        s = line.strip()
        if s.startswith(f"{key}=") and not s.startswith("#"):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def apply_fast_preset(env: dict) -> None:
    """--fast: route Stage-2 + agents through Groq Cloud instead of local Ollama.

    Groq is dramatically faster than CPU Ollama, so this is the recommended way
    to get quick, high-quality summaries + comment stance. Requires GROQ_API_KEY
    (exported, or in .env). Combine with COMMENT_STANCE_MAX_PER_POST=0 to LLM-label
    every comment (now tractable on Groq).
    """
    key = env.get("GROQ_API_KEY") or os.environ.get("GROQ_API_KEY") or _dotenv_value("GROQ_API_KEY")
    if not key:
        die("--fast needs GROQ_API_KEY — set it in .env or export it, then retry")
    env.update({
        "LLM_BACKEND": "groq",
        "GROQ_API_KEY": key,
        # Fast Groq models per role; override any of these via env to taste.
        "LLM_A_GROQ_MODEL": os.environ.get("LLM_A_GROQ_MODEL", "llama-3.3-70b-versatile"),
        "LLM_B_GROQ_MODEL": os.environ.get("LLM_B_GROQ_MODEL", "llama-3.1-8b-instant"),
        "VLM_GROQ_MODEL": os.environ.get("VLM_GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
    })
    # The dashboard LLM toggle persists a runtime backend override in Redis that
    # Stage-2 reads BEFORE the env default — pin it to groq so --fast always wins,
    # even if a prior run left it on local. (Runs after reset_data's FLUSHALL.)
    subprocess.run(["docker", "exec", "deploy-redis-1", "redis-cli", "SET",
                    "config:llm_backend", "groq"], capture_output=True)
    ok(f"--fast: Stage-2/agents → Groq  (llm_a={env['LLM_A_GROQ_MODEL']}, "
       f"llm_b={env['LLM_B_GROQ_MODEL']})")


def start_pipeline(py: str, env: dict) -> None:
    start("stage1", [py, "-m", "services.workers.stage1_nlp"], env, REPO)
    start("router", [py, "-m", "services.workers.router"], env, REPO)
    start("stage2", [py, "-m", "services.workers.stage2_llm"], env, REPO)
    start("assembler", [py, "__main__.py"], env, REPO / "services/workers/assembler")
    start("ingestion", [py, "-m", "services.ingestion"], env, REPO)
    start("api", [py, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                  "--port", str(API_PORT), "--log-level", _uvicorn_level()], env, REPO / "services/api")

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
    uvi = [py, "-m", "uvicorn", "--host", "127.0.0.1", "--log-level", _uvicorn_level()]
    # analytics_mcp + agents launch by module path from the repo root; retrieval/
    # ingest run from their own dir (server:app). Each FastMCP server exposes the
    # streamable-HTTP MCP endpoint at /mcp.
    start("analytics_mcp", uvi + ["mcp_servers.analytics_mcp.server:app", "--port", "8110"], a, REPO)
    start("retrieval_mcp", uvi + ["server:app", "--port", "8101"], a, REPO / "mcp_servers/retrieval_mcp")
    start("ingest_mcp", uvi + ["server:app", "--port", "8102"], a, REPO / "mcp_servers/ingest_mcp")
    start("agents", uvi + ["services.agents.main:app", "--port", "8010"], a, REPO)
    ok("agent layer started (analytics :8110, retrieval :8101, ingest :8102, agents :8010)")


def _free_port(start_port: int, tries: int = 10) -> int:
    """Return the first free TCP port at/after start_port (probes 127.0.0.1)."""
    for cand in range(start_port, start_port + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", cand)) != 0:  # nothing listening → free
                return cand
    return start_port


def serve_dashboard(py: str, env: dict) -> int:
    # The dashboard defaults to the dev API on :8001 (dashboard/app.js), so we
    # just serve the static files — no patching needed. If 8080 is held by a
    # stale process / another run, fall back to the next free port instead of
    # crashing with "Address already in use".
    port = _free_port(DASH_PORT)
    if port != DASH_PORT:
        warn(f"port {DASH_PORT} is in use — serving the dashboard on {port} instead")
    start("dashboard", [py, "-m", "http.server", str(port)], env, REPO / "dashboard")
    return port


def reset_data() -> None:
    # Must run BEFORE the workers start: FLUSHALL destroys the Redis consumer
    # groups the workers create at startup (workers also self-heal on NOGROUP,
    # but a pre-start reset avoids the error churn entirely).
    log("resetting data (Redis FLUSHALL + Postgres/ClickHouse truncate) …")
    subprocess.run(["docker", "exec", "deploy-redis-1", "redis-cli", "FLUSHALL"], capture_output=True)
    subprocess.run(["docker", "exec", "deploy-postgres-1", "psql", "-U", "defense", "-d", "defense",
                    "-c", "TRUNCATE analysis_results, posts, jobs CASCADE;"], capture_output=True)
    # ClickHouse is append-only analytics; clear it too so a reset is a true reset
    # (otherwise analysis_events and comment_sentiments accumulate stale rows from
    # prior runs — the per-comment table dedups on merge, but the events table does
    # not). TRUNCATE is a no-op when the tables don't exist yet (first run).
    subprocess.run(["docker", "exec", "deploy-clickhouse-1", "clickhouse-client",
                    "--user", "defense", "--password", "defense", "--database", "defense",
                    "-q", "TRUNCATE TABLE IF EXISTS analysis_events; TRUNCATE TABLE IF EXISTS comment_sentiments;"],
                   capture_output=True)


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


def banner(with_agents: bool, dash_port: int | None) -> None:
    print()
    ok("════════════════════════════════════════════════════════════")
    ok("  Smart Layer is up.")
    print(f"   • API        →  http://127.0.0.1:{API_PORT}/v1/health")
    if dash_port:
        print(f"   • Dashboard  →  http://127.0.0.1:{dash_port}   (log in with API key: demo)")
    if with_agents:
        print("   • Agents     →  POST http://127.0.0.1:%d/v1/agents/query" % API_PORT)
    if _STREAM:
        flt = f" (filtered: '{_LOG_FILTER}')" if _LOG_FILTER else ""
        print(f"   • Logs       →  streaming live below{flt}  +  /tmp/<service>.log")
    else:
        print("   • Logs       →  /tmp/<service>.log   (add --logs to stream them here live)")
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
    ap.add_argument("--fast", action="store_true",
                    help="route Stage-2 + agents through Groq Cloud (much faster than local Ollama; needs GROQ_API_KEY)")
    ap.add_argument("--logs", "-l", action="store_true",
                    help="stream every service's logs live to this terminal (still tee'd to /tmp/*.log)")
    ap.add_argument("--log-level", default="INFO",
                    help="log level for all services (DEBUG/INFO/WARNING/ERROR); default INFO")
    ap.add_argument("--log-filter", metavar="STR",
                    help="with --logs, only print lines containing STR (e.g. 'llm' to watch LLM activity)")
    args = ap.parse_args()

    global _STREAM, _LOG_LEVEL, _LOG_FILTER
    _STREAM = args.logs or bool(args.log_filter)
    _LOG_LEVEL = args.log_level.upper()
    _LOG_FILTER = args.log_filter

    py = ensure_deps()
    reap_stale()
    start_infra()
    ensure_ollama()
    env = build_env()
    if args.reset:
        reset_data()
    if args.fast:
        apply_fast_preset(env)
    start_pipeline(py, env)
    if args.with_agents:
        start_agents(py, env)
    if not args.no_load:
        load_posts(skip_if_loaded=not args.reset)
    dash_port = None
    if not args.no_dashboard:
        dash_port = serve_dashboard(py, env)

    banner(args.with_agents, dash_port)

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
