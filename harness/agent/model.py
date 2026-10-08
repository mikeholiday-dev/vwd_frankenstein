"""One model role = one Agent SDK session over our own tools. Owner: B.

Every model call in the harness goes through `run_role`. It turns off all of the
SDK's built-in tools (no Bash, file or web access) and loads no project settings,
so the model can only act through the ToolSpecs it is handed. Tests replace
`run_role` with a scripted stand-in, so nothing else in the agent imports the SDK.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harness.contracts import EventType, to_jsonable
from harness.kernel.limits import MAX_AGENT_TURNS, CapExceeded
from harness.ops.approvals import Killed
from harness.wiring import Context

SERVER = "frank"
BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent", "Skill"]
# Integrations the local Claude Code install may add on its own (the browser extension gives real web access).
HOST_INTEGRATIONS = ["mcp__claude-in-chrome", "mcp__computer-use"]
TOOL_TIMEOUT_MS = 60 * 60 * 1000  # an install blocks on the operator's approval
STOP_GRACE_TURNS = 3  # turns a stopped model gets to end on its own before the stream is cut


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]  # JSON Schema of the arguments
    fn: Callable[[dict[str, Any]], Any]  # plain and blocking; the result is sent to the model as JSON


class Stop(Exception):
    """A tool ends the session on purpose (the answer was accepted)."""


def obj(required: dict[str, Any] | None = None, optional: dict[str, Any] | None = None) -> dict[str, Any]:
    """JSON Schema for a tool's arguments. Values are a type name ("string") or a full schema."""
    props = {k: {"type": v} if isinstance(v, str) else v for k, v in {**(required or {}), **(optional or {})}.items()}
    return {"type": "object", "properties": props, "required": list(required or {})}


def run_role(ctx: Context, role: str, model: str, system: str, prompt: str, tools: list[ToolSpec]) -> str:
    """Run one role to the end of its turn. Returns its final text. Raises CapExceeded or Killed."""
    return asyncio.run(_run(ctx, role, model, system, prompt, tools))


async def _run(ctx: Context, role: str, model: str, system: str, prompt: str, tools: list[ToolSpec]) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage, TextBlock, create_sdk_mcp_server, query, tool

    raised: list[BaseException] = []  # a cap, the kill switch or Stop, raised inside a tool call
    denied: list[str] = []  # tools the model tried that aren't ours

    def wrap(spec: ToolSpec):
        @tool(spec.name, spec.description, spec.schema)
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            if raised:
                return _result("stopped by the harness", error=True)
            try:
                ctx.budget.check()
                out = await asyncio.to_thread(spec.fn, args)
            except (CapExceeded, Killed, Stop) as e:
                raised.append(e)
                return _result("stopped by the harness; end your turn now", error=not isinstance(e, Stop))
            except Exception as e:  # the model gets the error and can react; nothing here is fatal to the run
                return _result(f"{type(e).__name__}: {e}", error=True)
            return _result(out if isinstance(out, str) else json.dumps(to_jsonable(out), ensure_ascii=False))

        return handler

    allowed = [f"mcp__{SERVER}__{t.name}" for t in tools]

    async def only_ours(hook_input: dict[str, Any], _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        """Whatever else the local install exposes, the model may use our tools and nothing more."""
        name = hook_input.get("tool_name", "")
        if name in allowed:
            return {}
        denied.append(name)
        reason = f"{name} is not available to this agent"
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}

    async def one_message():  # hooks need streaming input
        yield {"type": "user", "message": {"role": "user", "content": prompt}}

    options = ClaudeAgentOptions(
        tools=[],  # no built-in tools: the capability gap has to be real (plan §4)
        disallowed_tools=BUILTIN_TOOLS + HOST_INTEGRATIONS,
        allowed_tools=allowed,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[only_ours])]},
        mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=[wrap(t) for t in tools])},
        strict_mcp_config=True,
        setting_sources=[],  # never the project's CLAUDE.md or README: they name the APIs (rule 2)
        system_prompt=system,
        model=model,
        cwd=str(ctx.workdir),
        max_turns=MAX_AGENT_TURNS,
        env={"MCP_TOOL_TIMEOUT": str(TOOL_TIMEOUT_MS)},
    )
    text: list[str] = []
    final = ""
    after_stop = 0
    # Once a cap, the kill switch or Stop is raised, every tool call is refused, so the model can only end its turn.
    # The stream is drained to its result instead of cut: cutting it mid-turn leaves the SDK's subprocess half closed.
    async for msg in query(prompt=one_message(), options=options):
        if isinstance(msg, AssistantMessage):
            text = [b.text for b in msg.content if isinstance(b, TextBlock)] or text
            if raised:
                after_stop += 1
                if after_stop > STOP_GRACE_TURNS:
                    break
                continue
            try:
                ctx.budget.turn()
            except (CapExceeded, Killed) as e:
                raised.append(e)
        elif isinstance(msg, ResultMessage):
            final = msg.result or ""
            if msg.total_cost_usd and all(isinstance(e, Stop) for e in raised):  # an accepted answer still costs
                try:
                    ctx.budget.charge_usd(msg.total_cost_usd)
                except (CapExceeded, Killed) as e:
                    raised.append(e)
    if denied:
        ctx.events.emit(EventType.ERROR, message=f"{role} tried tools outside the harness; all were refused: {sorted(set(denied))}")
    for e in raised:
        if not isinstance(e, Stop):
            raise e
    return final or "\n".join(text)


def _result(text: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": error}
