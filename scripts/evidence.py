"""Turn the event log into the evidence the video and the README point at (plan §8, §10, §12). Owner: C.

    uv run python scripts/evidence.py                    # every run in logs/events.jsonl, as markdown on stdout
    uv run python scripts/evidence.py --run 3f9a1c2e     # one run
    uv run python scripts/evidence.py --out EVIDENCE.md

Everything is read from the log and the registry's git history, never from what the agent
said about itself: "reused" is computed as "called in this run, installed by an earlier run".
Failures are listed, not summarised away, and a run containing fake events is labelled.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from harness import config
from harness.contracts import Event, EventType
from harness.ops.events import EventLog


@dataclass
class RunSummary:
    run_id: str
    session: str
    task: str = ""
    started: str = ""
    last: str = ""
    status: str = "unfinished"  # the log has no run_finished: still running, or the process died
    fake: bool = False
    attached: list[str] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)  # role -> model id, empty for a log recorded before FRANK_MODELS
    registry_at_start: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    builds: Counter = field(default_factory=Counter)  # role -> count
    tests: list[tuple[str, str, bool, str]] = field(default_factory=list)  # ref, suite, passed, first failing line
    approvals: list[tuple[str, bool, str]] = field(default_factory=list)  # ref, approved, by
    installs: list[tuple[str, bool, str]] = field(default_factory=list)  # ref, installed, reason
    calls: list[tuple[str, str, bool, str]] = field(default_factory=list)  # call_id, ref, ok, error
    egress_denied: Counter = field(default_factory=Counter)  # host:port -> times refused
    uses: set[str] = field(default_factory=set)
    studies: int = 0
    answer: dict | None = None
    cap_hits: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    killed_by: str = ""
    budget: dict = field(default_factory=dict)
    # computed across runs by `summarize`, from installs and calls only
    called_refs: set[str] = field(default_factory=set)
    installed_refs: set[str] = field(default_factory=set)
    reused_from_earlier: set[str] = field(default_factory=set)


def operator_actions(events: list[Event]) -> list[str]:
    """What the operator did from the console outside an approval: kill, rollback, quarantine."""
    out = []
    for e in events:
        d = e.data
        when = e.ts[11:19]
        if e.type == EventType.KILL:
            out.append(f"{when} kill switch pressed by {d['by']}")
        elif e.type == EventType.ROLLBACK:
            out.append(f"{when} `{d['name']}` rolled back to v{d['version']} by {d['by']}")
        elif e.type == EventType.QUARANTINE:
            out.append(f"{when} `{d['name']}` quarantined by {d['by']}")
    return out


def summarize(events: list[Event]) -> list[RunSummary]:
    runs: dict[str, RunSummary] = {}
    requests = {e.data["id"]: (e.run_id, e.data["ref"]) for e in events if e.type == EventType.APPROVAL_REQUESTED}
    for e in events:
        d = e.data
        if e.type == EventType.APPROVAL_DECIDED:  # written by the console under its own run id: file it under the request's run
            run_id, ref = requests.get(d["request_id"], (e.run_id, ""))
            runs.setdefault(run_id, RunSummary(run_id, e.session, started=e.ts)).approvals.append((ref, d["approved"], d["by"]))
            continue
        if e.session == "ui":
            continue
        r = runs.setdefault(e.run_id, RunSummary(e.run_id, e.session, started=e.ts))
        r.fake = r.fake or bool(d.get("fake"))
        r.last = e.ts
        match e.type:
            case EventType.RUN_STARTED:
                r.task, r.started = d["task"], e.ts
                r.attached, r.registry_at_start = d.get("attached", []), d.get("registry", [])
                r.models = d.get("models", {})
            case EventType.GAP:
                r.gaps.append(d["gap"] + (f" (upgrade of {d['upgrade']})" if d.get("upgrade") else ""))
            case EventType.STUDY:
                r.studies += 1
            case EventType.BUILD:
                r.builds[d["role"]] += 1
            case EventType.TEST_RUN:
                failing = next((line for line in d["output"].splitlines() if line.startswith(("FAILED", "E ", "[egress]", "[uses]", "[deps]"))), "")
                r.tests.append((d["ref"], d.get("suite", "own"), d["passed"], failing.strip()[:160]))
                r.egress_denied.update(d.get("egress_denied") or [])
            case EventType.INSTALL:
                r.installs.append((d["ref"], d["installed"], d["reason"]))
            case EventType.CALL:
                r.calls.append((d["call_id"], d["ref"], d["ok"], d.get("error", "")))
                r.egress_denied.update(d.get("egress_denied") or [])
                r.uses.update(d.get("uses") or [])
            case EventType.ANSWER:
                r.answer = d
            case EventType.CAP_HIT:
                r.cap_hits.append(f"{d['limit']} = {d['value']} (max {d['max']})")
            case EventType.ERROR:
                r.errors.append(d["message"])
            case EventType.BUDGET:
                r.budget = d
            case EventType.RUN_FINISHED:
                r.status = d["status"]
                r.budget = d.get("budget") or r.budget
    for e in events:
        if e.type == EventType.KILL and e.session == "ui":
            for r in runs.values():
                if r.started <= e.ts <= (r.last if r.status != "unfinished" else e.ts) and r.status in ("killed", "unfinished"):
                    r.killed_by = e.data["by"]
    ordered = [r for r in runs.values() if r.task or r.installs or r.calls or r.tests]  # drops runs made only of console actions
    earlier: set[str] = set()
    for r in sorted(ordered, key=lambda x: x.started):
        r.installed_refs = {ref for ref, ok, _ in r.installs if ok}
        r.called_refs = {ref for _, ref, ok, _ in r.calls if ok} | r.uses
        r.reused_from_earlier = r.called_refs & earlier
        earlier |= r.installed_refs
    return sorted(ordered, key=lambda x: x.started)


def registry_history(registry_dir: Path) -> list[dict[str, str]]:
    """The registry's git log, oldest first, with the tags each commit carries."""
    if not (registry_dir / ".git").exists():
        return []
    fmt = "%h%x1f%an%x1f%aI%x1f%s%x1f%D"
    out = subprocess.run(["git", "-C", str(registry_dir), "log", "--reverse", f"--format={fmt}"], capture_output=True, text=True)
    if out.returncode:
        return []
    keys = ("sha", "author", "date", "subject", "refs")
    return [dict(zip(keys, line.split("\x1f"))) for line in out.stdout.splitlines()]


def render(runs: list[RunSummary], history: list[dict[str, str]] = (), actions: list[str] = ()) -> str:
    out = ["# Evidence from the event log", ""]
    if not runs:
        return "\n".join([*out, "The log has no runs.", ""])
    fakes = [r.run_id for r in runs if r.fake]
    if fakes:
        out += [f"> ⚠️ Runs {', '.join(fakes)} contain events marked `fake` (scripts/fake_run.py). They are not evidence of anything the agent did.", ""]
    out += ["| Run | Session | Status | Built (installed) | Reused from earlier sessions | Provenance |", "|---|---|---|---|---|---|"]
    for r in runs:
        prov = (r.answer or {}).get("provenance", "no answer")
        out.append(
            f"| `{r.run_id}`{' (fake)' if r.fake else ''} | {r.session} | {r.status} | {_refs(sorted(r.installed_refs))} | "
            f"{_refs(sorted(r.reused_from_earlier))} | {prov} |"
        )
    out.append("")
    for r in runs:
        out += _run(r)
    if actions:
        out += ["## Operator actions from the console", "", *[f"- {a}" for a in actions], ""]
    if history:
        out += ["## Registry history (git)", "", "| Commit | Author | When | Message | Tags |", "|---|---|---|---|---|"]
        for c in history:
            tags = ", ".join(x.removeprefix("tag: ") for x in c["refs"].split(", ") if x.startswith("tag: "))
            out.append(f"| `{c['sha']}` | {c['author']} | {c['date'][:19].replace('T', ' ')} | {c['subject']} | {tags} |")
        out += ["", f"{len(history)} commits, {len({c['author'] for c in history})} authors: {', '.join(sorted({c['author'] for c in history}))}.", ""]
    return "\n".join(out)


def _run(r: RunSummary) -> list[str]:
    red = [t for t in r.tests if not t[2]]
    out = [f"## Run `{r.run_id}` · session {r.session} · {r.status}{' · FAKE' if r.fake else ''}", ""]
    out.append(f"**Task:** {r.task or '(none: direct install or call)'}")
    if r.attached:
        out.append(f"**Attached files:** {', '.join(r.attached)}")
    if r.models:
        out.append("**Models:** " + ", ".join(f"{role} {model}" for role, model in sorted(r.models.items())))
    out.append(f"**Registry at start:** {_refs(r.registry_at_start) if r.registry_at_start else 'empty'}")
    if r.gaps:
        out += ["", "**Gaps reported:**", *[f"- {g}" for g in r.gaps]]
    if r.builds or r.studies:
        out.append(f"\n**Building:** {', '.join(f'{n}× {role}' for role, n in sorted(r.builds.items()))}; {r.studies} study lookups.")
    if r.tests:
        out += ["", f"**Test runs by the harness:** {len(r.tests)} ({len(red)} failed)"]
        out += [f"- {'❌' if not ok else '✅'} `{ref}` [{suite}]{f' {first}' if first else ''}" for ref, suite, ok, first in r.tests]
    if r.installs:
        out += ["", "**Install decisions:**"]
        out += [f"- {'✅ installed' if ok else '⛔ not installed'} `{ref}`: {reason}" for ref, ok, reason in r.installs]
    if r.approvals:
        out.append("\n**Approvals:** " + ", ".join(f"{'approved' if ok else 'rejected'} by {by}" for _, ok, by in r.approvals))
    if r.calls:
        failed = sum(not ok for _, _, ok, _ in r.calls)
        out += ["", f"**Capability calls:** {len(r.calls)} ({failed} failed)"]
        out += [f"- `{cid}` `{ref}` {'ok' if ok else 'ERROR ' + err[:120]}" for cid, ref, ok, err in r.calls]
    if r.uses:
        out.append(f"\n**Composition (`uses`) exercised:** {_refs(sorted(r.uses))}")
    if r.egress_denied:
        out.append("\n**Egress refused by the proxy:** " + ", ".join(f"`{h}` ×{n}" for h, n in sorted(r.egress_denied.items())))
    if r.cap_hits:
        out.append("\n**Caps hit:** " + "; ".join(r.cap_hits))
    if r.killed_by:
        out.append(f"\n**Kill switch:** pressed by {r.killed_by}")
    for m in r.errors:
        out.append(f"\n**Error logged:** {m}")
    if r.answer:
        a = r.answer
        cited = [c for c in a["call_ids"]]
        known = {cid for cid, _, ok, _ in r.calls if ok}
        out += ["", f"**Answer** (provenance: {a.get('provenance', 'not recorded')}; cites {len(cited)} calls, "
                f"{len([c for c in cited if c in known])} of them successful calls in this run):", "", *[f"> {line}" for line in a["text"].splitlines()]]
    if r.budget:
        b = r.budget
        out.append(f"\n**Budget at the end:** ${b.get('usd', 0)} API-equivalent · {b.get('turns', 0)} turns · {b.get('gaps', 0)} gaps · {b.get('minutes', 0)} min")
    return [*out, ""]


def _refs(refs) -> str:
    return ", ".join(f"`{x}`" for x in refs) or "none"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--log", type=Path, default=config.LOG_PATH)
    p.add_argument("--registry", type=Path, default=config.REGISTRY_DIR)
    p.add_argument("--run", help="only this run id")
    p.add_argument("--out", type=Path)
    a = p.parse_args(argv)
    if not a.log.is_file():
        print(f"no event log at {a.log}", file=sys.stderr)
        return 1
    events = EventLog(a.log).read_from(0)[0]
    runs = summarize(events)
    if a.run:
        runs = [r for r in runs if r.run_id == a.run]
    text = render(runs, registry_history(a.registry), operator_actions(events))
    if a.out:
        a.out.write_text(text)
        print(f"wrote {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
