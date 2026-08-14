import asyncio
import pathlib
import sys
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
import redis.asyncio as aioredis
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

# Use the same schema setup script
import os
import subprocess

# ---------------------------------------------------------------------------
# Import roots
# ---------------------------------------------------------------------------
# REPO is derived, never hardcoded: the contract tests that grep the source have
# to keep working from any checkout, and a stale absolute path is exactly how
# they came to point at directories that no longer exist after the move into
# src/defense/.
REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PKG = SRC / "defense"
for _p in (REPO, SRC, PKG):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def src_file(rel: str) -> pathlib.Path:
    """Absolute path of a module inside the package, from its bare-import name.

    ``src_file("services/api/routers/chat.py")`` → ``src/defense/services/api/
    routers/chat.py``. The grep-the-source contract tests name modules the way
    the workers import them; this is the one place that mapping lives.
    """
    return PKG / rel


# Pytest configuration to use asyncio
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as asyncio")
    config.addinivalue_line("markers", "timeout(seconds): advisory; no-op without pytest-timeout")


def pytest_sessionstart(session):
    """No test may reach the network for model weights.

    Several files spent 25-200 seconds each waiting on Hugging Face — for
    models whose ABSENCE was the thing being asserted. Offline mode makes the
    loaders fail immediately, which is both the intended test condition and the
    difference between a 5-second file and a 200-second one. Set
    DEFENSE_TEST_ALLOW_DOWNLOADS=1 to opt out.
    """
    if os.environ.get("DEFENSE_TEST_ALLOW_DOWNLOADS") == "1":
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    # Same rule for the LLM. STAGE1_LLM defaults to true, so a bare
    # ModelRegistry() in a unit test tries to reach Ollama and spends the full
    # retry-with-backoff budget failing — 65 seconds for one assertion about a
    # stub embedding. Tests that exercise the LLM path set this themselves.
    os.environ.setdefault("STAGE1_LLM", "false")


@pytest.fixture(scope="session", autouse=True)
def _ignore_dotenv():
    """Keep the developer's .env out of the test run.

    `Settings` reads `.env` by default, so config assertions silently became
    assertions about whatever the machine's local file happened to contain —
    e.g. the JWT tests were checking a real deployment secret rather than the
    documented fallback. Tests configure their own environment or get the
    declared defaults.
    """
    try:
        from defense.libs.common.config import Settings
    except Exception:  # pragma: no cover
        yield
        return
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    yield
    Settings.model_config["env_file"] = original


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Drop the cached Settings around every test.

    ``get_settings()`` is ``lru_cache``d, so the first import in a session
    freezes the environment for the whole run and every test that monkeypatches
    an env var silently asserts against the snapshot instead of its own setup.
    """
    try:
        from defense.libs.common.config import get_settings
    except Exception:  # pragma: no cover - config import failures surface elsewhere
        yield
        return
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest_asyncio.fixture(scope="session")
async def postgres_container():
    """Spin up a Postgres container with pgvector for the entire test session."""
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as postgres:
        # We must initialize the schema
        conn_str = postgres.get_connection_url().replace("postgresql+asyncpg", "postgresql")
        schema_path = os.path.join(os.path.dirname(__file__), "..", "deploy", "init-db.sql")
        
        # Run init script via psql since testcontainers has no built-in script runner
        # Extract connection details
        import urllib.parse
        parsed = urllib.parse.urlparse(conn_str)
        host = parsed.hostname
        port = parsed.port
        user = parsed.username
        password = parsed.password
        db = parsed.path.lstrip("/")
        
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        subprocess.run(
            ["psql", "-h", host, "-p", str(port), "-U", user, "-d", db, "-f", schema_path],
            env=env,
            check=True
        )
        
        yield postgres.get_connection_url()

@pytest_asyncio.fixture(scope="session")
async def redis_container():
    """Spin up a Redis container for the entire test session."""
    with RedisContainer("redis:7-alpine") as redis_server:
        # testcontainers uses synchronous redis internally, we need to extract connection details
        host = redis_server.get_container_host_ip()
        port = redis_server.get_exposed_port(6379)
        yield f"redis://{host}:{port}"

@pytest_asyncio.fixture
async def db_session(postgres_container):
    """Provides an isolated database session per test."""
    engine = create_async_engine(postgres_container, echo=False)
    async with engine.begin() as conn:
        session = AsyncSession(bind=conn)
        yield session
        await session.rollback()
    await engine.dispose()

@pytest_asyncio.fixture
async def redis_client(redis_container):
    """Provides an isolated Redis client per test."""
    client = aioredis.from_url(redis_container)
    yield client
    await client.flushall()
    await client.aclose()
