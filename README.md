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
| Models | `claude-sonnet-5-5` for planning and the runtime loop and as the tester · `claude-opus-5-5` as the tool builder · `claude-haiku-5-5` for cheap classification and LLM-judge scoring |
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

# agent session (stream B; not implemented yet)
FRANK_APPROVER=ui uv run frank run --session A "Is the supplier with IČO 27082440 ..."
```

`FRANK_MODE=demo` refuses every fake and the auto approver. Use it for the recorded run.

## Real vs. simulated vs. missing

*Filled in honestly at the code freeze; see §12 of the plan for the template.*
