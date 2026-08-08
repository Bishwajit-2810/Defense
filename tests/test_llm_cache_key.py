"""Regression tests (§5.10 / §6.5): the cache key must name the MODEL, not the role.

``cache._build_key(backend, model, task, hash)`` was called with the role label
("stage2", "vlm") in the ``model`` slot, so the concrete model id
(STAGE2_LOCAL_MODEL, SUMMARY_LOCAL_MODEL, …) was not in the key at all — and
entries live 7 days. Changing the model and re-running the same posts returned
**the previous model's answers**.

That was already a correctness bug, and it is a direct threat to any model
comparison: the §8 Step 2 benchmark ("run BanglaBERT, XLM-R, gemma3:4b,
qwen2.5:7b, llama-3.3-70b and report macro-F1") would silently compare each
model against its own cached output.

Splitting summarization onto its own `summary` role (§6.5) turns the latent bug
active, because two different models are then in play for two different tasks —
which is why §6.5 says to fix the key in the same change, not after it.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from libs.llm.client import VALID_ROLES, LLMClient
from services.workers.stage2_llm import worker as stage2_worker
from services.workers.stage2_llm.cache import _build_key, get_cached, set_cached


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


# ---------------------------------------------------------------------------
# The role exists and is independently configurable
# ---------------------------------------------------------------------------

def test_summary_is_a_real_role():
    assert "summary" in VALID_ROLES


def test_summary_model_is_overridable_independently(monkeypatch):
    monkeypatch.setenv("SUMMARY_LOCAL_MODEL", "gemma4:31b")
    monkeypatch.setenv("STAGE2_LOCAL_MODEL", "qwen2.5:7b")
    llm = LLMClient()
    assert llm.default_model("summary", "local") == "gemma4:31b"
    assert llm.default_model("stage2", "local") == "qwen2.5:7b"


def test_summary_defaults_to_the_stage2_model(monkeypatch):
    """Nothing changes until a bake-off picks a winner."""
    for var in ("SUMMARY_LOCAL_MODEL", "STAGE2_LOCAL_MODEL"):
        monkeypatch.delenv(var, raising=False)
    llm = LLMClient()
    assert llm.default_model("summary", "local") == llm.default_model("stage2", "local")


# ---------------------------------------------------------------------------
# The key distinguishes models
# ---------------------------------------------------------------------------

def test_different_models_get_different_keys():
    a = _build_key("local", "qwen2.5:7b", "summary", "abc123")
    b = _build_key("local", "gemma4:31b", "summary", "abc123")
    assert a != b


def test_model_ids_with_slashes_are_sanitised():
    key = _build_key("groq", "meta-llama/llama-4-scout-17b", "summary", "abc")
    assert "/" not in key.split(":", 1)[1]


@pytest.mark.asyncio
async def test_switching_models_does_not_serve_the_old_answer():
    """The failure §8 Step 2 would otherwise hit, end to end."""
    redis = _FakeRedis()
    await set_cached(redis, "local", "qwen2.5:7b", "summary", "h1", {"post_summary": "A"})

    assert (await get_cached(redis, "local", "qwen2.5:7b", "summary", "h1"))["post_summary"] == "A"
    # The new model must MISS, not inherit qwen's summary.
    assert await get_cached(redis, "local", "gemma4:31b", "summary", "h1") is None


@pytest.mark.asyncio
async def test_cache_can_be_disabled_for_eval_runs(monkeypatch):
    monkeypatch.setenv("LLM_CACHE_DISABLED", "1")
    redis = _FakeRedis()
    await set_cached(redis, "local", "qwen2.5:7b", "summary", "h1", {"post_summary": "A"})
    assert redis.store == {}
    assert await get_cached(redis, "local", "qwen2.5:7b", "summary", "h1") is None


# ---------------------------------------------------------------------------
# The worker passes the resolved id, not the role label
# ---------------------------------------------------------------------------

def test_cache_model_resolves_the_role(monkeypatch):
    monkeypatch.setenv("SUMMARY_LOCAL_MODEL", "gemma4:26b")
    llm = LLMClient()
    assert stage2_worker._cache_model(llm, "summary", "local") == "gemma4:26b"


def test_cache_model_falls_back_to_the_role_label_on_failure():
    """A degraded key beats a failed task — but it must not be the normal path."""
    class _Broken:
        def default_model(self, role, backend):
            raise RuntimeError("no such backend")

    assert stage2_worker._cache_model(_Broken(), "summary", "nonsense") == "summary"


@pytest.mark.asyncio
async def test_run_summary_keys_the_cache_on_the_resolved_model(monkeypatch):
    monkeypatch.setenv("SUMMARY_LOCAL_MODEL", "gemma4:31b")
    seen: list[str] = []

    async def fake_get(redis, backend, model, task, content_hash):
        seen.append(model)
        return None

    async def fake_set(redis, backend, model, task, content_hash, value):
        seen.append(model)

    monkeypatch.setattr(stage2_worker, "get_cached", fake_get)
    monkeypatch.setattr(stage2_worker, "set_cached", fake_set)

    class _LLM(LLMClient):
        async def chat(self, **kwargs):
            return {"content": "একটি সারসংক্ষেপ।", "model": "gemma4:31b",
                    "backend": "local", "truncated": False, "usage": {}}

    class _Redis:
        async def incr(self, k): ...
        async def incrby(self, k, n): ...

    await stage2_worker._run_summary(
        _LLM(), _Redis(),
        {"caption": "কিছু", "language": "bn"}, {}, "summary", "local",
    )

    assert seen and all(m == "gemma4:31b" for m in seen), (
        f"cache keyed on {seen!r} — the role label would collide across models"
    )
