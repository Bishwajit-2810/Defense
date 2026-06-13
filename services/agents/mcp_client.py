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
        # Map logical server name -> streamable-HTTP MCP endpoint URL.
        self._servers: dict[str, str] = {
            "analytics-mcp": _mcp_url(os.getenv("ANALYTICS_MCP_URL", "http://analytics-mcp:8100")),
            "retrieval-mcp": _mcp_url(os.getenv("RETRIEVAL_MCP_URL", "http://retrieval-mcp:8101")),
            "ingest-mcp": _mcp_url(os.getenv("INGEST_MCP_URL", "http://ingest-mcp:8102")),
        }

        # Populated by get_all_manifests(): tool name -> server URL.
        self._tool_routing: dict[str, str] = {}

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

            log.info("manifest_fetched", server=server_name, tool_count=len(tools))

        self._tool_routing = routing
        self._manifest_cache = all_tools
        return all_tools

    def filter_tools(self, manifests: list[dict], tool_names: list[str]) -> list[dict]:
        """Return only the manifest entries whose function name is in tool_names."""
        allowed = set(tool_names)
        return [t for t in manifests if t.get("function", {}).get("name") in allowed]

    def invalidate_manifest_cache(self) -> None:
        """Force the next get_all_manifests() call to re-discover from servers."""
        self._manifest_cache = None
        self._tool_routing = {}

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
