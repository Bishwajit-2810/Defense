"""The canonical result JSON in the docs must satisfy the output schema.

`endpoints.md` §1 introduces its example as *"the JSON the whole system exists to
produce"*, and `architecture.md` carries a second copy. Both are what an
integrator codes against — and `endpoints.md`'s copy did **not** validate:

  * `"text_sentiment": "negative"` — a bare label. Schema 1.2 made each component
    sentiment its own `{label, score}` object, because the caption's score is not
    the fused overall one. A consumer reading the doc would have written
    `r.text_sentiment.toUpperCase()` against an object.
  * `"emotion": {"anger": 0.55, …}` — a flat score map, where the schema wants
    `{primary, scores}`; `primary` is the label every consumer actually reads.
  * `"entities": [{"type": …, "value": …}]` — the emitted keys are
    `{text, label, confidence}`.
  * `"schema_version": "1.0"` — three versions behind the builder's `1.3`, and
    version is precisely the field a consumer uses to decide what to expect.
  * No `post_text`, `language_method`, `role_models`, `nlp_engine` or `stub_mode`
    — all of them fields earlier passes added specifically so a result could be
    read in the light of what produced it.

This is the §5.1 species one layer out: the producer emits one shape and the
document that defines the contract describes a different one, and the mismatch
degrades to a silent misunderstanding rather than an error. The schema is the
arbiter, so assert the docs against it instead of proof-reading them.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "libs"))

from schemas.validator import assert_valid_output  # noqa: E402


def _strip_line_comments(block: str) -> str:
    """Drop `//` comments, honouring string literals so a URL survives."""
    out: list[str] = []
    for line in block.splitlines():
        kept, in_string, i = "", False, 0
        while i < len(line):
            ch = line[i]
            if in_string:
                kept += ch
                if ch == "\\":
                    kept += line[i + 1 : i + 2]
                    i += 2
                    continue
                if ch == '"':
                    in_string = False
                i += 1
                continue
            if ch == '"':
                in_string = True
                kept += ch
                i += 1
                continue
            if ch == "/" and line[i + 1 : i + 2] == "/":
                break
            kept += ch
            i += 1
        out.append(kept)
    return "\n".join(out)


def _first_result_object(doc_name: str, fence: str) -> dict:
    """Parse the first fenced JSON(C) block of `doc_name` as a result object."""
    text = (_REPO / doc_name).read_text(encoding="utf-8")
    assert fence in text, f"{doc_name} no longer has a ```{fence} block"
    block = text.split(fence, 1)[1].split("```", 1)[0]
    cleaned = _strip_line_comments(block)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)   # trailing commas
    return json.loads(cleaned)


# The doc, the fence it uses, and the schema_version its example must claim.
_DOCUMENTED_RESULTS = [
    ("endpoints.md", "```jsonc"),
    ("architecture.md", "```json"),
]


@pytest.mark.parametrize("doc_name,fence", _DOCUMENTED_RESULTS)
def test_documented_example_is_a_valid_analysis_result(doc_name, fence):
    result = _first_result_object(doc_name, fence)
    # Raises ValueError listing every violation, which is the useful failure.
    assert_valid_output(result)


@pytest.mark.parametrize("doc_name,fence", _DOCUMENTED_RESULTS)
def test_documented_example_claims_the_current_schema_version(doc_name, fence):
    """A stale version string is the one field a consumer branches on."""
    from services.workers.assembler.builder import SCHEMA_VERSION

    result = _first_result_object(doc_name, fence)
    documented = (result.get("processing") or {}).get("schema_version")
    assert documented == SCHEMA_VERSION, (
        f"{doc_name} documents schema_version {documented!r} but the builder "
        f"emits {SCHEMA_VERSION!r}"
    )


@pytest.mark.parametrize("doc_name,fence", _DOCUMENTED_RESULTS)
def test_documented_example_shows_the_fields_earlier_passes_added(doc_name, fence):
    """Fields added *because* they were being silently dropped must be shown.

    `insight` (§11.1), `post_text` (§11.4f) and the `stub_mode` / `nlp_engine`
    provenance (§9.11) each exist because something computed them and nothing
    read them. A contract document that omits them re-creates the conditions.
    """
    result = _first_result_object(doc_name, fence)
    proc = result.get("processing") or {}

    for field in ("insight", "post_text", "language_method"):
        assert field in result, f"{doc_name}'s example omits `{field}`"
    for field in ("nlp_engine", "stub_mode"):
        assert field in proc, f"{doc_name}'s example omits `processing.{field}`"
