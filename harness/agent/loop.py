"""Agent loop (plan §4, §5, §8). Owner: B.

One session = one OS process. Flow:
  plan → call an installed capability, or report a Gap → builder writes
  code + manifest, tester writes tests (separate role/prompt) → install gate
  → on failure repair, at most limits.MAX_REPAIRS_PER_GAP → the planner goes on
  with the new capability → answer citing call ids.

The planner is one model session. It never builds anything itself: reporting a
gap hands control to `Session.build`, which the harness drives step by step, so
the caps and the role separation are in code, not in a prompt.

Contract with the rest of the harness:
- use only harness.agent.tools + ctx; never touch registry files directly
- emit PLAN, GAP, BUILD, ANSWER events (schema: contracts.REQUIRED_FIELDS)
- ctx.budget counts every model turn, gap and repair, and is checked before every tool call
- on start, every active capability in ctx.registry is a tool (fresh-session composition);
  capabilities installed mid-session are called through `invoke_capability`
- the operator's attached files (`frank run --attach`) reach capability calls through ctx.host.attach; the
  planner is told their paths, and build roles can open them in `sandbox_exec` (never in the installed bundle)
- prompts live in prompts/*.md and must pass tests/test_prompt_hygiene.py
- every model call goes through model.run_role (claude-agent-sdk, built-in tools off)
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.agent import model, tools
from harness.agent.model import Stop, ToolSpec, obj
from harness.kernel import compose
from harness.contracts import CODE_FILE, MANIFEST_FILE, TESTS_DIR, CallResult, EventType, Gap, InstallResult, Kind, Manifest
from harness.wiring import Context

PLANNER_MODEL = "claude-sonnet-5-5"
BUILDER_MODEL = "claude-opus-5-5"
TESTER_MODEL = "claude-sonnet-5-5"
JUDGE_MODEL = "claude-haiku-5-5"

PROMPTS = Path(__file__).parent / "prompts"
EVENT_FILE_CHARS = 20_000  # per file shown in the console's lab panel
FEEDBACK_CHARS = 8_000  # test output handed to the repair role
ANSWER_RETRIES = 1  # how often an answer without provenance is sent back before it's accepted and flagged


INPUTS_DIR = "_frank_inputs"  # where Host.attach puts the operator's files for a call, relative to its working dir


def run_session(task: str, ctx: Context, attach: list[Path] = ()) -> str:
    """Run one task to an answer. Returns the answer text (also emitted as ANSWER).

    `attach`: the operator's input files for this session. Calls see them read-only at `_frank_inputs/<name>`.
    """
    session = Session(ctx)
    if attach:
        ctx.host.attach(*attach)  # on kernel.host.Host, not yet on the CapabilityHost Protocol
        session.inputs = {Path(p).name: Path(p).resolve() for p in attach}
    return session.run(task)


def prompt(name: str) -> str:
    """A role's system prompt, without the owner comment at the top of the file."""
    return re.sub(r"\A<!--.*?-->\s*", "", (PROMPTS / f"{name}.md").read_text(), flags=re.DOTALL)


@dataclass
class Session:
    ctx: Context
    calls: dict[str, CallResult] = field(default_factory=dict)  # every capability call of this run, by call id
    built: list[str] = field(default_factory=list)
    answer: str | None = None
    rejected_answers: int = 0
    inputs: dict[str, Path] = field(default_factory=dict)  # attached files by name

    # ---- planner ---------------------------------------------------------------

    def run(self, task: str) -> str:
        installed = self.ctx.host.available()
        listing = "\n".join(f"- {e.manifest.ref}: {e.manifest.description} | input {json.dumps(e.manifest.interface.get('input', {}))}" for e in installed)
        text = model.run_role(
            self.ctx, "planner", PLANNER_MODEL, prompt("planner"),
            f"Task:\n{task}\n\n{self._inputs_brief()}Installed capabilities:\n{listing or '(none)'}",
            self.planner_tools([e.manifest for e in installed]),
        )  # fmt: skip
        if self.answer is None:  # the planner stopped without submitting: keep what it said, flagged
            self._emit_answer(text or "(no answer)", [], "missing")
        return self.answer

    def _inputs_brief(self) -> str:
        if not self.inputs:
            return ""
        paths = ", ".join(f"{INPUTS_DIR}/{n}" for n in self.inputs)
        return f"Attached files: {paths}\nA capability call can open these paths. Pass a path as a plain string argument; you cannot read the files yourself.\n\n"

    def planner_tools(self, installed: list[Manifest]) -> list[ToolSpec]:
        ctx = self.ctx
        specs = [
            ToolSpec("record_plan", "Record your plan for the task before acting. Call again if the plan changes.",
                     obj({"text": "string"}), lambda a: self._plan(a["text"])),
            ToolSpec("registry_list", "Manifests of every active installed capability, as raw JSON.", obj(), lambda a: tools.registry_list(ctx)),
            ToolSpec("registry_read", "Manifest, code and tests of one installed capability version (default: the active one).",
                     obj({"name": "string"}, {"version": "integer"}), lambda a: tools.registry_read(ctx, a["name"], a.get("version"))),
            ToolSpec("invoke_capability", "Call an installed capability by name. Returns its output and the call_id to cite.",
                     obj({"name": "string", "args": {"type": "object"}}), lambda a: self._call(a["name"], a.get("args") or {})),
            ToolSpec("report_gap", "Report an operation the task needs that no installed capability provides. The harness then has it "
                     "built, tested and put before the operator, and returns whether it was installed. To extend an installed "
                     "capability instead of adding one, name it in `upgrade`.",
                     obj({"gap": "string", "why": "string", "inputs": {"type": "object"}, "outputs": {"type": "object"}, "registry_search": "string"},
                         {"kind": {"type": "string", "enum": [k.value for k in Kind]}, "upgrade": "string"}), self._gap),
            ToolSpec("registry_retest", "Have the harness re-run an installed capability's stored tests. Returns the test report.",
                     obj({"name": "string"}, {"version": "integer"}), lambda a: tools.registry_retest(ctx, a["name"], a.get("version"))),
            ToolSpec("submit_answer", "Give the final answer. `call_ids` lists the capability calls the answer's facts come from.",
                     obj({"text": "string", "call_ids": {"type": "array", "items": {"type": "string"}}}), self._submit),
        ]  # fmt: skip
        for m in installed:  # fresh-session composition: what earlier sessions installed is a tool from the first turn
            props = {k: {"description": str(v)} for k, v in m.interface.get("input", {}).items()}
            specs.append(ToolSpec(f"cap_{m.name}", f"{m.description} (installed capability {m.ref}; output: {json.dumps(m.interface.get('output', {}))})",
                                  {"type": "object", "properties": props}, lambda a, name=m.name: self._call(name, a)))  # fmt: skip
        return specs

    def _plan(self, text: str) -> str:
        self.ctx.events.emit(EventType.PLAN, text=text)
        return "recorded"

    def _call(self, name: str, args: dict[str, Any]) -> CallResult:
        result = self.ctx.host.call(name, args)
        self.calls[result.call_id] = result
        return result

    def _submit(self, a: dict[str, Any]) -> str:
        """Provenance check (plan §4): the answer has to cite successful capability calls of this run."""
        cited = list(a.get("call_ids") or [])
        unknown = [c for c in cited if c not in self.calls]
        failed = [c for c in cited if c in self.calls and not self.calls[c].ok]
        problem = (
            "it cites no capability calls" if not cited
            else f"these call ids are not from this run: {unknown}" if unknown
            else f"these calls failed, so they carry no data: {failed}" if failed
            else ""
        )  # fmt: skip
        if problem and self.rejected_answers < ANSWER_RETRIES:
            self.rejected_answers += 1
            return f"Not accepted: {problem}. Every fact in the answer must come from a capability call made in this run. Fix it and submit again, or say plainly what you could not establish."
        self._emit_answer(a["text"], cited, "missing" if not cited else "invalid" if problem else "ok")
        raise Stop

    def _emit_answer(self, text: str, call_ids: list[str], provenance: str) -> None:
        if provenance != "ok":
            text = f"[UNVERIFIED: {'no capability calls cited' if provenance == 'missing' else 'cited calls are not valid'}] {text}"
        used = sorted({self.calls[c].ref for c in call_ids if c in self.calls})
        self.ctx.events.emit(EventType.ANSWER, text=text, call_ids=call_ids, provenance=provenance,
                             reused=[r for r in used if r not in self.built], built=self.built)  # fmt: skip
        self.answer = text

    # ---- gap → build → test → install → repair ---------------------------------

    def _gap(self, a: dict[str, Any]) -> dict[str, Any]:
        gap = Gap(a["gap"], a["why"], a.get("inputs") or {}, a.get("outputs") or {}, Kind(a.get("kind") or Kind.CODE_TOOL),
                  a.get("registry_search", ""), id=f"gap-{uuid.uuid4().hex[:6]}")  # fmt: skip
        upgrade = a.get("upgrade") or None
        self.ctx.events.emit(EventType.GAP, **vars(gap), upgrade=upgrade or "")
        self.ctx.budget.gap()
        with self.ctx.budget.building(gap.id):  # its builder, tester and repairs share one turn budget
            result = self.build(gap, upgrade)
        out: dict[str, Any] = {"installed": result.installed, "ref": result.ref, "reason": result.reason}
        if result.installed:
            self.built.append(result.ref)
            name = result.ref.split("@")[0]
            out["how_to_call"] = f"invoke_capability(name={name!r}, args={{...}})"
            out["interface"] = self.ctx.registry.get(name).manifest.interface
        return out

    def build(self, gap: Gap, upgrade: str | None = None) -> InstallResult:
        ctx = self.ctx
        bundle = gap.id
        (ctx.workdir / bundle).mkdir(parents=True, exist_ok=True)
        spec = f"Gap:\n{json.dumps(vars(gap), ensure_ascii=False, indent=2)}\n\n{self._upgrade_brief(upgrade)}"
        if self.inputs:
            spec += (
                f"\nThe operator attached files to this session: {', '.join(f'{INPUTS_DIR}/{n}' for n in self.inputs)}. A capability call can open those paths, "
                "and so can a command you run in the sandbox while building. They are not there when the tests run, and they never become part of the "
                "bundle: tests need their own sample files."
            )

        self._role("builder", BUILDER_MODEL, "builder", spec, bundle, attempt=1)
        self._role("tester", TESTER_MODEL, "tester", spec + "\nThe builder's files are in the workspace. Read them, then write the tests.", bundle, attempt=1)

        attempt = 1
        while True:
            result = tools_install(ctx, bundle)
            if result.installed or result.reason.startswith("rejected by"):
                return result  # an operator's rejection is final: it is their call, not a defect to repair
            ctx.budget.repair(gap.id)  # raises CapExceeded after MAX_REPAIRS_PER_GAP: give up honestly
            attempt += 1
            output = (result.test_report.output if result.test_report else "")[-FEEDBACK_CHARS:]
            feedback = f"{spec}\nThe install gate did not install the bundle.\nReason: {result.reason}\nTest output (the harness ran it):\n{output or '(no tests were run)'}"
            note = self._role("repair", BUILDER_MODEL, "builder", feedback + "\nFix the bundle. You can't edit tests/: if a test itself is wrong, "
                              "end your reply with a line starting TESTS_WRONG: and say why.", bundle, attempt)  # fmt: skip
            if "TESTS_WRONG:" in note:
                complaint = note.split("TESTS_WRONG:", 1)[1].strip()
                self._role("tester", TESTER_MODEL, "tester", feedback + f"\nThe builder says a test is wrong: {complaint}\n"
                           "Check that against the gap and the docs. Fix the tests only if they are wrong.", bundle, attempt)  # fmt: skip

    def _upgrade_brief(self, upgrade: str | None) -> str:
        if not upgrade:
            return "This is a new capability: version 1."
        current = tools.registry_read(self.ctx, upgrade)
        return (
            f"This upgrades the installed capability {upgrade!r}. Keep its name. The new version must be {self._next_version(upgrade)}, "
            f"and it must still pass the current version's tests as well as new ones.\nCurrent version:\n{json.dumps(current, ensure_ascii=False, indent=2)}"
        )

    def _next_version(self, name: str) -> int:
        """Highest installed + 1, which is not always active + 1 after a rollback."""
        v = self.ctx.registry.get(name).manifest.version
        while True:
            try:
                self.ctx.registry.get(name, v + 1)
            except (KeyError, FileNotFoundError):
                return v + 1
            v += 1

    def _exec(self, bundle: str, argv: list[str], deps: list[str] | None) -> dict[str, Any]:
        """`sandbox_exec` with the bundle looking the way a call would see it."""
        root = self.ctx.workdir / bundle
        _unstage(root)  # the harness writes these names; a role's own copies don't count
        problem = _staged(self.inputs, self.ctx, root)
        try:
            if problem:
                return {"exit_code": -1, "stdout": "", "stderr": f"[uses] {problem}"}
            return tools.sandbox_exec(self.ctx, bundle, argv, deps)
        finally:
            _unstage(root)

    def _role(self, role: str, model_id: str, prompt_name: str, brief: str, bundle: str, attempt: int) -> str:
        """Run the builder, the tester or a repair in the bundle dir and log what it wrote."""
        ctx = self.ctx
        root = ctx.workdir / bundle
        before = {p: p.read_text(errors="replace") for p in _files(root)}
        tests_only = role == "tester"

        def inside(path: str) -> Path:
            """`path` relative to the bundle root, refusing anything that resolves outside the bundle."""
            target = tools._in_workspace(ctx, f"{bundle}/{path}")
            if not target.is_relative_to(root.resolve()):
                raise PermissionError(f"{path} is outside the bundle")
            return target.relative_to(root.resolve())

        def write(a: dict[str, Any]) -> str:
            rel = inside(a["path"])
            if (rel.parts[:1] == (TESTS_DIR,)) != tests_only:
                raise PermissionError(f"the {role} writes only {'inside' if tests_only else 'outside'} {TESTS_DIR}/")
            tools.write_file(ctx, f"{bundle}/{rel}", a["content"])
            return str(rel)

        specs = [
            ToolSpec("write_file", f"Write a text file in the bundle. Paths are relative to the bundle root ({MANIFEST_FILE}, {CODE_FILE}, {TESTS_DIR}/...).",
                     obj({"path": "string", "content": "string"}), write),
            ToolSpec("read_file", "Read a file from the bundle.", obj({"path": "string"}), lambda a: tools.read_file(ctx, f"{bundle}/{inside(a['path'])}")),
            ToolSpec("list_files", "List the files in the bundle.", obj(), lambda a: tools.list_files(ctx, bundle)),
            ToolSpec("study", "Search the web (query only) or read one page (query + url). GET only, text only. For documentation, never for task data.",
                     obj({"query": "string"}, {"url": "string"}), lambda a: tools.study(ctx, a["query"], a.get("url"))),
            ToolSpec("sandbox_exec", "Run a command in the bundle inside the sandbox to try something while building, e.g. "
                     '["python", "-c", "..."]. No network except the package index. This is not the install test run.',
                     obj({"argv": {"type": "array", "items": {"type": "string"}}}, {"deps": {"type": "array", "items": {"type": "string"}}}),
                     lambda a: self._exec(bundle, a["argv"], a.get("deps"))),
            ToolSpec("registry_list", "Manifests of every active installed capability.", obj(), lambda a: tools.registry_list(ctx)),
            ToolSpec("registry_read", "Manifest, code and tests of one installed capability.",
                     obj({"name": "string"}, {"version": "integer"}), lambda a: tools.registry_read(ctx, a["name"], a.get("version"))),
        ]  # fmt: skip
        note = model.run_role(ctx, role, model_id, prompt(prompt_name), brief, specs)

        after = {p: p.read_text(errors="replace") for p in _files(root)}
        changed = {str(p.relative_to(root)): text for p, text in after.items() if before.get(p) != text}
        try:
            ref = Manifest.load(root).ref
        except Exception:  # not written yet, or not valid: the gate will say so; the event still needs a ref
            ref = f"{bundle}@v0"
        ctx.events.emit(EventType.BUILD, ref=ref, role=role, attempt=attempt, files=sorted(changed),
                        contents={k: v[:EVENT_FILE_CHARS] for k, v in changed.items()}, note=note[-2000:])  # fmt: skip
        return note


def _staged(session_inputs: dict[str, Path], ctx: Context, root: Path) -> str:
    """Put what a call would find next to the code into the bundle dir: attached files and the `uses` it resolves to.

    Returns why `uses` can't be resolved, or "". `_unstage` removes it all again, so none of it reaches the gate.
    """
    if session_inputs:
        (root / INPUTS_DIR).mkdir(exist_ok=True)
        for name, src in session_inputs.items():
            shutil.copyfile(src, root / INPUTS_DIR / name)
    try:
        manifest = Manifest.load(root)
        if manifest.uses:
            compose.vendor(compose.resolve(ctx.registry, manifest), root)
    except compose.UsesError as e:
        return str(e)
    except Exception:  # no manifest yet, or not a valid one: nothing to resolve
        return ""
    return ""


def _unstage(root: Path) -> None:
    for name in (INPUTS_DIR, *compose.RESERVED):
        target = root / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def tools_install(ctx: Context, bundle: str) -> InstallResult:
    """`tools.registry_install` returns JSON for a model; the build loop needs the InstallResult itself."""
    return ctx.gate.submit(tools._in_workspace(ctx, bundle))


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts)
