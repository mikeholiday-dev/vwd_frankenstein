"""Builds the component graph for one process. Shared.

This is the only place that picks real vs fake implementations, driven by
config.FAKES / config.APPROVER. When your real component lands, change its
line here (and the FAKES default in config.py) in the same PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness import config, fakes
from harness.contracts import Approver, CapabilityHost, InstallGate, Registry, Sandbox
from harness.kernel.gate import Gate
from harness.kernel.host import Host
from harness.kernel.limits import Budget
from harness.ops.approvals import LogApprover
from harness.ops.events import EventLog


@dataclass
class Context:
    events: EventLog
    budget: Budget
    sandbox: Sandbox
    registry: Registry
    approver: Approver
    gate: InstallGate
    host: CapabilityHost
    workdir: Path  # this run's build workspace; the agent may write only here


def make_registry(root: Path | None = None) -> Registry:
    if "registry" in config.FAKES:
        return fakes.DirRegistry(root or config.REGISTRY_DIR)
    from harness.kernel.registry import GitRegistry

    return GitRegistry(root or config.REGISTRY_DIR)


def make_sandbox() -> Sandbox:
    if "sandbox" in config.FAKES:
        return fakes.LocalSandbox()
    from harness.kernel.sandbox import DockerSandbox

    return DockerSandbox()


def build_context(session: str, *, log_path: Path | None = None, registry_dir: Path | None = None) -> Context:
    events = EventLog(log_path or config.LOG_PATH, session=session)
    sandbox = make_sandbox()
    registry = make_registry(registry_dir)
    approver: Approver = {"ui": LogApprover, "cli": fakes.CliApprover, "auto": fakes.AutoApprover}[config.APPROVER](events)

    workdir = config.WORK_DIR / events.run_id
    workdir.mkdir(parents=True, exist_ok=True)
    return Context(
        events=events,
        budget=Budget(events),
        sandbox=sandbox,
        registry=registry,
        approver=approver,
        gate=Gate(sandbox, registry, approver, events),
        host=Host(sandbox, registry, events),
        workdir=workdir,
    )
