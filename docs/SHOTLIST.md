# Video shot list and agent task list

For the team. The recorded take must be ≤ 90 s, run in `FRANK_MODE=demo` from an empty registry, with waiting sped up and labelled, and no failure cut (plan §11). Nothing in this file is shown to Frankenstein.

## 1. Brief requirement → task → where it shows on the dashboard

| Brief requirement (plan §1) | Proven by | Dashboard element to film |
|---|---|---|
| Registry shown before the run | Pre-roll | Registry card `0`, Registry history empty, header chips `demo mode` · `auth: …` · `sandbox: Docker, no secrets`, Budget card with caps |
| A task exposes a missing capability | T1 | Lab → **gap** card (Why / Needs / Returns / Registry search) |
| Gap comes from the task, not hardcoded | T1, T2 | Lab → **study** rows (the agent finds the API hosts itself), the task text in Output |
| Agent creates and tests it, harness runs the tests | T1 | Lab → builder/tester steps, `tests failed` → repair attempt → `tests passed`, test output expanded, `sandbox run` id |
| No install without passing tests, operator approves | T1 | Approval modal: manifest, domains, harness test log, code → **Approve** → `installed` |
| …then completes the task | T1 | Output answer citing call IDs; Lab → `call … ok` with arguments and output |
| Fresh session reuses without rebuilding or wiring | T2 | Output → `new process · pid N` chip, **Registry at start:** tags, "Reused from earlier runs", "Extended a@v1 → a@v2", "Built …"; Lab → `reused` chips |
| Upgrade keeps old tests, permissions diff | T2 | Approval modal permissions diff (added/removed), `retest` / old-suite chip on the test step |
| Agent builds tooling to discover and manage capabilities | T3 (+ T3b) | Lab → new registry-reading capability built, approval shows no network; answer table; retest steps |
| Caps on iterations and spend | All | Budget meters (usd, turns, gaps, minutes vs caps); B-roll: real **Cap hit** banner |
| Operator keeps authority | After T3 | Registry card **Roll back** / **Quarantine** → Registry history shows the `frankenstein-operator` commit; Kill switch (B-roll) |
| Honesty | All | Failures stay on screen; `[UNVERIFIED]` flags if any; README real/simulated/missing table |

## 2. Before the take

```bash
uv run pytest
uv run python scripts/fresh_start.py --label take-N    # moves registry/, logs, work/ to rehearsals/, never deletes
export FRANK_MODE=demo FRANK_APPROVER=ui
uv run python scripts/preflight.py                      # mode, auth, Docker, empty state, API hosts
uv run python -m channels.run                           # dashboard on :8001 (+ bots, if keys are set)
```

- Browser at 100 % zoom, 1920×1080, dashboard only. Screen recorder running for the **whole** session (cut from the full recording, never re-stage).
- Second window: a terminal for the `frank registry` / `git log` cutaways.
- In demo mode the Models selector is locked to `full`. Check the header shows **no** `fake` or `models: cheap` chip.
- Have `testdata/invoice_ok.pdf` and `testdata/invoice_bad_account.pdf` in a Finder window ready to drag onto the page.

## 3. Tasks to give the agent

Type each one into the pinned task input (or run the CLI line from `testdata/tasks.md`). Every dashboard task already starts a new `frank run` process. Wait for `run_finished` before starting the next one.

| # | Prompt (type exactly) | Attach | Expect | Operator action |
|---|---|---|---|---|
| **T1** | Is the supplier with IČO 27082440 a reliable VAT payer, and what's their registered address? | — | 1–2 gaps (company lookup, VAT status). Rehearsals built one combined tool; either is fine. Ideally one red test → repair → green | Open the test output and the code in the modal, then **Approve** each install |
| **T2** | Here's an invoice PDF. Check the supplier, verify the bank account on the invoice is a published one, and tell me the total in EUR at today's ČNB rate. | `invoice_ok.pdf` | Reuses T1's tool(s); upgrades the VAT tool to v2 for bank accounts (permissions diff, old tests re-run); builds invoice parsing + exchange rate | Pause on the permissions diff, **Approve** |
| **T2b** | Same prompt as T2 | `invoice_bad_account.pdf` | **No builds.** Everything reused; answer says the account is **not** published | None. This is the "pure reuse" shot |
| **T3** | Which of my tools reach which domains, when were they last tested, and are any of them broken? | — | Builds a registry-reading report capability and a health check that re-runs stored tests (`retest` chips). No network domains in its manifest | **Approve**; check the permissions show no network |
| T3b (optional) | I'm about to check a new supplier before paying them. Which of my tools would I use, best match first, and what does each one need as input? | — | Builds a search/ranking capability over the registry (the discovery half of the brief). Only needed if T1–T3 built nothing that searches | **Approve** |
| OP | *(no task)* Registry card → VAT tool → select `v1` → **Roll back**. Then **Quarantine** another tool (two clicks) | — | Registry history gets two operator commits; quarantined row turns red | Film it |
| T4 (optional) | Run T1's prompt again | — | Must **not** use the quarantined tool: it either reports the gap honestly or rebuilds and asks for approval again | Approve or **Reject** (a rejection is a good shot too) |

B-roll, recorded in a separate scratch log (`FRANK_LOG=… FRANK_REGISTRY_DIR=… FRANK_WORK_DIR=…`), never mixed into the main take's log:

| # | What | How |
|---|---|---|
| K | Kill switch | Start any task, hit **Kill switch** twice → "Run stopped by the kill switch" banner, status `killed` |
| C | Cap hit | A task whose source fights back, e.g. "How much is a MacBook Pro M5 on alza.cz?" (rehearsal `20261009-022157` hit `repairs_per_gap`). Or point the dashboard at that rehearsal's log and label the clip "rehearsal" |
| V | Voice channel (ElevenLabs stretch) | Telegram/Discord: "Read me the supplier verdict aloud." Only with real keys, label it |

## 4. Shot list (≤ 90 s)

Speed-ups are labelled on screen (`4×`, `16×`). Red steps are never cut.

| Time | Shot | On screen | Source |
|---|---|---|---|
| 0–6 s | **Empty start** | Dashboard: Registry `0`, Registry history empty, header chips, Budget caps. 1 s terminal cutaway: `uv run frank registry` → empty | Pre-roll |
| 6–10 s | Task 1 typed | Pinned input, **Run**; Output shows `new process · pid` · **Registry at start: empty** | T1 |
| 10–16 s | **Gap → study** | Lab gap card (Why / Needs / Returns / Registry search), then study rows with the hosts the agent found | T1, 4× |
| 16–24 s | **Red → repair → green** | Lab: builder, tester, `tests failed` with output open → repair attempt 2 → `tests passed` · sandbox run id | T1, 8× |
| 24–30 s | **Approval** | Modal: manifest, domains (`ares.gov.cz`, `mojedane.gov.cz`, …), harness test log, code → **Approve** → `installed` | T1 |
| 30–34 s | Answer | Output answer (reliable payer + address), call IDs; Lab `call … ok` | T1 |
| 34–38 s | **Fresh process** | New pid chip; **Registry at start:** T1's tools. Optional 1 s terminal: old process gone, new `frank run` | T2 start |
| 38–44 s | Invoice in | Drag `invoice_ok.pdf` onto the page, file chip, **Run**. Lab: `reused` chips | T2 |
| 44–52 s | **Upgrade with permissions diff** | Gap "Upgrade of …"; modal: permissions diff highlighted, old suite re-run chip → **Approve** | T2, 4× |
| 52–58 s | Two new builds | Invoice parsing + rate tools: tests green, approve (fast cut) | T2, 16× |
| 58–64 s | **Composed answer** | Output: EUR total, rate date, account published ✔, supplier verdict; "Reused / Extended / Built" lines | T2 |
| 64–68 s | Pure reuse | Bad-account invoice → no builds, answer: account **not** published | T2b, 8× |
| 68–76 s | **Self-built management tools** | Task 3: report capability built (no network in manifest), answer table: tool · domains · last tested · status; `retest` steps | T3, 8× |
| 76–82 s | **Operator authority** | Registry card: roll back VAT tool v2 → v1, quarantine one; Registry history shows the operator commits; Budget meters | OP |
| 82–90 s | **Growth** | Registry 0 → N, Registry history (all installs by `frankenstein-agent`), tiles (runs, success rate, spend), Spend per run chart. End card: *"Its capabilities may grow; its authority may not."* | End |

If T1 produces no red test, don't stage one: keep the green run and show a real red step from T2 or T3 in the 16–24 s slot, labelled with its task.

## 5. After the take

```bash
uv run python scripts/evidence.py            # EVIDENCE.md: built / reused / tested / approved per run
uv run python scripts/package_submission.py  # refuses a log with fake events
```

- Fill the ⏳ rows in the README real/simulated/missing table from the evidence, including anything that failed.
- Keep the full screen recording next to the cut, so a judge can check no failure was removed.

## 6. Risks seen in rehearsals

| Seen | Do |
|---|---|
| No rehearsal has run T3 yet | Rehearse T3 on `full` models first. It is the only proof of "builds its own management tooling" |
| Discovery tooling (search over the registry) never triggered: the planner builds it only when the registry is "large enough" | Run T3b if T3 didn't produce a search capability |
| T1 sometimes builds one combined tool instead of two | Fine for the brief. Adjust the voice-over, not the prompt |
| T2 needed several gate attempts in the `021010` rehearsal | Budget ~10 min of wall clock for T2; speed it up in the edit |
| Off-scenario tasks (weather, prices) hit caps or anti-bot pages | Keep them out of the main take; use one as the cap-hit B-roll |
