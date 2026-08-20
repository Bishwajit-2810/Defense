"""Searching for a post by its identifier.

The two defects these pin down, both reproduced against the live corpus first:

* **Keyword search returned 0 results for a real post id.** It read only
  `post_summary`, `post_text`, `keywords`, `topics` and comment `themes` — never
  `post_id`, `platform_post_id`, `url` or `campaign_id`. The id is the single
  most likely thing to be pasted into a search box, and it matched nothing.
* **Semantic search returned 20 cosine neighbours for a real post id**, none of
  them the post asked for. That is the worse failure: an empty result reads as
  "not found", but twenty ranked results read as a successful search.

The rule the fix encodes: an identifier is *looked up*, not searched for. "Show
me this post" and "show me posts that read like these words" are different
questions and only one of them has a right answer.
"""

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src" / "defense"))

from defense.services.api.routers import search as search_mod  # noqa: E402
from defense.services.api.routers.search import _looks_like_identifier, search  # noqa: E402

USER = {"sub": "tester", "tenant_id": "default"}

POST = {
    "post_id": "cmp58e24s04pgwglq7g9u9jz0",
    "campaign_id": "cmold8r5301u8fu22m7flh3pc",
    "platform_post_id": "122162468462710684",
    "url": "https://facebook.com/122162468462710684",
    "post_summary": "একটি ক্ষুব্ধ মতামত পোস্ট",
}


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeDb:
    """Answers the identifier queries from one post; records every statement."""

    def __init__(self, keyword_hits=0):
        self.keyword_hits = keyword_hits
        self.sql: list[str] = []

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.sql.append(sql)
        p = params or {}

        row = {
            "post_id": POST["post_id"],
            "campaign_id": POST["campaign_id"],
            "snippet": POST["post_summary"],
            "result": {
                "platform_post_id": POST["platform_post_id"],
                "url": POST["url"],
                "post_summary": POST["post_summary"],
            },
        }

        if "LOWER(ar.post_id) = LOWER(:needle)" in sql:          # exact id
            needle = (p.get("needle") or "").lower()
            hit = needle in {
                POST["post_id"].lower(), POST["platform_post_id"].lower(),
                POST["url"].lower(), POST["campaign_id"].lower(),
            }
            return _Rows([row] if hit else [])

        if "LOWER(ar.post_id) LIKE :pattern" in sql and "post_summary') LIKE" not in sql:
            pattern = (p.get("pattern") or "").strip("%").lower()   # partial id
            hit = pattern and any(
                pattern in v.lower()
                for v in (POST["post_id"], POST["platform_post_id"], POST["url"], POST["campaign_id"])
            )
            return _Rows([row] if hit else [])

        if "post_summary') LIKE :pattern" in sql:                 # keyword arm
            return _Rows([row] * self.keyword_hits)

        return _Rows([])


def run(db, q, **kw):
    return asyncio.run(
        search(
            q=q,
            semantic=kw.get("semantic", False),
            mode=kw.get("mode"),
            campaign_id=kw.get("campaign_id"),
            limit=kw.get("limit", 20),
            db=db,
            current_user=USER,
        )
    )


@pytest.fixture
def no_semantic(monkeypatch):
    """Trip a flag if the semantic arm runs, and never load an encoder in a unit test."""
    called = {"n": 0}

    async def _never(*a, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(search_mod, "_semantic_search", _never)
    return called


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["post_id", "platform_post_id", "url", "campaign_id"])
def test_every_identifier_finds_the_post(field, no_semantic):
    res = run(FakeDb(), POST[field])

    assert res.total == 1, f"searching by {field} found nothing"
    assert res.results[0].post_id == POST["post_id"]
    assert res.match_type == "exact_id"
    assert res.id_lookup_missed is False


def test_an_identifier_is_not_answered_by_the_encoder(no_semantic):
    """`?semantic=true` with a post id must return the post, not its neighbours."""
    res = run(FakeDb(), POST["post_id"], semantic=True)

    assert res.match_type == "exact_id"
    assert [r.post_id for r in res.results] == [POST["post_id"]]
    assert no_semantic["n"] == 0, "the vector search ran for an identifier query"
    # An exact match is not a ranked guess, so the UI must not print a similarity.
    assert res.semantic is False


def test_hybrid_mode_too(no_semantic):
    res = run(FakeDb(), POST["post_id"], mode="hybrid")
    assert res.match_type == "exact_id" and res.total == 1
    assert no_semantic["n"] == 0


def test_the_eight_character_prefix_the_table_shows(no_semantic):
    """The posts table renders `post_id.slice(0, 8)` — that is what gets copied."""
    res = run(FakeDb(), POST["post_id"][:8], semantic=True)

    assert res.match_type == "id_prefix"
    assert [r.post_id for r in res.results] == [POST["post_id"]]
    assert no_semantic["n"] == 0


def test_an_id_that_does_not_exist_says_so_rather_than_guessing(no_semantic):
    """The failure mode being closed: 20 plausible, wrong results."""
    res = run(FakeDb(keyword_hits=0), "cmzzzz9999zzzz9999zzzz999", semantic=True)

    assert res.total == 0
    assert res.id_lookup_missed is True
    # Downgraded to keyword: an identifier has nothing to embed.
    assert res.match_type == "keyword"
    assert no_semantic["n"] == 0


def test_an_id_shaped_string_can_still_match_text(no_semantic):
    """The shape test is a guess, so a miss must not throw away a real match."""
    res = run(FakeDb(keyword_hits=2), "122162468462710000", semantic=True)

    assert res.total == 2
    assert res.match_type == "keyword"
    assert res.id_lookup_missed is True  # …but the UI is told not to call it the post


# ---------------------------------------------------------------------------
# Regression guard: ordinary queries must be untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["bangladesh", "disinformation", "মুসলিমদের", "india politics"])
def test_a_word_is_not_mistaken_for_an_identifier(word):
    """A one-word query is ordinary. Requiring a digit is what keeps them apart."""
    assert _looks_like_identifier(word) is False


@pytest.mark.parametrize("ident", [
    POST["post_id"],
    POST["platform_post_id"],
    POST["url"],
    "cmp58e24s0",
])
def test_identifiers_are_recognised(ident):
    assert _looks_like_identifier(ident) is True


def test_short_strings_are_never_identifiers():
    for q in ("a1", "abc", "x9", ""):
        assert _looks_like_identifier(q) is False


def test_a_text_query_still_reaches_the_requested_mode(monkeypatch):
    """The id path must not intercept a normal search."""
    seen = {}

    async def _semantic(db, q, campaign_id, limit, tenant_id="default"):
        seen["q"] = q
        return []

    monkeypatch.setattr(search_mod, "_semantic_search", _semantic)
    res = run(FakeDb(), "bangladesh", semantic=True)

    assert seen.get("q") == "bangladesh", "the semantic arm was skipped for a text query"
    assert res.match_type == "semantic"
    assert res.id_lookup_missed is False


def test_keyword_search_covers_the_identifier_columns():
    """Partial-id matching in the keyword arm is what the Posts page relies on."""
    db = FakeDb(keyword_hits=1)
    run(db, "some free text")

    kw = [s for s in db.sql if "post_summary') LIKE :pattern" in s]
    assert kw, "no keyword query ran"
    for col in ("ar.post_id", "platform_post_id", "'url'", "ar.campaign_id"):
        assert col in kw[0], f"keyword search does not cover {col}"


def test_like_metacharacters_are_still_escaped():
    """A `%` query must not become a wildcard in any of the new predicates."""
    db = FakeDb()
    run(db, "100%_x")

    for sql in db.sql:
        if ":pattern" in sql:
            assert r"ESCAPE '\'" in sql
