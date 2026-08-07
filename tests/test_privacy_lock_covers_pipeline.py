"""Regression tests: the privacy lock must cover the ANALYSIS PIPELINE.

PROJECT_ASSESSMENT §13.5. §3.3 calls the local⇄Groq policy "the best design
decision in the project", and §5.6 hardened how a tenant is *identified*. Nothing
then checked whether anything downstream **used** that identity, and nothing did:

  * `check_llm_backend_policy` guarded ``options["llm_backend"]`` — an option no
    worker ever read. Stage 1 and Stage 2 both took their backend from the global
    Redis key ``config:llm_backend``. The option's only observable effect was a
    403, which read as the guarantee working.
  * ``PUT /v1/config/llm`` set that global key with no policy check and no role
    check, so any authenticated caller could route **every tenant's** analysis to
    Groq.

The fix resolves the backend at enqueue time, where the tenant is known, and
stamps the decision into the job envelope — the workers have no database and
cannot evaluate a policy themselves.

The two asymmetric outcomes below are the design, not an accident:
an explicit request for groq is REFUSED (it is the caller's own request), while a
global toggle set to groq is silently DOWNGRADED to local (it is not the locked
tenant's choice, and 403-ing them would take them offline whenever an operator
flipped a switch they do not control).
"""

import asyncio
import sys

sys.path.insert(0, '/home/bk/code/defense')
sys.path.insert(0, '/home/bk/code/defense/services/api')

import pytest
from fastapi import HTTPException

import deps
from deps import resolve_llm_backend, tenant_is_privacy_locked


class _Row(tuple):
    pass


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Db:
    """Stub session returning a canned tenant_policies row."""

    def __init__(self, locked: bool | None = False, fail: bool = False):
        self._locked = locked
        self._fail = fail

    async def execute(self, *_a, **_k):
        if self._fail:
            raise RuntimeError("tenant_policies unreadable")
        if self._locked is None:
            return _Result(None)          # no policy row for this tenant
        return _Result(_Row((self._locked,)))


class _Redis:
    def __init__(self, toggle=None, fail=False):
        self._toggle = toggle
        self._fail = fail

    async def get(self, _key):
        if self._fail:
            raise RuntimeError("redis down")
        return self._toggle


LOCKED = {"sub": "u", "tenant_id": "locked-tenant"}
OPEN_T = {"sub": "u", "tenant_id": "open-tenant"}


def _resolve(db, redis, user, options):
    return asyncio.run(resolve_llm_backend(db, redis, user, options))


# ---------------------------------------------------------------------------
# The global toggle can no longer leak a locked tenant's content
# ---------------------------------------------------------------------------


def test_global_toggle_set_to_groq_is_downgraded_for_a_locked_tenant():
    """THE bug. The toggle said groq, the tenant was locked, and its posts went
    to Groq because no worker consulted a policy."""
    out = _resolve(_Db(locked=True), _Redis(toggle="groq"), LOCKED, {})
    assert out == "local"


def test_global_toggle_set_to_groq_is_honoured_for_an_unlocked_tenant():
    out = _resolve(_Db(locked=False), _Redis(toggle="groq"), OPEN_T, {})
    assert out == "groq"


def test_env_default_of_groq_is_also_downgraded(monkeypatch):
    """The env default is the same class of global decision as the toggle."""
    monkeypatch.setenv("LLM_BACKEND", "groq")
    out = _resolve(_Db(locked=True), _Redis(toggle=None), LOCKED, {})
    assert out == "local"


def test_an_explicit_request_for_groq_is_refused_not_downgraded():
    """Asymmetric on purpose: this one IS the caller's own request."""
    with pytest.raises(HTTPException) as exc:
        _resolve(_Db(locked=True), _Redis(toggle=None), LOCKED, {"llm_backend": "groq"})
    assert exc.value.status_code == 403


def test_an_unlocked_tenant_may_request_groq_explicitly():
    out = _resolve(_Db(locked=False), _Redis(toggle=None), OPEN_T, {"llm_backend": "groq"})
    assert out == "groq"


# ---------------------------------------------------------------------------
# Resolution order matches what the workers actually do
# ---------------------------------------------------------------------------


def test_request_option_beats_the_toggle(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    out = _resolve(_Db(locked=False), _Redis(toggle="local"), OPEN_T, {"llm_backend": "groq"})
    assert out == "groq"


def test_toggle_beats_the_env_default(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "groq")
    out = _resolve(_Db(locked=False), _Redis(toggle="local"), OPEN_T, {})
    assert out == "local"


def test_a_broken_redis_falls_back_to_the_env_default(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    out = _resolve(_Db(locked=False), _Redis(fail=True), OPEN_T, {})
    assert out == "local"


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_an_unreadable_policy_table_denies_rather_than_permits():
    """§5.6's rule, preserved: a guarantee that evaporates when the database
    hiccups is not a guarantee."""
    with pytest.raises(HTTPException) as exc:
        _resolve(_Db(fail=True), _Redis(toggle="groq"), LOCKED, {})
    assert exc.value.status_code == 503


def test_a_tenant_with_no_policy_row_is_not_locked():
    assert asyncio.run(tenant_is_privacy_locked(_Db(locked=None), OPEN_T)) is False


# ---------------------------------------------------------------------------
# The decision must REACH the workers
# ---------------------------------------------------------------------------


def test_both_enqueue_paths_stamp_the_resolved_backend_into_the_envelope():
    """Resolving it and not carrying it would leave the original bug intact."""
    for path in (
        '/home/bk/code/defense/services/api/routers/analysis.py',
        '/home/bk/code/defense/services/api/routers/ingest.py',
    ):
        src = open(path, encoding='utf-8').read()
        assert 'resolve_llm_backend(' in src, f"{path} does not resolve the backend"
        assert 'options["llm_backend"] =' in src, f"{path} does not stamp the decision"


def test_the_workers_prefer_the_stamped_backend_over_the_global_toggle():
    """The half that was missing: the option was checked and never read."""
    stage2 = open(
        '/home/bk/code/defense/services/workers/stage2_llm/worker.py', encoding='utf-8'
    ).read()
    assert 'options.get("llm_backend")' in stage2

    stage1 = open(
        '/home/bk/code/defense/services/workers/stage1_nlp/worker.py', encoding='utf-8'
    ).read()
    assert 'stamped_backend' in stage1
    assert '.get("llm_backend")' in stage1


def test_the_global_toggle_endpoint_is_guarded():
    """`PUT /v1/config/llm` had no policy check and no role check at all."""
    src = open(
        '/home/bk/code/defense/services/api/routers/config.py', encoding='utf-8'
    ).read()
    assert 'tenant_is_privacy_locked' in src
    assert '_ADMIN_ROLES' in src


def test_the_config_endpoint_reports_every_role_including_summary():
    """§13.7a: the hand-maintained mirror had drifted and lost `summary`."""
    from libs.llm.client import VALID_ROLES  # noqa: PLC0415
    from routers.config import _models  # noqa: PLC0415

    models = _models()
    for backend in ("local", "groq"):
        assert set(models[backend]) == set(VALID_ROLES)
        assert models[backend]["summary"], "the summary model must be reported"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
