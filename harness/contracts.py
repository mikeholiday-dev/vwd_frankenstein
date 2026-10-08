"""Shared contracts between the three workstreams.

Everything that crosses a workstream boundary lives here: data shapes, the
event schema the UI renders, and the Protocols each component implements.
Code against these types and the fakes in `harness/fakes.py`, never against
another workstream's implementation module.

Changing this file is a team decision: open a PR and tag both other owners.
Adding an optional field is fine. Renaming or removing one breaks someone.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import yaml

# --------------------------------------------------------------------------- #
# Capability bundle layout (what the agent writes, what the harness runs)
# --------------------------------------------------------------------------- #

MANIFEST_FILE = "manifest.yaml"
CODE_FILE = "capability.py"  # code_tool: defines `run(**inputs) -> dict`
TESTS_DIR = "tests"  # pytest suite; the harness runs it, never the agent
TEST_ARGV = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", TESTS_DIR]

# Call convention for code tools: the host drops CALL_SHIM_FILE next to
# capability.py and runs `python _frank_call.py` with {"args": {...}} on stdin.
# The shim prints one JSON line: {"ok": true, "output": ...} or {"ok": false, "error": "..."}.
CALL_SHIM_FILE = "_frank_call.py"

# Capabilities with `filesystem: registry_ro` find the read-only registry at
# the path in this env var (Docker mounts it; the fake sandbox points at the dir).
REGISTRY_ENV = "FRANK_REGISTRY"


class Kind(StrEnum):
    CODE_TOOL = "code_tool"
    PROMPT_SKILL = "prompt_skill"
    MCP_SERVER = "mcp_server"


class Phase(StrEnum):
    BUILD = "build"  # network: package index only
    TEST = "test"  # network: manifest domains only
    CALL = "call"  # network: manifest domains only


@dataclass
class Permissions:
    network: list[str] = field(default_factory=list)  # hostnames the proxy lets through
    filesystem: str = "none"  # "none" | "registry_ro"
    secrets: list[str] = field(default_factory=list)  # names only; the proxy injects values


@dataclass
class Manifest:
    """`manifest.yaml` of one capability version (plan §5)."""

    name: str
    version: int
    kind: Kind
    description: str
    interface: dict[str, dict[str, str]]  # {"input": {...}, "output": {...}}
    permissions: Permissions = field(default_factory=Permissions)
    dependencies: list[str] = field(default_factory=list)
    test_dependencies: list[str] = field(default_factory=list)  # installed for test runs only, never for calls
    tests: dict[str, str] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)
    uses: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.name}@v{self.version}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Manifest:
        d = dict(d)
        d["kind"] = Kind(d["kind"])
        d["permissions"] = Permissions(**(d.get("permissions") or {}))
        return cls(**d)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if path.is_dir():
            path = path / MANIFEST_FILE
        return cls.from_dict(yaml.safe_load(path.read_text()))

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    def dump(self, path: Path) -> None:
        if path.is_dir():
            path = path / MANIFEST_FILE
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True))


def permissions_diff(old: Permissions | None, new: Permissions) -> dict[str, dict[str, list[str]]]:
    """What an upgrade adds or removes. Shown on the approval card."""
    old = old or Permissions(filesystem="none")
    diff: dict[str, dict[str, list[str]]] = {"added": {}, "removed": {}}
    for key in ("network", "secrets"):
        a, b = set(getattr(old, key)), set(getattr(new, key))
        if b - a:
            diff["added"][key] = sorted(b - a)
        if a - b:
            diff["removed"][key] = sorted(a - b)
    if old.filesystem != new.filesystem:
        diff["added"]["filesystem"] = [new.filesystem]
        diff["removed"]["filesystem"] = [old.filesystem]
    return diff


# --------------------------------------------------------------------------- #
# Values passed between components
# --------------------------------------------------------------------------- #


@dataclass
class Gap:
    """Structured gap the agent emits when no capability covers a step (plan §4)."""

    gap: str
    why: str
    inputs: dict[str, str]
    outputs: dict[str, str]
    kind: Kind = Kind.CODE_TOOL
    registry_search: str = ""
    id: str = ""


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    run_id: str = ""  # sandbox run id, so a test report can be traced to the run that produced it
    egress_denied: list[str] = field(default_factory=list)  # "host:port" the egress proxy refused during this run


@dataclass
class TestReport:
    __test__ = False  # not a pytest class

    ref: str
    passed: bool
    exit_code: int
    output: str  # full pytest output, logged verbatim
    duration_s: float
    sandbox_run_id: str


@dataclass
class ApprovalRequest:
    id: str
    ref: str
    manifest: dict[str, Any]
    previous: dict[str, Any] | None  # manifest of the active version this replaces
    permissions_diff: dict[str, dict[str, list[str]]]
    test_report: TestReport
    code: str  # capability source, shown on the card


@dataclass
class ApprovalDecision:
    request_id: str
    approved: bool
    by: str  # "operator", "cli", "auto-dev"
    reason: str = ""


@dataclass
class InstallResult:
    ref: str
    installed: bool
    reason: str
    test_report: TestReport | None = None


@dataclass
class RegistryEntry:
    manifest: Manifest
    path: Path  # folder holding exactly this version's bundle; treat as read-only
    status: str = "active"  # "active" | "quarantined"
    installed_at: str = ""


@dataclass
class CallResult:
    call_id: str  # cited by the final answer (provenance check)
    ref: str
    ok: bool
    output: Any = None
    error: str = ""
    duration_s: float = 0.0


# --------------------------------------------------------------------------- #
# Component protocols: one implementation per owner, plus a fake
# --------------------------------------------------------------------------- #


class Sandbox(Protocol):
    """Runs untrusted code. Owner: A. Fake: fakes.LocalSandbox."""

    def run(
        self,
        workdir: Path,
        argv: list[str],
        *,
        phase: Phase,
        network: list[str] = (),
        deps: list[str] = (),
        stdin: str | None = None,
        timeout_s: int | None = None,
        registry_ro: bool = False,
    ) -> SandboxResult: ...


class Registry(Protocol):
    """Versioned store of installed capabilities. Owner: A. Fake: fakes.DirRegistry.

    Only the install gate calls `install`. Nothing else writes to the registry.
    `get` raises KeyError for an unknown name or version.
    """

    root: Path

    def list(self, include_quarantined: bool = False) -> list[RegistryEntry]: ...
    def get(self, name: str, version: int | None = None) -> RegistryEntry: ...  # None = active version
    def install(self, bundle_dir: Path, manifest: Manifest) -> RegistryEntry: ...
    def rollback(self, name: str, version: int) -> RegistryEntry: ...
    def quarantine(self, name: str) -> None: ...


class Approver(Protocol):
    """Blocks until the operator decides. Owner: C. Impls: approvals.LogApprover (UI), fakes.CliApprover, fakes.AutoApprover."""

    def request(self, req: ApprovalRequest) -> ApprovalDecision: ...


class InstallGate(Protocol):
    """Runs the tests itself, asks for approval, installs. Owner: A (kernel/gate.py)."""

    def submit(self, bundle_dir: Path) -> InstallResult: ...


class CapabilityHost(Protocol):
    """Calls installed capabilities in the sandbox. Owner: A (kernel/host.py)."""

    def available(self) -> list[RegistryEntry]: ...
    def call(self, name: str, args: dict[str, Any], version: int | None = None) -> CallResult: ...


# --------------------------------------------------------------------------- #
# Event log schema: the bus between the agent process(es) and the UI
# --------------------------------------------------------------------------- #


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    PLAN = "plan"
    GAP = "gap"
    STUDY = "study"
    BUILD = "build"
    TEST_RUN = "test_run"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"  # written by the UI (or CLI approver)
    INSTALL = "install"
    CALL = "call"
    ANSWER = "answer"
    BUDGET = "budget"
    CAP_HIT = "cap_hit"
    ROLLBACK = "rollback"
    QUARANTINE = "quarantine"
    KILL = "kill"  # written by the UI
    ERROR = "error"
    RUN_FINISHED = "run_finished"


# Keys every event of that type must carry in `data`. Extra keys are allowed.
# The UI may rely on these and nothing else.
REQUIRED_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.RUN_STARTED: ("task",),
    EventType.PLAN: ("text",),
    EventType.GAP: ("gap", "why", "inputs", "outputs", "kind"),
    EventType.STUDY: ("query",),
    EventType.BUILD: ("ref", "role", "attempt"),  # role: builder | tester | repair
    EventType.TEST_RUN: ("ref", "passed", "output"),
    EventType.APPROVAL_REQUESTED: ("id", "ref", "manifest", "permissions_diff", "test_report", "code"),
    EventType.APPROVAL_DECIDED: ("request_id", "approved", "by"),
    EventType.INSTALL: ("ref", "installed", "reason"),
    EventType.CALL: ("call_id", "ref", "ok"),
    EventType.ANSWER: ("text", "call_ids"),
    EventType.BUDGET: ("usd", "turns", "gaps", "minutes", "limits"),
    EventType.CAP_HIT: ("limit", "value", "max"),
    EventType.ROLLBACK: ("name", "version", "by"),
    EventType.QUARANTINE: ("name", "by"),
    EventType.KILL: ("by",),
    EventType.ERROR: ("message",),
    EventType.RUN_FINISHED: ("status",),  # ok | failed | killed | capped
}


@dataclass
class Event:
    id: int  # byte offset of the line in the log; unique and monotonic
    ts: str
    run_id: str
    session: str
    type: EventType
    data: dict[str, Any]


def to_jsonable(obj: Any) -> Any:
    """Dataclasses, enums and Paths → plain JSON values."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, StrEnum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return obj
