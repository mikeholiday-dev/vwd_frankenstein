# Working in parallel

Three people, three workstreams, one repo. The split follows plan §9: **A** kernel, **B** agent, **C** operator console + delivery.

## Rules

1. **Own your directories.** Edit someone else's only after a quick "ok?" from them.
2. **Cross boundaries only through [`harness/contracts.py`](../harness/contracts.py).** It defines the data shapes, the Protocols (Sandbox, Registry, Approver, InstallGate, CapabilityHost) and the event schema. Adding an optional field is fine. Anything else needs a PR that both other owners approve.
3. **Never wait on each other.** Every stream runs end to end today on fakes (`harness/fakes.py`). Swap a real component in through `harness/wiring.py`, nowhere else.
4. **Keep main green.** `uv run pytest` before every push. Use small PRs and a branch per stream (`a/docker-sandbox`, `b/builder-loop`, `c/approval-card`), and rebase often.

## Who owns what

| Path | Owner | State now |
|---|---|---|
| `harness/contracts.py`, `config.py`, `wiring.py`, `fakes.py` | shared (PR + 2 approvals) | done |
| `harness/kernel/limits.py`, `gate.py`, `host.py` | **A** | working over any Sandbox/Registry; TODOs inside |
| `harness/kernel/sandbox.py`, `proxy.py`, `registry.py` | **A** | stubs; spec in each docstring |
| `harness/agent/`, `harness/cli.py` | **B** | kernel tools done except `study`; `run_session` stub; empty prompts |
| `harness/ops/events.py`, `approvals.py` | **C** | done: JSONL log + approval/kill over the log |
| `ui/`, `scripts/`, `testdata/`, `README.md`, video | **C** | bare console that works; fake run script |
| `tests/test_<area>.py` | owner of the area | |
| `registry/` | **nobody**: only the agent, through the gate | |

## Switches

| Env | Values | Use |
|---|---|---|
| `FRANK_FAKE` | `sandbox,registry` (default), `registry`, `none` | which components are fakes. Flip the default in `config.py` when the real one lands |
| `FRANK_APPROVER` | `cli` (default), `ui`, `auto` | `ui` = block on the console's approval card |
| `FRANK_MODE` | `dev` (default), `demo` | `demo` refuses every fake and the auto approver |
| `FRANK_LOG`, `FRANK_REGISTRY_DIR`, `FRANK_WORK_DIR` | paths | point at a scratch dir so you can wipe freely without touching the others |

## Stream A: kernel (sandbox, proxy, registry, gate, limits)

Owns `harness/kernel/`. Doesn't need the agent or the UI.

1. `DockerSandbox` (start with `--network none`, add the proxy later). Done when `FRANK_FAKE=registry uv run pytest` runs the `docker` variants of `tests/test_install_flow.py` instead of skipping them, and they pass.
2. `GitRegistry`: same tests for the `git` variants. Then set the `FAKES` default to `none`.
3. `EgressProxy` wired into the sandbox: BUILD reaches the package index only, TEST and CALL reach the manifest domains only.
4. Gate hardening (TODOs in `gate.py`): v2 re-runs the old tests, refuses version clashes.
5. Quarantine revokes the capability's proxy domains. A "re-run stored tests" primitive for `capability_doctor`; agree its shape with B.

Dev loop: `uv run frank install tests/fixtures/bundles/echo_ok` then `uv run frank call echo '{"text":"hi"}'`.

## Stream B: agent (loop, roles, prompts, kernel tools)

Owns `harness/agent/` and `harness/cli.py`. Doesn't need Docker or the UI.

1. `study` in `agent/tools.py`.
2. `run_session` in `agent/loop.py`: the planner over the installed capabilities plus the kernel tools, with budget calls between steps.
3. Gap → builder → tester → `registry_install` → repair, at most `MAX_REPAIRS_PER_GAP` times.
4. Answer with cited call ids, plus the provenance check.
5. Session B: fresh process, composition, v2 upgrade. Then task 3.

Dev loop: `FRANK_APPROVER=cli uv run frank run --session A "<task 1>"`.
⚠️ `LocalSandbox` runs generated code **on your machine** (env scrubbed, nothing else isolated). Move to `FRANK_FAKE=registry` the moment A's Docker sandbox lands.
Prompts go in `agent/prompts/` and must pass `tests/test_prompt_hygiene.py`: no hosts, no endpoints, no expected tool names.

## Stream C: operator console, event log, test data, delivery

Owns `ui/`, `harness/ops/`, `scripts/`, `testdata/`, `README.md`. Doesn't need the agent.

1. The console against `scripts/fake_run.py`: lab panel (code, tests, red → green), approval card with the permissions diff, budget meter, registry view, kill, rollback, quarantine.
2. `testdata/` invoices (see `testdata/README.md`).
3. README: how to run, plus the real/simulated/missing table. Rehearse the video storyboard (plan §11).

Dev loop: `uv run uvicorn ui.app:app --reload` in one terminal, `uv run python scripts/fake_run.py` in another, then approve in the browser.

## Seams: where integration breaks, and what catches it

| Seam | Contract | Between | Caught by |
|---|---|---|---|
| Sandbox | `contracts.Sandbox` | A → gate, host, B | `test_install_flow.py` (docker variants) |
| Registry | `contracts.Registry` | A → gate, host, B, UI | `test_install_flow.py` (git variants) |
| Bundle layout, call convention | `MANIFEST_FILE`, `CODE_FILE`, `TESTS_DIR`, `CALL_SHIM_FILE` | B's builder → A's gate and host | fixture bundles in `tests/fixtures/bundles/` |
| Event schema | `EventType`, `REQUIRED_FIELDS` | everyone → UI | `EventLog.emit` rejects missing fields |
| Approve, kill, rollback | events through `harness/ops/approvals.py` | UI ↔ agent process | `scripts/fake_run.py` |
| No hints in prompts | banned list | B | `test_prompt_hygiene.py` |

## Integration checkpoints (plan §9)

| Hour | Checkpoint | Who |
|---|---|---|
| 2 | Docker sandbox + git registry pass the un-skipped tests. UI drives `fake_run.py` end to end | A, C |
| 4 | **Task 1 from an empty registry**, real sandbox, approval in the UI | all |
| 6 | Session B composes without rebuilding; v2 shows a permissions diff in the UI; task 3 | all |
| 8 | `FRANK_MODE=demo`, three clean rehearsals | all |

## Open questions (decide in the first hour)

- How do we ship `registry/` (a nested git repo, now gitignored) in the submission: a `git bundle`, or a copy plus its `git log`?
- `capability_doctor` needs "re-run the stored tests": a host primitive (A) exposed as a kernel tool (B)?
- Hot-load tools mid-session, or only `invoke_capability`? (B decides after a spike.)
- Commit the log of the recorded run? (`logs/` is gitignored now.)
