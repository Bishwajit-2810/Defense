"""
Agent runner — executes the agentic tool-use loop.

The runner speaks the OpenAI tool_calls protocol:
  1. Build system + user messages
  2. Fetch filtered tool manifests from MCP servers
  3. Call LLM; if it returns tool_calls → dispatch each, append results, loop
  4. If LLM returns a text answer (no tool_calls) → done
  5. Abort after max_tool_calls to enforce the budget cap

Every run records: backend, model, tools_used, tokens_used, run_id.
Post-IDs are extracted from tool results via CUID regex and surfaced as citations.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from .mcp_client import MCPClient
from .registry import AgentDefinition

log = structlog.get_logger("agent-runner")

# CUID / nanoid pattern: 20+ lowercase alphanumeric characters
_POST_ID_RE = re.compile(r"\b[a-z0-9]{20,}\b")

# Maximum tokens the LLM may generate per turn (individual tool-call response
# or the final answer).
_MAX_TOKENS_PER_TURN = 2048

# Temperature for agent reasoning (low = more deterministic tool selection)
_TEMPERATURE = 0.1


@dataclass
class AgentRun:
    run_id: str
    agent_name: str
    query: str
    campaign_id: str | None
    status: str = "running"       # running / completed / failed
    answer: str | None = None
    citations: list[str] = field(default_factory=list)
    tools_used: list[dict] = field(default_factory=list)
    llm_backend: str | None = None
    llm_model: str | None = None
    usage: dict = field(default_factory=dict)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None


def _extract_post_ids(text: str) -> list[str]:
    """Extract CUID-like post IDs from a string."""
    return _POST_ID_RE.findall(text)


def _accumulate_usage(
    total: dict,
    delta: dict,
) -> dict:
    """Merge a new usage dict into the running total."""
    return {
        "prompt_tokens": total.get("prompt_tokens", 0) + delta.get("prompt_tokens", 0),
        "completion_tokens": total.get("completion_tokens", 0) + delta.get("completion_tokens", 0),
        "total_tokens": total.get("total_tokens", 0) + delta.get("total_tokens", 0),
    }


class AgentRunner:
    """Runs the agentic tool-use loop."""

    def __init__(self, llm_client: Any, mcp_client: MCPClient) -> None:
        self.llm = llm_client
        self.mcp = mcp_client

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        agent_def: AgentDefinition,
        query: str,
        campaign_id: str | None = None,
        run_id: str | None = None,
        max_tool_calls: int = 10,
        tenant_policy: Any = None,
        backend_override: str | None = None,
    ) -> AgentRun:
        """Execute the agentic loop with tool use.

        Parameters
        ----------
        agent_def:
            The AgentDefinition from the registry.
        query:
            The user's natural language question or instruction.
        campaign_id:
            Optional campaign to scope context messages.
        run_id:
            Pre-assigned run ID; a new UUID4 is generated if not supplied.
        max_tool_calls:
            Hard budget cap — abort with a partial answer after this many calls.
        tenant_policy:
            TenantPolicy instance forwarded to LLMClient.enforce_policy.
        backend_override:
            Per-request backend selection ("local" / "groq"), subject to policy.

        Returns
        -------
        AgentRun
            Fully populated with status, answer, citations, tools_used, usage.
        """
        effective_run_id = run_id or str(uuid.uuid4())
        run = AgentRun(
            run_id=effective_run_id,
            agent_name=agent_def.name,
            query=query,
            campaign_id=campaign_id,
        )
        run_log = log.bind(run_id=effective_run_id, agent=agent_def.name)
        run_log.info("agent_run_start", query=query[:120])

        try:
            await self._execute(
                run=run,
                agent_def=agent_def,
                query=query,
                campaign_id=campaign_id,
                max_tool_calls=max_tool_calls,
                tenant_policy=tenant_policy,
                backend_override=backend_override,
                run_log=run_log,
            )
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            run.completed_at = time.time()
            run_log.error("agent_run_failed", error=str(exc))

        return run

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    async def _execute(
        self,
        run: AgentRun,
        agent_def: AgentDefinition,
        query: str,
        campaign_id: str | None,
        max_tool_calls: int,
        tenant_policy: Any,
        backend_override: str | None,
        run_log: Any,
    ) -> None:
        # ----------------------------------------------------------------
        # 1. Build initial messages
        # ----------------------------------------------------------------
        system_content = agent_def.system_prompt
        if campaign_id:
            system_content += f"\n\nCurrent campaign context: campaign_id={campaign_id}"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": query},
        ]

        # ----------------------------------------------------------------
        # 2. Fetch and filter tool manifests
        # ----------------------------------------------------------------
        all_manifests = await self.mcp.get_all_manifests()
        tools = self.mcp.filter_tools(all_manifests, agent_def.tools)

        run_log.debug(
            "tools_available",
            count=len(tools),
            names=[t["function"]["name"] for t in tools],
        )

        # ----------------------------------------------------------------
        # 3. Agentic loop
        # ----------------------------------------------------------------
        tool_call_count = 0
        total_usage: dict = {}
        seen_post_ids: set[str] = set()

        while True:
            # Call the LLM with tool definitions via the OpenAI tool_calls API
            llm_response = await self._llm_chat_with_tools(
                agent_def=agent_def,
                messages=messages,
                tools=tools,
                tenant_policy=tenant_policy,
                backend_override=backend_override,
            )

            # Track backend/model from first response
            if run.llm_backend is None:
                run.llm_backend = llm_response.get("backend")
                run.llm_model = llm_response.get("model")

            # Accumulate token usage
            total_usage = _accumulate_usage(
                total_usage, llm_response.get("usage", {})
            )

            # --------------------------------------------------------
            # Parse response: may contain tool_calls, text, or both
            # --------------------------------------------------------
            tool_calls: list[dict] = llm_response.get("tool_calls") or []
            content: str = llm_response.get("content") or ""

            if not tool_calls:
                # No tool calls → final answer
                run.answer = content
                break

            # Budget cap check — stop before executing if cap reached
            if tool_call_count >= max_tool_calls:
                run_log.warning(
                    "budget_cap_reached",
                    tool_call_count=tool_call_count,
                    max_tool_calls=max_tool_calls,
                )
                # Surface whatever partial answer exists
                run.answer = (
                    content
                    or (
                        f"[Budget cap of {max_tool_calls} tool calls reached. "
                        "Analysis may be incomplete.]"
                    )
                )
                break

            # Append the assistant turn (including the tool_calls it requested)
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            # --------------------------------------------------------
            # Execute each requested tool call
            # --------------------------------------------------------
            for tc in tool_calls:
                if tool_call_count >= max_tool_calls:
                    # Append a synthetic error for remaining uncalled tools
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(
                                {"error": "Budget cap reached; tool not executed."}
                            ),
                        }
                    )
                    continue

                tool_name = tc.get("function", {}).get("name", "")
                raw_args = tc.get("function", {}).get("arguments", "{}")

                try:
                    arguments: dict = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except json.JSONDecodeError:
                    arguments = {}

                tool_call_count += 1
                run_log.info(
                    "tool_call",
                    tool=tool_name,
                    call_num=tool_call_count,
                    arguments=arguments,
                )

                try:
                    result = await self.mcp.call_tool(tool_name, arguments)
                    result_str = json.dumps(result, default=str)
                    error_str: str | None = None
                except Exception as exc:
                    result = None
                    result_str = json.dumps({"error": str(exc)})
                    error_str = str(exc)
                    run_log.warning("tool_call_error", tool=tool_name, error=str(exc))

                # Record this tool invocation
                run.tools_used.append(
                    {
                        "tool_call_id": tc["id"],
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "call_number": tool_call_count,
                        "error": error_str,
                    }
                )

                # Extract post IDs from the result for citations
                for pid in _extract_post_ids(result_str):
                    seen_post_ids.add(pid)

                # Append tool result to the conversation
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    }
                )

        # ----------------------------------------------------------------
        # 4. Finalise the run record
        # ----------------------------------------------------------------
        run.citations = sorted(seen_post_ids)
        run.usage = total_usage
        run.status = "completed"
        run.completed_at = time.time()

        run_log.info(
            "agent_run_complete",
            tool_calls=tool_call_count,
            citations=len(run.citations),
            total_tokens=total_usage.get("total_tokens", 0),
        )

    # ------------------------------------------------------------------
    # LLM call that supports the OpenAI tool_calls protocol
    # ------------------------------------------------------------------

    async def _llm_chat_with_tools(
        self,
        agent_def: AgentDefinition,
        messages: list[dict[str, Any]],
        tools: list[dict],
        tenant_policy: Any,
        backend_override: str | None,
    ) -> dict[str, Any]:
        """Call the LLM and return a normalised response dict.

        The existing LLMClient.chat() does not forward tool definitions, so we
        call the underlying AsyncOpenAI client directly through the LLMClient's
        private helpers.  This preserves policy enforcement and model resolution
        while adding tool_calls support.

        Returns a dict with keys: content, tool_calls, usage, model, backend.
        """
        # Resolve backend and model via the existing LLMClient machinery
        from libs.llm.policy import enforce_policy

        effective_backend = enforce_policy(
            tenant_policy=tenant_policy,
            requested_backend=backend_override,
            default_backend=self.llm._default_backend,
        )
        model_id = self.llm._resolve_model(agent_def.llm_role, effective_backend)
        oai_client = self.llm._get_client(effective_backend)

        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "max_tokens": _MAX_TOKENS_PER_TURN,
            "temperature": _TEMPERATURE,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        completion = await oai_client.chat.completions.create(**kwargs)

        choice = completion.choices[0]
        usage = completion.usage
        message = choice.message

        # Normalise tool_calls into a serialisable list
        tool_calls_out: list[dict] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls_out.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )

        return {
            "content": message.content or "",
            "tool_calls": tool_calls_out,
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
            },
            "model": completion.model,
            "backend": effective_backend,
        }
