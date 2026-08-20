"""Tests for Agentic RAG Novelty implementation & agent ecosystem expansion.

Covers:
- Verification of all 9 specialized agent definitions in AGENT_REGISTRY
- Verification of new MCP tools:
    * retrieval-mcp: get_clusters
    * analytics-mcp: stance_by_target, stance_over_time, coverage_stats, agreement_stats
- Verification of prompt injection hardening across all registered agents
- Verification of agent types endpoint / metadata
"""

import sys
sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest
from defense.services.agents.registry import (
    AGENT_REGISTRY,
    ANALYST_AGENT,
    COVERAGE_AGENT,
    ALERTING_AGENT,
    STANCE_AGENT,
    COMPARATIVE_AGENT,
    TOXICITY_AGENT,
    NARRATIVE_AGENT,
    QUALITY_AGENT,
    REPORT_AGENT,
)
from defense.mcp_servers.retrieval_mcp import server as retrieval
from defense.mcp_servers.retrieval_mcp.server import (
    _stub_get_clusters,
    _stub_semantic_search,
)
from defense.mcp_servers.analytics_mcp import server as analytics
from defense.mcp_servers.analytics_mcp.server import (
    _handle_agreement_stats,
    _handle_top_posts,
    _stub_agreement_stats,
    _stub_top_posts,
)


# ---------------------------------------------------------------------------
# Fake Postgres plumbing — the stance/coverage tools are Postgres-backed, so the
# tests drive them through a fake session rather than a stub data generator.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _sql, _params=None):
        return _FakeResult(self._rows)


def _fake_pg(rows):
    """A drop-in for retrieval._AsyncSessionLocal that always yields `rows`."""
    return lambda: _FakeSession(rows)


# A post-level target_stances blob in the exact shape aggregate_target_stances()
# writes it (libs/stance_scoring.py).
def _stances(**targets):
    return {
        tid: {
            "display": vals[0],
            "polarity": "neutral",
            "mentions": vals[1] + vals[2] + vals[3],
            "supportive": vals[1],
            "opposing": vals[2],
            "neutral": vals[3],
            "method_breakdown": {vals[4]: vals[1] + vals[2] + vals[3]},
            "method": vals[4],
        }
        for tid, vals in targets.items()
    }


# The stance tools validate target_id against the operator watchlist, which is a
# machine-local config file. These tests assert FILTERING, so they supply their own
# roster — otherwise they would pass or fail on whoever happens to be on the real
# list, and their synthetic ids ("pm", "army") are on nobody's.
def _fake_watchlist(monkeypatch, **ids):
    from defense.libs.stance_targets import Target, Targets

    monkeypatch.setattr(
        retrieval,
        "_WATCHLIST_CACHE",
        Targets(targets=tuple(
            Target(id=tid, display=display, polarity="neutral", aliases=(tid,))
            for tid, display in ids.items()
        )),
    )


def test_all_nine_agents_registered():
    """Ensure all 9 agents are properly registered."""
    expected_agents = {
        "analyst",
        "coverage",
        "alerting",
        "stance",
        "comparator",
        "toxicity",
        "narrative",
        "quality",
        "reporter",
    }
    assert set(AGENT_REGISTRY.keys()) == expected_agents


@pytest.mark.parametrize(
    "agent_name",
    [
        "analyst",
        "coverage",
        "alerting",
        "stance",
        "comparator",
        "toxicity",
        "narrative",
        "quality",
        "reporter",
    ],
)
def test_agent_definition_properties(agent_name):
    """Every agent must have a non-empty prompt, designated tools, and a bounded budget cap."""
    agent = AGENT_REGISTRY[agent_name]
    assert agent.name == agent_name
    assert len(agent.description) > 10
    assert len(agent.system_prompt) > 20
    assert len(agent.tools) > 0
    assert agent.llm_role in ("llm_a", "llm_b", "stage2", "agent")
    assert 5 <= agent.max_tool_calls <= 20


def test_stance_agent_has_stance_tools():
    """Stance agent must possess stance_by_target and stance_over_time tools."""
    stance_agent = AGENT_REGISTRY["stance"]
    assert "stance_by_target" in stance_agent.tools
    assert "stance_over_time" in stance_agent.tools
    assert "semantic_search" in stance_agent.tools


def test_narrative_agent_has_clustering_tools():
    """Narrative agent must have access to embedding clustering tool."""
    narrative_agent = AGENT_REGISTRY["narrative"]
    assert "get_clusters" in narrative_agent.tools
    assert "semantic_search" in narrative_agent.tools


def test_quality_agent_has_audit_tools():
    """Quality agent must possess coverage and agreement audit tools."""
    quality_agent = AGENT_REGISTRY["quality"]
    assert "coverage_stats" in quality_agent.tools
    assert "agreement_stats" in quality_agent.tools


@pytest.mark.asyncio
async def test_stub_get_clusters():
    """Test retrieval-mcp get_clusters stub response shape."""
    clusters = await _stub_get_clusters(campaign_id="test_camp", max_clusters=4)
    assert len(clusters) <= 4
    for c in clusters:
        assert "cluster_id" in c
        assert "size" in c
        assert "dominant_sentiment" in c
        assert "representative_post_id" in c
        assert "representative_summary" in c
        assert "member_post_ids" in c
        assert c["is_stub"] is True


@pytest.mark.asyncio
async def test_stance_by_target_reads_real_target_stances(monkeypatch):
    """stance_by_target must aggregate the per-entity rollup, not post sentiment.

    The pipeline writes stance toward an ENTITY separately from sentiment toward
    the POST (stage2_llm/prompts.py) — reading one as the other reports a number
    that was never measured.
    """
    rows = [
        {"post_id": "p1", "target_stances": _stances(
            pm=("The PM", 2, 8, 1, "llm"),
            army=("Army", 5, 1, 0, "deterministic"),
        )},
        {"post_id": "p2", "target_stances": _stances(
            pm=("The PM", 1, 4, 0, "llm"),
        )},
    ]
    monkeypatch.setattr(retrieval, "_AsyncSessionLocal", _fake_pg(rows))

    out = await retrieval.stance_by_target(campaign_id="cm0camp123456")

    assert [t["target_id"] for t in out] == ["pm", "army"]  # sorted by mentions
    pm = out[0]
    assert pm["supportive"] == 3 and pm["opposing"] == 12 and pm["neutral"] == 1
    assert pm["mentions"] == 16
    assert pm["posts_mentioning"] == 2
    assert pm["dominant_stance"] == "opposing"
    assert pm["net_polarity"] == -9
    assert pm["oppose_share"] == 0.75
    assert pm["method"] == "llm"          # provenance survives the rollup
    assert out[1]["method"] == "deterministic"


@pytest.mark.asyncio
async def test_stance_by_target_filters_and_reports_absence(monkeypatch):
    """A target nobody mentioned is absent, never zero-filled or invented."""
    rows = [{"post_id": "p1", "target_stances": _stances(pm=("The PM", 1, 2, 0, "llm"))}]
    monkeypatch.setattr(retrieval, "_AsyncSessionLocal", _fake_pg(rows))
    _fake_watchlist(monkeypatch, pm="The PM", nobody="Nobody At All")

    only_pm = await retrieval.stance_by_target(campaign_id="all", target_id="pm")
    assert len(only_pm) == 1 and only_pm[0]["target_id"] == "pm"

    # Tracked, but unmentioned in these posts — a real answer, not an error.
    missing = await retrieval.stance_by_target(campaign_id="all", target_id="nobody")
    assert missing == []

    monkeypatch.setattr(retrieval, "_AsyncSessionLocal", _fake_pg([]))
    assert await retrieval.stance_by_target(campaign_id="all") == []


@pytest.mark.asyncio
async def test_stance_tools_reject_an_invented_target(monkeypatch):
    """An id nobody tracks must not read as an empty corpus.

    The model cannot see the watchlist and has no tool that lists it, so asked
    about "the primary political figures" it invents an id. Filtering on it
    returned `[]`, which the stance agent reported as "no stance over time data
    is available" while the one tracked entity had rows in seven posts. The error
    has to name the roster: that is the only way the model learns what to ask for.
    """
    rows = [{"post_id": "p1", "target_stances": _stances(pm=("The PM", 1, 2, 0, "llm"))}]
    monkeypatch.setattr(retrieval, "_AsyncSessionLocal", _fake_pg(rows))
    _fake_watchlist(monkeypatch, pm="The PM")

    for tool in (retrieval.stance_by_target, retrieval.stance_over_time):
        with pytest.raises(ValueError) as exc:
            await tool(campaign_id="all", target_id="primary_political_figures")
        assert "pm" in str(exc.value)          # the roster is in the message
        assert "watchlist" in str(exc.value).lower()

    # The several-in-one-string shape a model reaches for when the question names
    # a group: every part is resolved, and one bad part fails the call.
    with pytest.raises(ValueError) as exc:
        await retrieval.stance_by_target(campaign_id="all", target_id="pm, imran_khan")
    assert "imran_khan" in str(exc.value) and "'pm'" not in str(exc.value)

    # Display names and aliases resolve, so a target named in prose still works.
    by_display = await retrieval.stance_by_target(campaign_id="all", target_id="The PM")
    assert [t["target_id"] for t in by_display] == ["pm"]

    # "all" means the unfiltered query, not an entity nobody tracks.
    assert retrieval._clean_target_ids("all") is None
    assert retrieval._clean_target_ids("  ") is None


@pytest.mark.asyncio
async def test_stance_over_time_buckets_by_period(monkeypatch):
    """stance_over_time returns one row per (period, target) from real rollups."""
    from datetime import datetime

    rows = [
        {"post_id": "p1", "period": datetime(2026, 8, 1),
         "target_stances": _stances(pm=("The PM", 1, 3, 0, "llm"))},
        {"post_id": "p2", "period": datetime(2026, 8, 1),
         "target_stances": _stances(pm=("The PM", 2, 1, 0, "llm"))},
        {"post_id": "p3", "period": datetime(2026, 8, 2),
         "target_stances": _stances(pm=("The PM", 0, 5, 0, "llm"))},
    ]
    monkeypatch.setattr(retrieval, "_AsyncSessionLocal", _fake_pg(rows))
    _fake_watchlist(monkeypatch, pm="The PM")

    series = await retrieval.stance_over_time(campaign_id="all", granularity="day", target_id="pm")

    assert len(series) == 2
    assert series[0]["period"].startswith("2026-08-01")
    assert series[0]["supportive"] == 3 and series[0]["opposing"] == 4
    assert series[1]["net_polarity"] == -5


@pytest.mark.asyncio
async def test_coverage_stats_reports_real_denominators(monkeypatch):
    """Coverage is analysed-over-collected, and stub embeddings are disclosed."""
    row = {
        "total_posts": 10,
        "analyzed_posts": 6,
        "reported_comments": 1000,
        "stored_comments": 250,
        "analyzed_comments": 240,
        "mean_post_coverage": 0.25,
        "coverage_anomalies": 1,
        "posts_with_embedding": 6,
        "stub_embeddings": 2,
    }
    monkeypatch.setattr(retrieval, "_AsyncSessionLocal", _fake_pg([row]))

    stats = await retrieval.coverage_stats(campaign_id="cm0camp123456")

    # analyzed_posts is NOT total_posts: the fraction has to be able to be < 1.
    assert stats["post_analysis_coverage"] == 0.6
    assert stats["comment_coverage"] == 0.25
    assert stats["stub_embeddings"] == 2
    assert stats["stub_embedding_share"] == round(2 / 6, 4)
    assert any("stub" in n for n in stats["notes"])
    assert any("no analysis result" in n for n in stats["notes"])


def test_agreement_stats_is_derived_not_hardcoded(monkeypatch):
    """Every share must come from a column; an audit that invents numbers is not one."""
    monkeypatch.setattr(analytics, "STUB_MODE", False)

    calls: list[str] = []

    def fake_query(sql, params=None):
        calls.append(sql)
        if "GROUP BY method" in sql:
            return [{"method": "ensemble", "n": 60}, {"method": "llm", "n": 40}]
        return [{
            "comments": 100,
            "mean_agreement": 0.9,
            "unanimous": 55,
            "abstained": 7,
            "unread": 3,
            "propagated": 10,
            "single_voter": 25,
        }]

    monkeypatch.setattr(analytics, "_ch_query", fake_query)
    stats = _handle_agreement_stats("cm0camp123456")

    # Reads the per-comment table, not the post-level one.
    assert all("comment_sentiments" in sql for sql in calls)
    assert stats["unanimous_share"] == 0.55
    assert stats["abstained_share"] == 0.07
    assert stats["single_voter_share"] == 0.25
    assert stats["unread_share"] == 0.03
    assert stats["propagated_share"] == 0.1
    assert stats["method_breakdown"] == {"ensemble": 60, "llm": 40}
    assert stats["is_stub"] is False

    # Same tool, different corpus -> different numbers (nothing is a constant).
    monkeypatch.setattr(analytics, "_ch_query", lambda sql, params=None: (
        [{"method": "fast", "n": 50}] if "GROUP BY method" in sql else
        [{"comments": 50, "mean_agreement": 0.4, "unanimous": 5, "abstained": 20,
          "unread": 0, "propagated": 0, "single_voter": 25}]
    ))
    other = _handle_agreement_stats("cm0camp999999")
    assert other["abstained_share"] == 0.4
    assert other["quality_verdict"] == "contested_labels"


def test_agreement_stats_stub_is_flagged():
    """Stub-mode figures must announce themselves — a quality audit above all."""
    stats = _stub_agreement_stats("cm0camp123456")
    assert stats["is_stub"] is True
    assert any("SYNTHETIC" in n for n in stats["notes"])


@pytest.mark.parametrize("junk", [
    "CUID/UUID of the campaign to query.",
    "the campaign id",
    "<campaign_id>",
])
def test_placeholder_campaign_id_is_rejected_not_widened(junk):
    """A garbled id must not silently become a whole-corpus query."""
    for mod in (analytics, retrieval):
        with pytest.raises(ValueError):
            mod._clean_campaign_id(junk)


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("all", None), ("*", None),
    ("cm0abc123456789", "cm0abc123456789"),
    ("7f3e4b21-1c9a-4a51-9b0e-2f1d3c4b5a60", "7f3e4b21-1c9a-4a51-9b0e-2f1d3c4b5a60"),
])
def test_campaign_id_normalisation(value, expected):
    for mod in (analytics, retrieval):
        assert mod._clean_campaign_id(value) == expected


def test_top_posts_rejects_unknown_metric():
    """Ranking by a different metric than asked answers a different question."""
    with pytest.raises(ValueError):
        _handle_top_posts("cm0camp123456", metric="engagement_rate")


def test_top_posts_min_toxicity_filter():
    """The toxicity agent's threshold filter actually filters."""
    rows = _stub_top_posts("cm0camp123456", "toxicity_score", 20, min_toxicity=0.5)
    assert rows, "stub corpus should retain some posts above the threshold"
    assert all(r["toxicity_score"] >= 0.5 for r in rows)
    assert len(rows) < len(_stub_top_posts("cm0camp123456", "toxicity_score", 20))


@pytest.mark.asyncio
async def test_store_delete_and_clear():
    """Test AgentRunStore delete and clear_all methods."""
    from defense.services.agents.store import AgentRunStore
    from defense.services.agents.runner import AgentRun

    store = AgentRunStore(redis=None)
    run1 = AgentRun(run_id="run_1", agent_name="analyst", query="q1", campaign_id="c1")
    run2 = AgentRun(run_id="run_2", agent_name="stance", query="q2", campaign_id="c1")

    await store.save(run1)
    await store.save(run2)

    recent = await store.list_recent()
    assert len(recent) == 2

    # Delete single run
    deleted = await store.delete("run_1")
    assert deleted is True
    assert await store.get("run_1") is None
    assert len(await store.list_recent()) == 1

    # Clear all
    cleared = await store.clear_all()
    assert cleared == 1
    assert len(await store.list_recent()) == 0

