"""Regression tests: every LLM caller must reach the usage counters.

PROJECT_ASSESSMENT §13.4. `_track_usage` lived in the Stage-2 worker and was
called from nowhere else, so five other callers spent tokens that reached no
counter — `/v1/chat`, `/v1/chat/stream`, the report narrative, the per-cluster
report summaries, Stage-1's LLM analyzer (live whenever `STAGE1_LLM=true`) and
the agent runner — while `scope_note` reported "All figures are system-wide."

The fix moved tracking down into `LLMClient`, which every one of those callers
already goes through. So the test that matters is **structural**: a new call site
must be counted without its author having to know a counter exists. §13.4 did not
happen because someone made a mistake; it happened because five call sites were
added over time and nothing made the omission visible.

That is what `test_no_llm_caller_bypasses_the_client` asserts, via the AST rather
than a grep, so a comment mentioning `chat.completions.create` cannot satisfy it.
"""

import ast
import asyncio
import pathlib
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from libs.llm import usage as usage_mod
from libs.llm.usage import (
    ALL_LANES,
    LANE_AGENT,
    LANE_COMMENT,
    LANE_INTERACTIVE,
    LANE_POST,
    LANE_STAGE1,
    PIPELINE_LANES,
    track_usage,
)

_REPO = pathlib.Path('/home/bk/code/defense')

#: Modules permitted to call the OpenAI SDK directly. Exactly one: the client
#: that provides the breaker, the failover, truncation recovery and the counters.
_SDK_ALLOWLIST = {"libs/llm/client.py"}


class _FakeRedis:
    """Records counter operations instead of performing them."""

    def __init__(self):
        self.incr_calls: list[str] = []
        self.incrby_calls: list[tuple[str, int]] = []
        self.sadd_calls: list[tuple[str, str]] = []

    async def incr(self, key):
        self.incr_calls.append(key)

    async def incrby(self, key, n):
        self.incrby_calls.append((key, n))

    async def sadd(self, key, member):
        self.sadd_calls.append((key, member))


def _py_files():
    for base in ("services", "libs"):
        for path in (_REPO / base).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


# ---------------------------------------------------------------------------
# Structural: nothing may reach around LLMClient
# ---------------------------------------------------------------------------


def test_no_llm_caller_bypasses_the_client():
    """`chat.completions.create` may appear in exactly one module.

    The agent runner called it directly (§13.6), which cost it the circuit
    breaker, the Groq→local failover, truncation recovery AND usage tracking in
    one move. Any future caller that does the same loses the same four things.
    """
    offenders = []
    for path in _py_files():
        rel = str(path.relative_to(_REPO))
        if rel in _SDK_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # match `<...>.chat.completions.create(...)`
            f = node.func
            if (
                isinstance(f, ast.Attribute) and f.attr == "create"
                and isinstance(f.value, ast.Attribute) and f.value.attr == "completions"
                and isinstance(f.value.value, ast.Attribute) and f.value.value.attr == "chat"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "these modules call the OpenAI SDK directly, bypassing LLMClient's "
        f"circuit breaker, failover, truncation recovery and usage counters: {offenders}"
    )


def test_nothing_outside_libs_llm_touches_the_clients_private_helpers():
    """The general form of §13.6: private helpers are how the bypass happened."""
    private = {"_get_client", "_resolve_model", "_breakers", "_default_backend"}
    offenders = []
    for path in _py_files():
        rel = str(path.relative_to(_REPO))
        if rel.startswith("libs/llm/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in private:
                offenders.append(f"{rel}:{node.lineno} ({node.attr})")
    assert not offenders, f"private LLMClient helpers used outside libs/llm: {offenders}"


def test_the_stage2_worker_is_no_longer_the_only_place_that_counts():
    """`track_usage` must live in libs, not in one worker."""
    assert (_REPO / "libs/llm/usage.py").exists()
    client_src = (_REPO / "libs/llm/client.py").read_text(encoding="utf-8")
    assert "track_usage" in client_src, "LLMClient must record its own calls"


# ---------------------------------------------------------------------------
# Every caller labels its lane
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected_task",
    [
        ("services/api/routers/chat.py", "chat"),
        ("services/api/routers/chat.py", "chat_stream"),
        ("services/api/routers/reports.py", "report_narrative"),
        ("services/api/routers/reports.py", "report_cluster_summary"),
        ("services/workers/stage1_nlp/llm_analyzer.py", "stage1_post"),
        ("services/workers/stage1_nlp/llm_analyzer.py", "stage1_comments"),
        ("services/agents/runner.py", "agent"),
        ("services/workers/stage2_llm/worker.py", "summary"),
        ("services/workers/stage2_llm/worker.py", "comment_stance"),
    ],
)
def test_each_caller_names_its_usage_task(path, expected_task):
    """Counted is not enough — the spend has to be attributable."""
    src = (_REPO / path).read_text(encoding="utf-8")
    assert f'usage_task="{expected_task}"' in src


def test_pipeline_lanes_exclude_the_interactive_ones():
    """§6.8's per-post cost model must not absorb a chatbot session."""
    assert set(PIPELINE_LANES) == {LANE_POST, LANE_COMMENT, LANE_STAGE1}
    assert LANE_INTERACTIVE not in PIPELINE_LANES
    assert LANE_AGENT not in PIPELINE_LANES
    assert set(PIPELINE_LANES).issubset(set(ALL_LANES))


# ---------------------------------------------------------------------------
# The counter semantics §12.4b established must survive the move
# ---------------------------------------------------------------------------


def test_a_fresh_call_increments_the_cache_hit_rate_denominator():
    r = _FakeRedis()
    asyncio.run(track_usage(
        r,
        {"usage": {"total_tokens": 100}, "backend": "local", "model": "qwen2.5:7b"},
        lane=LANE_POST, task="summary",
    ))
    assert "usage:llm_calls" in r.incr_calls
    assert "usage:calls:task:summary" in r.incr_calls
    assert ("usage:tokens:total", 100) in r.incrby_calls
    assert ("usage:tokens:lane:post", 100) in r.incrby_calls
    assert ("usage:tokens:local:qwen2.5:7b", 100) in r.incrby_calls


def test_a_cache_hit_counts_the_task_but_not_the_api_call():
    """§12.4b: `usage:llm_calls` is fresh-only because cache_hit_rate divides by
    it; `usage:calls:task:{task}` counts cached and fresh alike. The asymmetry is
    deliberate — do not "fix" it into consistency."""
    r = _FakeRedis()
    asyncio.run(track_usage(r, cache_hit=True, lane=LANE_POST, task="summary"))
    assert "usage:cache_hits" in r.incr_calls
    assert "usage:calls:task:summary" in r.incr_calls
    assert "usage:llm_calls" not in r.incr_calls


def test_tracking_never_raises_when_redis_is_broken():
    """A cost counter must not be able to fail a request."""

    class _Broken:
        async def incr(self, *_a):
            raise RuntimeError("redis down")

        async def incrby(self, *_a):
            raise RuntimeError("redis down")

        async def sadd(self, *_a):
            raise RuntimeError("redis down")

    asyncio.run(track_usage(_Broken(), {"usage": {"total_tokens": 5}}, lane=LANE_POST))


def test_tracking_is_a_noop_without_a_redis_and_without_a_default(monkeypatch):
    monkeypatch.setattr(usage_mod, "_shared_redis_failed", True)
    asyncio.run(track_usage(None, {"usage": {"total_tokens": 5}}))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Behavioural: LLMClient.chat must actually increment, not merely import
# ---------------------------------------------------------------------------
#
# The structural tests above all pass if `chat()` imports `track_usage` and never
# calls it — mutation-testing `if track:` -> `if False:` proved exactly that. So
# drive the real method with a stubbed backend and watch the counters.


class _Usage:
    prompt_tokens = 10
    completion_tokens = 20
    total_tokens = 30


class _Msg:
    content = "hello"
    tool_calls = None


class _Choice:
    message = _Msg()
    finish_reason = "stop"


class _Completion:
    choices = [_Choice()]
    usage = _Usage()
    model = "qwen2.5:7b"


def _client_with_stubbed_backend(monkeypatch):
    from libs.llm.client import LLMClient  # noqa: PLC0415

    client = LLMClient()

    async def _fake_call_api(*_a, **_k):
        return _Completion()

    monkeypatch.setattr(client, "_call_api", _fake_call_api)
    return client


def test_llmclient_chat_increments_the_counters(monkeypatch):
    """The regression that structural tests miss."""
    client = _client_with_stubbed_backend(monkeypatch)
    r = _FakeRedis()

    out = asyncio.run(client.chat(
        role="llm_b",
        messages=[{"role": "user", "content": "hi"}],
        backend_override="local",
        usage_redis=r,
        usage_lane=LANE_INTERACTIVE,
        usage_task="chat",
    ))

    assert out["content"] == "hello"
    assert "usage:llm_calls" in r.incr_calls, "chat() did not record the call"
    assert "usage:calls:lane:interactive" in r.incr_calls
    assert "usage:calls:task:chat" in r.incr_calls
    assert ("usage:tokens:total", 30) in r.incrby_calls
    assert ("usage:tokens:local:qwen2.5:7b", 30) in r.incrby_calls


def test_llmclient_chat_can_opt_out(monkeypatch):
    """`track=False` for offline evals, which must not pollute the counters."""
    client = _client_with_stubbed_backend(monkeypatch)
    r = _FakeRedis()

    asyncio.run(client.chat(
        role="llm_b",
        messages=[{"role": "user", "content": "hi"}],
        backend_override="local",
        usage_redis=r,
        track=False,
    ))
    assert r.incr_calls == []
    assert r.incrby_calls == []


def test_llmclient_chat_defaults_the_task_to_the_role(monkeypatch):
    client = _client_with_stubbed_backend(monkeypatch)
    r = _FakeRedis()
    asyncio.run(client.chat(
        role="llm_b",
        messages=[{"role": "user", "content": "hi"}],
        backend_override="local",
        usage_redis=r,
    ))
    assert "usage:calls:task:llm_b" in r.incr_calls
