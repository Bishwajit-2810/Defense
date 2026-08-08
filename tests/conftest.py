import asyncio
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
import redis.asyncio as aioredis
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

# Use the same schema setup script
import os
import subprocess

# Pytest configuration to use asyncio
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as asyncio")

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
