# 3. Frankenstein: Self-Building Agent

**Shared defaults (see [README](README.md)):** Python, Claude Agent SDK, `claude-sonnet-5-5` for the main agent loop, `claude-opus-5-5` as the tool builder, and `claude-haiku-5-5` for cheap work (classification, LLM-judge scoring). **Deviation:** the builder runs on Sonnet and only repairs run on Opus (see [Model per role](#model-per-role)). Agent events are streamed over SSE so judges can watch the agent think. **Deviation:** the UI is a thin FastAPI + SSE + single HTML page instead of Next.js, because the night is short and the UI is not what gets judged.

**Pitch:** An agent that notices when it can't do something, writes the missing capability itself, tests it in a sandbox, asks the operator to approve it, installs it in a versioned registry, and in a *fresh session* combines it with other capabilities to solve a *different* task. It also builds the tools it uses to find and manage its own capabilities. **Its capabilities may grow; its authority may not.**

**Brief:** Case 03 Frankenstein (Etnetera, prg.ai). Code freeze at sunrise. Deliverables are the repo plus a video of **≤ 90 s**.

---

## 1. Definition of done, mapped to the plan

| Brief requirement | Where it is met |
|---|---|
| A task exposes a missing capability | §4 Gap detection: the agent has no network except through capabilities, so the gap is real and provable |
| Agent creates, tests, registers it, then completes the task | §5 Core loop: builder → independent tester → harness-run tests → approval → install → resume |
| Agent builds or extends tooling for discovering and managing capabilities | §7 The agent builds `find_capability`, `capability_report`, `capability_doctor` from an operator task |
| Fresh session: a different task combines earlier capabilities, without rebuilding or manual wiring | §8 New process, no chat history, registry auto-loaded, task 2 composes task 1's tools |
| Sandbox, never on a host holding credentials | §6 Docker sandbox, no secrets inside, egress proxy outside |
| No install without passing tests, test run visible in the log | §5 Install gate in the harness. It runs the tests itself and writes the full output to the log |
| Gap comes from a task, not a hardcoded "build tool X" | §4 The prompts contain no tool names or API endpoints. The agent finds the APIs itself |
| Self-iterations and spend per run capped in code | §6 `limits.py`, enforced by the harness |
| Show the registry before the run | §11 First shot of the video plus `registry/` git hash |

---

## 2. Scenario: Czech supplier due diligence

**User:** a freelancer, small-business owner or accountant who receives invoices from Czech suppliers. Before paying, they must check that the supplier exists, that it is not an **unreliable VAT payer** (nespolehlivý plátce DPH), and that the bank account on the invoice is one the supplier has **published to the tax authority**. Otherwise the buyer can be liable for the supplier's VAT. Today people do this by hand on three different government websites.

Why this scenario:
- **Real value:** a legal and financial check people actually have to make. Not a toy.
- **Real gaps:** public government APIs of different kinds (REST JSON, a SOAP service, a plain-text rate list, PDF parsing). None of them is trivial, and none is pre-built.
- **Demo-safe:** public, keyless, stable APIs with no anti-bot protection, so tests are deterministic.
- **Composes naturally:** task 2 needs task 1's tools plus new ones.

**Data sources** (all three checked on 2026-10-08, results in the [README](README.md#api-check-2026-10-08). The team only *checks* that they work; the agent must discover them through `study`, and none of these URLs appear in any prompt):
- ARES, the business register (REST/JSON)
- Ministry of Finance VAT payer register (SOAP: unreliable payer status + published bank accounts)
- ČNB daily exchange rates (plain text)

### The three demo tasks

| # | Session | Task (as typed by the operator) | Expected capabilities |
|---|---|---|---|
| 1 | Session A | "Is the supplier with IČO 27082440 a reliable VAT payer, and what's their registered address?" | **Builds** `ares_lookup`, `vat_payer_status` |
| 2 | **Session B (fresh process)** | "Here's an invoice PDF. Check the supplier, verify the bank account on the invoice is a published one, and tell me the total in EUR at today's ČNB rate." | **Reuses** `ares_lookup`, `vat_payer_status` (v2 if it needs the bank-account field). **Builds** `parse_invoice`, `cnb_rate` |
| 3 | Session B or C | "Which of my tools reach which domains, when were they last tested, and are any of them broken?" | **Builds** `capability_report`, `capability_doctor` (§7) |

A ready-made sample invoice PDF is **test data, not code**. It is listed under "What is seeded" in §12.

Fallback if an API is down during the demo: the scenario still runs on recorded fixtures (the tests use them anyway), and the README says so.

---

## 3. Architecture

```
┌──────────────────────────── HOST (holds ANTHROPIC_API_KEY, never runs generated code) ────────────┐
│                                                                                                    │
│  Operator UI (FastAPI + SSE)  ←──────  Event log (JSONL)  ←──────  Harness (team-written, trusted) │
│    approve / reject / rollback /                                     • agent loop (Agent SDK)       │
│    quarantine / kill switch /                                        • limits.py (caps)             │
│    budget meter / registry view                                      • install gate                 │
│                                                                      • registry primitives          │
│                                                                      • capability host (loader)     │
│                                                                                                    │
│   registry/  (git repo: one folder per capability, tags = versions)                                │
│                                                                                                    │
│                  ┌──────────── Egress proxy (CONNECT allowlist per container) ───────────┐          │
│                  │  allows only the domains the manifest declares and the operator approved │       │
│                  │  injects API keys for keyed APIs (gateway mode); code never sees them    │       │
│                  └──────────────────────────────▲─────────────────────────────────────────┘       │
└─────────────────────────────────────────────────┼──────────────────────────────────────────────────┘
                                                  │
                     ┌────────────────────────────┴──────────────────────┐
                     │ Docker sandbox (per build / test / call)           │
                     │  • no env secrets, no host mounts except the       │
                     │    capability folder (read-only at run time)       │
                     │  • --network = proxy only, CPU / mem / time limits │
                     └────────────────────────────────────────────────────┘
```

**What the team writes (the harness):** agent loop, sandbox runner, egress proxy, install gate, registry primitives, capability loader, limits, event log, operator UI.
**What the agent writes (everything else):** every capability, its tests, and its manifest, including the tools for finding and managing capabilities.

### Tech stack

| Layer | Choice |
|---|---|
| Agent loop | Claude Agent SDK (Python). Planner/runtime `claude-sonnet-5-5`, builder `claude-sonnet-5-5`, repair `claude-opus-5-5`, tester `claude-sonnet-5-5` (a different role and prompt than the builder, so the builder doesn't grade itself), judge `claude-haiku-5-5`. See [Model per role](#model-per-role) |
| Sandbox | Docker, one container per build, test or call. `uv` for per-capability dependencies |
| Egress | Small Python proxy: CONNECT allowlist per container token, plus gateway mode that injects keys for keyed APIs |
| Registry | `registry/` git repo. Folder per capability, git tag per version (`ares_lookup@v2`). Diff, history and rollback come free |
| Capability formats | **Code tool** (Python function), **prompt-skill** (`SKILL.md` + eval cases), optional **MCP server** export (stretch) |
| Log | Append-only JSONL. Every event: plan, gap, study, build, test output, approval, install, call, cap hit |
| UI | Chat + live "lab" (code, tests, status) + registry view + approval cards + budget meter |

### Model per role

| Role | `FRANK_MODELS=full` (default, demo) | `FRANK_MODELS=cheap` (rehearsals only) |
|---|---|---|
| Planner | `claude-sonnet-5-5` | `claude-haiku-5-5` |
| Builder (first attempt) | `claude-sonnet-5-5` | `claude-sonnet-5-5` |
| Repair (after the gate refuses a build) | `claude-opus-5-5` | `claude-sonnet-5-5` |
| Tester | `claude-sonnet-5-5` | `claude-haiku-5-5` |

The table lives in `harness/agent/loop.py` (`TIERS`). Every `run_started` event records the models the run used.

The first plan put every build on Opus. We changed that after looking at where the tokens go:

- **Builds cost the most.** Every tool call is a model turn, and every turn sends the whole session again. On task 2 the builder took ~20 turns, a repair ~10 and the tester ~13 (`limits.py`). Opus costs twice as much per token as Sonnet (`PRICES_PER_MTOK`), so the Opus builder was most of the cost of a run.
- **On a subscription, cost means rehearsals.** We don't pay per token, but Opus builds use up the usage limits fastest. Our defence against flaky runs is three full rehearsals from an empty registry (§13), so cheaper builds buy rehearsals.
- **The gate sets the quality bar, not the model.** A weak first build is refused by the harness's own test run and never installed. The worst case of building on Sonnet is an extra repair, not a worse capability in the registry.
- **Opus goes where it's needed.** A gap whose build the gate refused has shown it's hard (SOAP, a large nested response). The repair also gets the harness's test output, so Opus works with the most context. A gap that passes the first time costs about half what it did. We haven't yet measured how often Sonnet's first builds pass (see §13).
- **The builder and tester now share a model.** Their independence never depended on the model: they run as separate sessions with separate prompts, the tester writes only `tests/` and the builder can't edit it, and the harness, not either role, runs the tests.

`cheap` is for testing the plumbing (gate, approvals, events, console) without spending the limits. It says nothing about how well the demo models do. `FRANK_MODE=demo` refuses it, like the fakes and the auto approver.

We didn't put the builder on Haiku: the extra repairs would cost more than they save. We didn't put the tester on Haiku in the demo either, because its tests are the gate's evidence and weak tests weaken the gate.

---

## 4. Gap detection: making the gap real

The weak point of most self-building agents is that they never *need* to build anything: with a generic `web_fetch` the LLM can answer most questions ad hoc. So:

- **The agent has no network access of its own.** Its kernel tools are: `study` (search + read docs, GET only, text only), `sandbox_exec` (only while building), `write_file` (only inside its build workspace), and the registry primitives (§7).
- **Task data must come from capabilities.** The final answer must cite the capability call IDs it used. The harness checks this provenance in the log and flags any answer without it.
- When the plan needs an operation no installed capability provides, the agent outputs a structured gap:

```json
{
  "gap": "look up a Czech company by IČO",
  "why": "task asks for registered address; no capability declares ares.gov.cz or returns company records",
  "inputs": {"ico": "string, 8 digits"},
  "outputs": {"name": "string", "address": "string", "vat_id": "string|null"},
  "kind": "code_tool",
  "registry_search": "find_capability('company lookup') → no match"
}
```

- **Generalization rule for the builder:** build the general operation (`ares_lookup(ico)`), not the one-off (`get_address_of_27082440`).

**Honest limit:** `study` could in principle read data from a web page. Mitigations: it is GET-only text, it is logged as "study", and the provenance check catches answers that don't come from capability calls. This goes in the README.

---

## 5. Core loop

```
Task ─→ Plan ─→ find_capability ──have it──→ call (sandbox) ──→ answer (with provenance)
                      │ missing
                      ↓
                 Gap (structured)
                      ↓
                 Study (docs, API shape; logged)
                      ↓
           ┌─ Builder: code + manifest ─┐   Tester (independent): tests + fixtures
           └──────────────┬─────────────┘
                          ↓
             HARNESS runs tests in sandbox   ← full output to log + lab panel
                 │ fail                 │ pass
                 ↓                      ↓
       repair (≤ MAX_REPAIRS)    Operator approval card
       then give up honestly     (manifest, permissions diff, test log, code)
                                        │ approve
                                        ↓
                         registry_install → git commit + tag → hot-load → resume task
```

**Install gate (harness, not agent):** `registry_install` refuses unless the harness itself ran the test suite in a fresh sandbox, all tests passed, and the operator approved. The agent can't claim tests passed; the gate only trusts its own run.

**Upgrades (v2):** when a capability can't handle a new input (e.g. task 2 also needs the published bank accounts from `vat_payer_status`), the agent extends it. The new version must pass **all old tests plus the new ones**. If the new version asks for a new domain or permission, the approval card shows the **permissions diff** and the operator must approve it again.

**Hot-loading:** after install, the harness resumes the session with the refreshed tool list (SDK session resume). Fallback: a generic `invoke_capability(name, args)` tool that reads the registry on every call.

### Capability contract (`manifest.yaml`)

```yaml
name: vat_payer_status
version: 2
kind: code_tool                   # code_tool | prompt_skill | mcp_server
description: Czech VAT payer status (reliable/unreliable) and published bank accounts by DIČ.
interface:
  input:  { dic: "string, CZ + 8–10 digits" }
  output: { reliable: bool, status_since: "date|null", bank_accounts: ["string"] }
permissions:
  network: [mojedane.gov.cz, adisrws.mfcr.cz]   # WSDL lives on adisrws, service location is mojedane
  filesystem: none
  secrets: []                     # names only; values injected by the proxy, never visible
dependencies: [zeep==4.3.1]
tests: { unit: tests/test_unit.py, fixtures: tests/fixtures/, live_smoke: tests/test_live.py }
origin: { task_id: t-0001, session: A, built_by: builder@claude-sonnet-5-5 }
uses: []                          # other capabilities it calls (composition)
```

A **prompt-skill** (e.g. `supplier_risk_summary`: how to combine registry checks into a short risk verdict) has the same manifest, plus eval cases with labels that the LLM judge scores. It must reach a pass threshold before install.

---

## 6. Safety and operator control

**Sandbox rules**
- Generated code runs only in Docker. No host secrets, no `ANTHROPIC_API_KEY`, no home directory.
- Build phase: network only to PyPI (through the proxy). Run and test phase: only the manifest's approved domains.
- CPU, memory and wall-clock limits per container.

**Caps in code (`limits.py`, enforced by the harness; every cap hit is logged and shown in the UI)**

| Limit | Value (demo) |
|---|---|
| `MAX_GAPS_PER_RUN` | 4 |
| `MAX_REPAIRS_PER_GAP` | 3 |
| `MAX_AGENT_TURNS` | 60 |
| `MAX_USD_PER_RUN` | $5 API-equivalent, counted from token usage (we run on subscriptions, see README) |
| `MAX_RUN_MINUTES` | 20 |
| `MAX_SANDBOX_SECONDS` | 60 per test or call |

**Operator control (the UI is the operator console)**
- **Approve or reject** each install, with the manifest, permissions diff, test log and code.
- **Rollback** any capability to an earlier tag. Callers keep working because the interface is versioned.
- **Quarantine or revoke:** disables the capability and removes its domains from the proxy.
- **Kill switch** stops the run, and a **live budget meter** shows $ spent, turns and gaps against the caps.
- **The agent can't touch** `limits.py`, the proxy allowlist, the install gate, or its own system prompt. They live outside its workspace and are read-only to it.

---

## 7. Agent-built discovery and management tooling

The harness gives the agent only **primitives**:
- `registry_list()`: raw manifests as JSON. No search, ranking or health.
- `registry_read(name, version)`
- `registry_install(bundle)`: gated (§5).
- `registry_propose_rollback(name, version)`: the operator confirms.

Everything above that is **built by the agent as normal capabilities**, with tests and manifests. Each one mounts the registry **read-only** and has no network:

| Capability | Gap that triggers it | What it does |
|---|---|---|
| `find_capability(query)` | Task 1: "do I have something for company lookup?", and a raw `registry_list` dump isn't a search | Matches on interface and description text, returns ranked candidates |
| `capability_report()` | Task 3: "which tools reach which domains, when were they last tested?" | Table of name, version, domains, last test run, pass/fail, usage count |
| `capability_doctor(name?)` | Task 3: "...are any of them broken?" | Asks the harness to re-run the stored tests, reports regressions, proposes quarantine |

All three are created from a task, not hardcoded. If the agent solves task 1 without building `find_capability` (e.g. because the registry is empty), that's fine: it will hit the gap in session B, when the registry has tools in it. We record whatever actually happens.

---

## 8. Fresh-session composition

- Session B is a **new OS process** with an empty conversation. The only state carried over is `registry/` (and the log).
- On startup the capability host loads every approved capability from the registry as a tool. No hand wiring, no config edits.
- Task 2 is **different** from task 1. The agent uses `find_capability` and gets `ares_lookup` + `vat_payer_status`. It upgrades `vat_payer_status` to v2 if it needs the bank-account field, and builds only `parse_invoice` and `cnb_rate`.
- **Evidence in the log:** `reused: [ares_lookup@v1, vat_payer_status@v2]`, `built: [parse_invoice@v1, cnb_rate@v1]`, plus the git log of `registry/` showing that task 1's tools were not rebuilt.

---

## 9. Night plan (~11 h to sunrise)

| Hours | Goal | Exit check |
|---|---|---|
| 0–2 | Harness kernel: Agent SDK loop, Docker runner, egress proxy, git registry, install gate, JSONL log, `limits.py` | A hand-run test bundle installs only when tests pass |
| 2–4 | Builder + tester + repair loop. Gap object. `study` tool | **Checkpoint: task 1 works end-to-end from an empty registry** |
| 4–6 | Capability loader for fresh sessions, task 2, v2 upgrade with regression tests, rollback, task 3 | Session B composes without rebuilding |
| 6–8 | Operator UI: lab panel, approval card with permissions diff, budget meter, registry view, quarantine and kill switch | Whole flow driven from the UI |
| 8–9.5 | Wipe the registry, run all 3 tasks **three times**, fix flaky parts, record the take | 3 clean runs, including one real repair |
| 9.5–11 | README (real/simulated/missing, how to run), cut the video, submit in HQ | Submitted **before the freeze** |

**With three people:** (1) harness, sandbox and proxy, (2) agent prompts plus builder/tester/gap, (3) UI, log, video and README. From hour 8, everyone works on rehearsal. Ownership, contracts, fakes and checkpoints: [docs/WORKSTREAMS.md](docs/WORKSTREAMS.md).

**Cut order if behind:** prompt-skill → `capability_doctor` → UI polish (fall back to log view + approval) → task 3. **Never cut:** task 1, fresh-session task 2, install gate, caps.

---

## 10. Judging criteria → what we show

| Criterion (weight) | Our answer |
|---|---|
| Value and track relevance (35 %) | A real legal and financial check (VAT liability for unreliable payers), done from a PDF in one request. A registry the agent built itself that grows and is reused |
| Originality (25 %) | "Capabilities may grow, authority may not": permission manifests enforced by an egress proxy, permission diffs on upgrade, and management tooling the agent builds itself |
| Working end-to-end (20 %) | Three tasks across two sessions from an empty registry, in one take |
| Technical execution (10 %) | Harness-run install gate, git-versioned registry with rollback, regression tests on v2, caps in code |
| Validation and honesty (10 %) | Failures stay in the video, all test output is in the log, and a real/simulated/missing table is in the README |

---

## 11. Video storyboard (≤ 90 s)

| Time | Shot |
|---|---|
| 0–8 s | `registry/` is empty (`git log` + UI registry view). The caps are on screen |
| 8–35 s | Task 1 → gap card → study → builder code + tester tests → **a red test → repair → green** → approval card (domains: `ares.gov.cz`, `mojedane.gov.cz`) → approve → answer with provenance |
| 35–40 s | Kill the process. Start a new one (visible terminal). |
| 40–65 s | Task 2 with the invoice PDF → `find_capability` hits the two old tools → v2 upgrade with **permissions diff** and old tests re-run → builds `parse_invoice` + `cnb_rate` → answer in EUR + supplier verdict |
| 65–80 s | Task 3 → the agent builds `capability_report` → table. Operator rolls back one tool, and the budget meter is shown |
| 80–90 s | Registry growth: 0 → 7 capabilities, all agent-written, all tested |

Speed up waiting, label it as sped up ("4×"), and **never cut failures**.

---

## 12. Real vs. simulated vs. missing (README template, filled in honestly at the end)

| Item | Status |
|---|---|
| All capabilities in `registry/` | **Real:** written by the agent during the recorded run. `git log` shows the author and the originating task |
| Harness (loop, sandbox, proxy, gate, loader, UI, primitives) | Written by the team. Not self-modifying |
| Sample invoice PDF, task prompts | Seeded **test data**, not code |
| Government APIs | Real live calls in the demo. Tests use recorded fixtures for repeatability |
| Speed | Waiting is sped up in the video and labelled |
| Model access | Demo runs on a Claude subscription via the Agent SDK, not an API key. `$` figures are API-equivalent estimates from token usage |
| Known limits | `study` can technically read data (mitigated by the provenance check). Text matching in `find_capability` is basic. The approval gate is one operator. *(Add whatever else is true at the freeze.)* |

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| The builder can't get SOAP right within 3 repairs | It's a real gap and a good on-screen repair. If it still fails, the agent reports the failure honestly. Rehearse it, but don't hint endpoints in prompts |
| A government API is down during recording | Fixtures-based tests still pass. Record the live take early (hour 8) and keep it |
| Hot-loading tools mid-session misbehaves in the SDK | Fallback is the generic `invoke_capability` tool |
| Flaky LLM behaviour across runs | Three full rehearsals from an empty registry. Low temperature where the SDK allows it. Keep the best *honest* take |
| Run costs more than expected | `MAX_USD_PER_RUN` stops it. Builds start on Sonnet and only repairs use Opus. Haiku judges. Rehearse the plumbing with `FRANK_MODELS=cheap` |
| Sonnet's first builds fail the gate much more often than Opus's did | Repairs already run on Opus. If rehearsals show most gaps need a repair, set the builder back to Opus in `TIERS` (one line) |
| HTTPS interception complexity | Use a CONNECT allowlist (no TLS MITM) for keyless APIs. Gateway mode only for keyed APIs |

---

## 14. Stretch (only after §9 hour 8 is green)

- **ElevenLabs side prize:** a task "read me the supplier verdict aloud" → the agent builds a `speak` capability. The key is injected by the proxy in gateway mode. Tick the box on the submission.
- **Export as MCP server:** the agent builds an `export_mcp` capability that packages selected registry tools as an MCP server other agents can install.
- **QR Platba (SPAYD) on invoices:** an invoice with a payment QR exposes the `parse_spayd` gap, and the result is checked against the published accounts.
- **Adopt path (Apify, MCP registries):** search a market, then wrap and test what you find. This scores as *install*, not *create*, so it's only extra.
- **Future work (README only):** learning whole systems, such as shopping and checkout on Czech e-shops (Shoptet adapters, Heureka feeds, 3DS approval). Out of scope for one night and risky with real money.
