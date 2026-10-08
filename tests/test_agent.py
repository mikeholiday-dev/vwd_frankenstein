"""Agent loop with a scripted stand-in for the model. Owner: B.

The script drives the same ToolSpecs a real model session gets, so the gap →
build → test → install → repair flow, the caps and the provenance check all run
for real over the fake sandbox and registry. Only the model's choices are scripted.
"""

import json

import pytest

from conftest import BUNDLES
from harness import fakes
from harness.agent import loop, model, tools
from harness.contracts import EventType
from harness.kernel.gate import Gate
from harness.kernel.host import Host
from harness.kernel.limits import MAX_REPAIRS_PER_GAP, Budget, CapExceeded
from harness.wiring import Context

OK = {f: (BUNDLES / "echo_ok" / f).read_text() for f in ("capability.py", "manifest.yaml", "tests/test_unit.py")}
BROKEN = (BUNDLES / "echo_broken" / "capability.py").read_text()
GAP = {"gap": "repeat a text", "why": "nothing installed repeats text", "inputs": {"text": "string"}, "outputs": {"echo": "string"}, "registry_search": "registry empty"}


@pytest.fixture
def ctx(events, tmp_path):
    sandbox, registry, approver = fakes.LocalSandbox(), fakes.DirRegistry(tmp_path / "registry"), fakes.AutoApprover(events)
    workdir = tmp_path / "work"
    workdir.mkdir()
    return Context(events, Budget(events), sandbox, registry, approver, Gate(sandbox, registry, approver, events), Host(sandbox, registry, events), workdir)


@pytest.fixture
def script(monkeypatch):
    """Replace the model: `roles[role]` is called with the role's tools by name, and returns the role's reply."""
    roles, seen = {}, []

    def run_role(ctx, role, model_id, system, prompt, specs):
        seen.append((role, model_id, prompt, [s.name for s in specs]))
        ctx.budget.turn()
        by_name = {s.name: s.fn for s in specs}

        def call(tool, /, **args):
            ctx.budget.check()
            return by_name[tool](args)

        try:
            return roles[role](call) or ""
        except model.Stop:
            return ""

    monkeypatch.setattr(model, "run_role", run_role)
    roles["tester"] = lambda call: call("write_file", path="tests/test_unit.py", content=OK["tests/test_unit.py"])
    roles["builder"] = lambda call: [call("write_file", path=f, content=OK[f]) for f in ("capability.py", "manifest.yaml")] and "done"
    roles["repair"] = lambda call: call("write_file", path="capability.py", content=OK["capability.py"])
    return roles, seen


def events_of(ctx, *types):
    return [e for e in ctx.events.read_from(0)[0] if not types or e.type in types]


def test_gap_is_built_installed_and_answered_with_provenance(ctx, script):
    roles, seen = script

    def planner(call):
        call("record_plan", text="1. repeat the text")
        built = call("report_gap", **GAP)
        assert built["installed"] and built["ref"] == "echo@v1"
        out = call("invoke_capability", name="echo", args={"text": "ahoj"})
        call("submit_answer", text=f"It says {out.output['echo']}", call_ids=[out.call_id])

    roles["planner"] = planner
    assert loop.run_session("repeat ahoj", ctx) == "It says ahoj"

    assert [e.type for e in events_of(ctx) if e.type != EventType.BUDGET] == [
        EventType.PLAN, EventType.GAP, EventType.BUILD, EventType.BUILD, EventType.TEST_RUN, EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_DECIDED, EventType.INSTALL, EventType.CALL, EventType.ANSWER,
    ]  # fmt: skip
    builder, tester = events_of(ctx, EventType.BUILD)
    assert (builder.data["role"], builder.data["ref"], builder.data["files"]) == ("builder", "echo@v1", ["capability.py", "manifest.yaml"])
    assert builder.data["contents"]["capability.py"] == OK["capability.py"]
    assert (tester.data["role"], tester.data["files"]) == ("tester", ["tests/test_unit.py"])
    answer = events_of(ctx, EventType.ANSWER)[0].data
    assert answer["provenance"] == "ok" and answer["built"] == ["echo@v1"] and answer["reused"] == []
    assert [(role, model_id) for role, model_id, _, _ in seen] == [("planner", loop.PLANNER_MODEL), ("builder", loop.BUILDER_MODEL), ("tester", loop.TESTER_MODEL)]


def test_failed_tests_are_repaired(ctx, script):
    roles, seen = script
    roles["builder"] = lambda call: [call("write_file", path="capability.py", content=BROKEN), call("write_file", path="manifest.yaml", content=OK["manifest.yaml"])] and ""
    roles["planner"] = lambda call: call("report_gap", **GAP)

    loop.run_session("repeat ahoj", ctx)

    assert [e.data["passed"] for e in events_of(ctx, EventType.TEST_RUN)] == [False, True]
    assert [(e.data["role"], e.data["attempt"]) for e in events_of(ctx, EventType.BUILD)] == [("builder", 1), ("tester", 1), ("repair", 2)]
    assert [e.data["installed"] for e in events_of(ctx, EventType.INSTALL)] == [False, True]
    repair_prompt = next(prompt for role, _, prompt, _ in seen if role == "repair")
    assert "tests failed" in repair_prompt and "assert" in repair_prompt  # the gate's reason and the harness's own test output
    gap_id = events_of(ctx, EventType.GAP)[0].data["id"]
    assert ctx.budget.planner_turns == 1 and ctx.budget.gap_turns == {gap_id: 3}  # builder, tester and repair count against the gap


def test_repairs_stop_at_the_cap_and_nothing_is_installed(ctx, script):
    roles, _ = script
    roles["builder"] = lambda call: [call("write_file", path="capability.py", content=BROKEN), call("write_file", path="manifest.yaml", content=OK["manifest.yaml"])] and ""
    roles["repair"] = lambda call: "still broken"
    roles["planner"] = lambda call: call("report_gap", **GAP)

    with pytest.raises(CapExceeded):
        loop.run_session("repeat ahoj", ctx)

    assert sum(e.data["role"] == "repair" for e in events_of(ctx, EventType.BUILD)) == MAX_REPAIRS_PER_GAP
    assert events_of(ctx, EventType.CAP_HIT)[0].data["limit"] == "repairs_per_gap"
    assert ctx.registry.list() == [] and not events_of(ctx, EventType.ANSWER)


def test_tester_can_be_asked_to_fix_a_wrong_test(ctx, script):
    roles, seen = script
    wrong = OK["tests/test_unit.py"].replace('== "ahoj"', '== "AHOJ"')
    turns = iter([wrong, OK["tests/test_unit.py"]])
    roles["tester"] = lambda call: call("write_file", path="tests/test_unit.py", content=next(turns))
    roles["repair"] = lambda call: "The code is right.\nTESTS_WRONG: the gap says repeat, not upper-case"
    roles["planner"] = lambda call: call("report_gap", **GAP)

    loop.run_session("repeat ahoj", ctx)

    assert [e.data["role"] for e in events_of(ctx, EventType.BUILD)] == ["builder", "tester", "repair", "tester"]
    assert "repeat, not upper-case" in [prompt for role, _, prompt, _ in seen if role == "tester"][1]
    assert [e.manifest.ref for e in ctx.registry.list()] == ["echo@v1"]


def test_roles_write_only_their_own_files(ctx, script):
    roles, _ = script
    errors = []

    def attempt(call, path):
        try:
            call("write_file", path=path, content="x")
        except PermissionError as e:
            errors.append(str(e))

    roles["builder"] = lambda call: [attempt(call, "tests/test_unit.py"), attempt(call, "../escape.py")] and ""
    roles["tester"] = lambda call: attempt(call, "capability.py")
    roles["repair"] = lambda call: ""
    roles["planner"] = lambda call: call("report_gap", **GAP)

    with pytest.raises(CapExceeded):
        loop.run_session("repeat ahoj", ctx)
    assert len(errors) == 3 and not (ctx.workdir.parent / "escape.py").exists()


def test_fresh_session_gets_installed_capabilities_as_tools(ctx, script):
    roles, seen = script
    assert ctx.gate.submit(BUNDLES / "echo_ok").installed  # what an earlier session left in the registry

    def planner(call):
        out = call("cap_echo", text="znovu")
        call("submit_answer", text=out.output["echo"], call_ids=[out.call_id])

    roles["planner"] = planner
    assert loop.run_session("repeat znovu", ctx) == "znovu"

    _, _, prompt, names = seen[0]
    assert "cap_echo" in names and "echo@v1" in prompt
    answer = events_of(ctx, EventType.ANSWER)[0].data
    assert answer["reused"] == ["echo@v1"] and answer["built"] == []
    assert not events_of(ctx, EventType.GAP, EventType.BUILD)


def test_upgrade_builds_the_next_version(ctx, script):
    roles, seen = script
    assert ctx.gate.submit(BUNDLES / "echo_ok").installed
    v2 = OK["manifest.yaml"].replace("version: 1", "version: 2")
    roles["builder"] = lambda call: [call("write_file", path="capability.py", content=OK["capability.py"]), call("write_file", path="manifest.yaml", content=v2)] and ""
    roles["planner"] = lambda call: call("report_gap", **GAP, upgrade="echo")

    loop.run_session("repeat ahoj, louder", ctx)

    brief = next(prompt for role, _, prompt, _ in seen if role == "builder")
    assert "must be 2" in brief and "def run(text" in brief  # told the version, shown the current code
    assert ctx.registry.get("echo").manifest.version == 2
    assert [e.data.get("suite") for e in events_of(ctx, EventType.TEST_RUN)][-1].startswith("regression")


def test_answer_without_provenance_is_sent_back_then_flagged(ctx, script):
    roles, _ = script
    replies = []

    def planner(call):
        replies.append(call("submit_answer", text="Prague, I think", call_ids=[]))
        replies.append(call("submit_answer", text="Prague, I think", call_ids=["call-made-up"]))

    roles["planner"] = planner
    answer = loop.run_session("where is it?", ctx)

    assert "Not accepted" in replies[0] and len(replies) == 1
    assert answer.startswith("[UNVERIFIED")
    assert events_of(ctx, EventType.ANSWER)[0].data["provenance"] == "invalid"


def test_planner_that_never_submits_is_flagged(ctx, script):
    roles, _ = script
    roles["planner"] = lambda call: "It is probably fine."
    assert loop.run_session("is it fine?", ctx).startswith("[UNVERIFIED: no capability calls cited]")


def test_failed_call_is_not_provenance(ctx, script):
    roles, _ = script
    assert ctx.gate.submit(BUNDLES / "echo_ok").installed

    def planner(call):
        bad = call("invoke_capability", name="echo", args={"wrong": 1})
        assert not bad.ok
        assert "failed" in call("submit_answer", text="x", call_ids=[bad.call_id])

    roles["planner"] = planner
    loop.run_session("repeat", ctx)
    assert events_of(ctx, EventType.ANSWER)[0].data["provenance"] != "ok"


def test_study_logs_and_caps(ctx, monkeypatch):
    monkeypatch.setattr(tools, "_fetch_text", lambda url: "docs " * 10_000)
    text = tools.study(ctx, "how does it work", "https://docs.example/page")
    assert len(text) == tools.STUDY_MAX_CHARS
    event = events_of(ctx, EventType.STUDY)[0].data
    assert event["query"] == "how does it work" and event["url"] == "https://docs.example/page" and event["ok"]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/", "http://localhost/x", "file:///etc/passwd", "ftp://example.org/x"])
def test_study_refuses_local_and_non_http_targets(ctx, url):
    assert tools.study(ctx, "q", url).startswith("study failed")
    assert events_of(ctx, EventType.STUDY)[0].data["ok"] is False


def test_tool_schemas_are_valid_json_schema(ctx):
    for spec in loop.Session(ctx).planner_tools([]):
        assert spec.schema["type"] == "object" and set(spec.schema["required"]) <= set(spec.schema["properties"])
        json.dumps(spec.schema)


READER = {
    "capability.py": "from pathlib import Path\n\n\ndef run(path: str) -> dict:\n    return {\"text\": Path(path).read_text().strip()}\n",
    "manifest.yaml": OK["manifest.yaml"].replace("name: echo", "name: read_text").replace("{text: string}", "{path: string}").replace("{echo: string}", "{text: string}"),
    "tests/test_unit.py": "from capability import run\n\n\ndef test_reads(tmp_path):\n    (tmp_path / \"a.txt\").write_text(\"hi\\n\")\n    assert run(str(tmp_path / \"a.txt\")) == {\"text\": \"hi\"}\n",
}


def test_attached_file_reaches_builds_and_calls_but_not_the_bundle(ctx, script, tmp_path):
    roles, seen = script
    note = tmp_path / "note.txt"
    note.write_text("faktura 42\n")
    peeked = []

    def builder(call):
        for f in ("capability.py", "manifest.yaml"):
            call("write_file", path=f, content=READER[f])
        peeked.append(call("sandbox_exec", argv=["python", "-c", "print(open('_frank_inputs/note.txt').read())"])["stdout"].strip())

    def planner(call):
        assert call("report_gap", **GAP)["installed"]
        out = call("invoke_capability", name="read_text", args={"path": "_frank_inputs/note.txt"})
        call("submit_answer", text=out.output["text"], call_ids=[out.call_id])

    roles.update(builder=builder, planner=planner, tester=lambda call: call("write_file", path="tests/test_unit.py", content=READER["tests/test_unit.py"]))
    assert loop.run_session("what does the note say?", ctx, attach=[note]) == "faktura 42"

    assert peeked == ["faktura 42"]
    assert "Attached files: _frank_inputs/note.txt" in seen[0][2] and "_frank_inputs/note.txt" in seen[1][2]
    installed = ctx.registry.get("read_text").path
    assert not (installed / "_frank_inputs").exists()
    assert not any("_frank_inputs" in f for e in events_of(ctx, EventType.BUILD) for f in e.data["files"])


def test_builder_can_compose_an_installed_capability(ctx, script):
    roles, _ = script
    assert ctx.gate.submit(BUNDLES / "echo_ok").installed
    code = "from frank import use\n\n\ndef run(text: str) -> dict:\n    return {\"echo\": use(\"echo\", text=text)[\"echo\"] * 2}\n"
    manifest = OK["manifest.yaml"].replace("name: echo", "name: echo_twice").replace("uses: []", "uses: [echo]")
    tests = "from capability import run\n\n\ndef test_twice():\n    assert run(\"ab\")[\"echo\"] == \"abab\"\n"
    tried = []

    def builder(call):
        call("write_file", path="capability.py", content=code)
        call("write_file", path="manifest.yaml", content=manifest)
        tried.append(call("sandbox_exec", argv=["python", "-c", "from capability import run; print(run('x')['echo'])"])["stdout"].strip())

    def planner(call):
        assert call("report_gap", **GAP)["installed"]
        out = call("invoke_capability", name="echo_twice", args={"text": "ha"})
        call("submit_answer", text=out.output["echo"], call_ids=[out.call_id])

    roles.update(builder=builder, planner=planner, tester=lambda call: call("write_file", path="tests/test_unit.py", content=tests))
    assert loop.run_session("say ha twice", ctx) == "haha"

    assert tried == ["xx"]  # the used capability was next to the code while building, as it is in a call
    assert events_of(ctx, EventType.CALL)[0].data["uses"] == ["echo@v1"]
    assert not any(f.startswith(("frank.py", "_frank_uses")) for e in events_of(ctx, EventType.BUILD) for f in e.data["files"])


def test_unresolvable_uses_is_reported_to_the_builder(ctx, script):
    roles, _ = script
    results = []

    def builder(call):
        call("write_file", path="capability.py", content=OK["capability.py"])
        call("write_file", path="manifest.yaml", content=OK["manifest.yaml"].replace("uses: []", "uses: [not_there]"))
        results.append(call("sandbox_exec", argv=["python", "-c", "print(1)"]))

    roles.update(builder=builder, planner=lambda call: call("report_gap", **GAP), repair=lambda call: call("write_file", path="manifest.yaml", content=OK["manifest.yaml"]))
    loop.run_session("repeat", ctx)
    assert "isn't installed" in results[0]["stderr"]
    assert [e.manifest.ref for e in ctx.registry.list()] == ["echo@v1"]  # the gate refused it, the repair fixed it


def test_cli_attach_refuses_a_missing_file(tmp_path, monkeypatch, capsys):
    from harness import cli, config

    monkeypatch.setattr(config, "FAKES", {"sandbox", "registry"})
    for name in ("LOG_PATH", "REGISTRY_DIR", "WORK_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    with pytest.raises(SystemExit):
        cli.main(["run", "--attach", str(tmp_path / "nope.pdf"), "task"])
    assert "not a file" in capsys.readouterr().err
