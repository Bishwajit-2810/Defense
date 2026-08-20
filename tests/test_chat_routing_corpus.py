"""A routing corpus for ``POST /v1/chat/agent``: 62 questions, one expected agent each.

Why this file exists
--------------------
Chat routing is one LLM classification whose ONLY signal is the registry's
one-line ``description`` fields (``chat._route`` builds the candidate list from
them at call time). That makes routing quality a property of prose, and prose
has no type checker. The failure that motivated this corpus:

    "What are the most active discussions and sentiment trends across all posts?"

went to a specialist, because ``analyst``'s description read "Natural language
Q&A over campaign corpus" — containing none of the words a general corpus
question actually uses, while every specialist description contained one. The
general-purpose agent was the WORST lexical match for the most ordinary class
of question in the product.

Three tiers, so the corpus is useful without a model running:

  1. Always-on, offline — the catalogue contract (the descriptions still say
     what the router needs), the degraded keyword path, and the endpoint
     plumbing for every case.
  2. Opt-in, live — every case through the REAL router against the real local
     model, asserting an accuracy floor and a hard no-regression subset.
     On qwen2.5:7b the pre-fix catalogue scored 74% (46/62) and misrouted 11
     of the 15 general-corpus questions; the current one scores 94% (58/62)
     and misroutes none of them.

Run the live tier with::

    DEFENSE_LIVE_ROUTER=1 pytest tests/test_chat_routing_corpus.py -k live -s

It uses whatever model the ``llm_a`` role resolves to (``LLM_A_LOCAL_MODEL``,
default ``qwen2.5:7b``) — the same call the endpoint makes, not a replica.
"""

import json
import os
import socket
import sys
import urllib.parse

sys.path.insert(0, "/home/bk/code/defense/src/defense")
sys.path.insert(0, "/home/bk/code/defense/src/defense/services/api")

import pytest
from fastapi.testclient import TestClient

import defense.services.api.main as main  # noqa: E402
import defense.services.api.routers.chat as chat_router  # noqa: E402
from defense.services.agents.registry import AGENT_REGISTRY  # noqa: E402
from defense.services.api.deps import (  # noqa: E402
    get_current_user,
    get_db,
    get_redis,
    rate_limit,
)

# --------------------------------------------------------------------------- #
# The corpus
# --------------------------------------------------------------------------- #
# (question, expected agent or None for plain chat). Questions are phrased the
# way an operator types them — including the sloppy ones, which is where the
# router earns its keep.
#
# CHAT is spelled as None to match what _route returns.
CHAT = None

CASES: tuple[tuple[str, str | None], ...] = (
    # -- analyst: general corpus questions. The regression class. --------------
    ("What are the most active discussions and sentiment trends across all posts?", "analyst"),
    ("How do people feel about the new policy overall?", "analyst"),
    ("Which posts got the most engagement last month?", "analyst"),
    ("What is the overall sentiment in the corpus right now?", "analyst"),
    ("Show me the most discussed posts", "analyst"),
    ("What are people saying about the election?", "analyst"),
    ("How positive or negative are the reactions overall?", "analyst"),
    ("What's the reaction mix on the top posts?", "analyst"),
    ("Give me a sense of what's happening in the data", "analyst"),
    ("Which posts have the highest comment counts?", "analyst"),
    ("How has sentiment moved over the past few weeks?", "analyst"),
    ("What are the busiest threads?", "analyst"),
    ("Summarise the public mood", "analyst"),
    ("Which posts are driving the most reactions?", "analyst"),
    ("What's trending in the comments?", "analyst"),
    # -- stance: opinion toward a named target ---------------------------------
    ("Is Tarek Rahman supported or opposed in this campaign?", "stance"),
    ("What is the stance breakdown for the ruling party?", "stance"),
    ("Who opposes the new education policy?", "stance"),
    ("Are people in favour of the interim government?", "stance"),
    ("How does support for Khaleda Zia compare to opposition?", "stance"),
    ("Show the supportive versus opposing split for each named target", "stance"),
    # -- comparator: two explicit sides ----------------------------------------
    ("Compare sentiment across the two campaigns", "comparator"),
    ("How does this week compare to last week?", "comparator"),
    ("What's the difference between campaign A and campaign B in toxicity?", "comparator"),
    ("Compare engagement on political posts versus sports posts", "comparator"),
    ("Did negativity go up or down compared with the previous month?", "comparator"),
    ("Contrast the two campaigns side by side", "comparator"),
    # -- toxicity ---------------------------------------------------------------
    ("Which posts are the most toxic?", "toxicity"),
    ("Show me the worst hate speech in the comments", "toxicity"),
    ("Where is the harassment concentrated?", "toxicity"),
    ("Are there abusive comments targeting women?", "toxicity"),
    ("Give me representative examples of toxic replies", "toxicity"),
    ("What proportion of comments cross the toxicity threshold?", "toxicity"),
    # -- narrative --------------------------------------------------------------
    ("What themes are emerging in the comments?", "narrative"),
    ("Show me the topic clusters", "narrative"),
    ("Which narratives are declining?", "narrative"),
    ("What counter-narratives are circulating?", "narrative"),
    ("Group the posts into themes for me", "narrative"),
    # -- coverage ---------------------------------------------------------------
    ("Which viral posts have almost no comments analysed?", "coverage"),
    ("How many comments have we actually collected for the top posts?", "coverage"),
    ("Find under-analysed posts and fetch more comments", "coverage"),
    ("Is our comment coverage good enough on the big threads?", "coverage"),
    # -- quality ----------------------------------------------------------------
    ("How reliable are these labels?", "quality"),
    ("What was the ensemble agreement rate?", "quality"),
    ("How much of the labelling came from the LLM versus cheap voters?", "quality"),
    ("Audit the data quality for me", "quality"),
    ("What fraction of the corpus has actually been analysed?", "quality"),
    # -- reporter ---------------------------------------------------------------
    ("Draft an executive report on this campaign", "reporter"),
    ("Write me an intelligence briefing", "reporter"),
    ("I need a structured report for the client", "reporter"),
    ("Produce a full written summary of the campaign", "reporter"),
    # -- alerting ---------------------------------------------------------------
    ("Any negative sentiment spikes in the last 24 hours?", "alerting"),
    ("Alert me to anything unusual right now", "alerting"),
    ("Was there a surge in toxicity today?", "alerting"),
    ("Are there viral posts with no analysis yet?", "alerting"),
    # -- plain chat: no corpus lookup needed ------------------------------------
    ("What does toxicity score actually mean?", CHAT),
    ("How does your stance detection work?", CHAT),
    ("Explain the difference between sentiment and stance", CHAT),
    ("What can you help me with?", CHAT),
    ("Who built this platform?", CHAT),
    ("Thanks, that's helpful", CHAT),
    ("What is a topic cluster, conceptually?", CHAT),
)

ANALYST_CASES = tuple(q for q, a in CASES if a == "analyst")
CHAT_CASES = tuple(q for q, a in CASES if a is CHAT)

# The subset that must never regress: the exact failure this corpus was written
# for, plus one definitional question that must stay out of the agent layer.
# A live run may miss a few of the 62 — a 7B classifier is not an oracle — but
# missing these means the catalogue has lost the property the fix installed.
MUST_HOLD: tuple[tuple[str, str | None], ...] = (
    ("What are the most active discussions and sentiment trends across all posts?", "analyst"),
    ("What is the overall sentiment in the corpus right now?", "analyst"),
    ("Which posts got the most engagement last month?", "analyst"),
    ("Is Tarek Rahman supported or opposed in this campaign?", "stance"),
    ("Compare sentiment across the two campaigns", "comparator"),
    ("Which posts are the most toxic?", "toxicity"),
    ("What themes are emerging in the comments?", "narrative"),
    ("What does toxicity score actually mean?", CHAT),
)

# Live accuracy floor over the whole corpus, measured on qwen2.5:7b:
#
#   pre-fix catalogue   74% (46/62) — 11 of the 15 general questions misrouted
#   current catalogue   94% (58/62) — 0 of the 15 misrouted
#
# The floor sits between the two: high enough that reverting the descriptions
# fails the build, low enough that a 7B classifier's ordinary sampling noise
# does not. If you edit a description, re-run the live tier and move this
# number deliberately — never to make a red build green.
LIVE_ACCURACY_FLOOR = 0.85


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class FakeRedis:
    """Absorbs whatever the usage tracker calls.

    Enumerating methods here was a trap: the live tier logged
    ``usage_tracking_failed: no attribute 'incr'`` on every single question,
    because ``_route`` passes this object to the usage recorder. Routing is
    correct either way — the tracker swallows its own errors — but 62 warnings
    hide the output that matters.
    """

    async def get(self, key):
        return None

    def __getattr__(self, _name):
        async def _noop(*a, **kw):
            return 0

        return _noop


class FakeDb:
    """No tenant-policy row => tenant is not privacy-locked."""

    async def execute(self, *args, **kwargs):
        class _Result:
            def first(self_inner):
                return None

        return _Result()


@pytest.fixture
def client():
    app = main.app

    async def _redis():
        return FakeRedis()

    async def _db():
        return FakeDb()

    async def _user():
        return {"sub": "tester", "tenant_id": "default", "auth_method": "test"}

    app.dependency_overrides[get_redis] = _redis
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[rate_limit] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def router_says(monkeypatch):
    """Stub the routing LLM with a fixed verdict."""

    def _set(agent, reason="because"):
        async def fake_chat(self, role, messages, **kw):
            assert role == "llm_a", "routing must use the cheap role"
            return {
                "content": json.dumps({"agent": agent, "reason": reason}),
                "backend": "local",
                "model": "stub",
                "usage": {},
            }

        monkeypatch.setattr("defense.libs.llm.client.LLMClient.chat", fake_chat)

    return _set


@pytest.fixture
def agents_service_stub(monkeypatch):
    """Intercept the outbound call to the agents service; record its payload."""
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"run_id": "run-1", "status": "completed", "answer": "briefing"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            seen["url"] = url
            seen["payload"] = json
            return _Resp()

    monkeypatch.setattr(chat_router.httpx, "AsyncClient", _Client)
    return seen


# --------------------------------------------------------------------------- #
# Tier 1 — the corpus itself
# --------------------------------------------------------------------------- #


def test_corpus_is_large_and_well_formed():
    assert len(CASES) >= 50, "the routing corpus is the regression surface; keep it >= 50"

    questions = [q for q, _ in CASES]
    assert len(set(questions)) == len(questions), "duplicate question in the corpus"

    for question, expected in CASES:
        assert expected is CHAT or expected in AGENT_REGISTRY, (
            f"{question!r} expects agent {expected!r}, which is not in the registry"
        )

    # Every agent the product ships must have questions pointed at it, or the
    # corpus stops covering it the moment someone adds one.
    covered = {a for _, a in CASES if a is not CHAT}
    assert covered == set(AGENT_REGISTRY), (
        f"agents with no routing cases: {sorted(set(AGENT_REGISTRY) - covered)}"
    )
    assert len(ANALYST_CASES) >= 10, "general corpus questions are the failure class; keep them well covered"
    assert len(CHAT_CASES) >= 5, "the router must also be tested on questions needing no data"


# --------------------------------------------------------------------------- #
# Tier 2 — the catalogue contract (the actual fix, asserted offline)
# --------------------------------------------------------------------------- #


def _catalogue() -> str:
    """The exact candidate list ``_route`` renders into the router prompt."""
    return "\n".join(f"- {a.name}: {a.description}" for a in AGENT_REGISTRY.values())


def test_analyst_description_names_the_general_vocabulary():
    """The bug in prose form: analyst must not be the worst lexical match.

    A general question uses words like "sentiment", "engagement", "posts",
    "trends". When none of them appeared in the analyst description and all of
    them appeared in specialist descriptions, the classifier could only pick a
    specialist.
    """
    desc = AGENT_REGISTRY["analyst"].description.lower()
    for word in ("sentiment", "engagement", "posts", "trends"):
        assert word in desc, (
            f"analyst's description no longer mentions {word!r}; general questions "
            "will be routed to whichever specialist does mention it"
        )
    assert "default" in desc, (
        "analyst must advertise itself as the default, or an unsure classifier "
        "has no safe landing place among nine specialists"
    )
    # The description says "the specialities below", which is only true while
    # analyst is rendered first. Registry order is prose-load-bearing here.
    assert next(iter(AGENT_REGISTRY)) == "analyst", (
        "analyst must stay first in the registry: its description refers to "
        "'the specialities below', and _route renders the catalogue in dict order"
    )


@pytest.mark.parametrize("agent", ["stance", "narrative"])
def test_overlapping_specialists_are_scoped(agent):
    """Specialists whose nouns overlap general questions must say "only when".

    "stance patterns" and "emerging narratives" both read as general opinion
    work to a small model. The qualifier is what keeps them from claiming
    ordinary sentiment traffic.
    """
    desc = AGENT_REGISTRY[agent].description.lower()
    assert "only when" in desc, (
        f"{agent}'s description lost its scoping qualifier; it will start "
        "absorbing general sentiment questions again"
    )


def test_catalogue_is_one_line_per_agent():
    """``_route`` renders one bullet per agent; a newline inside a description
    would silently split it into a fake extra candidate."""
    catalogue = _catalogue()
    assert len(catalogue.splitlines()) == len(AGENT_REGISTRY)
    for line in catalogue.splitlines():
        assert line.startswith("- ") and ": " in line


# --------------------------------------------------------------------------- #
# Tier 3 — the degraded path (router down => keyword fallback)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("question", ANALYST_CASES)
def test_keyword_fallback_never_hijacks_a_general_question(question):
    """When the router is down, a general question must not land on a specialist.

    ``_keyword_agent`` is coarse by design and returning ``None`` is a fine
    answer for it (the caller then answers as plain chat). What it must never
    do is confidently hand an ordinary sentiment question to a specialist —
    that is the same misroute as the LLM bug, just on the degraded path.
    """
    assert chat_router._keyword_agent(question) in (None, "analyst"), (
        f"keyword fallback sends {question!r} to "
        f"{chat_router._keyword_agent(question)!r}"
    )


def test_keyword_fallback_is_deliberately_not_a_prepass(router_says, client):
    """Pins the design decision recorded above ``_KEYWORD_FALLBACK``.

    The coarse table DOES mis-hit a definitional question — that is precisely
    why it is a fallback rather than a pre-pass. If someone ever promotes it to
    run first, this test says what breaks.
    """
    assert chat_router._keyword_agent("What does toxicity score actually mean?") == "toxicity"

    router_says(None, "definitional question")
    resp = client.post(
        "/v1/chat/agent", json={"message": "What does toxicity score actually mean?"}
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "chat", "a working router must beat the keyword table"


# --------------------------------------------------------------------------- #
# Tier 4 — plumbing: every verdict in the corpus dispatches correctly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("question,expected", CASES, ids=[q[:40] for q, _ in CASES])
def test_verdict_dispatches_end_to_end(question, expected, router_says, client, agents_service_stub):
    """For every case in the corpus: the router's verdict reaches the right destination.

    This does not test the classifier's judgement (Tier 5 does). It tests that
    every agent the corpus names is actually reachable through the endpoint —
    a registry entry with a typo'd name, or an agent whose tools fail to
    resolve, shows up here rather than in production.
    """
    router_says(expected, "corpus case")
    resp = client.post("/v1/chat/agent", json={"message": question})
    assert resp.status_code == 200
    body = resp.json()

    if expected is CHAT:
        assert body["mode"] == "chat"
        assert body["agent"] is None
        assert "payload" not in agents_service_stub, "plain chat must not call the agents service"
    else:
        assert body["mode"] == "agent"
        assert body["agent"] == expected
        assert agents_service_stub["payload"]["agent"] == expected
        assert agents_service_stub["payload"]["question"] == question


# --------------------------------------------------------------------------- #
# Tier 5 — live: the real classifier against the real catalogue
# --------------------------------------------------------------------------- #


def _local_llm_reachable() -> bool:
    from defense.libs.common.config import get_settings  # noqa: PLC0415

    parsed = urllib.parse.urlparse(get_settings().local_llm_base_url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=1.5):
            return True
    except OSError:
        return False


live_router = pytest.mark.skipif(
    os.environ.get("DEFENSE_LIVE_ROUTER") != "1" or not _local_llm_reachable(),
    reason="needs DEFENSE_LIVE_ROUTER=1 and a reachable local LLM",
)


async def _live_route(question: str) -> str | None:
    agent, _reason = await chat_router._route(question, [], None, FakeRedis())
    return agent


@live_router
@pytest.mark.asyncio
async def test_live_router_holds_the_no_regression_subset():
    """The cases that must not regress, whatever the overall accuracy."""
    wrong = []
    for question, expected in MUST_HOLD:
        got = await _live_route(question)
        if got != expected:
            wrong.append(f"  {question!r}\n    expected {expected!r}, got {got!r}")
    assert not wrong, "no-regression subset broke:\n" + "\n".join(wrong)


@live_router
@pytest.mark.asyncio
async def test_live_router_accuracy_over_the_whole_corpus():
    """Accuracy floor over the whole corpus, with a per-agent breakdown on failure.

    Printed even on success (``-s``) because the number is the point: it is the
    only measurement of whether a catalogue edit helped or hurt.
    """
    misses = []
    for question, expected in CASES:
        got = await _live_route(question)
        if got != expected:
            misses.append((question, expected, got))

    accuracy = 1 - len(misses) / len(CASES)
    report = "\n".join(
        f"  {q!r}\n    expected {e!r}, got {g!r}" for q, e, g in misses
    )
    print(f"\nlive routing accuracy: {accuracy:.0%} ({len(CASES) - len(misses)}/{len(CASES)})")
    if misses:
        print("misroutes:\n" + report)

    assert accuracy >= LIVE_ACCURACY_FLOOR, (
        f"routing accuracy {accuracy:.0%} is below the {LIVE_ACCURACY_FLOOR:.0%} floor\n{report}"
    )
