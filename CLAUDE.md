# CLAUDE.md

Shared by three teammates, each running their own Claude Code session on the same repo at the same time. Read this, then [docs/WORKSTREAMS.md](docs/WORKSTREAMS.md) (who owns what) and the plan [03-frankenstein-self-building-agent.md](03-frankenstein-self-building-agent.md) when you need the why.

**Terminology:** "the agent" in this repo means **Frankenstein**, the self-building agent we are writing. You are the coding assistant that helps build its harness. Don't confuse the two.

Hackathon, code freeze at sunrise. Prefer the simplest thing that works and keeps `main` green. If behind, follow the cut order in plan §9.

## Which workstream am I in?

| Stream | Owns | Branch prefix |
|---|---|---|
| A, kernel | `harness/kernel/` | `a/` |
| B, agent | `harness/agent/`, `harness/cli.py` | `b/` |
| C, delivery | `harness/ops/`, `scripts/`, `testdata/`, `README.md` | `c/` |
| D, dashboard + channels | `channels/` | `d/` |

Work out the stream from `CLAUDE.local.md` (personal, gitignored, e.g. `I'm on stream B`), then from the branch prefix. If neither tells you, ask.

- Edit only your stream's paths. If a fix needs a change in someone else's area, stop and tell the user what to ask that owner. Don't make the change yourself.
- **Shared files** (`harness/contracts.py`, `config.py`, `wiring.py`, `fakes.py`, `pyproject.toml`, this file): change only when the user explicitly asks. Adding an optional field to `contracts.py` is fine. Renaming or removing anything breaks the other two streams and needs a PR they both approve.
- Code against the Protocols in `harness/contracts.py` and the fakes in `harness/fakes.py`, never against another stream's implementation module. `harness/wiring.py` is the only place that chooses real or fake.

## Commands

```bash
uv sync
uv run pytest                                   # must pass before every push
uv run frank install tests/fixtures/bundles/echo_ok   # gate smoke test
uv run frank call echo '{"text":"ahoj"}'
uv run frank run --session A "<task>"           # one Frankenstein session (stream B)
uv run uvicorn channels.web:app --reload --port 8001   # dashboard on :8001
uv run python scripts/fake_run.py               # scripted run for UI work
```

Point `FRANK_LOG`, `FRANK_REGISTRY_DIR` and `FRANK_WORK_DIR` at a scratch dir for experiments. Don't delete `registry/` or `logs/` without asking: they may hold a rehearsal or the recorded demo run. Other switches: `FRANK_FAKE`, `FRANK_APPROVER`, `FRANK_MODE` (see `harness/config.py`).

## Hard rules from the brief (never trade these for speed)

1. **Nothing in `registry/` is written by the team.** Only Frankenstein writes it, and only through the install gate (`harness/kernel/gate.py`). Don't hand-craft, seed or fix capabilities there. Test bundles go in `tests/fixtures/bundles/`.
2. **No hints to Frankenstein.** No API hosts, endpoints, WSDLs or expected capability names (`ares_lookup`, `cnb_rate`, ...) in its prompts or anywhere under `harness/agent/`. The README "API check" is for humans only. `tests/test_prompt_hygiene.py` enforces this: never shrink its banned list to make a prompt pass.
3. **Generated code runs only in the sandbox.** Never run Frankenstein-written code directly on the host. `fakes.LocalSandbox` is for team-written fixtures. Keep the guards that make `FRANK_MODE=demo` refuse fakes and the auto approver.
4. **No install without the harness's own passing test run.** Don't add bypasses, "skip tests" flags or trust in test results reported by the agent.
5. **Caps live in `harness/kernel/limits.py`.** Don't raise them to get a run through, and don't add code paths that skip `budget.check()`.
6. **Honesty.** Don't fake success: no mocked answers in the real path, no hiding failures from the event log, no deleting failed runs. If something is simulated, it goes in the README real/simulated/missing table.

## Claude access

Dev and the demo run on each person's own Claude subscription through `claude-agent-sdk`, with `ANTHROPIC_API_KEY` unset. Only a deployment would use an API key.

- Make every model call through `claude-agent-sdk`. Never use the plain `anthropic` client: it needs an API key.
- Allow Frankenstein only our own tools. Disable the SDK's built-in Bash, Read/Write/Edit, WebFetch and WebSearch, or the capability gap stops being real (plan §4).
- Don't let Frankenstein's SDK sessions load project settings: don't pass `setting_sources=["project"]` or similar. Its working dir (`work/`) is inside this repo, so it would pick up this file and the README, which name the APIs and capabilities. That would break rule 2.
- Never write the user's credentials, `~/.claude` contents or any key into the repo, the event log or a sandbox mount.

## Code conventions

- Python 3.12, managed with `uv`. Add dependencies with `uv add` (it updates `uv.lock`). Tell the user, because `pyproject.toml` is shared.
- Match the surrounding code: dataclasses, type hints, `from __future__ import annotations`, short docstrings that start with the owner, little inline commentary. Line length 140.
- Emit events with `EventLog.emit(EventType.X, **data)` and include every field in `contracts.REQUIRED_FIELDS`. The UI relies on them. A new event type or required field is a contract change.
- Pass data between components as contract dataclasses. Convert to JSON with `to_jsonable`.
- Tests sit next to their area (`tests/test_<area>.py`). Kernel tests run against both the fake and the real implementation through the `sandbox` and `registry` fixtures in `tests/conftest.py`. A real implementation is done when its skipped variants run and pass. Never weaken or skip a test to get green.

## Git

- Work on your stream's branch, make small commits, and rebase on `main` often. Run `uv run pytest` before pushing.
- Commit or push only when the user asks. Never force-push `main`.
- Don't commit `registry/`, `logs/`, `work/`, `.env` or `CLAUDE.local.md`.
