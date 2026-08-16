"""
MCP client that talks to the analytics / retrieval / ingest MCP servers over the
real Model Context Protocol (streamable-HTTP transport via the ``fastmcp`` SDK).

Each server exposes its tools over MCP at ``<base>/mcp``. This client:
  - discovers tools with ``list_tools()`` and adapts each to the OpenAI
    function-call shape the agent runner expects, and
  - dispatches ``call_tool()`` to whichever server advertised the tool.

The public surface (``get_all_manifests``, ``filter_tools``, ``call_tool``,
``invalidate_manifest_cache``) is unchanged, so the agent runner is agnostic to
the transport.
"""

from __future__ import annotations

import os
import re
from typing import Any

import structlog
from fastmcp import Client

log = structlog.get_logger("mcp-client")


def _mcp_url(base: str) -> str:
    """Normalise a server base URL to its streamable-HTTP MCP endpoint.

    Accepts either a bare ``http://host:port`` or one that already ends in
    ``/mcp`` (with or without a trailing slash) so existing deployment env vars
    keep working.
    """
    base = base.rstrip("/")
    if base.endswith("/mcp"):
        return base + "/"
    return base + "/mcp/"


class MCPClient:
    """Calls MCP tools over the protocol (fastmcp streamable-HTTP)."""

    def __init__(self) -> None:
        from defense.libs.common.config import get_settings
        config = get_settings()
        
        # Map logical server name -> streamable-HTTP MCP endpoint URL.
        self._servers: dict[str, str] = {
            "analytics-mcp": _mcp_url(config.analytics_mcp_url),
            "retrieval-mcp": _mcp_url(config.retrieval_mcp_url),
            "ingest-mcp": _mcp_url(config.ingest_mcp_url),
        }

        # Populated by get_all_manifests(): tool name -> server URL.
        self._tool_routing: dict[str, str] = {}

        # Populated alongside it: tool name -> logical server name
        # ("analytics-mcp" / "retrieval-mcp" / "ingest-mcp").
        #
        # The routing table above is keyed by URL, which is what dispatch needs
        # and useless to anyone asking *which MCP answered this*. The runner
        # records a tool trace the operator reads, and "trend_query" alone does
        # not say whether it came from ClickHouse analytics or pgvector
        # retrieval. Keeping the logical name is a dict; deriving it later from
        # a URL is guesswork.
        self._tool_server: dict[str, str] = {}

        # In-process manifest cache: populated lazily on first call.
        self._manifest_cache: list[dict] | None = None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def get_all_manifests(self) -> list[dict]:
        """Discover tools from all MCP servers via ``list_tools()``.

        Returns a combined list of tool definitions in OpenAI function-call
        format. Also (re)builds the tool->server routing table. Results are
        cached in-process; call :meth:`invalidate_manifest_cache` to refresh.

        If a server is unreachable the error is logged and that server's tools
        are omitted (the agent simply won't see them).
        """
        if self._manifest_cache is not None:
            return self._manifest_cache

        all_tools: list[dict] = []
        routing: dict[str, str] = {}
        servers: dict[str, str] = {}

        for server_name, url in self._servers.items():
            try:
                async with Client(url) as client:
                    tools = await client.list_tools()
            except Exception as exc:
                log.warning("manifest_fetch_failed", server=server_name, url=url, error=str(exc))
                continue

            for tool in tools:
                all_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        },
                    }
                )
                routing[tool.name] = url
                servers[tool.name] = server_name

            log.info("manifest_fetched", server=server_name, tool_count=len(tools))

        self._tool_routing = routing
        self._tool_server = servers
        self._manifest_cache = all_tools
        return all_tools

    def server_for(self, tool_name: str) -> str | None:
        """Logical MCP server that advertised ``tool_name``, if discovery ran."""
        return self._tool_server.get(tool_name)

    def filter_tools(self, manifests: list[dict], tool_names: list[str]) -> list[dict]:
        """Return only the manifest entries whose function name is in tool_names."""
        allowed = set(tool_names)
        return [t for t in manifests if t.get("function", {}).get("name") in allowed]

    def schema_for(self, tool_name: str) -> dict | None:
        """JSON Schema for ``tool_name``'s arguments, if discovery has run."""
        for tool in self._manifest_cache or []:
            fn = tool.get("function", {})
            if fn.get("name") == tool_name:
                params = fn.get("parameters")
                return params if isinstance(params, dict) else None
        return None

    def coerce_arguments(self, tool_name: str, arguments: dict) -> tuple[dict, list[str]]:
        """Repair argument shapes and check enum values against the tool's schema.

        Returns ``(arguments, unrepairable)``. ``unrepairable`` is a list of
        operator-readable problems; when it is non-empty the caller must NOT
        dispatch — the messages are written to be handed straight to the model
        so it can retry correctly.

        Only *shape* is repaired, never meaning: a one-element list where a
        scalar belongs is unwrapped, a scalar where a list belongs is wrapped,
        and an enum value is case-corrected when exactly one value matches
        case-insensitively. Anything requiring a choice (a two-element list for
        a single-valued parameter, a value that is not in the enum at all) is
        reported instead of guessed at, because picking one silently answers a
        question nobody asked.

        This exists because the manifest already carried every tool's full
        JSON Schema and used it only to describe tools to the model. Two live
        runs, two ways of getting the same parameter wrong:

        * ``trend_query`` with ``metric=["post_count", "avg_sentiment"]``, then
          a retry with ``["avg_sentiment"]`` — a one-element list, correct in
          meaning, wrong in shape — rejected again. Three calls, no data, and
          the model fabricated a briefing.
        * ``top_posts`` with ``metric="avg_sentiment"``: right shape, and not a
          value the enum contains. The schema said so and the model asked
          anyway, spending a round trip to be told by pydantic.
        """
        schema = self.schema_for(tool_name)
        props = (schema or {}).get("properties")
        if not isinstance(props, dict) or not isinstance(arguments, dict):
            return arguments, []

        repaired = dict(arguments)
        unrepairable: list[str] = []

        for key, value in arguments.items():
            prop = props.get(key)
            if not isinstance(prop, dict):
                continue
            wants_array = _expects_array(prop)
            if wants_array is None:
                continue

            if not wants_array and isinstance(value, list):
                if len(value) == 1:
                    repaired[key] = value[0]
                    log.info("mcp_arg_unwrapped", tool=tool_name, argument=key)
                else:
                    unrepairable.append(_one_value_message(tool_name, key, prop, value))
                    continue
            elif wants_array and not isinstance(value, list) and value is not None:
                repaired[key] = [value]
                log.info("mcp_arg_wrapped", tool=tool_name, argument=key)

            problem = self._check_enum(tool_name, key, prop, repaired)
            if problem:
                unrepairable.append(problem)

        return repaired, unrepairable

    def _check_enum(self, tool_name: str, key: str, prop: dict, repaired: dict) -> str | None:
        """Validate a value against its enum, correcting case but nothing else.

        Case is a spelling difference and safe to fix. Anything else is the
        model asking for something the tool does not offer, and the useful
        answer is the list of what it does — plus, where the tool advertises
        it, what the rows already contain: ``top_posts`` cannot RANK by
        sentiment but returns ``overall_sentiment`` on every row, and not
        knowing that is why the bad call was made.
        """
        allowed = _allowed_values(prop)
        value = repaired.get(key)
        if not allowed or isinstance(value, (list, dict)) or value is None:
            return None
        if value in allowed:
            return None

        if isinstance(value, str):
            matches = [a for a in allowed if isinstance(a, str) and a.lower() == value.lower()]
            if len(matches) == 1:
                repaired[key] = matches[0]
                log.info("mcp_arg_case_corrected", tool=tool_name, argument=key)
                return None

        log.info("mcp_arg_not_in_enum", tool=tool_name, argument=key, value=str(value))
        message = (
            f"{key}={value!r} is not a value {tool_name} accepts, so this call did "
            f"NOT run and returned no data. Valid values: "
            f"{', '.join(repr(a) for a in allowed)}."
        )
        # The hint is about ranking, so it only belongs on a ranking parameter.
        # Appended to `granularity='daily'` it read "trend_query already returns
        # period, count, avg_sentiment ... instead of ranking by it", which is
        # advice about a question the model never asked.
        returns = _returned_fields(self.description_for(tool_name)) if key in _RANKING_KEYS else None
        if returns:
            message += (
                f" Note that {tool_name} already returns {returns} on every row — "
                "if you wanted one of those, call it with a valid value and read "
                "the field from the result instead of ranking by it."
            )
        return message

    def description_for(self, tool_name: str) -> str:
        """The description the model was shown for ``tool_name``."""
        for tool in self._manifest_cache or []:
            fn = tool.get("function", {})
            if fn.get("name") == tool_name:
                return fn.get("description") or ""
        return ""

    def invalidate_manifest_cache(self) -> None:
        """Force the next get_all_manifests() call to re-discover from servers."""
        self._manifest_cache = None
        self._tool_routing = {}
        self._tool_server = {}

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Call a named MCP tool and return its result payload.

        Raises
        ------
        ValueError
            If the tool name is not advertised by any server.
        Exception
            Propagated from the MCP server when the tool itself errors
            (fastmcp raises on tool errors by default); the agent runner
            catches this and records the error for the LLM.
        """
        url = self._tool_routing.get(tool_name)
        if url is None:
            # Routing table may not be built yet (e.g. call before discovery).
            await self.get_all_manifests()
            url = self._tool_routing.get(tool_name)
        if url is None:
            raise ValueError(
                f"Unknown tool: {tool_name!r}. Known tools: {sorted(self._tool_routing)}"
            )

        log.debug("mcp_tool_call", tool=tool_name, url=url, arguments=arguments)

        async with Client(url) as client:
            result = await client.call_tool(tool_name, arguments)

        log.debug("mcp_tool_result", tool=tool_name)
        return _extract_result(result)


def _expects_array(prop: dict) -> bool | None:
    """Does this property want a list? ``None`` when the schema does not say.

    Unknown must stay distinct from False: coercing against a schema we did not
    understand would turn a working call into a broken one.
    """
    declared = prop.get("type")
    if isinstance(declared, str):
        return declared == "array"
    if isinstance(declared, list):  # {"type": ["string", "null"]}
        return "array" in declared
    for branch in prop.get("anyOf") or prop.get("oneOf") or []:
        if isinstance(branch, dict) and _expects_array(branch):
            return True
    # An enum with no declared type is still a single value (Literal[...]).
    if isinstance(prop.get("enum"), list):
        return False
    if any(k in prop for k in ("const", "minimum", "maximum", "pattern")):
        return False
    return None


# Tool descriptions state their result shape with this phrase, so an error
# message can quote it back. The phrase is a convention shared with the MCP
# servers (see analytics_mcp/server.py); when a tool has not adopted it the
# message simply omits the hint rather than guessing at field names.
_RETURNS_RE = re.compile(r"each row includes:?\s*([^.]+)", re.IGNORECASE)

# Parameters that choose what a tool ranks or sorts by — the only ones where
# "you could have read that field from the rows instead" is useful advice.
_RANKING_KEYS = frozenset({"metric", "sort", "sort_by", "order_by", "rank_by"})


def _returned_fields(description: str) -> str | None:
    """The fields a tool's description says come back on every row.

    Whitespace is collapsed because the source is a wrapped docstring, and a
    newline in the middle of the list makes the message it lands in look
    corrupted in a log line.
    """
    match = _RETURNS_RE.search(description or "")
    return " ".join(match.group(1).split()) if match else None


def _allowed_values(prop: dict) -> list | None:
    """Enum values for a property, looking through anyOf/oneOf wrappers."""
    if isinstance(prop.get("enum"), list):
        return prop["enum"]
    for branch in prop.get("anyOf") or prop.get("oneOf") or []:
        if isinstance(branch, dict) and isinstance(branch.get("enum"), list):
            return branch["enum"]
    return None


def _one_value_message(tool_name: str, key: str, prop: dict, value: list) -> str:
    """Tell the model how to fix a multi-value argument on a single-valued field.

    Phrased as an instruction with the retry spelled out, because the raw
    pydantic error ("Input should be 'post_count', 'avg_sentiment' or
    'avg_toxicity'") never said the problem was the list, and the model read it
    as a data problem rather than a call problem.
    """
    allowed = _allowed_values(prop)
    calls = " then ".join(f"{key}={v!r}" for v in value[:3])
    msg = (
        f"{key!r} takes a single value, not a list — you sent {value!r}, so this "
        f"call did NOT run and returned no data. Call {tool_name} once per value: "
        f"{calls}."
    )
    if allowed:
        msg += f" Valid values: {', '.join(repr(v) for v in allowed)}."
    return msg


def _extract_result(result: Any) -> Any:
    """Pull the Python payload out of a fastmcp CallToolResult.

    Prefers the deserialised ``.data``; falls back to ``.structured_content``
    (unwrapping the ``{"result": ...}`` envelope fastmcp adds for non-object
    return types), then to concatenated text content.
    """
    data = getattr(result, "data", None)
    if data is not None:
        return data

    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        # fastmcp wraps non-dict returns (lists/scalars) as {"result": ...}.
        if set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured

    content = getattr(result, "content", None) or []
    texts = [getattr(block, "text", None) for block in content]
    texts = [t for t in texts if t is not None]
    if len(texts) == 1:
        return texts[0]
    return texts
