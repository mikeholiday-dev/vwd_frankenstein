# Frankenstein: a self-building agent

An agent that notices when it can't do something, writes the missing capability itself, tests it in a sandbox, asks the operator to approve it, installs it in a versioned registry, and later, in a **fresh session**, combines it with other capabilities to solve a **different** task. It also builds the tools it uses to find and manage its own capabilities.

> Its capabilities may grow; its authority may not.

Built for **Agents 0.0.7 · From Dusk Till Dawn**, Case 03 *Frankenstein* (Etnetera, prg.ai).
Full plan: [03-frankenstein-self-building-agent.md](03-frankenstein-self-building-agent.md).

---

## Shared defaults

| Area | Default |
|---|---|
| Language | Python 3.12 for the harness, and Python for capabilities generated in the sandbox |
| Agent framework | Claude Agent SDK (Python) |
| Models | `claude-sonnet-5-5` for planning and the runtime loop, as the tester and as the tool builder · `claude-opus-5-5` repairs a build the install gate refused · `claude-haiku-5-5` for cheap classification and LLM-judge scoring. `FRANK_MODELS=cheap` (rehearsals only, refused in demo mode) moves the planner and tester to Haiku and repairs to Sonnet |
| Sandbox | Docker, one container per build, test or call. No secrets inside. Network only through the egress proxy |
| Dependencies | `uv`, isolated per capability |
| Registry | `registry/`, a git repo with one folder per capability and a git tag per version |
| Log | Append-only JSONL. Every plan, gap, build, test output, approval, install, call and cap hit is logged |
| UI | FastAPI + SSE + one HTML page: chat, live lab panel, approval cards, registry view, budget meter |
| Claude auth | Local dev and the demo: each person's own Claude subscription through the Agent SDK (Claude Code login, `ANTHROPIC_API_KEY` unset). Anything deployed: an API key. All model calls go through the Agent SDK so both work |
| Secrets | Only the Claude login (or key), on the host. Generated code never sees it. The proxy injects keys for keyed APIs |

## Brief rules we follow

- [ ] Generated code runs only in the sandbox, never on a host holding credentials.
- [ ] No install without passing tests. The **harness** runs the tests, and the full output goes to the log.
- [ ] Gaps come from tasks. **No tool names or API endpoints appear in any prompt.**
- [ ] Self-iterations and spend per run are capped in code (`limits.py`).
- [ ] The registry is shown empty before the demo run.
- [ ] Nothing in `registry/` is written by the team.
- [ ] The video is ≤ 90 s. Waiting may be sped up (and labelled), but failures are never cut.

## Demo scenario: Czech supplier due diligence

| # | Session | Task | Agent should |
|---|---|---|---|
| 1 | A | "Is the supplier with IČO 27082440 a reliable VAT payer, and what's their registered address?" | Build `ares_lookup`, `vat_payer_status` |
| 2 | **B (fresh process)** | "Here's an invoice PDF. Check the supplier, verify the bank account is a published one, and give me the total in EUR at today's ČNB rate." | Reuse both, upgrade `vat_payer_status` to v2 if needed, build `parse_invoice`, `cnb_rate` |
| 3 | B or C | "Which of my tools reach which domains, when were they last tested, and are any broken?" | Build `capability_report`, `capability_doctor` |

---

## API check (2026-10-08)

The team ran these checks with `curl` to confirm the scenario is feasible. **These details are for the team only. They must not go into agent prompts**: the agent has to discover the APIs itself through `study`.

### 1. ARES, business register (REST/JSON): ✅ works

- `GET https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}`
- `27082440` → HTTP 200, `"obchodniJmeno":"Alza.cz a.s."`, `"textovaAdresa":"Jankovcova 1522/53, Holešovice, 17000 Praha 7"`, `"dic":"CZ27082440"`. The `seznamRegistraci.stavZdrojeDph` field is `AKTIVNI` (registered for VAT).
- Not found (`12345678`) → **HTTP 404**, `{"kod":"NENALEZENO", "subKod":"VYSTUP_SUBJEKT_NENALEZEN"}`.
- Bad format (`abc`) → **HTTP 400**, `{"kod":"CHYBA_VSTUPU", "subKod":"VSTUP_NEVALIDNI_FORMAT_ICO"}`. IČO must match `[0-9]{8}`.
- Name search also works: `POST .../ekonomicke-subjekty/vyhledat` with `{"obchodniJmeno":"Etnetera","pocet":3}` → 7 matches.
- 10 rapid calls in a row all returned 200. No auth, no rate limiting seen.
- The response is large and nested (dozens of fields). That's good for the demo: the builder must map it to a clean output shape.

### 2. Ministry of Finance VAT payer register (SOAP): ✅ works

- WSDL: `https://adisrws.mfcr.cz/dpr/axis2/services/rozhraniCRPDPH.rozhraniCRPDPHSOAP?wsdl`
- ⚠️ **The WSDL's service `location` is now `https://mojedane.gov.cz/dpr/axis2/services/rozhraniCRPDPH.rozhraniCRPDPHSOAP`.** Both hosts answer the same way today. A wrapper generated from the WSDL (e.g. `zeep`) will call `mojedane.gov.cz`, so **the proxy allowlist must accept both domains**.
- Operations: `getStatusNespolehlivyPlatce`, `getStatusNespolehlivyPlatceRozsireny` (+ published bank accounts, name, address), `getStatusNespolehlivySubjektRozsireny`, `...V2`, `getSeznamNespolehlivyPlatce` (full list).
- Input `dic` is **digits only, without the `CZ` prefix** (`\d{1,10}`), and you can send several per request. A wrapper has to strip the `CZ` that ARES returns. This is a nice small gap for the tests to catch.
- `getStatusNespolehlivyPlatceRozsireny` for `27082440` → `statusCode="0" OK`, `nespolehlivyPlatce="NE"` (reliable), **19 published bank accounts**. There are two formats, `standardniUcet` (`predcisli`, `cislo`, `kodBanky`) and `nestandardniUcet` (IBAN, including SK/DE/AT/HU), so matching an invoice account needs normalisation.
- Unknown DIČ `12345678` → `nespolehlivyPlatce="NENALEZEN"` (not an error, HTTP 200).
- `getSeznamNespolehlivyPlatce` → HTTP 200, 0.2 s, ~505 KB, **4,340 unreliable payers** (`nespolehlivyPlatce="ANO"`, with `datumZverejneniNespolehlivosti`). Example: DIČ `00121100` = LIDRU, a.s. (found in ARES too). It's a usable "bad supplier" test case, but prefer a fixture over naming a real company in the video.
- Latency was 0.1–0.2 s.

### 3. ČNB daily exchange rates (plain text): ✅ works

- `GET https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt`
- Format: first line `08.10.2026 #194`, then the header `země|měna|množství|kód|kurz`, then pipe-separated rows. **Decimal comma** (`EUR|24,400`), and **`množství` (amount) varies** (`JPY` per 100, `IDR` per 1000). Both are easy to get wrong, so they're good test cases.
- English version: `.../en/financial-markets/foreign-exchange-market/central-bank-exchange-rate-fixing/central-bank-exchange-rate-fixing/daily.txt` uses a decimal point (`EUR|24.400`, `USD|21.811` on 08 Oct 2026).
- History: `?date=DD.MM.YYYY`. For a weekend date (`03.10.2026`, a Saturday) it returns the **previous business day** (`02.10.2026 #190`). The tool should report the actual rate date.

### Summary

| API | Status | Auth | Format | Gotchas for tests |
|---|---|---|---|---|
| ARES | ✅ 200 | none | REST/JSON | 404/400 error bodies, 8-digit IČO, large nested response |
| MFČR VAT register | ✅ 200 | none | SOAP 1.1 | Endpoint on `mojedane.gov.cz` (allow both hosts), DIČ without `CZ`, two account formats, `NENALEZEN` ≠ error |
| ČNB rates | ✅ 200 | none | pipe-separated text | Decimal comma, `množství` per 100/1000, weekend → previous business day |

The demo still needs a **sample invoice PDF** (test data, not code) with supplier IČO/DIČ, a bank account and a total. Use one of the published Alza accounts for the "valid" invoice and a made-up account for the "invalid" one.

---

## Repo layout

Three parallel workstreams. Ownership, rules and checkpoints are in **[docs/WORKSTREAMS.md](docs/WORKSTREAMS.md)**.

```
harness/
  contracts.py    # SHARED: data shapes, Protocols, event schema. Change by PR only
  config.py       # SHARED: paths + FRANK_* env switches
  wiring.py       # SHARED: picks real vs fake components
  fakes.py        # SHARED: dev stand-ins (local sandbox, dir registry, cli/auto approver)
  cli.py          # B: `frank run|install|call|registry`
  kernel/         # A: sandbox, egress proxy, git registry, install gate, capability host, limits.py
  agent/          # B: agent loop, kernel tools, builder/tester prompts
  ops/            # C: JSONL event log, approvals and kill over the log
ui/               # C: FastAPI + SSE operator console
scripts/          # C: fake_run.py replays a scripted run for UI work
tests/            # one file per area; kernel tests run against fakes AND real impls
testdata/         # C: sample invoices and other seeded test data, no code
registry/         # agent-written capabilities only (own git repo, gitignored here)
logs/             # JSONL event log (gitignored)
work/             # agent build workspaces (gitignored)
```

## Running

```bash
uv sync
uv run pytest                                     # main stays green

# kernel smoke test: push a hand-made bundle through the gate
uv run frank install tests/fixtures/bundles/echo_ok
uv run frank call echo '{"text":"ahoj"}'

# operator console
uv run uvicorn ui.app:app --reload                # http://localhost:8000
uv run python scripts/fake_run.py                 # scripted run, approve it in the UI
uv run python scripts/fake_run.py --scenario upgrade   # v2 upgrade with a permissions diff
uv run python scripts/fake_run.py --scenario capped    # repairs fail until the cap stops the run

# agent sessions: the exact prompts are in testdata/tasks.md
FRANK_APPROVER=ui uv run frank run --session A "Is the supplier with IČO 27082440 ..."
FRANK_APPROVER=ui uv run frank run --session B --attach testdata/invoice_ok.pdf "Here's an invoice PDF ..."

# delivery
uv run python scripts/preflight.py                # before a take: mode, auth, Docker, empty state, hosts (--offline skips the network)
uv run python scripts/evidence.py                 # EVIDENCE.md from the event log: per run, what was built, tested, approved, called
uv run python scripts/package_submission.py       # submission/<timestamp>/: log, evidence, registry bundle; refuses a log with fake events
```

`FRANK_MODE=demo` refuses every fake, the auto approver and `FRANK_MODELS=cheap`. Use it for the recorded run.

`FRANK_MODELS=cheap uv run frank run ...` rehearses the plumbing (gate, approvals, events, console) on cheaper models. It says nothing about how well the demo models do.

### Rehearsal and the recorded run

```bash
uv run python scripts/fresh_start.py --label take-2   # moves registry/, the log and work/ to rehearsals/, never deletes
uv run uvicorn ui.app:app                             # the open console reloads itself on the new, empty log
export FRANK_MODE=demo FRANK_APPROVER=ui
uv run frank registry                                 # empty: the 0–8 s shot (plus the console's Registry history)
uv run frank run --session A "<task 1>"               # then a new terminal (fresh process) for task 2, then task 3
```

Test data for task 2 is in `testdata/` (`invoice_ok.pdf`, `invoice_bad_account.pdf`).

## Real vs. simulated vs. missing

*Current state; re-checked at the code freeze. Rows marked ⏳ depend on the recorded run.*

| Item | Status |
|---|---|
| Capabilities in `registry/` | ⏳ **Real:** written by the agent during the recorded run. `registry/` is its own git repo: every install is a commit by `frankenstein-agent` naming the originating task and session, every rollback/quarantine a commit by `frankenstein-operator` |
| Harness (loop, sandbox, proxy, gate, host, UI, kernel tools) | Written by the team. Not self-modifying |
| Sandbox | **Real:** Docker, one container per build, test or call. No host env, no home, no credentials; read-only code mount for calls; CPU, memory, pids and time limits; dependencies cached per hash |
| Network | **Real:** an egress proxy with a CONNECT allowlist per container. Builds and dependency installs reach PyPI only; tests and calls reach exactly the hosts in the manifest (HTTPS only). No TLS interception, so it sees hosts, not URLs |
| Install gate | **Real:** the harness runs the tests itself on a throwaway copy; a v2 must also pass the active version's stored tests; bundle-shape refusals before any test run; then the operator approves with the permissions diff |
| Composition (`uses`) | **Real:** a capability calls installed ones in the same container under the caller's permissions; authority can't grow through it |
| Agent loop | **Real:** one Agent SDK session per role (planner, builder, tester) over our own tools only; the SDK's built-in Bash, file and web tools and the local install's browser tools are disabled and refused by a hook; no project settings are loaded |
| Provenance check | **Real:** an answer must cite successful capability calls from this run, otherwise it is flagged `[UNVERIFIED ...]` in the log and the console. "Reused from earlier sessions" in the evidence is computed from the log, not from what the agent says |
| Input files | **Real:** the operator's files (the invoice PDF) are copied read-only next to every call; test runs don't see them |
| Sample invoice PDFs, task prompts | Seeded **test data**, not code (`testdata/`, generated by `scripts/make_invoices.py`) |
| Government APIs | Real live calls in the demo. The agent's own tests may use recorded fixtures for repeatability |
| `scripts/fake_run.py` | **Simulated:** a scripted event replay for UI work. Every event is marked `fake`, the console shows a banner, and `FRANK_MODE=demo` refuses fakes. Never used for the video |
| Speed | Waiting is sped up in the video and labelled |
| Model access | The demo runs on a Claude subscription through the Agent SDK, not an API key. `$` figures are API-equivalent estimates from token usage |
| Not yet verified | A full real build, test and install run has not been exercised in the cloud dev container (no Docker daemon there). It needs a rehearsal on a machine with Docker before the recorded run |
| Missing | Prompt-skill installs (cut, plan §9). The agent can't yet propose a rollback itself; the operator rolls back from the console |
| Known limits | Quarantine doesn't cut a call already in flight (≤ 60 s). Dependencies need PyPI reachable. `study` can technically read data (mitigated by the provenance check) and follows redirects without re-checking the target address. Capabilities installed mid-session are called through `invoke_capability`. Text matching in `find_capability` is basic. The approval gate is one operator |
