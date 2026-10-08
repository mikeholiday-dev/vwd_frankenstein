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
| `harness/kernel/limits.py`, `gate.py`, `host.py` | **A** | done: hardened gate, v2 regression tests, `Host.retest`. TODO: prompt_skill, `uses` |
| `harness/kernel/sandbox.py`, `proxy.py`, `registry.py` | **A** | done: Docker sandbox behind the egress proxy, git registry. The default now |
| `harness/agent/`, `harness/cli.py` | **B** | kernel tools done except `study`; `run_session` stub; empty prompts |
| `harness/ops/events.py`, `approvals.py` | **C** | done: JSONL log + approval/kill over the log |
| `ui/`, `scripts/`, `testdata/`, `README.md`, video | **C** | bare console that works; fake run script |
| `tests/test_<area>.py` | owner of the area | |
| `registry/` | **nobody**: only the agent, through the gate | |

## Claude access

No API key from the organisers, so local dev and the demo run on our own subscriptions:

- Each person logs in to Claude Code with **their own** subscription (`claude`, then `/login`). Don't share logins.
- Keep `ANTHROPIC_API_KEY` **unset**. If it's set, the Agent SDK uses it and bills it. `config.AUTH` says which one is active, and every `run_started` event records it.
- Every model call goes through `claude-agent-sdk`. The plain `anthropic` client needs an API key.
- The `$` in `limits.py` and the UI meter is an **API-equivalent estimate**. It's still capped in code, which is what the brief asks for.
- Subscription usage limits are the real constraint. Builder runs on Opus burn through them fastest, so spread rehearsals across people and don't leave the agent looping.
- Anything we deploy later switches to an API key. No code change is needed, only the env var.

## Switches

| Env | Values | Use |
|---|---|---|
| `FRANK_FAKE` | `none` (default), `sandbox`, `registry`, `sandbox,registry` | which components are fakes. Without Docker use `sandbox` (dev only: it runs code on your machine) |
| `FRANK_APPROVER` | `cli` (default), `ui`, `auto` | `ui` = block on the console's approval card |
| `FRANK_MODE` | `dev` (default), `demo` | `demo` refuses every fake and the auto approver |
| `FRANK_LOG`, `FRANK_REGISTRY_DIR`, `FRANK_WORK_DIR` | paths | point at a scratch dir so you can wipe freely without touching the others |

## Stream A: kernel (sandbox, proxy, registry, gate, limits)

Owns `harness/kernel/`. Doesn't need the agent or the UI.

1. ✅ `DockerSandbox`: one container per run, no host env or home, read-only workdir for calls, CPU/memory/pids/time limits, deps cached per hash.
2. ✅ `GitRegistry`: agent-authored commit + `<name>@v<N>` tag per install, operator commits for rollback/quarantine. `FAKES` default is `none`.
3. ✅ `EgressProxy`: CONNECT-only allowlist per container. BUILD and deps installs reach the package index only, TEST and CALL the manifest's exact hosts only.
4. ✅ Gate hardening: refusals before testing (see "From stream A" below), tests on a throwaway copy, v2 must pass the active version's tests too.
5. ✅ `Host.retest(name, version=None)`, the "re-run stored tests" primitive. ⚠️ Quarantine doesn't cut a call already in flight (≤ 60 s): proxy tokens live one container, and the host refuses new calls of a quarantined capability.
6. Next: `uses` (a capability calling another one) in `host.py`; prompt_skill installs (eval cases + judge); rehearsal support.

Dev loop: `uv run frank install tests/fixtures/bundles/echo_ok` then `uv run frank call echo '{"text":"hi"}'`.

## Stream B: agent (loop, roles, prompts, kernel tools)

Owns `harness/agent/` and `harness/cli.py`. Needs Docker (or `FRANK_FAKE=sandbox` in dev), not the UI.

1. `study` in `agent/tools.py`.
2. `run_session` in `agent/loop.py`: the planner over the installed capabilities plus the kernel tools, with budget calls between steps.
3. Gap → builder → tester → `registry_install` → repair, at most `MAX_REPAIRS_PER_GAP` times.
4. Answer with cited call ids, plus the provenance check.
5. Session B: fresh process, composition, v2 upgrade. Then task 3.

Dev loop: `FRANK_APPROVER=cli uv run frank run --session A "<task 1>"`.
The default is now the Docker sandbox and git registry. ⚠️ `FRANK_FAKE=sandbox` (`LocalSandbox`) runs generated code **on your machine**: only for team-written fixtures.
Prompts go in `agent/prompts/` and must pass `tests/test_prompt_hygiene.py`: no hosts, no endpoints, no expected tool names.

## Stream C: operator console, event log, test data, delivery

Owns `ui/`, `harness/ops/`, `scripts/`, `testdata/`, `README.md`. Doesn't need the agent.

1. The console against `scripts/fake_run.py`: lab panel (code, tests, red → green), approval card with the permissions diff, budget meter, registry view, kill, rollback, quarantine.
2. `testdata/` invoices (see `testdata/README.md`).
3. README: how to run, plus the real/simulated/missing table. Rehearse the video storyboard (plan §11).

Dev loop: `uv run uvicorn ui.app:app --reload` in one terminal, `uv run python scripts/fake_run.py` in another, then approve in the browser.

## From stream A: what the real kernel means for B and C

Everything below is live on `main` once `a/sandbox-registry` merges. Proof for each rule is in `tests/test_gate.py`, `test_proxy.py`, `test_sandbox.py`, `test_registry.py`.

### Rules every bundle now meets (the gate refuses otherwise)

| Rule | Refusal reason starts with |
|---|---|
| `name` is lowercase letters, digits, `_` (max 64) | `refused: name ...` |
| `version` is an int: 1 for a new name, otherwise highest installed + 1 (also after a rollback) | `refused: version must be N` / `... is already installed` |
| `kind: code_tool` only for now | `refused: prompt_skill installs aren't supported yet` |
| has `capability.py` and at least one `tests/test_*.py` | `refused: missing capability.py` / `... without tests` |
| no symlinks anywhere in the bundle | `refused: bundle contains symlinks` |
| a v2+ also passes the **active version's stored tests** | `tests failed` (output has `=== previous version's tests ===`) |
| `dependencies` are package specs, never options like `--index-url` | the test output says `[deps] refused` |

Refusals come back as `InstallResult(installed=False, reason=..., test_report=None)` and an `install` event. No test runs, nothing reaches the operator.

### Network, as the sandbox enforces it

- BUILD (`sandbox_exec`) and deps installs reach **PyPI only**. Nothing else is reachable while building; `study` is the only docs channel.
- TEST and CALL reach exactly the hosts in `permissions.network`: exact host match, port 443 unless written `host:port`, no wildcards or subdomains, HTTPS only (plain HTTP is refused).
- A refused host shows up in the test output as `[egress] refused host:443: not in this capability's allowlist`, and in `egress_denied` on the event.

### Stream B tasks

1. Builder output meets the bundle rules above. For an upgrade, read the active version with `registry_read` and bump to highest installed + 1.
2. Repair loop: feed it `InstallResult.reason` plus `test_report.output`. A `refused:` reason is a bundle-shape problem, `[egress] refused` lines mean a missing host in the manifest, and `previous version's tests` failures mean the v2 broke v1's behaviour. Keep the prompts generic (rule 2): no hosts or tool names in them.
3. `capability_doctor` kernel tool: wrap `ctx.host.retest(name, version)`. It returns a `TestReport` and logs `test_run` with `suite="retest"`. It's on `kernel.host.Host`, not yet on the `CapabilityHost` Protocol: add it there in a small PR (optional method, A approves).
4. Run with the defaults (Docker). Use `FRANK_FAKE=sandbox` only without Docker.

### Stream C tasks

1. Lab panel: `test_run` now carries `suite`: `own`, `regression: <name>@v<N> tests`, or `retest`. Show the regression run as its own red/green row. The approval card's `test_report.output` has both suites.
2. Evidence for "authority may not grow": `test_run` and `call` carry `egress_denied: ["host:port", ...]`. Show refused hosts (red) next to the capability.
3. `install` events with `reason` starting `refused:` have no test report: render the reason.
4. Registry view: `registry/` is a git repo. A `git log --format='%h %an %s'` panel shows `frankenstein-agent` as the author of every install (storyboard 0–8 s and 80–90 s). Rollback and quarantine from the UI work as before; they commit as `frankenstein-operator`.
5. README real/simulated/missing table, add:
   - Real: Docker sandbox (no host env, no home, read-only call mounts, limits); egress proxy (CONNECT allowlist, no TLS interception, so it sees hosts, not URLs).
   - Limits: quarantine doesn't cut a call already in flight (≤ 60 s); prompt skills aren't installable; deps need PyPI reachable.
6. Before switching the console to real runs: move aside any `registry/` left from fake runs. The git registry refuses a non-git folder.

### Shared

- `fakes.DirRegistry.get(name, version)` raises `FileNotFoundError` for an uninstalled version; the Protocol says `KeyError`. Small fix in `fakes.py` (shared, needs the PR); the gate tolerates both meanwhile.
- First run on each machine builds the sandbox image (~30 s, needs internet). The `frank-egress-*` container stays up between runs on purpose.

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
- ~~`capability_doctor` needs "re-run the stored tests"~~: done as `Host.retest` (A); B exposes it as a kernel tool.
- Hot-load tools mid-session, or only `invoke_capability`? (B decides after a spike.)
- Commit the log of the recorded run? (`logs/` is gitignored now.)
