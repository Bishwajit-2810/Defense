"""Regression tests (§11.2 / §11.3): the analytics layer must only read what
something actually writes, and must not double-count re-analysed posts.

Three defects of the same species — a query whose source cannot deliver — are
pinned here. None was reachable without a live ClickHouse/Postgres, which is
exactly why they survived: the stub paths returned plausible numbers and the
real paths were never exercised.

  1. Re-analysis is a first-class operation, but `analysis_events` is an
     append-only MergeTree, so a post analysed twice had two rows and every
     aggregate counted it twice. Fixed on the read side with `LIMIT 1 BY
     post_id` over `ORDER BY inserted_at DESC`.
  2. `get_reaction_mix` read `FROM reaction_events` — a table no migration ever
     created and no writer ever populated. It raised in any non-stub deployment.
  3. `GET /v1/usage` summed tokens out of a Postgres `llm_cache` table that has
     never had a writer (the Stage-2 cache is Redis-only).

These assert the SQL the handlers actually emit — captured by stubbing the
ClickHouse driver — rather than grepping the source, so a comment mentioning a
table name cannot pass or fail a test.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

# Derived from __file__, not hardcoded: an absolute repo path makes these tests
# unable to run against a copy of the tree, which is what a mutation-testing
# pass (§9.12) does to check they fail when a fix is reverted.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import mcp_servers.analytics_mcp.server as analytics  # noqa: E402
_CH_INIT = (_REPO / 'services' / 'workers' / 'assembler' / 'clickhouse_init.sql').read_text(encoding='utf-8')
_USAGE_SRC = (_REPO / 'services' / 'api' / 'routers' / 'usage.py').read_text(encoding='utf-8')
_INIT_DB = (_REPO / 'deploy' / 'init-db.sql').read_text(encoding='utf-8')

_DATES = {"from_date": "2026-01-01", "to_date": "2026-12-31"}


@pytest.fixture
def emitted_sql(monkeypatch):
    """Run every real-path analytics handler and collect the SQL it emits."""
    captured: list[str] = []

    def _capture(sql, params=None):
        captured.append(sql)
        return []

    monkeypatch.setattr(analytics, "STUB_MODE", False)
    monkeypatch.setattr(analytics, "_ch_query", _capture)

    analytics._handle_trend_query("c1", **_DATES)
    analytics._handle_sentiment_over_time("c1", **_DATES)
    analytics._handle_top_posts("c1", metric="total_reactions", limit=10, **_DATES)
    analytics._handle_reaction_mix("c1", **_DATES)

    assert len(captured) == 4, "every handler must have issued exactly one query"
    return captured


# ---------------------------------------------------------------------------
# 1. Re-analysis must not double-count
# ---------------------------------------------------------------------------

def test_every_aggregate_dedups_to_one_row_per_post(emitted_sql):
    """`analysis_events` is append-only, so aggregates must collapse per post.

    Re-analysis (`POST /v1/analysis/run`) appends a second row for the same
    post. Without this, `count()` counts it twice, `avg()` weights it twice, and
    a top-N list can show it twice — and the skew tracks whichever posts were
    re-run, which is exactly what a live "flip the backend and re-run" demo
    does.
    """
    for sql in emitted_sql:
        assert "analysis_events" in sql
        assert "LIMIT 1 BY post_id" in sql, (
            f"this aggregate counts a re-analysed post more than once:\n{sql}"
        )


def test_dedup_keeps_the_newest_row(emitted_sql):
    """Latest-wins, matching the Postgres upsert's ON CONFLICT semantics."""
    for sql in emitted_sql:
        order_at = sql.find("ORDER BY inserted_at DESC")
        limit_at = sql.find("LIMIT 1 BY post_id")
        assert order_at != -1, f"no inserted_at ordering, so LIMIT BY keeps an arbitrary row:\n{sql}"
        assert order_at < limit_at, f"ordering must precede LIMIT BY:\n{sql}"


def test_dedup_is_inside_the_subquery_not_after_aggregation(emitted_sql):
    """Collapsing after a GROUP BY would deduplicate the wrong thing."""
    for sql in emitted_sql:
        if "GROUP BY" not in sql:
            continue
        limit_at = sql.find("LIMIT 1 BY post_id")
        assert limit_at != -1, f"no dedup at all in an aggregating query:\n{sql}"
        assert limit_at < sql.index("GROUP BY"), (
            f"dedup must happen before aggregation:\n{sql}"
        )


# ---------------------------------------------------------------------------
# 2. No query may name a ClickHouse table nothing creates
# ---------------------------------------------------------------------------

def test_queries_only_read_tables_the_migration_creates(emitted_sql):
    created = set(re.findall(r'CREATE TABLE IF NOT EXISTS\s+(\w+)', _CH_INIT, re.IGNORECASE))
    queried: set[str] = set()
    for sql in emitted_sql:
        queried |= {t for t in re.findall(r'FROM\s+([a-z_][a-z0-9_]*)', sql)}

    assert queried, "no table names were extracted — the parser is wrong, not the code"
    missing = queried - created
    assert not missing, (
        f"analytics_mcp queries ClickHouse table(s) that clickhouse_init.sql never "
        f"creates: {sorted(missing)}. `reaction_events` was one of these — "
        f"get_reaction_mix raised in every non-stub deployment."
    )


def test_reaction_mix_reads_the_post_level_table(emitted_sql):
    reaction_sql = emitted_sql[3]
    assert "reaction_events" not in reaction_sql
    assert "FROM analysis_events" in reaction_sql


def test_reaction_columns_are_created_read_and_written(emitted_sql):
    """The reaction-mix columns need a schema, a reader AND a writer."""
    from services.workers.assembler.persistence import _REACTION_TYPES

    reaction_sql = emitted_sql[3]
    for kind in _REACTION_TYPES:
        assert f'{kind}_count' in _CH_INIT, f'{kind}_count missing from clickhouse_init.sql'
        assert f'{kind}_count' in reaction_sql, f'{kind}_count not read by the reaction-mix query'


def test_the_writer_maps_reaction_breakdown_onto_those_columns():
    from services.workers.assembler.persistence import _reaction_columns

    cols = _reaction_columns({'reaction_breakdown': {'LIKE': 10, 'sad': 3, 'ANGRY': '4'}})
    assert cols['like_count'] == 10       # upper-case key, as Stage 1 re-emits it
    assert cols['sad_count'] == 3         # lower-case key, as the normalizer emits it
    assert cols['angry_count'] == 4       # numeric string coerced
    assert cols['care_count'] == 0        # absent type defaults to 0


@pytest.mark.parametrize("junk", [{}, None, {'LIKE': None}, {'LIKE': 'not-a-number'}, []])
def test_reaction_columns_survive_junk_input(junk):
    from services.workers.assembler.persistence import _reaction_columns

    cols = _reaction_columns({'reaction_breakdown': junk})
    assert len(cols) == 7
    assert all(isinstance(v, int) for v in cols.values())


def test_insert_column_list_matches_the_row_dict():
    """A column-list/row-dict mismatch silently shifts every value one over."""
    from services.workers.assembler import persistence

    src = Path(persistence.__file__).read_text(encoding='utf-8')
    column_list = src.split('"INSERT INTO analysis_events "')[1].split('VALUES"')[0]
    for kind in persistence._REACTION_TYPES:
        assert f'{kind}_count' in column_list, (
            f'{kind}_count is in the row dict but missing from the INSERT column list'
        )


# ---------------------------------------------------------------------------
# 3. /v1/usage must not read a table nothing writes
# ---------------------------------------------------------------------------

def _usage_sql_literals() -> list[str]:
    """Every string constant in usage.py — comments are not in the AST."""
    tree = ast.parse(_USAGE_SRC)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_usage_endpoint_issues_no_query_against_llm_cache():
    """The Stage-2 cache is Redis-only; the Postgres table had no writer.

    Reading it produced a permanent 0, a dead `cache_hit_rate` fallback, and —
    because that query was not wrapped like the Redis block — a 500 from
    /v1/usage on any deployment whose init-db.sql had not been applied.
    """
    for literal in _usage_sql_literals():
        assert not re.search(r'FROM\s+llm_cache', literal, re.IGNORECASE), (
            f"usage.py still queries llm_cache:\n{literal}"
        )


def test_no_dangling_cache_row_variable():
    """The dead fallback's variables must be gone, not just unreferenced."""
    tree = ast.parse(_USAGE_SRC)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert 'cache_rows' not in names
    assert 'token_row' not in names


def test_llm_cache_table_is_not_created_either():
    assert not re.search(r'CREATE TABLE IF NOT EXISTS\s+llm_cache', _INIT_DB, re.IGNORECASE)


def test_nothing_writes_a_postgres_llm_cache_table():
    """If a writer is ever added, this should fail and the removal reconsidered."""
    hits = [
        str(p)
        for p in (_REPO / 'services').rglob('*.py')
        if re.search(r'INSERT\s+INTO\s+llm_cache', p.read_text(encoding='utf-8'), re.IGNORECASE)
    ]
    assert not hits, f"a writer for the Postgres llm_cache table appeared in {hits}"


# ---------------------------------------------------------------------------
# 4. The inverse: no migration may create a ClickHouse table nothing writes
# ---------------------------------------------------------------------------
# §11.3/§11.3b pinned "no query reads a table nothing creates". The mirror image
# went unpinned, and `llm_usage` was sitting in clickhouse_init.sql the whole
# time with no writer and no reader anywhere in the tree — a schema whose only
# other mention was a prose line in the assessment. An unfilled shape is how the
# `llm_cache` defect started: it looks like a source until someone points a
# reader at it. Both directions are asserted now.


def _python_sources() -> list[Path]:
    return [
        p
        for d in ('services', 'mcp_servers', 'libs', 'eval')
        for p in (_REPO / d).rglob('*.py')
    ]


def test_every_clickhouse_table_created_has_a_writer():
    created = set(re.findall(r'CREATE TABLE IF NOT EXISTS\s+(\w+)', _CH_INIT, re.IGNORECASE))
    assert created, 'no CREATE TABLE found — the parser is wrong, not the schema'

    written: set[str] = set()
    for path in _python_sources():
        src = path.read_text(encoding='utf-8')
        written |= {t.lower() for t in re.findall(r'INSERT\s+INTO\s+([a-z_][a-z0-9_]*)', src, re.IGNORECASE)}

    unwritten = {t for t in created if t.lower() not in written}
    assert not unwritten, (
        f"clickhouse_init.sql creates table(s) nothing writes: {sorted(unwritten)}. "
        f"`llm_usage` was one of these — created, never written, never read. "
        f"Either add the writer or drop the table; do not leave a shape nothing fills."
    )
