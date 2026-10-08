# 3. Frankenstein: Self-Building Agent

**Shared defaults (see [README](README.md)):** TypeScript or Python, Claude Agent SDK, `claude-sonnet-5-5` for the main agent loop, `claude-haiku-4-5` for cheap fan-out work like extraction and classification, and `claude-opus-5-5` for final synthesis and judging. Use Next.js + Tailwind + shadcn/ui for the UI, and stream agent events over SSE so judges can watch the agent think.

**Pitch:** An agent that notices when it lacks a capability, writes the tool itself, tests it in a sandbox, installs it in its own toolbox, and reuses it next time. Each task makes it more capable.

It starts with a small kernel and **two built-in markets**: **Apify**, where it rents scrapers and data Actors, and **Masumi**, where it hires other AI agents and pays them with test stablecoins held in escrow. Everything else it builds itself, driven by what the user asks for. Over time it can also **sell** the tools it has built back to other agents on Masumi.

## Tech stack

| Layer | Choice |
|---|---|
| Core agent | Claude Agent SDK (`claude-sonnet-5-5` runtime, `claude-opus-5-5` as tool builder) |
| Capability format | **Agent Skills** (`SKILL.md` + scripts) and/or generated **MCP servers**, both of which load dynamically |
| Sandbox | E2B or Docker containers (network allowlist, CPU and time limits) |
| Testing | Generated pytest/vitest suites plus an LLM judge for fuzzy outputs |
| Registry | SQLite + embeddings (capability descriptions → vector search) with versioning |
| Dependencies | `uv` (Python) for fast, isolated installs per tool |
| Base capabilities | Given from the first run: kernel (files, sandbox exec, web fetch) + **Apify** + **Masumi**. Everything else is built on request. See "Base toolbox" below |
| Capability sourcing | Search order: local registry → markets (**Apify Store** and **Masumi registry**, searched in parallel) → build from scratch. See "Capability sourcing" below |
| Apify | Apify MCP server (`mcp.apify.com`) with `APIFY_TOKEN`, plus the REST API for wrapper tools. Account-level spend limit |
| Masumi | Own Masumi node (Registry + Payment Service via the `masumi-services-dev-quickstart` Docker Compose), Cardano **Preprod** with test ADA + test USDM, Blockfrost API key. Official **Masumi MCP server** for hiring, `pip-masumi` for the seller storefront. See "Masumi: hiring and selling agents" below |
| Secrets | `APIFY_TOKEN` and the Masumi node tokens are set up **before the first run** (they are base capabilities) and live in the local vault. Keys for anything else are requested from the user at runtime and stored in the same vault. Secrets never appear in generated code; the vault injects each one only into the tools that declare it |
| UI | Next.js: chat on the left, a live "lab" panel on the right (code, tests, status), and a "toolbox" gallery |
| Browser automation (Phase 4) | Playwright (persistent profile per site, screenshot streaming) + the Claude computer-use tool as a fallback |
| Commerce + payments (Phase 4) | Any Czech shop via ways to connect (UCP/ACP/MCP → platform adapters for Shoptet, Upgates, WooCommerce, Shopify, PrestaShop → Heureka/Zboží XML feeds + Playwright → computer use); Alza.cz via browser (community Alza MCP for search), Rohlík official MCP (OAuth), virtual card with a limit + 3DS, SPAYD QR parsing, Fio banka API; backup: Shoptet/Shopify test store with Comgate/GoPay test mode |

## The core loop

```
Task → Plan → Capability check ──have it──→ Use tool → Done
                    │ missing
                    ↓
        Source check (base capabilities):
        Apify Store · Masumi registry
          │ fits                    │ nothing fits / cheaper to build
          ↓                         ↓
  Trial run → wrapper spec      Tool spec
          └───────────┬─────────────┘
                      ↓
        Generate code → Generate tests → Sandbox run
              ↑                               │
              └──── fix (max N tries) ←─ fail ┘
                                              │ pass
                                              ↓
        Security review → Install to registry → Hot-load → Resume task
```

## Base toolbox: what Frankenstein starts with

Frankenstein starts with three groups of tools. Their credentials are set up before the first run. **Everything not listed here is built, wrapped, or adopted in response to the user's requests.**

| Group | Tools | What it is for |
|---|---|---|
| Kernel | read/write files, run code in the sandbox, web fetch | Building and testing its own tools |
| **Apify** (market for data) | Apify MCP server (`mcp.apify.com`): search the Store, read an Actor's details and input schema, run an Actor, read its dataset | Scraping and public data at scale: proxies, anti-bot handling, and headless browsers that run on Apify's infrastructure |
| **Masumi**, buying (market for agents) | Official Masumi MCP server: `list_agents`, `get_agent_input_schema`, `hire_agent`, `check_job_status`, `get_job_full_result`; plus harness tools `masumi_request_refund` and `masumi_job_state` | Hiring other AI agents for judgement-heavy jobs, paid in test USDM held in escrow |
| **Masumi**, selling | Harness tools `masumi_publish(tool, price)`, `masumi_unpublish(tool)`, `masumi_earnings()` | Selling Frankenstein's own tools to other agents (Phase 3) |

The base tools give raw access to the markets. When Frankenstein wants a capability it can use again, it still **wraps** the Actor or agent as a normal registry tool, with tests, so it can find, reuse and compose it later.

The harness tools are small and hand-written. They call the local Masumi Payment Service. They are part of the harness and outside the code the agent can rewrite, as rule F in Phase 4 requires.

## Capability sourcing: local → markets (Apify, Masumi) → build

When the capability check fails, the agent does not go straight to writing code. It first looks for something to reuse, rent, or hire:

```
Gap (name, inputs, outputs, purpose, kind: data | judgement | function)
  │
  ├─ 1. Local registry (embeddings search) ── match ──→ use / compose / extend (v2)
  │
  ├─ 2. Markets, searched in parallel (base capabilities)
  │     ├─ Apify Store ── candidate Actors
  │     │     ranked by: fit, success rate, users, last update, price per result
  │     │     → read input schema + README → trial on a tiny input (1 run, ≤ N results, ≤ $X)
  │     │     → security review (third-party code running on Apify, not in our sandbox)
  │     │     → WRAP as an L1 tool, origin = "apify:<actor-id>@<build>"
  │     │
  │     └─ Masumi registry ── candidate agents
  │           ranked by: fit, price, /availability, past results in our own log
  │           → read /input_schema and the free /demo example → hire on a tiny input (≤ budget)
  │           → judge the result before the dispute window ends → refund if it fails
  │           → WRAP as an L1 tool (or L3 if it needs judgement around the call),
  │             origin = "masumi:<agentIdentifier>"
  │
  └─ 3. Nothing suitable, or cheaper to build → build from scratch (the normal loop)
```

**Rent, hire, or build?** The agent decides and shows its reasoning in the lab panel:
- **Build** when the capability is a simple, deterministic function: unit conversion, parsing a format, generating a QR code, or calling a free public API. Owning it costs nothing per call, and building is what the track is about.
- **Rent from Apify** when it needs scraping infrastructure or public data in volume: maps and reviews, social profiles, product catalogs, or crawling a site.
- **Hire on Masumi** when it is a judgement-heavy job another agent already does well, such as a research report, a website audit, or translating and summarizing a long document, and building it would take longer than the task itself.
- **Hybrid:** a local tool that composes rented and hired parts, for example Apify for the data and a Masumi agent for the analysis.

**What "wrap" means.** The Actor or agent is not the tool; the wrapper is. The builder subagent generates a Python function plus manifest that:
- calls the market: an Apify Actor through the Apify API (`run-sync-get-dataset-items`, or an async run plus polling for slow Actors), or a Masumi agent through the local Payment Service (start job → pay → poll status → fetch result),
- maps our clean, general inputs to the provider's input schema (`find_places(query, location, max_results)`, not an Actor's 40 raw fields),
- normalizes the output into a stable, typed shape, so the provider can be swapped later without breaking callers,
- enforces a per-call budget (max results, max cost, timeout) and logs the actual cost,
- declares its permissions: network access only to `api.apify.com` or the local Masumi node (`localhost:3000/3001`) plus the hired agent's API URL, with tokens injected by the vault.

**Rules**
- **Budget outside the agent's reach:** per-task and per-day spend caps for Apify and Masumi, enforced by the harness and not by generated code. They are backed by limits the agent can't touch: the Apify account's spend limit, and a Masumi purchasing wallet that only ever holds a small amount of test USDM.
- **Pin the version:** record the Actor's build number, or the Masumi `agentIdentifier` plus a hash of its input schema, in the manifest. If either changes, the health check fails and the agent re-wraps (v2) or picks another provider.
- **Tests:** the tester subagent writes tests independently from the wrapper. Offline fixtures come from a recorded Apify dataset sample or from a Masumi agent's free `/demo` endpoint. Live health checks use a tiny Apify run, or `/availability` plus a rare paid job on Masumi.
- **Composition:** wrappers are normal tools, so later tools can build on them (e.g. `cafe_review_summary` = `find_places` + `fetch_reviews` + LLM summary).
- **Fallback:** if the chosen provider fails or is too expensive, try the next candidate, then build from scratch.
- **Gallery:** each toolbox card shows its origin (local build / Apify Actor / Masumi agent / hybrid), cost per call, and provider version.
- **Which input is allowed:** public data only. Logged-in actions (checkout, accounts) stay on our own browser profile, never on Apify. Masumi job inputs go to a third-party agent, so they **never contain secrets, buyer-profile data, or personal data**. The security reviewer checks each wrapper's inputs for this.

**Demo addition:** "How many Google reviews does the best-rated café near Karlín have, and what do people complain about?" The agent finds no local tool. It searches both markets, shows 3 candidate Apify Actors (and no fitting Masumi agent), trial-runs one, and wraps it as `find_places` + `fetch_reviews`. The tests go green and the answer arrives. Ask a similar question about a restaurant in Brno and it reuses the tools instantly.

## Masumi: hiring and selling agents

[Masumi](https://www.masumi.network) is a payment and identity layer for AI agents on Cardano. Agents are registered on-chain (each as an NFT), expose a standard HTTP API (**MIP-003**: `/start_job`, `/status`, `/availability`, `/input_schema`, plus optional `/provide_input` and `/demo`), and are paid through an **escrow smart contract**. The buyer's funds are locked, the seller submits a hash of the result, and the seller is paid only after a dispute window in which the buyer can ask for a refund. The hashes of each job's input and output are recorded on-chain (**decision logging**). **Sokosumi** is the marketplace built on top. Masumi also supports **x402** on Cardano (the Payment Service has `POST /payment/x402`).

### Setup (once, before the first run)

1. **Masumi node:** clone `masumi-network/masumi-services-dev-quickstart` and set `ADMIN_KEY` (≥ 15 characters), `ENCRYPTION_KEY` (≥ 20 characters), and `BLOCKFROST_API_KEY_PREPROD` in `.env`. Then run `docker compose up -d`. This starts the Registry Service (`localhost:3000`), the Payment Service (`localhost:3001`, admin UI at `/admin`), and Postgres.
2. **Wallets:** in the admin UI, export the generated mnemonics right away and store them safely. Fund the **purchasing wallet** with test ADA (Cardano faucet) and **test USDM** (Masumi faucet), but only enough for the demo. The **selling wallet** needs only a little ADA for network fees.
3. **API keys:** create Payment and Registry tokens in the admin UI and store them in the vault.
4. **Masumi MCP server:** clone `masumi-network/masumi-mcp-server`, run `uv sync`, and point it at the local node (`MASUMI_NETWORK=Preprod`, both base URLs and both tokens).
5. **Builder knowledge:** give the builder subagent the official `masumi-skills` skill and Masumi's `llms.txt` / `.md` docs. Then it writes correct Masumi wrappers without studying the protocol first.

### Buying: pay only for work that passes

Hiring a Masumi agent fits Frankenstein's testing loop, because the escrow gives the tester a veto:

```
masumi:<agent> wrapper call
  → GET /input_schema, POST /start_job        (agent returns job_id + payment terms)
  → hire via the Payment Service              (USDM locked in escrow: FundsLocked)
  → poll /status                              (seller submits result hash: ResultSubmitted)
  → verify the result hash, then the tester / LLM judge checks the result against the spec
       ├─ pass → keep result; seller is paid after the dispute window   (Withdrawn)
       └─ fail → masumi_request_refund (POST /purchase/request-refund)  (RefundRequested → RefundWithdrawn)
```

- The lab panel shows the on-chain state of each job live, with links to a Preprod explorer.
- Results from a hired agent are **untrusted data**, never instructions. They go through the same judging as any other tool output.
- Our own log of each agent's past results (pass rate, refunds, latency) feeds the ranking the next time that agent is a candidate.

### Selling: Frankenstein sells its organs (Phase 3)

Phase 3 item 20 goes one step further: Frankenstein publishes the tools it built as paid Masumi agents.

- **Storefront:** a single harness service, built on `pip-masumi`, that exposes every published tool under its own MIP-003 base URL (`/t/<tool>/start_job`, `/status`, `/availability`, `/input_schema`, `/demo`). One process covers all the tools, so no new L4 service is needed per tool. It is reachable through a public URL (ngrok or Fly.io).
- **Publish (`masumi_publish`):** only after the tool passes its tests and the security review, **and a human approves it**. The tool's `input_schema` and `/demo` come from its manifest and test fixtures. `POST /registry` on the Payment Service mints the agent's NFT. Listing on Sokosumi is optional.
- **Serve a job:** check that the buyer's funds are locked → run the tool in the sandbox → `POST /payment/submit-result` with the result hash → payment arrives after the dispute window. `masumi_earnings` feeds the gallery.
- **What can be sold:** only tools that need no vault secrets, no user data, and no logged-in sessions. Tools that call paid Apify or Masumi wrappers must be priced above their own cost. The human sets the price.

### Demo

1. **Hire:** "Audit the website of example-obchod.cz and tell me the three biggest problems." This is a judgement-heavy job, so Frankenstein searches Masumi, finds a website-audit agent, reads its schema and demo, pays in test USDM, and the judge accepts the report. Wrapped as `website_audit`, it is reused for a second site.
2. **Refund:** a second agent that we run ourselves returns junk. The judge fails it, Frankenstein requests a refund, and the job moves to `RefundRequested` on-chain.
3. **Sell:** after the Phase 0–3 demo, publish the freshly built QR-code tool. It is a pure function with no secrets, so it can be sold. A second agent (or a teammate in Sokosumi) hires it, and the gallery card shows "earned 0.50 USDM".

### Masumi risks

| Risk | Mitigation |
|---|---|
| Preprod confirmations take ~20 s to a few minutes | Show job states streaming in the lab panel. Register the storefront agents and make one test purchase before the demo |
| The dispute window delays the seller's payout | Show "funds locked → result submitted" live and the payout from a rehearsal. The refund path is the stronger moment anyway |
| Few useful agents on the Preprod registry | Run our own seller agents (one good, one junk) so the hire and refund demos never depend on third parties |
| Endpoint names differ between doc versions | Treat the node's live OpenAPI (`localhost:3001/docs`) as the source of truth. The `masumi-skills` reference notes that all paths are singular (`/payment`, `/purchase`, `/registry`) |

## Features by phase

**Phase 0: One full self-extension (MVP)**
1. Base agent with only the base toolbox: the kernel (read/write files, run code in the sandbox, web fetch) plus **Apify** and **Masumi** (see "Base toolbox"). Nothing else is pre-built.
2. **Gap detection and source check:** the agent produces a structured "capability gap" (name, inputs, outputs, purpose, kind) when its current tools can't do the task. It then searches Apify and Masumi and makes the rent / hire / build decision, explaining it in the UI.
3. **Builder subagent** writes the tool as a Python function plus a manifest.
4. **Tester subagent** writes tests independently from the builder, which avoids self-grading. Tests run in the sandbox.
5. Repair loop: test failures go back to the builder, up to 3 attempts.
6. Install: the tool is saved to the registry and hot-loaded into the agent's tool list. The agent resumes and finishes the original task.

**Phase 1: Memory and reuse (the real point of the track)**
7. Semantic capability registry: new tasks query existing tools first, so nothing gets rebuilt, re-rented, or re-hired.
8. **Second-run demo:** a similar task runs instantly using the tool built earlier.
9. **Generalization:** the builder writes general tools (`convert_any_currency`, not `convert_usd_to_eur`).
10. Composition: new tools can call existing tools.
11. Versioning and upgrades: if a tool fails on a new input, the agent extends it (v2) and re-runs all old tests to catch regressions.

**Phase 2: Safety and quality**
12. **Security reviewer agent** checks generated code for network calls, file access, and secrets before install. It also reviews third-party Apify Actors and Masumi agents before they are wrapped, and checks that no secrets or personal data reach a Masumi job input. Risky tools need human approval.
13. Permission manifest per tool (network domains, filesystem scope) enforced by the sandbox.
14. Dependency handling: the tool declares pip packages, which are installed into an isolated environment.
15. Secrets request flow: "This tool needs an OpenWeather API key. Please provide it." Stored in the vault and injected at runtime.
16. Health checks: periodic re-testing of installed tools, with broken ones quarantined.
17. **Spend caps** for Apify and Masumi (per task, per day), enforced by the harness and shown as a live budget meter in the UI.

**Phase 3: Wow factor**
18. **Toolbox gallery:** cards for each tool showing its origin (local build / Apify Actor / Masumi agent / hybrid), origin task, tests passed, usage count, cost per call, earnings, and version history.
19. **Growth chart:** capabilities over time and success rate on a fixed benchmark of tasks (before vs. after self-building).
20. **Export and sell capabilities:** export tools as real Claude Agent Skills or MCP servers that other agents can install, and **sell them as paid Masumi agents** through the storefront (see "Selling: Frankenstein sells its organs"). Frankenstein builds organs for others.
21. Self-improvement of the builder: the agent edits its own builder prompt based on failure patterns (meta-level).
22. Tool retirement: unused or superseded tools are archived automatically, and unpublished from Masumi if they were for sale.

**Demo:** Ask: "What's the weather in Prague, converted to Fahrenheit, and make me a QR code linking to the forecast." The agent has none of these tools. The lab panel shows the source check first ("Apify: 2 weather Actors, Masumi: none. Building: a free weather API plus two pure functions are cheaper to own than to rent per call"). It builds three tools live and the tests turn green. Then ask a similar question about Tokyo, and it answers instantly using the tools it just built. Show the toolbox gallery growing.

---

## Phase 4 (stretch): Learning whole systems: shopping on any Czech e-shop

Phases 0–3 teach Frankenstein to build **single, stateless functions**. The other two tracks (Dossier, AgentBazaar) are **systems**: multi-step workflows, external accounts, long-running services, real money or personal data, and results with no single correct answer. This phase adds what it takes to learn those on the fly. It is proven on one concrete, real-world goal: **buy a real product from a Czech e-shop and pay for it.** It starts with Alza.cz (4.2) and then extends to **any Czech shop on request** (4.3).

### 4.1 What Frankenstein must gain

**A. A ladder of capability types.** The gap detector must also decide *what kind* of capability is missing:

| Level | What it is | Example |
|---|---|---|
| L1 Tool | One function (Phases 0–3) | `parse_spayd_qr`, `fetch_github_profile` |
| L2 Skill | A procedure that uses several tools (`SKILL.md` + scripts) | "Check out on Alza", "Pay an HTTP 402 response" |
| L3 Subagent | Its own prompt, tools, and judgement | Shopping agent that compares products, arbitrator |
| L4 Service | A long-running process with an endpoint and state | Seller agent behind x402, escrow contract |

**B. A tree of capabilities instead of one gap.** "Buy X on Alza" breaks down into a tree of capabilities. Frankenstein builds the leaves first, reuses what already exists, and estimates the cost of each piece before starting.

**C. A "study" step before the build step.** Find the docs, SDK, or existing MCP servers. Write a small trial script against the real thing. Save what it learned, such as "Alza checkout: cart → delivery → payment → summary", as notes for future builds.

**D. Adopt before building.** Search the local registry, then the two base markets, **Apify** and **Masumi** (see "Capability sourcing"), then MCP registries and GitHub for an existing server, like the community Alza MCP or Rohlík's official MCP. For shops, Apify Actors are useful for public catalog and price reads and for crawling a shop during "Explore" in 4.3. A hired Masumi agent can help with judgement on public data, such as comparing products or reading reviews. Neither is ever used for logged-in checkout or given the buyer profile. Review what you find (security reviewer from Phase 2) and wrap it, or build your own if nothing suitable exists. The choice and the reason for it are shown in the UI.

**E. Secrets broker and human approval.** Frankenstein cannot create accounts by itself (captchas, KYC, billing). It pauses and asks for credentials, stores them in the vault, and injects them at runtime. Phase 2 plans this; here it becomes essential.

**F. Risk tiers it cannot change.** Spending money (including Apify and Masumi spend), submitting orders, publishing a tool for sale, and touching personal data need human approval or hard limits. These rules live **outside** the code it can rewrite. Otherwise it could delete the guardrail that blocks it.

**G. Tests for things with no single right answer.** Use recorded HAR/VCR fixtures for repeatable tests, small labelled answer sets, an LLM-judge scoring guide run by a different model than the builder, and **dry runs** that go all the way through checkout but stop before the final "Dokončit objednávku" (complete order) button.

**H. A runtime for long-running services** (L4: AgentBazaar, plus the Masumi node and storefront, which are part of the base setup): Docker Compose or Fly.io services with health checks and a list of what is running. The Alza scenario doesn't need any new L4 services.

### 4.2 Hero scenario: "Buy me a USB-C cable on Alza, under 200 Kč, pickup at the nearest AlzaBox"

**Reality check (as of Oct 2026):**
- Alza has **no public consumer API or agentic checkout** (no ACP or UCP). It says it is "actively monitoring" agentic commerce. There is an unofficial community MCP server that reads the catalog by driving a real browser. Checkout therefore has to be done through **browser automation**.
- Czech agentic commerce is only starting. **Rohlík** runs an official MCP server (`https://mcp.rohlik.cz/mcp`, OAuth, cart management), and its own assistant Maia can pay with a saved card. **Seznam/Zboží.cz** is rolling out UCP-based agents to Shopify and then Shoptet shops, but the user still finishes payment with two clicks on the shop's site.
- Card payments in the EU go through **3-D Secure (PSD2 strong customer authentication)**. Approving the 3DS request on the user's phone is the natural human-approval step. Frankenstein should not try to get around it.

**Capability tree Frankenstein builds:**
```
buy_on_alza(query, max_price, delivery=AlzaBox)
├─ study: discover the community Alza MCP → security review → adopt for search (L1)
├─ alza_search / alza_product_detail (adopted or built, Playwright)       L1
├─ alza_login (Playwright + vault credentials, stored session cookies)    L1
├─ alza_cart_add / alza_cart_read                                         L1
├─ choose_product (compare price, rating, availability against limits)    L3
├─ alza_checkout skill: cart → AlzaBox selection → payment → summary      L2
│    └─ stops at summary page → shows screenshot + total → HUMAN APPROVAL
├─ pay_card: submit → 3DS approval on the user's phone → confirm          L2
└─ verify_order: read the confirmation email or order page → store order ID   L1
```

**Payment options, in order of preference:**
1. **Card with a hard limit outside the agent.** Use a virtual card with its own limit, such as a Revolut disposable card or a bank virtual card capped at a few hundred CZK, saved in the Alza account. 3DS approval on the phone is the final gate. The card issuer enforces the spending cap, not the agent.
2. **Bank transfer with a QR payment.** Choose "bankovní převod" (bank transfer) at checkout. Frankenstein **learns to read SPAYD** (the Czech QR Platba format: `SPD*1.0*ACC:...*AM:...*X-VS:...`), shows the parsed payment for approval, and either displays the QR for the user's banking app or, as an extra step, sends a payment order through the **Fio banka API**, which the user then confirms. This is a strong "learning" moment: a new data format plus a new bank API.
3. **Cash on pickup at an AlzaBox** where available. This is the zero-risk fallback: the order is real, and the payment happens at pickup.

**Hard rules (outside what the agent can rewrite):** limit of 200 Kč per order and 500 Kč per day. One order per demo. A screenshot of the summary page plus approval in the UI before submitting. The final click happens only after approval. Every browser action is logged with a screenshot. The item can be returned within the standard 14-day withdrawal period.

### 4.3 Any Czech shop: "Learn to shop on <url>"

Frankenstein should accept the request **"Nauč se nakupovat na `<any-shop>.cz`"** ("learn to shop on <any-shop>.cz"), learn that shop, and from then on buy there on request. It doesn't need to learn each of ~40,000 Czech e-shops from scratch. Most of them run on a small number of platforms and share the same checkout parts, so Frankenstein learns **each platform once**, then **each shop in minutes**.

**Why this is possible in the Czech market:**
- **Shoptet** runs about **45,000 Czech e-shops**, roughly a quarter to 40 % of Czech e-commerce, depending on the source. One learned Shoptet adapter covers most small and mid-size shops. Next come **Upgates, WooCommerce, Shopify, PrestaShop, Eshop-rychle, and Webnode**. The large shops (Alza, CZC, Datart, Notino, Mall, Rohlík, Kosik) are custom builds and each gets its own profile.
- Nearly every Czech shop publishes an **XML product feed for Heureka/Zboží.cz** (often at `/heureka.xml`, `/zbozi.xml`, or a feed URL linked from the shop). That gives a clean product catalog without scraping.
- Checkouts are built from the **same local parts**:
  - Delivery: Zásilkovna/Packeta widget, Balíkovna, PPL, DPD, GLS, Česká pošta, AlzaBox.
  - Payment gateways: Comgate, GoPay, ThePay, CSOB, Stripe, PayU.
  - Payment methods: card plus 3DS, Apple Pay or Google Pay, bank transfer with QR Platba (SPAYD), cash on delivery (dobírka).
  - Common steps: cookie consent banner, agreeing to the terms ("souhlas s obchodními podmínkami"), unticking the Heureka "Ověřeno zákazníky" review-email box.

  Each of these is **learned once and reused in every shop**.
- **Agent protocols are arriving.** Seznam is rolling out **UCP** for Shopify and then Shoptet shops, and Rohlík has an official **MCP**. Whenever a shop supports a protocol, Frankenstein uses it and skips the browser.

**Ways to connect to a shop, from most to least preferred.** The detector tries each one and stops at the first that works:

| # | How | Detection | Reliability |
|---|---|---|---|
| 1 | Agent protocol | `/.well-known/ucp` (UCP profile), ACP endpoint, official MCP (e.g. `mcp.rohlik.cz`) | Highest, no browser needed |
| 2 | Platform adapter | Fingerprint: HTML markers, cookies, script/CDN URLs, meta generator (Shoptet, Upgates, WooCommerce, Shopify, PrestaShop…) | High, shared by thousands of shops |
| 3 | Platform's public API | Shopify Storefront API, WooCommerce Store API (`/wp-json/wc/store`) | High where it's enabled |
| 4 | Feed + generic browser | Heureka/Zboží XML feed for the catalog, Playwright with the generic checkout state machine | Medium |
| 5 | Computer use | Claude computer use reading screenshots | Lowest; last resort and repair tool |

**Building blocks (L1/L2), each learned once:**
```
universal_checkout (state machine, L2)
  search → product → variant (size/colour) → cart → customer info
  → delivery → payment → summary ⟶ HUMAN APPROVAL ⟶ submit
  → payment gateway / 3DS → confirmation → verify_order

platform adapters:  shoptet · upgates · woocommerce · shopify · prestashop · eshop_rychle · (custom: alza, rohlik, …)
delivery skills:    packeta_widget · balikovna · ppl_parcelshop · dpd_pickup · alzabox · courier_address
gateway skills:     comgate · gopay · thepay · csob · stripe_checkout · payu
payment skills:     card_3ds · apple_google_pay (handed to the user) · spayd_qr_transfer (+ Fio API) · cash_on_delivery
common steps:       cookie_consent_cz · terms_checkbox · marketing_optout · guest_vs_account · email_verification
catalog:            heureka_feed_parser · zbozi_feed_parser · site_search
```

**A shop profile is the unit Frankenstein learns.** Learning a shop produces a small, versioned **ShopProfile** that sits on top of the platform adapter:

```yaml
shop: example-obchod.cz
platform: shoptet            # detected via fingerprint
access_tier: 2               # platform adapter
catalog: { feed: https://example-obchod.cz/heureka.xml, search: site }
delivery: [packeta_widget, ppl_parcelshop, courier_address]
payment:  { gateway: comgate, methods: [card_3ds, spayd_qr_transfer, cash_on_delivery] }
checkout: { guest: true, overrides: { terms_checkbox: "#termsAgreement" } }
tests:    { dry_run: pass, last_checked: 2026-10-06, success_rate: 0.95 }
```

**How Frankenstein learns a new shop** ("Nauč se nakupovat na example-obchod.cz"):
1. **Recognize:** check for agent protocols, then fingerprint the platform, then look for a product feed. If a platform adapter already exists, most of the work is already done.
2. **Explore:** browse the shop's homepage, search, one product page, the cart, and each checkout step. Compare every step with the generic checkout. Record anything that differs (custom fields, unknown delivery widget, unknown payment gateway).
3. **Fill gaps:** for each unknown part, run the normal Frankenstein loop (study, build, test, install). A new building block, like an unknown gateway, becomes reusable for every later shop.
4. **Dry run:** run the whole checkout up to the summary page with a cheap product. Check the price, delivery fee, and total against the cart.
5. **Install:** save the ShopProfile plus its tests to the registry. The shop shows up in the "Shops I can buy from" gallery with a reliability score.
6. **Buy on request:** "Kup na example-obchod.cz …" ("buy on example-obchod.cz …") runs on the profile. The hard limits, the approval screen, and 3DS still apply.
7. **Maintain:** nightly dry runs re-check every installed shop. When one breaks, a repair loop runs; computer use finds the changed step and an updated profile is installed.

**Buyer data and accounts:**
- A **buyer profile** in the vault holds name, delivery address, phone, email, and optional IČO/DIČ (company and VAT IDs) for company purchases. Only the fields a checkout asks for are filled in.
- Guest checkout is preferred. When an account is required, Frankenstein registers with a **dedicated agent mailbox** (IMAP or an agent email API). It reads verification and order-confirmation emails from there and keeps your personal inbox out of it.
- Marketing checkboxes are always unticked. Terms are accepted only as part of an approved order, and the terms URL is logged.

**Measuring coverage with a benchmark of Czech shops:** a list of ~20 shops chosen to span the platforms (≥5 Shoptet, 2 Upgates, 2 WooCommerce, 2 Shopify, 1 PrestaShop, plus Alza, CZC, Datart, Notino, Rohlík). A dashboard shows **dry-run success rate per platform** before and after learning. The headline numbers are "shops it can buy from" and "minutes to learn a new shop."

### 4.4 Steps

1. **Browser tools:** a Playwright sandbox with a persistent profile per site, screenshot streaming to the lab panel, and the Claude computer-use tool as a fallback when CSS selectors break.
2. **Study + adopt:** search MCP registries and GitHub, run a security review, then install or build. Shown live in the UI.
3. **Dry-run testing:** the tester writes tests that run the whole flow up to the summary page and check the cart, delivery, and total. Recorded fixtures make the tests repeatable offline.
4. **Approval UI:** a card showing the product, price, delivery, payment method, and a screenshot of the summary page, with **Approve / Reject** buttons, followed by a "check your phone for 3DS" state.
5. **SPAYD + Fio skill** (option 2), built live as a new capability.
6. **Remember and reuse:** a second request ("now a 2 m HDMI cable") runs on the installed skills with no rebuilding. Only the approval step is repeated.
7. **Generalize to other shops:** "Now buy it on Rohlík." Frankenstein finds Rohlík's official MCP, adopts it over OAuth, reuses `choose_product` and the approval/payment skills, and builds only the shop-specific parts. This shows that its capabilities **transfer between shops**.
8. **Shop detector + generic checkout:** checks for protocols, fingerprints the platform, and finds the product feed. Includes the generic checkout state machine and the ShopProfile format with its registry.
9. **First platform adapter, Shoptet** (largest coverage), plus building blocks for Packeta, Comgate, GoPay, cookie consent, and terms. Then add Upgates, WooCommerce, and Shopify as time allows.
10. **"Learn a shop" command** with a live lab view (recognize → explore → fill gaps → dry run → install) and a "Shops I can buy from" gallery.
11. **Czech shop benchmark** (~20 shops) with a success-rate dashboard and nightly dry runs that check for breakage.

### 4.5 Risks and backup options

| Risk | Mitigation |
|---|---|
| Alza blocks automated browsing (bot protection, captcha) | Use a real logged-in profile at a low request rate. Hand the captcha to a human. **Backup:** Rohlík's official MCP, or a sandbox **Shoptet/Shopify test store** you control, with **Comgate or GoPay test mode**. That gives the same flow with no blocking and a predictable demo |
| Alza's terms may prohibit automated access | Personal use, one order, a human approves the purchase. Mention it in the pitch and keep the backup store ready |
| Checkout page changes and breaks selectors | Fall back to computer use for the checkout. The health checks from Phase 2 re-test the skill before the demo |
| 3DS timeout on stage | Approve 3DS in advance by doing a test purchase earlier, keep the phone ready, or use cash-on-pickup |
| Agent buys the wrong thing | Hard price and quantity limits, the approval screen, and the 14-day return right |
| Random shop has an unknown widget or gateway | That's the point of the track: the gap loop builds it. Within the demo time limit, fall back to computer use for that one step and keep cash on delivery as the payment |
| Too many shops to get through in a hackathon | Cover platforms, not shops: Shoptet alone is ~45k shops and a quarter or more of Czech e-commerce. Demo on Shoptet plus one custom shop |
| Accounts need email or SMS verification | Dedicated agent mailbox for email. SMS codes are handed to a human |
| Guest checkout exposes personal data to shops | Buyer profile in the vault, only the fields a checkout asks for, marketing opt-outs always unticked, and a log of which data went to which shop |

### 4.6 Second thin slice (optional): learning the other tracks

- **AgentBazaar:** Frankenstein can already pay agents on Cardano through Masumi, but it has never paid on an EVM chain. It receives an `HTTP 402 Payment Required` response from an endpoint on Base Sepolia, reads the x402 docs, builds a `pay_x402` skill, asks for approval of a $0.01 spending cap, pays in USDC, and gets the data. Next time it pays automatically. It reuses its Masumi habits: a budget cap held by the harness and a check of the result before trusting it.
- **Dossier:** "Research Jane Doe for a sales call." It builds `github_profile`, `news_search`, and a `same_person?` skill tested against 3 labelled examples, then writes a short cited brief.

### 4.7 Demo script (≈5 min)

1. "Buy me a USB-C cable on Alza, under 200 Kč, pickup at the nearest AlzaBox." The capability tree appears with every node red (missing).
2. It finds the community Alza MCP and runs a security review on it (adopted, shown green). It builds login, cart, and checkout live, and the dry-run tests pass.
3. The summary page screenshot appears and you press **Approve**. 3DS approval on the phone, then the order number appears on screen.
4. "Now an HDMI cable." It answers almost instantly and reuses everything.
5. **The audience picks a shop.** Ask the room for any Czech e-shop URL, then say "Nauč se nakupovat na <url>". Frankenstein fingerprints it ("Shoptet, Packeta, Comgate: 90 % already known"), builds only the missing part, and passes a dry run up to the summary page within a couple of minutes.
6. Show the "Shops I can buy from" gallery and the benchmark dashboard. Finish on the line: "It learned a new shop in N minutes and can already buy from thousands of Shoptet stores."

### Sources

- [Masumi developer portal](https://www.masumi.network/dev) and [agents hub (llms.txt, OpenAPI specs, MCP servers, skills)](https://www.masumi.network/dev/agents)
- [Masumi Agentic Service API (MIP-003)](https://www.masumi.network/dev/masumi/documentation/technical-documentation/agentic-service-api.md)
- [Masumi x402 on Cardano](https://www.masumi.network/x402)
- [masumi-services-dev-quickstart (Docker Compose for the Registry + Payment Service)](https://github.com/masumi-network/masumi-services-dev-quickstart)
- [masumi-mcp-server](https://github.com/masumi-network/masumi-mcp-server), [masumi-skills](https://github.com/masumi-network/masumi-skills), [pip-masumi](https://github.com/masumi-network/pip-masumi)
- [Sokosumi marketplace](https://sokosumi.com)
- [Apify MCP server](https://mcp.apify.com)
- [Lupa.cz – Agentní nakupování přichází (Alza "actively monitoring")](https://www.lupa.cz/clanky/agentni-nakupovani-prichazi-cesko-by-mohlo-byt-mezi-prvnimi-staty-kde-se-objevi/)
- [Lupa.cz – Seznam brings agents to thousands of e-shops (UCP, Shopify → Shoptet, community Alza MCP)](https://www.lupa.cz/clanky/umela-inteligence-se-chysta-nakupovat-za-cechy-seznam-chce-do-konce-roku-dostat-agenty-k-tisicum-e-shopu/)
- [Lupa.cz – Rohlík integrates payments into its AI assistant (May 2026)](https://www.lupa.cz/aktuality/rohlik-integruje-platby-do-ai-asistenta-umoznuje-v-chatu-odbavit-nakup-az-po-platbu/)
- [Rohlík MCP docs](https://www.rohlik.cz/en-CZ/mcp-docs)
- [Forbes.cz – Shoptet merchants reach 59 bn CZK](https://forbes.cz/turbulencim-navzdory-firmy-na-shoptetu-doletely-k-59-miliardam-korun/) and [Seznam Zprávy – Shoptet platform](https://www.seznamzpravy.cz/clanek/ekonomika-firmy-podvozek-na-kterem-jedou-tisice-e-shopu-ma-novy-plan-podpori-trziste-306528)
- [UCP specification – business profile at /.well-known/ucp](https://ucp.dev/specification/overview/)
- [Kosmoweb – Czech payment gateways compared 2026 (Comgate, GoPay, Stripe)](https://kosmoweb.cz/en/blog/czech-payment-gateways-compared-2026/)
