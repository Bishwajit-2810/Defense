"""
HTTP client that calls MCP server REST endpoints.

Each MCP server exposes:
  GET  /mcp/manifest  — returns {"tools": [...]} in OpenAI function-call format
  POST /tools/call    — {"tool_name": str, "arguments": dict} → {"result": ..., "error": str|null}

MCPClient routes tool calls to the correct server and caches manifests for the
lifetime of the process (manifests are static once a server is deployed).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

log = structlog.get_logger("mcp-client")

# Manifest fetch timeout is generous; individual tool calls get 30 s.
_MANIFEST_TIMEOUT = 10.0
_TOOL_CALL_TIMEOUT = 30.0


class MCPClient:
    """Calls MCP tool endpoints over HTTP."""

    def __init__(self) -> None:
        # Read MCP server URLs from env
        self.analytics_url = os.getenv("ANALYTICS_MCP_URL", "http://analytics-mcp:8100")
        self.retrieval_url = os.getenv("RETRIEVAL_MCP_URL", "http://retrieval-mcp:8101")
        self.ingest_url = os.getenv("INGEST_MCP_URL", "http://ingest-mcp:8102")

        # Map tool names to their server base URL
        self._tool_routing: dict[str, str] = {
            # analytics-mcp tools
            "trend_query": self.analytics_url,
            "sentiment_over_time": self.analytics_url,
            "top_posts": self.analytics_url,
            "reaction_mix": self.analytics_url,
            # retrieval-mcp tools
            "semantic_search": self.retrieval_url,
            "get_post": self.retrieval_url,
            "get_thread": self.retrieval_url,
            "representative_comments": self.retrieval_url,
            # ingest-mcp tools
            "pull_campaign": self.ingest_url,
            "fetch_more_comments": self.ingest_url,
            "refresh_post": self.ingest_url,
        }

        # In-process manifest cache: populated lazily on first call.
        self._manifest_cache: list[dict] | None = None

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Call a named MCP tool and return the result.

        Raises
        ------
        ValueError
            If the tool name is not in the routing table.
        RuntimeError
            If the MCP server returns an error payload.
        httpx.HTTPStatusError
            If the HTTP request itself fails (4xx / 5xx).
        """
        base_url = self._tool_routing.get(tool_name)
        if not base_url:
            raise ValueError(
                f"Unknown tool: {tool_name!r}. "
                f"Known tools: {sorted(self._tool_routing)}"
            )

        log.debug("mcp_tool_call", tool=tool_name, arguments=arguments)

        async with httpx.AsyncClient(timeout=_TOOL_CALL_TIMEOUT) as client:
            resp = await client.post(
                f"{base_url}/tools/call",
                json={"tool_name": tool_name, "arguments": arguments},
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("error"):
            raise RuntimeError(f"MCP tool error [{tool_name}]: {data['error']}")

        log.debug("mcp_tool_result", tool=tool_name)
        return data["result"]

    async def get_all_manifests(self) -> list[dict]:
        """Fetch tool definitions from all three MCP servers.

        Calls GET /mcp/manifest on each server, collects all tool definition
        objects from the returned ``tools`` lists, and returns a single
        combined list in OpenAI function-call format.

        Results are cached in-process so subsequent calls are free.

        If a server is unreachable the error is logged and that server's tools
        are omitted from the result (the agent will simply not see those tools).
        """
        if self._manifest_cache is not None:
            return self._manifest_cache

        all_tools: list[dict] = []
        servers = {
            "analytics-mcp": self.analytics_url,
            "retrieval-mcp": self.retrieval_url,
            "ingest-mcp": self.ingest_url,
        }

        async with httpx.AsyncClient(timeout=_MANIFEST_TIMEOUT) as client:
            for server_name, base_url in servers.items():
                try:
                    resp = await client.get(f"{base_url}/mcp/manifest")
                    resp.raise_for_status()
                    payload = resp.json()
                    # MCP servers return either a bare list of tool definitions
                    # or a {"tools": [...]} wrapper — accept both.
                    tools = payload if isinstance(payload, list) else payload.get("tools", [])
                    all_tools.extend(tools)
                    log.info(
                        "manifest_fetched",
                        server=server_name,
                        tool_count=len(tools),
                    )
                except Exception as exc:
                    log.warning(
                        "manifest_fetch_failed",
                        server=server_name,
                        url=base_url,
                        error=str(exc),
                    )

        self._manifest_cache = all_tools
        return all_tools

    def filter_tools(
        self, manifests: list[dict], tool_names: list[str]
    ) -> list[dict]:
        """Return only the manifest entries whose function name is in tool_names."""
        allowed = set(tool_names)
        return [
            t for t in manifests
            if t.get("function", {}).get("name") in allowed
        ]

    def invalidate_manifest_cache(self) -> None:
        """Force the next get_all_manifests() call to re-fetch from servers."""
        self._manifest_cache = None
