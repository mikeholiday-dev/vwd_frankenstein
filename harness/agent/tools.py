"""Kernel tools the agent gets (plan §4, §7). Owner: B.

Plain functions over a Context; loop.py wraps them as Agent SDK tools. Keep the
set small and dumb: the agent has no network of its own, so task data can only
come from capability calls, and the discovery/management tooling is something
the agent builds itself on top of these primitives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.contracts import CODE_FILE, TESTS_DIR, Phase, to_jsonable
from harness.wiring import Context


def registry_list(ctx: Context) -> list[dict[str, Any]]:
    """Raw manifests of active capabilities. No search, ranking or health on purpose."""
    return [e.manifest.to_dict() for e in ctx.registry.list()]


def registry_read(ctx: Context, name: str, version: int | None = None) -> dict[str, Any]:
    e = ctx.registry.get(name, version)
    tests = {p.name: p.read_text() for p in sorted((e.path / TESTS_DIR).glob("*.py"))}
    code = (e.path / CODE_FILE).read_text() if (e.path / CODE_FILE).exists() else ""
    return {"manifest": e.manifest.to_dict(), "status": e.status, "code": code, "tests": tests}


def write_file(ctx: Context, path: str, content: str) -> str:
    """Write inside this run's build workspace only."""
    target = _in_workspace(ctx, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return str(target.relative_to(ctx.workdir))


def sandbox_exec(ctx: Context, bundle: str, argv: list[str], deps: list[str] | None = None) -> dict[str, Any]:
    """Try things while building (build phase: package index only). Doesn't count as the install test run."""
    r = ctx.sandbox.run(_in_workspace(ctx, bundle), argv, phase=Phase.BUILD, deps=deps or [])
    return to_jsonable(r)


def registry_install(ctx: Context, bundle: str) -> dict[str, Any]:
    """Hand a bundle to the install gate. The gate runs the tests itself and asks the operator."""
    return to_jsonable(ctx.gate.submit(_in_workspace(ctx, bundle)))


def invoke_capability(ctx: Context, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Generic fallback if hot-loading tools mid-session misbehaves (plan §13)."""
    return to_jsonable(ctx.host.call(name, args))


def registry_propose_rollback(ctx: Context, name: str, version: int) -> dict[str, Any]:
    """TODO(B, with C): emit a proposal the operator confirms in the UI; the UI does the rollback."""
    raise NotImplementedError("stream B")


def study(ctx: Context, query: str, url: str | None = None) -> str:
    """TODO(B): search + read docs. GET only, text only, size-capped, emits a STUDY event.

    Runs on the host (it's team code, not generated code) but must not become a
    data channel: the provenance check flags answers without capability calls.
    """
    raise NotImplementedError("stream B")


def _in_workspace(ctx: Context, path: str) -> Path:
    target = (ctx.workdir / path).resolve()
    if not target.is_relative_to(ctx.workdir.resolve()):
        raise PermissionError(f"{path} is outside the build workspace")
    return target
