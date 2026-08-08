"""Unit tests for libs/sentiment_models.py — language-aware sentiment routing."""

import importlib
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

import libs.sentiment_models as sm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start each test from the shipped defaults (only XLM-R available)."""
    for key in ("SENTIMENT_MODEL", "BANGLABERT_SENTIMENT_MODEL",
                "BANGLISHBERT_SENTIMENT_MODEL", "MBERT_SENTIMENT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(sm)
    yield
    importlib.reload(sm)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def test_only_xlmr_available_by_default():
    assert sm.is_available("xlmr") is True
    assert sm.is_available("banglabert") is False
    assert sm.is_available("banglishbert") is False
    assert sm.is_available("mbert") is False


def test_configuring_env_makes_slot_available(monkeypatch):
    monkeypatch.setenv("BANGLABERT_SENTIMENT_MODEL", "csebuetnlp/banglabert-sent")
    importlib.reload(sm)
    assert sm.is_available("banglabert") is True
    assert sm.configured_hf_name("banglabert") == "csebuetnlp/banglabert-sent"


# ---------------------------------------------------------------------------
# Auto-route + fallback
# ---------------------------------------------------------------------------

def test_all_buckets_fall_back_to_xlmr_when_no_bangla_checkpoints():
    assert sm.resolve("bengali", False, "bn")[0] == "xlmr"
    assert sm.resolve("latin", True, "bn")[0] == "xlmr"       # banglish
    assert sm.resolve("latin", False, "en")[0] == "xlmr"
    assert set(sm.route_table().values()) == {"xlmr"}


def test_bengali_routes_to_banglabert_when_configured(monkeypatch):
    monkeypatch.setenv("BANGLABERT_SENTIMENT_MODEL", "csebuetnlp/banglabert-sent")
    importlib.reload(sm)
    key, hf = sm.resolve("bengali", False, "bn")
    assert key == "banglabert"
    assert hf == "csebuetnlp/banglabert-sent"
    # English still routes to XLM-R
    assert sm.resolve("latin", False, "en")[0] == "xlmr"
    rt = sm.route_table()
    assert rt["bn"] == "banglabert" and rt["en"] == "xlmr"


def test_banglish_routes_to_banglishbert_when_configured(monkeypatch):
    monkeypatch.setenv("BANGLISHBERT_SENTIMENT_MODEL", "csebuetnlp/banglishbert-sent")
    importlib.reload(sm)
    assert sm.resolve("latin", True, "bn")[0] == "banglishbert"   # banglish bucket
    assert sm.resolve("mixed", False, "bn")[0] == "banglishbert"  # mixed -> banglish
    assert sm.resolve("bengali", False, "bn")[0] == "xlmr"        # pure bn still falls back


# ---------------------------------------------------------------------------
# Manual override
# ---------------------------------------------------------------------------

def test_unavailable_override_is_ignored_and_auto_routes():
    # mbert has no checkpoint -> override ignored -> falls back to auto-route
    assert sm.resolve("latin", False, "en", override="mbert")[0] == "xlmr"


def test_available_override_wins_over_language(monkeypatch):
    monkeypatch.setenv("BANGLABERT_SENTIMENT_MODEL", "csebuetnlp/banglabert-sent")
    importlib.reload(sm)
    # English text but forced to BanglaBERT
    assert sm.resolve("latin", False, "en", override="banglabert")[0] == "banglabert"


# ---------------------------------------------------------------------------
# Options serialization
# ---------------------------------------------------------------------------

def test_options_status_shape():
    opts = sm.options_status()
    keys = {o["key"] for o in opts}
    assert {"xlmr", "banglabert", "banglishbert", "mbert"} <= keys
    xlmr = next(o for o in opts if o["key"] == "xlmr")
    assert xlmr["available"] is True
    assert xlmr["hf_name"] == "cardiffnlp/twitter-xlm-roberta-base-sentiment"
