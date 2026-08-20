"""Both FastAPI apps must actually run their startup logic.

`@app.on_event("startup")` was deprecated (6 of the suite's 29 warnings came from
it), so both apps moved to a lifespan context. That migration is the kind that
passes every existing test while breaking production: nothing in the suite used
`TestClient` as a context manager, so a lifespan that never fired would look
identical to one that did — until the agents service served a request with
`_agent_runner` still `None` and returned 503 for every run.

These enter the context deliberately.
"""

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src" / "defense"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src" / "defense" / "services" / "api"))

from fastapi.testclient import TestClient  # noqa: E402


def test_api_app_declares_a_lifespan_not_on_event():
    import defense.services.api.main as api

    src = pathlib.Path(api.__file__).read_text(encoding="utf-8")
    # A line-anchored decorator, not a substring: the lifespan's own docstring
    # names the API it replaced, and prose must not fail a test.
    assert not re.search(r"^@app\.on_event\(", src, re.M), (
        "on_event is deprecated; use the lifespan context"
    )
    assert "lifespan=lifespan" in src


def test_api_app_serves_inside_its_lifespan():
    import defense.services.api.main as api

    with TestClient(api.app) as client:
        assert client.get("/v1/health").status_code == 200


def test_agents_app_declares_a_lifespan_not_on_event():
    import defense.services.agents.main as agents

    src = pathlib.Path(agents.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^@app\.on_event\(", src, re.M)
    assert "lifespan=lifespan" in src


def test_agents_startup_wires_the_runner():
    """The half that a skipped lifespan would break silently."""
    import defense.services.agents.main as agents

    with TestClient(agents.app) as client:
        assert client.get("/health").status_code == 200
        # Routes read these module globals; None means every agent run 503s.
        assert agents._mcp_client is not None
        assert agents._run_store is not None
        assert agents._agent_runner is not None


def test_agents_startup_registers_every_agent():
    from defense.services.agents.registry import AGENT_REGISTRY

    # Nine as of 20 Aug 2026 — the count is asserted so a silently-dropped agent
    # shows up here rather than as a 404 in the dashboard's Agents tab.
    assert len(AGENT_REGISTRY) == 9


@pytest.mark.parametrize("mod", [
    "defense.services.api.main",
    "defense.services.agents.main",
])
def test_no_deprecated_startup_hooks_anywhere(mod):
    import importlib

    m = importlib.import_module(mod)
    # `router.on_startup` is where on_event handlers land; a lifespan leaves it
    # empty, so this catches a half-finished migration.
    assert not getattr(m.app.router, "on_startup", []), "an on_event handler is still registered"
    assert not getattr(m.app.router, "on_shutdown", [])
