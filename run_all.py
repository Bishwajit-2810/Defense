#!/usr/bin/env python3
"""run_all.py — one-command launcher for the Smart Layer.

Run it with uv (recommended):

    uv run run_all.py                 # core pipeline + dashboard, load the 50 posts
    uv run run_all.py --with-agents   # also start the agents + 3 MCP servers
    uv run run_all.py --reset         # wipe prior data, then load the 50 posts fresh
    uv run run_all.py --groq          # run LLM work on Groq Cloud (needs GROQ_API_KEY) — much faster (--fast synonym)
    uv run run_all.py --ollama        # force local Ollama for LLM work (the default backend)
    uv run run_all.py --no-load       # don't push the sample posts
    uv run run_all.py --manual-load   # bring the UI up first; you push the posts yourself via the API
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
# Mirror log lines into Redis for the dashboard's log drawer / GET /v1/logs.
# On by default; --no-server-logs turns it off for a lean run.
_LOG_TO_REDIS = True
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
        if "run_all.py" in cmdline or "lsp_server" in cmdline or "pytest" in cmdline:
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
def _needs_real_encoder() -> bool:
    """Whether this configuration requires a REAL sentence encoder to function.

    Two settings make the encoder load-bearing rather than optional:

    * ``EMBEDDING_STUB_MODE=false`` — the operator asked for semantic vectors.
    * ``EMBEDDING_ALLOW_STUB=false`` — persistence *refuses* to write a hash
      stub, so an absent encoder does not degrade the run, it fails every post.

    ``EMBEDDING_STUB_MODE`` unset follows ``MODEL_STUB_MODE``, mirroring
    ``libs/embeddings._stub_mode()``.
    """
    stub_mode = (_dotenv_value("EMBEDDING_STUB_MODE") or "").strip().lower()
    if not stub_mode:
        stub_mode = (_dotenv_value("MODEL_STUB_MODE") or "true").strip().lower()
    allow_stub = (_dotenv_value("EMBEDDING_ALLOW_STUB") or "true").strip().lower()
    return stub_mode == "false" or allow_stub == "false"


def ensure_deps() -> str:
    """uv sync (best-effort) and return the interpreter to launch services with.

    Syncs the ``embeddings`` extra when the configuration needs a real encoder.
    This is not a nicety — a bare ``uv sync`` **uninstalls** it, because it is an
    optional extra. So this function used to guarantee the exact failure the
    stub-refusal switch exists to make loud:

        uv sync                -> removes sentence-transformers
        --reset                -> truncates posts / analysis_results / chunks /
                                  comment vectors
        ingestion              -> encoder unavailable -> hash stub ->
                                  EMBEDDING_ALLOW_STUB=false raises
                                  StubEmbeddingRefused for EVERY post

    Nothing catches that exception, so each post retried to
    ``ASSEMBLER_MAX_RETRIES`` and dead-lettered. The wipe had already happened,
    so the run ended with an empty database and the whole corpus in
    ``assembler:queue:dlq`` — and the reason was three steps upstream in a
    dependency sync.
    """
    if shutil.which("uv"):
        cmd = ["uv", "sync"]
        if _needs_real_encoder():
            # Keep the encoder installed. Without this the sync silently removes
            # it and the pipeline cannot persist a single post.
            cmd += ["--extra", "embeddings"]
            log("uv sync --extra embeddings … (config requires a real encoder)")
        else:
            log("uv sync …")
        if subprocess.run(cmd, cwd=str(REPO)).returncode != 0:
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
    sql = (REPO / "src/defense/services/workers/assembler/clickhouse_init.sql").read_bytes()
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
        "STAGE1_LOCAL_MODEL": "gemma3:4b",
        "STAGE2_LOCAL_MODEL": "qwen2.5:7b",
        "STAGE1_LLM": "true",
        # Stage 1 uses the LLM for the post (summary + post-type) but NOT for
        # the comments: Stage 2 labels every post's comments with qwen2.5:7b
        # plus the seven classifiers, so doing it here as well spends the slowest
        # calls in the pipeline re-deriving a label that then gets outvoted.
        # Measured on a live run: one 25-comment gemma3:4b batch took 120 s and
        # returned invalid JSON (16 of 25 labels salvaged).
        #
        # STAGE1_LLM_COMMENT_MAX is deliberately NOT set here. It used to be 60,
        # which was inert while the line above is "false" — and a silent 60-comment
        # coverage cap the moment anyone flipped it to "true", overriding whatever
        # .env said. Coverage knobs belong to the operator; see the note in
        # build_env's tail.
        "STAGE1_LLM_COMMENTS": "false",
        "LLM_A_LOCAL_MODEL": "qwen2.5:7b",
        "LLM_B_LOCAL_MODEL": "qwen2.5:7b",
        "VLM_LOCAL_MODEL": "qwen3-vl:4b",
        "COMMENT_STANCE_BATCH": "40",
        "MODEL_STUB_MODE": "true",
        # The seven Stage-2 comment classifiers that vote alongside the LLM
        # (see STAGE2_CLASSIFIER_1..7). They load from the local HF cache only —
        # stub mode means "download nothing", not "refuse models you already
        # have" — so a machine without the checkpoints degrades to LLM-only
        # rather than stalling on a 3 GB fetch. Fetch them once with
        # `uv run python deploy/prefetch_classifiers.py`; until then the
        # `stage2_cheap_voters` log line reports voted<declared and names the
        # heads that stayed silent.
        "STAGE2_CLASSIFIERS_ENABLED": "true",
        # ...strictly from the local HF cache. Set in the process environment
        # (not from Python) because transformers reads these at import time, so
        # anything set later is ignored and the loader quietly hits the network
        # — which is where the "unauthenticated requests to the HF Hub" warning
        # came from. Pre-fetch a checkpoint once with HF_OFFLINE=false.
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        # ...on the CPU: the GPU is already hosting the Ollama models, and on a
        # 4 GB card there is not room for both. Measured 3× faster end-to-end
        # than letting them fight Ollama for VRAM.
        "STAGE2_CLASSIFIER_DEVICE": "cpu",
        "JWT_SECRET": "demo",
        "LOG_LEVEL": _LOG_LEVEL,
        # Mirror every service's log lines into Redis so the dashboard's log
        # drawer (and GET /v1/logs) can show them — the services don't share a
        # filesystem, but they do share a Redis. --no-server-logs turns it off.
        "LOG_TO_REDIS": "true" if _LOG_TO_REDIS else "false",
        "LOG_REDIS_MAX": "3000",
        "PYTHONPATH": str(REPO),
        # The API proxies /v1/agents/* to the agents service; its default
        # (http://agents:8010) is the compose hostname, which doesn't resolve
        # in host mode — so the host URL must be in the API's env too.
        "AGENTS_SERVICE_URL": "http://127.0.0.1:8010",
    })

    # NO COVERAGE CAPS ARE SET HERE, deliberately. `env.update` above beats .env
    # (pydantic-settings ranks the process environment above the file), so any
    # coverage default hardcoded in this launcher silently overrides a deliberate
    # setting — which is how `COMMENT_STANCE_MAX_PER_POST=0` in .env still ran with
    # a cap of 40, and the post detail read "✓ all 2857 analyzed" next to
    # "⚠ 2606 capped out".
    #
    # The full payload is analysed: ROUTER_COMMENT_TOP_N, STAGE1_LLM_COMMENT_MAX and
    # COMMENT_STANCE_MAX_PER_POST all default to 0 in `Settings`. On local CPU that
    # is slow on a long thread — seven heads are ~0.92 s/comment, so a
    # 2,857-comment post is ~44 min of classifier time plus ~115 LLM stance
    # batches. Cap it for a quick demo by exporting `ROUTER_COMMENT_TOP_N=100`, or
    # run `--fast` (Groq) — but do it in the environment, not here, so the run's
    # own setting is the one that shows up in the output.
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
            val = s.split("=", 1)[1].strip()
            # Strip surrounding quotes first, then inline comments (§P7.17f).
            # Quoted values are taken as-is; unquoted values have # comments.
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                return val[1:-1]
            return val.split("#", 1)[0].strip()
    return ""


def _env_flag(env: dict, key: str, default: str = "false") -> str:
    """Resolve a boolean env flag from the process env, then .env, then default.

    Same reason as _dotenv_value: services started outside the repo root cannot
    find .env themselves, so flags they need have to be injected explicitly.
    """
    return env.get(key) or os.environ.get(key) or _dotenv_value(key) or default


def apply_fast_preset(env: dict) -> None:
    """--fast: route Stage-2 + agents through Groq Cloud instead of local Ollama.

    Groq is dramatically faster than CPU Ollama, so this is the recommended way
    to get quick, high-quality summaries + comment stance. Requires GROQ_API_KEY
    (exported, or in .env). Combine with ROUTER_COMMENT_TOP_N=0 to analyse every
    comment on every post rather than the top 100 by reaction count — tractable on
    Groq, and the setting a scoring run wants so the ensemble also labels the tail.
    """
    key = env.get("GROQ_API_KEY") or os.environ.get("GROQ_API_KEY") or _dotenv_value("GROQ_API_KEY")
    if not key:
        die("--fast needs GROQ_API_KEY — set it in .env or export it, then retry")
    env.update({
        "LLM_BACKEND": "groq",
        "GROQ_API_KEY": key,
        # Fast Groq models per role; override any of these via env to taste.
        "LLM_A_GROQ_MODEL": "llama-3.3-70b-versatile",
        "LLM_B_GROQ_MODEL": "llama-3.1-8b-instant",
        "VLM_GROQ_MODEL": "meta-llama/llama-4-scout-17b-16e-instruct",
    })
    # The dashboard LLM toggle persists a runtime backend override in Redis that
    # Stage-2 reads BEFORE the env default — pin it to groq so --fast always wins,
    # even if a prior run left it on local. (Runs after reset_data's FLUSHALL.)
    subprocess.run(["docker", "exec", "deploy-redis-1", "redis-cli", "SET",
                    "config:llm_backend", "groq"], capture_output=True)
    ok(f"--fast: Stage-2/agents → Groq  (llm_a={env['LLM_A_GROQ_MODEL']}, "
       f"llm_b={env['LLM_B_GROQ_MODEL']})")


def apply_ollama_preset(env: dict) -> None:
    """--ollama: pin Stage-2 + agents to the local Ollama backend (the default).

    build_env already sets LLM_BACKEND=local, so the only extra work is clearing
    the dashboard's runtime backend override in Redis — Stage-2 reads that key
    BEFORE the env default, so a prior --groq/--fast run could otherwise leave it
    pinned to groq. (Runs after reset_data's FLUSHALL.)
    """
    env["LLM_BACKEND"] = "local"
    subprocess.run(["docker", "exec", "deploy-redis-1", "redis-cli", "SET",
                    "config:llm_backend", "local"], capture_output=True)
    ok(f"--ollama: Stage-2/agents → local Ollama  (stage1={env['STAGE1_LOCAL_MODEL']}, "
       f"stage2={env['STAGE2_LOCAL_MODEL']})")


def start_pipeline(py: str, env: dict) -> None:
    start("stage1", [py, "-m", "defense.services.workers.stage1_nlp"], env, REPO)
    start("router", [py, "-m", "defense.services.workers.router"], env, REPO)
    start("stage2", [py, "-m", "defense.services.workers.stage2_llm"], env, REPO)
    start("assembler", [py, "__main__.py"], env, REPO / "src/defense/services/workers/assembler")
    start("ingestion", [py, "-m", "defense.services.ingestion"], env, REPO)
    start("api", [py, "-m", "uvicorn", "defense.services.api.main:app", "--host", "127.0.0.1",
                  "--port", str(API_PORT), "--log-level", _uvicorn_level()], env, REPO)

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
        # Stub mode is a deployment choice, not a launcher one. Hardcoding it to
        # "true" here silently overrode ANALYTICS_MCP_STUB=false in .env, so an
        # operator who had configured real ClickHouse got synthetic numbers with
        # no indication — /health said stub_mode:true and nothing else did.
        # Injected explicitly rather than left to the settings layer because
        # retrieval_mcp and ingest_mcp are started from their own directories,
        # where the repo-root .env is not on the search path.
        "ANALYTICS_MCP_STUB": _env_flag(env, "ANALYTICS_MCP_STUB"),
        "RETRIEVAL_MCP_STUB": _env_flag(env, "RETRIEVAL_MCP_STUB"),
    })
    uvi = [py, "-m", "uvicorn", "--host", "127.0.0.1", "--log-level", _uvicorn_level()]
    # analytics_mcp + agents launch by module path from the repo root; retrieval/
    # ingest run from their own dir (server:app). Each FastMCP server exposes the
    # streamable-HTTP MCP endpoint at /mcp.
    start("analytics_mcp", uvi + ["defense.mcp_servers.analytics_mcp.server:app", "--port", "8110"], a, REPO)
    start("retrieval_mcp", uvi + ["server:app", "--port", "8101"], a, REPO / "src/defense/mcp_servers/retrieval_mcp")
    start("ingest_mcp", uvi + ["server:app", "--port", "8102"], a, REPO / "src/defense/mcp_servers/ingest_mcp")
    start("agents", uvi + ["defense.services.agents.main:app", "--port", "8010"], a, REPO)
    ok("agent layer started (analytics :8110, retrieval :8101, ingest :8102, agents :8010)")


def _free_port(start_port: int, tries: int = 10) -> int:
    """Return the first free TCP port at/after start_port (probes 127.0.0.1)."""
    for cand in range(start_port, start_port + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", cand)) != 0:  # nothing listening → free
                return cand
    return start_port


def serve_dashboard(py: str, env: dict) -> int:
    # The dashboard defaults to the dev API on :8001 (dashboard/src/App.jsx/AppConfig),
    # we run the Vite dev server. If 8080 is held, fall back to next free port.
    port = _free_port(DASH_PORT)
    if port != DASH_PORT:
        warn(f"port {DASH_PORT} is in use — serving the dashboard on {port} instead")
    
    # We use npx vite (or npm run dev) to start the dashboard
    # The user must have run `npm install` in dashboard/ previously.
    dash_dir = REPO / "dashboard"
    if not (dash_dir / "node_modules").exists():
        warn("node_modules not found in dashboard/. Running 'npm install' first ...")
        subprocess.run(["npm", "install"], cwd=dash_dir, check=False)
        
    start("dashboard", ["npm", "run", "dev", "--", "--port", str(port)], env, dash_dir)
    return port


def preflight_encoder(py: str) -> None:
    """Refuse to continue if the encoder cannot produce a real vector.

    Runs BEFORE ``reset_data()``, which is the whole point. With
    ``EMBEDDING_ALLOW_STUB=false`` a missing encoder does not degrade the run —
    ``_resolve_embedding`` raises ``StubEmbeddingRefused`` for every post, the
    assembler retries to its cap and dead-letters, and because the wipe already
    happened the operator is left with an empty database and the corpus sitting
    in ``assembler:queue:dlq``. The failure is discovered minutes later, several
    layers from its cause.

    ``ensure_deps`` fixes the common cause (a bare ``uv sync`` dropping the
    extra). This catches every other one — CUDA OOM on a GPU already hosting the
    LLM, undownloaded weights, ``HF_OFFLINE=true`` blocking the fetch — by
    asking the encoder for one vector and checking its provenance flag rather
    than by inferring anything.

    Checked in the CHILD interpreter (``.venv/bin/python``), because that is what
    the workers will use; a check run under a different interpreter would prove
    nothing about them.
    """
    if not _needs_real_encoder():
        return
    log("preflight: verifying the sentence encoder produces real vectors …")
    probe = (
        "import sys;"
        "sys.path.insert(0, 'src');"
        "from defense.libs.embeddings import embed_text_with_provenance, active_model_name;"
        "vec, is_stub = embed_text_with_provenance('preflight probe');"
        "print('STUB' if is_stub else 'REAL', active_model_name(), len(vec))"
    )
    try:
        r = subprocess.run([py, "-c", probe], cwd=str(REPO),
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        die("preflight: the encoder took over 5 minutes to load — aborting before "
            "any data is touched. Check the model download, or set "
            "EMBEDDING_ALLOW_STUB=true to run on hash vectors deliberately.")
        return
    out = (r.stdout or "").strip().splitlines()
    verdict = out[-1] if out else ""
    if verdict.startswith("REAL"):
        ok(f"preflight: encoder OK — {verdict.split(maxsplit=1)[1]}")
        return
    detail = (r.stderr or "").strip().splitlines()
    tail = detail[-1][:300] if detail else "(no error output)"
    die(
        "preflight: the sentence encoder is NOT producing real vectors "
        f"({verdict or 'probe failed'}).\n"
        "         Nothing has been modified — this check runs before any wipe.\n\n"
        "         EMBEDDING_ALLOW_STUB=false means persistence REFUSES a hash stub, so "
        "every post would\n"
        "         fail to persist and dead-letter to assembler:queue:dlq. With --reset "
        "the wipe would\n"
        "         already have happened, leaving an empty database.\n\n"
        f"         encoder error: {tail}\n\n"
        "         Fix one of these:\n"
        "           uv sync --extra embeddings     # install the encoder\n"
        "           EMBEDDING_DEVICE=cpu           # if CUDA is out of memory\n"
        "           HF_OFFLINE=false               # if the weights need downloading\n"
        "           EMBEDDING_ALLOW_STUB=true      # accept hash vectors deliberately"
    )


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


# The pipeline's Redis Stream queues → their consumer group, in flow order.
# Consumers read only new entries (XREADGROUP ">"), so any messages a prior run
# enqueued but never delivered sit in the stream and get processed the moment the
# workers restart. (Pending/unacked entries are never reclaimed — no worker uses
# XAUTOCLAIM — so undelivered backlog is the only thing that auto-resumes.)
_QUEUE_GROUPS = [
    ("ingestion:queue",  "ingestion-workers"),
    ("nlp:stage1:queue", "stage1-nlp-group"),
    ("router:queue",     "router-workers"),
    ("llm:stage2:queue", "stage2-llm-workers"),
    ("assembler:queue",  "assembler-group"),
]


def pause_pipeline_queues() -> None:
    """Quiet the pipeline for a PRIOR run's leftover work WITHOUT deleting any
    stream data: reset each stage's consumer group so it has **0 in-flight and 0
    backlog**, then only NEW messages — your manual uploads — get delivered.

    We DESTROY the consumer group (dropping its pending-entries list — those are
    the delivered-but-unacked messages the Pipeline tab reports as "processing/
    in-flight", and which no worker ever reclaims) and re-CREATE it at the tail
    (``$``) so its lag is 0 too. The stream's entries are left untouched, so
    nothing is cleared from Redis — the workers just start past them.

    This is what --manual-load / --no-load want: an idle pipeline with all data
    (queue entries + Postgres/ClickHouse results) intact. Use --reset for a wipe.
    """
    def rc(*cmd):
        subprocess.run(["docker", "exec", "deploy-redis-1", "redis-cli", *cmd],
                       capture_output=True)
    log("quieting the pipeline (reset consumer groups → 0 waiting / 0 in-flight) — "
        "stream data kept, nothing auto-processes …")
    for stream, group in _QUEUE_GROUPS:
        rc("XGROUP", "DESTROY", stream, group)                  # drop the group + its pending list
        rc("XGROUP", "CREATE", stream, group, "$", "MKSTREAM")  # recreate fresh, at the tail


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


def banner(with_agents: bool, dash_port: int | None, manual_load: bool = False) -> None:
    print()
    ok("════════════════════════════════════════════════════════════")
    ok("  Smart Layer is up.")
    print(f"   • API        →  http://127.0.0.1:{API_PORT}/v1/health")
    if dash_port:
        print(f"   • Dashboard  →  http://127.0.0.1:{dash_port}   (log in with API key: demo)")
    if with_agents:
        print("   • Agents     →  POST http://127.0.0.1:%d/v1/agents/query" % API_PORT)
    if manual_load:
        print(f"   • {_C['yellow']}No posts auto-loaded{_C['off']} — load them yourself when ready:")
        if dash_port:
            print("       – UI:  Dashboard → Posts tab → drop  posts_with_details.json  onto the upload box")
        print(f"       – API: POST http://127.0.0.1:{API_PORT}/v1/posts/upload  with body {{\"posts\": [...]}}")
        print("              (ready-to-run command in easy_run.md §D; or re-run without --manual-load to auto-load)")
    if _STREAM:
        flt = f" (filtered: '{_LOG_FILTER}')" if _LOG_FILTER else ""
        print(f"   • Logs       →  streaming live below{flt}  +  /tmp/<service>.log")
    else:
        print("   • Logs       →  /tmp/<service>.log   (add --log to stream them here live)")
    if _LOG_TO_REDIS:
        print(f"                   in the browser: dashboard → Logs (Ctrl+`), or "
              f"GET http://127.0.0.1:{API_PORT}/v1/logs")
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
    ap.add_argument("--manual-load", action="store_true",
                    help="serve the UI first and DON'T auto-load the sample posts (also skips any leftover "
                         "queued work — kept in Redis, not deleted — so nothing processes in the background) — "
                         "push data yourself via POST /v1/posts/upload or the dashboard Posts tab")
    ap.add_argument("--reset", action="store_true", help="wipe prior data before loading posts")
    ap.add_argument("--down", action="store_true", help="on exit, also `docker compose down`")
    backend = ap.add_mutually_exclusive_group()
    backend.add_argument("--groq", "--fast", dest="groq", action="store_true",
                         help="route Stage-2 + agents through Groq Cloud (much faster than local Ollama; needs GROQ_API_KEY). --fast is a synonym.")
    backend.add_argument("--ollama", action="store_true",
                         help="pin Stage-2 + agents to local Ollama (the default backend); also clears any Groq override left in Redis by a prior --groq run")
    ap.add_argument("--log", "--logs", "-l", dest="logs", action="store_true",
                    help="stream every service's logs live to this terminal (still tee'd to /tmp/*.log "
                         "and mirrored to Redis for the dashboard's Logs drawer). --logs is a synonym.")
    ap.add_argument("--log-level", default="INFO",
                    help="log level for all services (DEBUG/INFO/WARNING/ERROR); default INFO")
    ap.add_argument("--log-filter", metavar="STR",
                    help="with --log, only print lines containing STR (e.g. 'llm' to watch LLM activity)")
    ap.add_argument("--no-server-logs", action="store_true",
                    help="don't mirror service logs into Redis — the dashboard's Logs drawer and "
                         "GET /v1/logs will be empty (saves one Redis write per log line)")
    args = ap.parse_args()

    global _STREAM, _LOG_LEVEL, _LOG_FILTER, _LOG_TO_REDIS
    _STREAM = args.logs or bool(args.log_filter)
    _LOG_LEVEL = args.log_level.upper()
    _LOG_FILTER = args.log_filter
    _LOG_TO_REDIS = not args.no_server_logs

    # --manual-load / --no-load mean "don't auto-load". They must also NOT let the
    # workers resume a PRIOR run's leftover queue backlog (that's the phantom
    # background loading), so we park the pipeline backlog — skip it, delete
    # nothing — before the workers start.
    defer_load = args.no_load or args.manual_load

    py = ensure_deps()
    reap_stale()
    start_infra()
    ensure_ollama()
    env = build_env()
    # BEFORE the wipe, never after: with EMBEDDING_ALLOW_STUB=false a dead
    # encoder makes every post fail to persist, and --reset has already thrown
    # the old data away by the time the first failure appears.
    preflight_encoder(py)
    if args.reset:
        reset_data()               # full wipe (Redis + Postgres + ClickHouse)
    elif defer_load:
        pause_pipeline_queues()    # skip leftover backlog, delete nothing
    if args.groq:
        apply_fast_preset(env)
    elif args.ollama:
        apply_ollama_preset(env)
    start_pipeline(py, env)
    if args.with_agents:
        start_agents(py, env)

    # Serve the dashboard BEFORE loading any posts so the UI is up immediately.
    # You open it first, then either watch the auto-load populate it or push the
    # data yourself (--manual-load / --no-load). Previously the blocking load ran
    # first, so the UI only appeared minutes later.
    dash_port = None
    if not args.no_dashboard:
        dash_port = serve_dashboard(py, env)
        if dash_port:
            ok(f"dashboard live → http://127.0.0.1:{dash_port}   (log in with API key: demo)")

    if not defer_load:
        load_posts(skip_if_loaded=not args.reset)

    banner(args.with_agents, dash_port, manual_load=defer_load)

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
