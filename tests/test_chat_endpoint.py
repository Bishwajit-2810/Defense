"""Tests for the /v1/chat chatbot endpoint.

Exercises request → message assembly → backend resolution → response, with the
LLM client stubbed out (no Ollama/Groq needed) and auth/redis/db dependencies
overridden. Verifies the endpoint honours the Redis backend toggle and an
explicit per-request override.
"""

import sys

# The API app is written to run with services/api on sys.path (it does
# `from routers import ...` / `from deps import ...`), so mirror that here.
sys.path.insert(0, "/home/bk/code/defense")
sys.path.insert(0, "/home/bk/code/defense/services/api")

import pytest
from fastapi.testclient import TestClient

import main  # noqa: E402
from deps import get_current_user, get_db, get_redis, rate_limit  # noqa: E402


class FakeRedis:
    """Minimal async Redis stand-in whose `get` returns a preset toggle value."""

    def __init__(self, backend_value=None):
        self._value = backend_value

    async def get(self, key):
        return self._value


def _make_client(toggle=None):
    """A TestClient with auth/redis/db/rate-limit dependencies overridden."""
    app = main.app

    async def _redis():
        return FakeRedis(toggle)

    async def _db():
        return object()  # policy check short-circuits before touching it

    async def _user():
        return {"sub": "tester", "tenant_id": "default", "auth_method": "test"}

    app.dependency_overrides[get_redis] = _redis
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[rate_limit] = lambda: None
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    main.app.dependency_overrides.clear()


@pytest.fixture
def capture_chat(monkeypatch):
    """Stub LLMClient.chat, capturing the args it was called with."""
    captured = {}

    async def fake_chat(self, role, messages, backend_override=None, **kw):
        captured["role"] = role
        captured["messages"] = messages
        captured["backend_override"] = backend_override
        captured["model"] = kw.get("model")
        return {
            "content": "hello there",
            "backend": backend_override or "local",
            "model": "qwen2.5:7b",
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }

    monkeypatch.setattr("libs.llm.client.LLMClient.chat", fake_chat)
    return captured


def test_single_message_returns_reply(capture_chat):
    client = _make_client()
    r = client.post("/v1/chat", json={"message": "hi there"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "hello there"
    assert body["model"] == "qwen2.5:7b"
    assert body["usage"]["total_tokens"] == 5
    # A system prompt is injected, then the user's turn.
    msgs = capture_chat["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[-1] == {"role": "user", "content": "hi there"}


def test_backend_defaults_to_toggle(capture_chat):
    # Redis toggle says groq, request doesn't specify → groq is used.
    client = _make_client(toggle="groq")
    r = client.post("/v1/chat", json={"message": "hi"})
    assert r.status_code == 200
    assert capture_chat["backend_override"] == "groq"


def test_explicit_backend_overrides_toggle(capture_chat):
    # Toggle says groq but the request forces local → local wins.
    client = _make_client(toggle="groq")
    r = client.post("/v1/chat", json={"message": "hi", "backend": "local"})
    assert r.status_code == 200
    assert capture_chat["backend_override"] == "local"


def test_auto_backend_with_no_toggle_is_none(capture_chat):
    client = _make_client(toggle=None)
    r = client.post("/v1/chat", json={"message": "hi", "backend": "auto"})
    assert r.status_code == 200
    # None → client falls back to its env default.
    assert capture_chat["backend_override"] is None


def test_custom_system_prompt(capture_chat):
    client = _make_client()
    r = client.post("/v1/chat", json={"message": "hi", "system": "Be a pirate."})
    assert r.status_code == 200
    assert capture_chat["messages"][0] == {"role": "system", "content": "Be a pirate."}


def test_empty_request_is_422(capture_chat):
    client = _make_client()
    r = client.post("/v1/chat", json={})
    assert r.status_code == 422


def test_invalid_backend_is_422(capture_chat):
    client = _make_client()
    r = client.post("/v1/chat", json={"message": "hi", "backend": "openai"})
    assert r.status_code == 422


def test_stream_emits_delta_and_done(monkeypatch):
    async def fake_stream(self, role, messages, backend_override=None, **kw):
        yield {"type": "meta", "backend": "local", "model": "qwen2.5:7b"}
        yield {"type": "delta", "content": "he"}
        yield {"type": "delta", "content": "llo"}
        yield {"type": "done", "backend": "local", "model": "qwen2.5:7b",
               "usage": {"total_tokens": 3}}

    monkeypatch.setattr("libs.llm.client.LLMClient.chat_stream", fake_stream)
    client = _make_client()
    r = client.post("/v1/chat/stream", json={"message": "hi"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    text = r.text
    assert "event: meta" in text
    assert "event: delta" in text
    assert "event: done" in text
    # The two deltas concatenate to the streamed answer.
    assert '"content": "he"' in text and '"content": "llo"' in text


def test_model_override_passed_through(capture_chat):
    client = _make_client()
    r = client.post("/v1/chat", json={"message": "hi", "model": "gemma3:4b"})
    assert r.status_code == 200
    assert capture_chat["model"] == "gemma3:4b"


def test_blank_model_means_default(capture_chat):
    client = _make_client()
    # Omitted, and explicit empty string, both mean "use the backend default".
    client.post("/v1/chat", json={"message": "hi"})
    assert capture_chat["model"] is None
    client.post("/v1/chat", json={"message": "hi", "model": "  "})
    assert capture_chat["model"] is None


def test_list_models_endpoint(monkeypatch):
    catalogue = {"local": ["gemma3:4b", "qwen2.5:7b"], "groq": ["llama-3.3-70b-versatile"]}
    defaults = {"local": "qwen2.5:7b", "groq": "llama-3.3-70b-versatile"}

    async def fake_list(self, backend):
        return catalogue[backend]

    def fake_default(self, role, backend):
        return defaults[backend]

    monkeypatch.setattr("libs.llm.client.LLMClient.list_models", fake_list)
    monkeypatch.setattr("libs.llm.client.LLMClient.default_model", fake_default)

    client = _make_client(toggle="local")
    r = client.get("/v1/chat/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active_backend"] == "local"
    assert body["backends"]["local"]["default"] == "qwen2.5:7b"
    assert "gemma3:4b" in body["backends"]["local"]["models"]
    assert body["backends"]["groq"]["default"] == "llama-3.3-70b-versatile"


def test_list_models_surfaces_default_when_catalogue_empty(monkeypatch):
    async def fake_list(self, backend):
        return []  # backend unreachable / unauthenticated

    def fake_default(self, role, backend):
        return "qwen2.5:7b" if backend == "local" else "llama-3.3-70b-versatile"

    monkeypatch.setattr("libs.llm.client.LLMClient.list_models", fake_list)
    monkeypatch.setattr("libs.llm.client.LLMClient.default_model", fake_default)

    client = _make_client()
    r = client.get("/v1/chat/models")
    assert r.status_code == 200
    # Even with an empty live list, the default is still offered.
    assert r.json()["backends"]["local"]["models"] == ["qwen2.5:7b"]
