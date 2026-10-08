"""Agent loop (plan §4, §5, §8). Owner: B.

One session = one OS process. Flow:
  plan → find a capability → call it, or emit a Gap → study → builder writes
  code + manifest, tester writes tests (separate role/prompt) → registry_install
  (gate) → on failure repair, at most limits.MAX_REPAIRS_PER_GAP → resume with
  refreshed tools → answer citing call ids.

Contract with the rest of the harness:
- use only harness.agent.tools + ctx; never touch registry files directly
- emit PLAN, GAP, BUILD, ANSWER events (schema: contracts.REQUIRED_FIELDS)
- call ctx.budget.turn()/.gap()/.repair()/.charge(...) and ctx.budget.check() between steps
- on start, every active capability in ctx.registry is a tool (fresh-session composition)
- prompts live in prompts/*.md and must pass tests/test_prompt_hygiene.py
- every model call (planner, builder, tester, judge) goes through claude-agent-sdk,
  never the plain `anthropic` client: the SDK runs on a subscription login, the
  plain client needs an API key we don't have (config.AUTH)
- turn off the SDK's built-in tools (Bash, Read/Write, WebFetch, WebSearch, ...):
  allow only our own tools, or the agent gets network access and the gap stops being real (plan §4)
"""

from __future__ import annotations

from harness.wiring import Context

PLANNER_MODEL = "claude-sonnet-5-5"
BUILDER_MODEL = "claude-opus-5-5"
TESTER_MODEL = "claude-sonnet-5-5"
JUDGE_MODEL = "claude-haiku-5-5"


def run_session(task: str, ctx: Context) -> str:
    """Run one task to an answer. Returns the answer text (also emitted as ANSWER)."""
    raise NotImplementedError("stream B")
