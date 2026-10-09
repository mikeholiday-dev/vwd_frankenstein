// Owner: D. The one operator page. Live state (runs, lab, approvals, budget, timeline) is derived
// event by event from the shared log over SSE, so it never needs a server round trip; the overview
// tiles, charts and the outputs list come from /api/summary (scripts.evidence.summarize under the
// hood), refetched on the events that change it, so "what happened" is computed the same way the
// submission's evidence report computes it.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const json = (v) => esc(typeof v === "string" ? v : JSON.stringify(v, null, 2));
const clock = (ts) => new Date(ts).toLocaleTimeString([], { hour12: false });
const chip = (text, cls = "") => `<span class="chip ${cls}">${esc(text)}</span>`;
const tags = (xs, cls = "") => (xs || []).map((x) => `<span class="tag ${cls}">${esc(x)}</span>`).join("");
const none = "<span class='muted'>none</span>";
const denied = (xs) => (xs?.length ? ` <span class="refused">egress refused:</span> ${tags(xs, "deny")}` : "");
const refused = (reason) => String(reason || "").startsWith("refused:");
// own | "regression: name@vN tests" | retest (gate.run_tests)
const suite = (x) => (!x || x === "own" ? "" : x === "retest" ? chip("retest", "info") : chip(x.replace(/ tests$/, ""), "info"));

const STATUS_TONE = { ok: "ok", failed: "bad", killed: "bad", capped: "warn", unfinished: "info", running: "info" };
const STATUS_LABEL = { ok: "ok", failed: "failed", killed: "killed", capped: "capped", unfinished: "running", running: "running" };
const CRED_LABELS = { telegram: "Telegram bot token", discord: "Discord bot token", elevenlabs: "ElevenLabs API key", apify: "Apify API token" };
const OPERATOR = new Set(["kill", "rollback", "quarantine"]);  // written by an operator surface, not by a run

// ---- state, all derived from the event log --------------------------------------------------
const S = {
  runs: new Map(),       // run_id -> run
  order: [],             // run ids, oldest first
  selected: null,        // null = follow the latest run
  pending: new Map(),    // approval request id -> { data, run_id, session, ts, fake }
  requests: new Map(),   // approval request id -> { run_id, ref }
  reasons: new Map(),    // approval request id -> reason typed so far (survives a re-render)
  operator: [],          // kill, rollback, quarantine, and decisions on unknown requests
  versions: new Map(),   // capability name -> Set of versions seen installed in the log
  registry: [],
  reglog: [],            // git log of registry/: who wrote each install
  config: null,
  summary: null,
  creds: {},
  lastId: -1,
  killedAt: null,
  open: new Map(),       // <details> the operator opened or closed, by data-key
  labNew: 0,             // lab activity of the selected run that arrived while the Lab was folded
  openRuns: new Set(),   // expanded rows of the outputs list
  openCreds: new Set(),
  dirty: new Set(["all"]),
};

function run(e) {
  let r = S.runs.get(e.run_id);
  if (!r) {
    r = { id: e.run_id, session: e.session, task: "", plan: "", answer: null, status: "running", fake: false, started: e.ts,
          budget: null, caps: [], errors: [], lab: [], capByRef: new Map(), events: [] };
    S.runs.set(e.run_id, r);
    S.order.push(e.run_id);
    S.dirty.add("runs");
  }
  return r;
}

function cap(r, ref) {
  let c = r.capByRef.get(ref);
  if (!c) {
    c = { kind: "cap", ref, steps: [], files: new Map(), built: false, state: "used", tone: "" };
    r.capByRef.set(ref, c);
    r.lab.push(c);
  }
  return c;
}

function apply(e) {
  if (e.id <= S.lastId) return;  // EventSource reconnects replay from 0
  S.lastId = e.id;
  const d = e.data;
  S.dirty.add("timeline");

  // approval_decided is written by whichever approver (this page, a chat bot, the CLI) under its
  // own session: file it under the run that asked, via the request id approval_requested recorded.
  if (e.type === "approval_decided") {
    const req = S.requests.get(d.request_id);
    S.pending.delete(d.request_id);
    S.reasons.delete(d.request_id);
    S.dirty.add("approvals");
    if (req) {
      const r = S.runs.get(req.run_id);
      const c = cap(r, req.ref);
      c.steps.push({ type: "decision", ts: e.ts, d });
      c.state = d.approved ? "approved" : "rejected"; c.tone = d.approved ? "green" : "red";
      r.events.push(e);
      S.dirty.add("lab");
      labActivity(r, e.type);
    } else S.operator.push(e);
    return;
  }
  if (OPERATOR.has(e.type)) {
    if (e.type === "kill") {
      S.killedAt = e.ts;
      S.pending.clear();
      S.dirty.add("approvals").add("header");
      refreshSummarySoon();
    } else loadRegistry();
    S.operator.push(e);
    return;
  }

  const r = run(e);
  r.events.push(e);
  if (d.fake) r.fake = true;
  S.dirty.add("header").add("lab");
  switch (e.type) {
    case "run_started": r.task = d.task; r.started = e.ts; r.auth = d.auth; r.models = d.models; r.registryAtStart = d.registry; r.attached = d.attached || []; S.dirty.add("runs"); refreshSummarySoon(); break;
    case "plan": r.plan = d.text; break;
    case "gap": r.lab.push({ kind: "gap", ts: e.ts, d }); break;
    case "study": r.lab.push({ kind: "study", ts: e.ts, d }); break;
    case "build": {
      const c = cap(r, d.ref);
      c.built = true; c.state = "building"; c.tone = "";
      c.steps.push({ type: "build", ts: e.ts, d });
      for (const [name, text] of Object.entries(d.contents || {})) c.files.set(name, { text, attempt: d.attempt, role: d.role });
      break;
    }
    case "test_run": {
      const c = cap(r, d.ref);
      c.steps.push({ type: "test", ts: e.ts, d, id: e.id });
      c.state = d.passed ? "tests pass" : "tests fail"; c.tone = d.passed ? "green" : "red";
      break;
    }
    case "approval_requested": {
      const c = cap(r, d.ref);
      c.steps.push({ type: "request", ts: e.ts, d });
      c.state = "awaiting approval"; c.tone = "wait";
      if (d.code && !c.files.size) c.files.set("capability.py", { text: d.code });
      S.requests.set(d.id, { run_id: e.run_id, ref: d.ref });
      S.pending.set(d.id, { data: d, run_id: e.run_id, session: e.session, ts: e.ts, fake: !!d.fake });
      S.dirty.add("approvals");
      break;
    }
    case "install": {
      const c = cap(r, d.ref);
      c.steps.push({ type: "install", ts: e.ts, d });
      c.state = d.installed ? "installed" : "not installed"; c.tone = d.installed ? "green" : "red";
      if (d.installed) {
        const [name, v] = d.ref.split("@v");
        if (!S.versions.has(name)) S.versions.set(name, new Set());
        S.versions.get(name).add(Number(v));
      }
      loadRegistry();
      refreshSummarySoon();
      break;
    }
    case "call": cap(r, d.ref).steps.push({ type: "call", ts: e.ts, d, id: e.id }); break;
    case "answer": r.answer = d; refreshSummarySoon(); break;
    case "budget": r.budget = d; break;
    case "cap_hit": r.caps.push(d); break;
    case "error": r.errors.push(d.message); break;
    case "run_finished":
      r.status = d.status;
      if (d.budget) r.budget = d.budget;
      for (const [id, p] of S.pending) if (p.run_id === r.id) S.pending.delete(id);
      S.dirty.add("approvals").add("runs");
      refreshSummarySoon();
      break;
  }
  labActivity(r, e.type);
}

const current = () => S.runs.get(S.selected ?? S.order[S.order.length - 1]);

// Counts what the folded Lab is hiding, so its header can say something new came in.
const LAB_TYPES = new Set(["gap", "study", "build", "test_run", "approval_requested", "approval_decided", "install", "call"]);
function labActivity(r, type) {
  if (!LAB_TYPES.has(type) || $("labSection").open || r !== current()) return;
  S.labNew++;
  S.dirty.add("labBadge");
}

// ---- small rendering helpers ----------------------------------------------------------------

function details(key, summary, body, openByDefault = false) {
  const open = S.open.has(key) ? S.open.get(key) : openByDefault;
  return `<details class="more" data-key="${esc(key)}"${open ? " open" : ""}><summary>${summary}</summary>${body}</details>`;
}

function ioTable(obj) {
  return Object.entries(obj || {}).map(([k, v]) => `<span class="tag">${esc(k)}: ${esc(v)}</span>`).join("") || none;
}

function banner(cls, text) {
  const slot = document.querySelector("#approvalModal[open] .modal-error");  // the modal covers the page's banners
  if (slot && cls === "bad") { slot.textContent = text; slot.hidden = false; }
  const el = document.createElement("div");
  el.className = `banner ${cls}`;
  el.textContent = text;
  $("banners").hidden = false;
  $("banners").prepend(el);
  setTimeout(() => { el.remove(); $("banners").hidden = !$("banners").children.length; }, 15000);
}

// ---- header and the selected run --------------------------------------------------------------

function renderHeader() {
  const c = S.config;
  if (c) {
    $("chips").innerHTML = (c.mode === "demo" ? chip("demo mode", "ok") : chip("dev mode"))
      + (c.fakes.length ? " " + chip("fake " + c.fakes.join(" + "), "warn") : "") + " " + chip("auth: " + c.auth)
      + (c.models === "cheap" ? " " + chip("models: cheap", "warn") : "");
    $("reset").hidden = c.mode !== "dev";
  }
  const r = current();
  $("runStatus").className = "chip " + (r ? STATUS_TONE[r.status] || "" : "");
  $("runStatus").textContent = r ? r.status : "idle";

  const banners = [];
  if (r?.fake) banners.push(`<div class="banner warn">Scripted fake run (scripts/fake_run.py). Nothing here was built, tested or installed for real.</div>`);
  if (r && r.status === "killed") banners.push(`<div class="banner bad">Run stopped by the kill switch.</div>`);
  else if (S.killedAt && r?.status === "running" && S.killedAt > r.started) banners.push(`<div class="banner bad">Kill sent at ${clock(S.killedAt)}. Waiting for the run to stop.</div>`);
  for (const h of r?.caps || []) banners.push(`<div class="banner bad">Cap hit: ${esc(h.limit)} reached ${esc(h.value)} (max ${esc(h.max)}). The harness stopped the work.</div>`);
  for (const m of r?.errors || []) banners.push(`<div class="banner warn">${esc(m)}</div>`);
  $("runBanners").hidden = !banners.length;
  $("runBanners").innerHTML = banners.join("");

  if (r) {
    const started = r.registryAtStart ? `<div class="muted">Registry at start: ${r.registryAtStart.length ? tags(r.registryAtStart) : "empty"}</div>` : "";
    const a = r.answer, unverified = a && a.provenance && a.provenance !== "ok";
    const answer = a ? `<div class="answer${unverified ? " unverified" : ""}"><div>${esc(a.text)}</div>
      <div class="muted">Cites ${a.call_ids?.length ? tags(a.call_ids) : "no calls"}${a.provenance ? " " + chip(unverified ? "provenance: " + a.provenance : "provenance checked", unverified ? "bad" : "ok") : ""}</div>
      ${a.reused?.length ? `<div class="muted">Reused ${tags(a.reused)}</div>` : ""}${a.built?.length ? `<div class="muted">Built ${tags(a.built, "add")}</div>` : ""}</div>` : "";
    $("task").className = "";
    $("task").innerHTML = `<div class="task">${esc(r.task || "(no task: capability installed or called directly)")}</div>`
      + `<div class="muted">Session ${esc(r.session)} · run ${esc(r.id)} · started ${clock(r.started)}</div>${started}${r.attached?.length ? `<div class="muted">Attached: ${tags(r.attached)}</div>` : ""}`
      + (r.models ? `<div class="muted">Models: ${ioTable(r.models)}</div>` : "")
      + (r.plan ? `<div class="plan"><span class="muted">Plan:</span> ${esc(r.plan)}</div>` : "") + answer;
  }
  renderMeters(r);
}

// Meters follow the selected run, the same as the lab: a budget event from another run in
// parallel never moves them, and a cap hit in an old run doesn't keep them red.
function renderMeters(r) {
  const lim = r?.budget?.limits || S.config?.limits;
  if (!lim) return;
  const b = r?.budget || { usd: 0, planner_turns: 0, gap_turns: 0, gaps: 0, minutes: 0 };
  const hit = new Set((r?.caps || []).map((h) => h.limit));
  const meter = (key, label, value, shown, unit) => {
    const pct = Math.min(100, (value / lim[key]) * 100);
    const cls = hit.has(key) || value > lim[key] ? "hit" : pct >= 80 ? "warn" : "";
    return `<div class="meter ${cls}"><div class="label"><span>${label}</span><span>${Math.round(pct)}%</span></div>
      <div class="value">${shown} <small>/ ${unit}</small></div><div class="bar"><i style="width:${pct}%"></i></div></div>`;
  };
  // Enforced caps are planner_turns (the planner's own turns) and turns_per_gap (shared by one gap's
  // builder/tester/repairs). A log recorded before that split has neither in `lim`: skip the meter instead of showing NaN%.
  const have = (key) => lim[key] != null;
  $("meters").innerHTML = meter("usd", "Spend (API-equivalent)", b.usd, "$" + Number(b.usd).toFixed(2), "$" + lim.usd.toFixed(2))
    + (have("planner_turns") ? meter("planner_turns", "Planner turns", b.planner_turns ?? 0, b.planner_turns ?? 0, lim.planner_turns) : "")
    + (have("turns_per_gap") ? meter("turns_per_gap", "Builder turns (this gap)", b.gap_turns ?? 0, b.gap_turns ?? 0, lim.turns_per_gap) : "")
    + meter("gaps", "Gaps this run", b.gaps, b.gaps, lim.gaps)
    + meter("minutes", "Run time", b.minutes, Number(b.minutes).toFixed(1), lim.minutes + " min");
  $("caps").innerHTML = `· also capped: ${esc(lim.repairs_per_gap)} repairs per gap${hit.has("repairs_per_gap") ? " " + chip("hit", "bad") : ""}, ${esc(lim.sandbox_seconds)} s per sandbox run`;
}

function renderRunSelect() {
  const opts = [`<option value="">Latest (follow)</option>`];
  for (const id of [...S.order].reverse()) {
    const r = S.runs.get(id);
    opts.push(`<option value="${esc(id)}"${S.selected === id ? " selected" : ""}>${esc(r.session)} · ${clock(r.started)} · ${esc(r.status)} · ${esc((r.task || "direct install").slice(0, 50))}</option>`);
  }
  $("runSelect").innerHTML = opts.join("");
}

function select(runId) {
  S.selected = runId || null;
  S.labNew = 0;  // a different run's lab: nothing in it is "new" to the operator
  S.dirty.add("all");
  schedule();
}

// ---- lab: each gap, build, test run, approval, install and call of the selected run ------------

function renderStep(s, c) {
  const d = s.d;
  const t = `<span class="muted">${clock(s.ts)}</span>`;
  switch (s.type) {
    case "build":
      return `<li>${t} <b>${esc(d.role)}</b> · attempt ${esc(d.attempt)}${d.files ? ` · <span class="muted">${esc(d.files.join(", "))}</span>` : ""}</li>`;
    case "test": {
      const last = c.steps.filter((x) => x.type === "test").pop() === s;
      const meta = [d.duration_s != null ? d.duration_s.toFixed(1) + " s" : "", d.sandbox_run_id ? "sandbox run " + d.sandbox_run_id : ""].filter(Boolean).join(" · ");
      return `<li class="${d.passed ? "pass" : "fail"}">${t} ${suite(d.suite)} ${chip(d.passed ? "tests passed" : "tests failed", d.passed ? "ok" : "bad")} <span class="muted">${esc(meta)}</span>${denied(d.egress_denied)}
        ${details(`test-${s.id}`, "test output", `<pre>${esc(d.output)}</pre>`, last || !d.passed)}</li>`;
    }
    case "request": return `<li class="wait">${t} approval requested from the operator</li>`;
    case "decision":
      return `<li class="${d.approved ? "pass" : "fail"}">${t} ${chip(d.approved ? "approved" : "rejected", d.approved ? "ok" : "bad")} by ${esc(d.by)}${d.reason ? ` · “${esc(d.reason)}”` : ""}</li>`;
    case "install":
      return `<li class="${d.installed ? "pass" : "fail"}">${t} ${chip(d.installed ? "installed" : refused(d.reason) ? "refused by the gate" : "not installed", d.installed ? "ok" : "bad")}
        <span class="${refused(d.reason) ? "refused" : "muted"}">${esc(d.reason)}</span></li>`;
    case "call": {
      const body = `<pre>${json({ args: d.args, output: d.output, error: d.error || undefined })}</pre>`;
      return `<li class="${d.ok ? "pass" : "fail"}">${t} call <span class="tag">${esc(d.call_id)}</span> ${chip(d.ok ? "ok" : "error", d.ok ? "ok" : "bad")}
        ${d.duration_s != null ? `<span class="muted">${d.duration_s.toFixed(2)} s</span>` : ""}${d.uses?.length ? ` <span class="muted">uses</span> ${tags(d.uses)}` : ""}${denied(d.egress_denied)}${details(`call-${s.id}`, "arguments and output", body)}</li>`;
    }
  }
  return "";
}

function renderLab() {
  const r = current();
  if (!r || !r.lab.length) { $("lab").className = "muted"; $("lab").textContent = "Gaps, builds and test runs of the selected run appear here."; $("labCount").textContent = 0; return; }
  $("lab").className = "";
  $("labCount").textContent = r.lab.filter((x) => x.kind === "cap").length;
  $("lab").innerHTML = [...r.lab].reverse().map((x) => {  // newest first, like the timeline
    if (x.kind === "gap") {
      const d = x.d;
      return `<div class="item gap"><div class="head">${chip("gap", "info")} <span>${esc(d.gap)}</span> <span class="muted">${clock(x.ts)}</span></div>
        <dl class="kv"><dt>Why</dt><dd>${esc(d.why)}</dd><dt>Needs</dt><dd>${ioTable(d.inputs)}</dd><dt>Returns</dt><dd>${ioTable(d.outputs)}</dd>
        <dt>Kind</dt><dd>${esc(d.kind)}</dd>${d.upgrade ? `<dt>Upgrade of</dt><dd><span class="tag">${esc(d.upgrade)}</span></dd>` : ""}${d.registry_search ? `<dt>Registry search</dt><dd>${esc(d.registry_search)}</dd>` : ""}</dl></div>`;
    }
    if (x.kind === "study") {
      const d = x.d, failed = d.ok === false;
      return `<div class="item"><div class="head">${chip("study", failed ? "bad" : "")} <span>${esc(d.query)}</span> <span class="muted">${clock(x.ts)}</span></div>
        ${d.url ? `<div class="muted mono">${esc(d.url)}${d.chars != null ? ` · ${esc(d.chars)} chars` : ""}${failed ? " · failed" : ""}</div>` : ""}</div>`;
    }
    const tone = { green: "ok", red: "bad", wait: "warn" }[x.tone] || "";
    const files = [...x.files].map(([name, f]) =>
      details(`file-${r.id}-${x.ref}-${name}`, `<span class="mono">${esc(name)}</span>${f.attempt ? ` <span class="muted">from ${esc(f.role)}, attempt ${esc(f.attempt)}</span>` : ""}`, `<pre>${esc(f.text)}</pre>`)).join("");
    return `<div class="item cap ${x.tone}"><div class="head"><b>${esc(x.ref)}</b> ${chip(x.built ? x.state : "reused", x.built ? tone : "info")}</div>
      <ul class="steps">${[...x.steps].reverse().map((s) => renderStep(s, x)).join("")}</ul>${files}</div>`;
  }).join("");
}

// ---- one line per event: the timeline and each output's expanded log ----------------------------

function line(e) {
  const d = e.data;
  switch (e.type) {
    case "run_started": return `<b>${esc(d.task)}</b>`;
    case "plan": return esc(d.text);
    case "gap": return `${esc(d.gap)} <span class="muted">${esc(d.why)}</span>`;
    case "study": return esc(d.query);
    case "build": return `${esc(d.ref)} · ${esc(d.role)} attempt ${esc(d.attempt)}`;
    case "test_run": return `${chip(d.passed ? "pass" : "fail", d.passed ? "ok" : "bad")} ${esc(d.ref)} ${suite(d.suite)}${denied(d.egress_denied)}`;
    case "approval_requested": return `${esc(d.ref)} waits for the operator`;
    case "approval_decided": return `${chip(d.approved ? "approved" : "rejected", d.approved ? "ok" : "bad")} by ${esc(d.by)}${d.reason ? ` · “${esc(d.reason)}”` : ""}`;
    case "install": return `${esc(d.ref)} ${d.installed ? "installed" : "not installed"} <span class="${refused(d.reason) ? "refused" : "muted"}">${esc(d.reason)}</span>`;
    case "call": return `${esc(d.ref)} ${chip(d.ok ? "ok" : "error", d.ok ? "ok" : "bad")} <span class="tag">${esc(d.call_id)}</span>${denied(d.egress_denied)}`;
    case "answer": return esc(d.text);
    case "budget": return `<span class="muted">$${esc(d.usd)} · ${esc(d.turns)} turns · ${esc(d.gaps)} gaps · ${esc(d.minutes)} min</span>`;
    case "cap_hit": return `${chip("cap hit", "bad")} ${esc(d.limit)} = ${esc(d.value)} (max ${esc(d.max)})`;
    case "rollback": return `${esc(d.name)} rolled back to v${esc(d.version)} by ${esc(d.by)}`;
    case "quarantine": return `${esc(d.name)} quarantined by ${esc(d.by)}`;
    case "kill": return `${chip("kill", "bad")} by ${esc(d.by)}`;
    case "error": return `<span style="color:var(--bad)">${esc(d.message)}</span>`;
    case "run_finished": return chip(d.status, STATUS_TONE[d.status] || "");
    default: return `<span class="mono">${json(d)}</span>`;
  }
}

const evRow = (e) => `<div class="ev"><time>${clock(e.ts)}</time><span class="kind">${esc(e.type)}</span><span>${line(e)}</span></div>`;

function renderTimeline() {
  const r = current();
  const all = (r ? r.events : []).concat(S.operator).sort((a, b) => b.id - a.id);
  const evs = all.filter((e, i) => !(e.type === "budget" && all[i - 1]?.type === "budget"));  // a run of budget ticks shows its latest
  $("evCount").textContent = evs.length;
  $("timeline").innerHTML = evs.slice(0, 400).map(evRow).join("") || "<span class='muted'>No events yet.</span>";
}

function renderRunLog(runId) {
  const evs = (S.runs.get(runId)?.events || []).filter((e) => e.type !== "budget");  // budget: the meters show it live
  if (!evs.length) return `<div class="run-log"><div class="empty">No detail recorded for this run yet.</div></div>`;
  return `<div class="run-log">${[...evs].reverse().map(evRow).join("")}</div>`;  // newest first, like the timeline
}

function refreshOpenRunLogs() {
  for (const id of S.openRuns) {
    const el = document.querySelector(`details.run-item[data-run="${CSS.escape(id)}"] .run-log`);
    if (el) el.outerHTML = renderRunLog(id);
  }
}

// ---- approval cards: manifest, permissions diff, harness test log and code (plan §6) ------------

function permRows(m, prev, diff) {
  const p = m.permissions || {}, old = prev?.permissions || {};
  const list = (key) => {
    const added = new Set(diff?.added?.[key] || []), removed = diff?.removed?.[key] || [];
    const now = (p[key] || []).map((x) => `<span class="tag ${added.has(x) ? "add" : ""}">${added.has(x) ? "+ " : ""}${esc(x)}</span>`);
    const gone = removed.map((x) => `<span class="tag del">− ${esc(x)}</span>`);
    return now.concat(gone).join("") || none;
  };
  const fsChanged = prev && (old.filesystem || "none") !== (p.filesystem || "none");
  const fs = fsChanged ? `<span class="tag del">− ${esc(old.filesystem || "none")}</span><span class="tag add">+ ${esc(p.filesystem)}</span>` : `<span class="tag">${esc(p.filesystem || "none")}</span>`;
  return `<dt>Network</dt><dd>${list("network")}</dd><dt>Filesystem</dt><dd>${fs}</dd><dt>Secrets</dt><dd>${list("secrets")}</dd>`;
}

// One request at a time, oldest first, in a modal dialog: while an install waits for a decision the
// rest of the page is inert and Escape doesn't close it, so the operator has to approve, reject or
// pull the kill switch. The card is only rebuilt when the request shown changes, so a reason being
// typed or an opened <details> survives other events arriving.
function renderApprovals() {
  const dlg = $("approvalModal");
  const [first] = S.pending.values();
  if (!first) {
    if (dlg.open) dlg.close();
    dlg.innerHTML = "";
    delete dlg.dataset.id;
    return;
  }
  if (dlg.dataset.id !== first.data.id) {
    dlg.innerHTML = approvalCard(first);
    dlg.dataset.id = first.data.id;
  }
  $("apprWaiting").textContent = S.pending.size === 1 ? "1 install waiting" : `1 of ${S.pending.size} installs waiting`;
  if (!dlg.open) dlg.showModal();
}

function approvalCard({ data: d, session, ts, fake }) {
  const m = d.manifest || {}, prev = d.previous, rep = d.test_report || {};
  const changes = Object.keys(d.permissions_diff?.added || {}).length + Object.keys(d.permissions_diff?.removed || {}).length;
  const headline = prev
    ? `${chip("upgrade from v" + prev.version, "info")} ${changes ? chip("permissions change", "warn") : chip("same permissions", "ok")}`
    : `${chip("new capability", "info")} ${changes ? chip("asks for permissions", "warn") : chip("no permissions", "ok")}`;
  return `<div class="modal-head"><h2 id="approvalTitle">Approval needed</h2><span id="apprWaiting" class="count"></span></div>
    <div class="approval"><div class="head"><b class="mono">${esc(d.ref)}</b> ${headline} ${fake ? chip("fake", "warn") : ""}</div>
      <div class="desc">${esc(m.description)}</div>
      <div class="muted small">Session ${esc(session)} · requested ${clock(ts)}</div>
      <dl class="kv">${permRows(m, prev, d.permissions_diff)}
        <dt>Input</dt><dd>${ioTable(m.interface?.input)}</dd><dt>Output</dt><dd>${ioTable(m.interface?.output)}</dd>
        <dt>Dependencies</dt><dd>${tags(m.dependencies) || none}</dd>
        <dt>Tests</dt><dd>${chip(rep.passed ? "passed" : "failed", rep.passed ? "ok" : "bad")} <span class="muted">run by the harness${rep.sandbox_run_id ? ", sandbox run " + esc(rep.sandbox_run_id) : ""}</span></dd></dl>
      ${details(`ap-test-${d.id}`, "test output", `<pre>${esc(rep.output)}</pre>`, !rep.passed)}
      ${details(`ap-code-${d.id}`, "code", `<pre>${esc(d.code)}</pre>`)}
      ${details(`ap-manifest-${d.id}`, "full manifest", `<pre>${json(m)}</pre>`)}
    </div>
    <div class="banner bad modal-error" hidden></div>
    <div class="actions"><input type="text" data-reason="${esc(d.id)}" value="${esc(S.reasons.get(d.id) || "")}" placeholder="Reason (optional)" maxlength="200" aria-label="Reason" autofocus>
      <button class="ok" data-act="approve" data-id="${esc(d.id)}"${rep.passed ? "" : " disabled title='Tests failed'"}>Approve</button>
      <button class="danger" data-act="reject" data-id="${esc(d.id)}">Reject</button>
      <button class="danger" data-act="kill" title="Stop every run instead of deciding">Kill switch</button></div>`;
}

// ---- registry and its git history -------------------------------------------------------------

// Versions you can switch to: every one below the active, plus any installed later (seen in the log),
// so after rolling v2 back to v1 you can still go forward to v2.
function otherVersions(m) {
  const seen = new Set(S.versions.get(m.name) || []);
  for (let v = 1; v < m.version; v++) seen.add(v);
  seen.delete(m.version);
  return [...seen].sort((a, b) => b - a);
}

function renderRegistry() {
  $("regCount").textContent = S.registry.length;
  if (!S.registry.length) { $("registry").innerHTML = "<span class='muted'>Empty. Only the agent adds capabilities, through the install gate.</span>"; return; }
  $("registry").innerHTML = `<table class="reg-table"><tr><th>Capability</th><th>Reaches</th></tr>` + S.registry.map((e) => {
    const m = e.manifest, q = e.status === "quarantined", others = otherVersions(m);
    const rollback = others.length
      ? `<select data-rb="${esc(m.name)}" aria-label="Version to roll back to">${others.map((v) => `<option value="${v}">v${v}</option>`).join("")}</select><button data-act="rollback" data-name="${esc(m.name)}">Roll back</button>`
      : "";
    return `<tr class="${q ? "quarantined" : ""}"><td><span class="name">${esc(m.name)}@v${esc(m.version)}</span> ${q ? chip("quarantined", "bad") : chip("active", "ok")}
        <div class="muted small">${esc(m.description)}</div>${m.uses?.length ? `<div class="muted small">composes ${tags(m.uses)}</div>` : ""}<div class="muted small">${e.installed_at ? "installed " + esc(e.installed_at.replace("T", " ").slice(0, 16)) : ""}</div>
        ${rollback || !q ? `<div class="actions">${rollback}${q ? "" : `<button class="danger" data-act="quarantine" data-name="${esc(m.name)}">Quarantine</button>`}</div>` : ""}</td>
      <td>${tags(m.permissions?.network) || "<span class='muted'>no network</span>"}</td></tr>`;
  }).join("") + "</table>";
}

function renderRegistryLog() {
  $("logCount").textContent = S.reglog.length;
  if (!S.reglog.length) { $("reglog").innerHTML = "<span class='muted'>No git history (fake registry, or none yet).</span>"; return; }
  const who = { "frankenstein-agent": "info", "frankenstein-operator": "warn" };
  $("reglog").innerHTML = `<table class="reg-table">${S.reglog.map((c) => `<tr><td class="mono">${esc(c.sha)}</td><td>${chip(c.author, who[c.author] || "")}</td>
    <td>${esc(c.subject)}${c.refs ? ` ${tags(c.refs.split(", ").filter((x) => x.startsWith("tag: ")).map((x) => x.slice(5)))}` : ""}
    <div class="muted small">${esc(c.date.replace("T", " ").slice(0, 19))}</div></td></tr>`).join("")}</table>`;
}

// ---- overview: stat tiles, runs by status, spend per run (from /api/summary) ---------------------

function renderTiles() {
  const s = S.summary;
  if (!s) return;
  const active = s.by_status.unfinished || 0;
  const tile = (label, value, small = "") => `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}${small ? ` <small>${esc(small)}</small>` : ""}</div></div>`;
  $("tiles").innerHTML = tile("Runs", s.total_runs)
    + tile("Active now", active)
    + tile("Success rate", s.success_rate == null ? "—" : `${s.success_rate}%`)
    + tile("Spend (API-equivalent)", `$${s.total_usd.toFixed(2)}`);
}

// One reserved status color per bar, the label *is* the legend.
function renderStatusChart() {
  const s = S.summary;
  const el = $("statusChart");
  if (!s || !s.total_runs) { el.innerHTML = `<div class="muted small">No runs yet.</div>`; return; }
  const max = Math.max(...Object.values(s.by_status));
  const order = ["ok", "unfinished", "capped", "failed", "killed"];
  el.innerHTML = order.filter((k) => s.by_status[k]).map((k) => {
    const n = s.by_status[k];
    return `<div class="status-row ${STATUS_TONE[k]}"><span>${esc(STATUS_LABEL[k])}</span>
      <span class="swatch-bar"><i style="width:${Math.round((n / max) * 100)}%"></i></span>
      <span class="n">${n}</span></div>`;
  }).join("");
}

// One sequential hue, thin line, rounded cap, SVG <title> for the tooltip. Drawn in real pixels (not a
// stretched viewBox) so axis text stays crisp; value labels are thinned so they never collide.
function renderSpendChart() {
  const el = $("spendChart");
  const runs = (S.summary?.runs || []).filter((r) => r.usd > 0).slice(0, 20).reverse();
  if (runs.length < 2) { el.innerHTML = `<div class="empty">Not enough runs yet for a trend.</div>`; return; }
  const w = Math.max(el.clientWidth || 560, 240), h = 180;
  const m = { l: 58, r: 22, t: 20, b: 40 };
  const pw = w - m.l - m.r, ph = h - m.t - m.b;
  const max = Math.max(...runs.map((r) => r.usd), 0.01);
  const rough = max / 3, mag = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((v) => v >= rough);
  const top = Math.ceil(max / step) * step;
  const dp = step < 0.01 ? 3 : 2;
  const usd = (v, d = dp) => `$${v.toFixed(d)}`;
  const x = (i) => m.l + (i * pw) / (runs.length - 1);
  const y = (v) => m.t + ph - (v / top) * ph;
  const pts = runs.map((r, i) => [x(i), y(r.usd)]);
  const pline = pts.map((p) => p.join(",")).join(" ");
  const area = `${m.l},${m.t + ph} ${pline} ${m.l + pw},${m.t + ph}`;
  const yTicks = Array.from({ length: Math.round(top / step) + 1 }, (_, k) => k * step);
  const grid = yTicks.map((v) => `<line class="${v ? "grid" : "baseline"}" x1="${m.l}" y1="${y(v)}" x2="${m.l + pw}" y2="${y(v)}"/>
    <text class="tick" x="${m.l - 6}" y="${y(v)}" text-anchor="end" dominant-baseline="middle">${usd(v)}</text>`).join("");
  // Label every k-th point (plus the last and the peak) so ~46px separate neighbouring labels.
  const every = Math.ceil(46 / (pw / (runs.length - 1)));
  const peak = runs.findIndex((r) => r.usd === max);
  const last = runs.length - 1;
  const shown = (i) => i === last || i === peak || (i % every === 0 && last - i >= every && Math.abs(i - peak) >= every);
  const xTicks = runs.map((_, i) => shown(i) ? `<text class="tick" x="${x(i)}" y="${m.t + ph + 14}" text-anchor="middle">${i + 1}</text>` : "").join("");
  const dots = pts.map(([px, py], i) => `<g><title>Run ${i + 1}: ${esc(runs[i].task || runs[i].run_id)} · ${usd(runs[i].usd, 3)}</title>
    <circle class="hit" cx="${px}" cy="${py}" r="10"/><circle class="dot" cx="${px}" cy="${py}" r="4"/>
    ${shown(i) ? `<text class="val" x="${px}" y="${py - 9}" text-anchor="middle">${usd(runs[i].usd, 3)}</text>` : ""}</g>`).join("");
  el.innerHTML = `<svg width="${w}" height="${h}" role="img" aria-label="Spend per run in USD, oldest to newest">
    ${grid}
    <polygon class="area" points="${area}"/>
    <polyline class="line" points="${pline}"/>
    ${dots}
    ${xTicks}
    <text class="axis-title" x="${m.l + pw / 2}" y="${h - 4}" text-anchor="middle">Run (oldest → newest)</text>
    <text class="axis-title" transform="translate(12 ${m.t + ph / 2}) rotate(-90)" text-anchor="middle">USD per run</text>
  </svg>`;
}

// ---- outputs: every run's answer, expandable into its full log --------------------------------

function renderRuns() {
  const runs = S.summary?.runs || [];
  $("runsCount").textContent = runs.length;
  if (!runs.length) { $("runs").className = "muted"; $("runs").textContent = "No runs yet."; return; }
  $("runs").className = "";
  const shown = current()?.id;
  $("runs").innerHTML = runs.slice(0, 15).map((r) => {
    const tone = STATUS_TONE[r.status] || "";
    const open = S.openRuns.has(r.run_id);
    const status = r.status === "unfinished"
      ? `<span class="chip ${tone}"><span class="spinner" aria-hidden="true"></span>${esc(STATUS_LABEL[r.status])}</span>`
      : chip(STATUS_LABEL[r.status] || r.status, tone);
    const killBtn = r.status === "unfinished" ? `<button type="button" class="danger small run-kill" data-act="kill-run" data-run="${esc(r.run_id)}">Kill this run</button>` : "";
    const inspect = r.run_id === shown ? chip("in the lab", "info")
      : `<button type="button" class="small run-inspect" data-act="inspect" data-run="${esc(r.run_id)}">Inspect</button>`;
    return `<details class="run-item${r.run_id === shown ? " selected" : ""}" data-run="${esc(r.run_id)}"${open ? " open" : ""}>
      <summary>
        <div class="head"><span class="chev" aria-hidden="true"></span>${status}${r.fake ? chip("fake", "warn") : ""}<span class="task">${esc(r.task || "(direct install or call)")}</span><span class="spacer"></span><span class="toggle-hint" data-show="Show log" data-hide="Hide log" aria-hidden="true"></span>${inspect}${killBtn}</div>
        <div class="muted small">session ${esc(r.session)} · $${Number(r.usd || 0).toFixed(3)}${r.built.length ? ` · built ${r.built.map(esc).join(", ")}` : ""}${r.reused.length ? ` · reused ${r.reused.map(esc).join(", ")}` : ""}</div>
        ${r.answer ? `<div class="answer">${esc(r.answer)}</div>` : ""}
      </summary>
      ${open ? renderRunLog(r.run_id) : ""}
    </details>`;
  }).join("");
  $("runs").querySelectorAll("details.run-item").forEach((el) => el.addEventListener("toggle", () => {
    if (el.open) S.openRuns.add(el.dataset.run);
    else S.openRuns.delete(el.dataset.run);
    if (el.open && !el.querySelector(".run-log")) el.insertAdjacentHTML("beforeend", renderRunLog(el.dataset.run));
  }));
}

// ---- credentials ------------------------------------------------------------------------------

function renderCreds() {
  $("creds").innerHTML = Object.entries(S.creds).map(([service, st]) => `
    <details class="cred-item" data-service="${esc(service)}"${S.openCreds.has(service) ? " open" : ""}>
      <summary class="cred-row">
        <span class="chev" aria-hidden="true"></span>
        <b>${esc(CRED_LABELS[service] || service)}</b>
        <span class="spacer"></span>
        <span>${st.configured ? chip("configured", "ok") : chip("not set")}${st.remembered ? " " + chip("remembered", "warn") : ""}</span>
        <span class="toggle-hint" data-show="${st.configured ? "Change key" : "Set key"}" data-hide="Hide" aria-hidden="true"></span>
      </summary>
      <form class="cred-form" data-service="${esc(service)}">
        <input type="password" placeholder="paste the key, nothing is shown back" autocomplete="off">
        <label><input type="checkbox"> remember</label>
        <button type="submit">Save</button>
        ${st.remembered ? `<button type="button" class="danger" data-forget="${esc(service)}">Forget</button>` : ""}
      </form>
    </details>`).join("");
  $("creds").querySelectorAll("details.cred-item").forEach((el) => el.addEventListener("toggle", () => {
    if (el.open) S.openCreds.add(el.dataset.service);
    else S.openCreds.delete(el.dataset.service);
  }));
  $("creds").querySelectorAll("form").forEach((f) => f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const value = f.querySelector("input[type=password]").value;
    const remember = f.querySelector("input[type=checkbox]").checked;
    if (!value) return;
    if (!(await post(`/api/credentials/${encodeURIComponent(f.dataset.service)}`, { value, remember }, "Saving the key"))) return;
    S.openCreds.delete(f.dataset.service);  // saved: fold the row back to its status line
    loadCreds();
  }));
  $("creds").querySelectorAll("[data-forget]").forEach((b) => b.addEventListener("click", async () => {
    await j(`/api/credentials/${encodeURIComponent(b.dataset.forget)}`, { method: "DELETE" }).catch((e) => banner("bad", `Forget failed: ${e.message}`));
    loadCreds();
  }));
}

// ---- render loop ------------------------------------------------------------------------------

// Batches a burst of events into one render. setTimeout, not requestAnimationFrame: rAF never fires
// in a tab the browser isn't painting, and the operator's tab is often in the background.
let scheduled = false;
function schedule() {
  if (scheduled) return;
  scheduled = true;
  setTimeout(() => {
    scheduled = false;
    const all = S.dirty.has("all"), has = (k) => all || S.dirty.has(k);
    if (has("runs")) renderRunSelect();
    if (has("header")) renderHeader();
    if (has("lab")) renderLab();
    if (has("labBadge")) { $("labNew").hidden = !S.labNew; $("labNew").textContent = `${S.labNew} new`; }
    if (has("approvals")) renderApprovals();
    if (has("registry")) renderRegistry();
    if (has("reglog")) renderRegistryLog();
    if (has("timeline")) { renderTimeline(); refreshOpenRunLogs(); }
    if (has("summary")) { renderTiles(); renderStatusChart(); renderSpendChart(); renderRuns(); }
    S.dirty.clear();
  }, 30);
}

// ---- server calls -----------------------------------------------------------------------------

async function j(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${url}: ${r.status}`);
  return r.status === 204 ? null : r.json();
}

async function post(url, body, what) {
  try {
    return await j(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined }) ?? true;
  } catch (e) {
    banner("bad", `${what} failed: ${e.message}`);
    return false;
  }
}

async function loadConfig() {
  S.config = await j("/api/config");
  S.dirty.add("header");
  schedule();
}

async function loadSummary() {
  try {
    S.summary = await j("/api/summary");
    S.dirty.add("summary");
    schedule();
  } catch { /* the live chip already shows a dead server */ }
}

let summaryTimer = null;
function refreshSummarySoon() {
  clearTimeout(summaryTimer);
  summaryTimer = setTimeout(loadSummary, 400);
}

async function loadRegistry() {
  try {
    [S.registry, S.reglog] = await Promise.all([j("/api/registry"), j("/api/registry/log")]);
    S.dirty.add("registry").add("reglog");
    schedule();
  } catch { /* the live chip already shows a dead server */ }
}

async function loadCreds() {
  S.creds = await j("/api/credentials");
  renderCreds();
}

// ---- actions ------------------------------------------------------------------------------------

// Destructive buttons need a second click within 4 s: a stray click can't kill or quarantine
// anything, without a native confirm() dialog in the way.
function armed(btn, label) {
  if (btn.dataset.armed) { delete btn.dataset.armed; btn.textContent = btn.dataset.label; return true; }
  btn.dataset.label = btn.textContent;
  btn.dataset.armed = "1";
  btn.textContent = label;
  setTimeout(() => { if (btn.dataset.armed) { delete btn.dataset.armed; btn.textContent = btn.dataset.label; } }, 4000);
  return false;
}

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-act]");
  if (!btn) return;
  const { act, id, name } = btn.dataset;
  if (act === "approve" || act === "reject") {
    btn.disabled = true;  // the card goes away when approval_decided comes back over the stream
    const reason = document.querySelector(`[data-reason="${CSS.escape(id)}"]`)?.value || "";
    if (!(await post(`/api/approvals/${encodeURIComponent(id)}`, { approved: act === "approve", reason }, act === "approve" ? "Approve" : "Reject"))) btn.disabled = false;
  } else if (act === "kill") {
    if (armed(btn, "Click again to stop every run")) await post("/api/kill", null, "Kill switch");
  } else if (act === "kill-run" || act === "inspect") {
    ev.preventDefault();  // inside <summary>: don't also toggle the row open or closed
    if (act === "inspect") { select(btn.dataset.run); $("run").scrollIntoView({ block: "start" }); return; }
    if (!armed(btn, "Click again to confirm")) return;
    btn.disabled = true;
    btn.textContent = "Killing…";
    if (await post(`/api/runs/${encodeURIComponent(btn.dataset.run)}/kill`, null, "Kill")) refreshSummarySoon();
    else { btn.disabled = false; btn.textContent = "Kill this run"; }
  } else if (act === "quarantine") {
    if (armed(btn, "Confirm") && await post(`/api/registry/${encodeURIComponent(name)}/quarantine`, null, `Quarantine of ${name}`)) banner("ok", `${name} quarantined.`);
  } else if (act === "rollback") {
    const version = Number(document.querySelector(`[data-rb="${CSS.escape(name)}"]`).value);
    if (await post(`/api/registry/${encodeURIComponent(name)}/rollback`, { version }, `Rollback of ${name} to v${version}`)) banner("ok", `${name} rolled back to v${version}.`);
  }
});

document.addEventListener("input", (ev) => {
  const id = ev.target.dataset?.reason;
  if (id) S.reasons.set(id, ev.target.value);
});

document.addEventListener("toggle", (ev) => {
  const key = ev.target.dataset?.key;
  if (key) S.open.set(key, ev.target.open);
}, true);

$("runSelect").addEventListener("change", (ev) => select(ev.target.value));
$("labSection").addEventListener("toggle", () => {
  if ($("labSection").open) { S.labNew = 0; $("labNew").hidden = true; }
});
$("approvalModal").addEventListener("cancel", (ev) => ev.preventDefault());  // Escape: a decision is required

// ---- new task -----------------------------------------------------------------------------------

$("taskFiles").addEventListener("change", () => {
  const files = $("taskFiles").files;
  const label = $("taskFilesLabel");
  const pick = $("taskFiles").closest(".file-pick");
  if (!files.length) { label.textContent = "Attach files"; pick.classList.remove("has-files"); return; }
  label.textContent = files.length === 1 ? files[0].name : `${files.length} files`;
  pick.classList.add("has-files");
});

$("newTask").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("taskInput");
  const task = input.value.trim();
  if (!task) return;
  const button = ev.target.querySelector("button[type=submit]");
  button.disabled = true;
  $("taskStatus").textContent = "Starting…";
  const form = new FormData();
  form.append("task", task);
  form.append("models", $("taskModels").value);
  for (const f of $("taskFiles").files) form.append("files", f);
  try {
    await j("/api/tasks", { method: "POST", body: form });
    input.value = "";
    $("taskFiles").value = "";
    $("taskFiles").dispatchEvent(new Event("change"));
    $("taskStatus").textContent = "Started — the lab follows it as it runs.";
    select(null);  // follow the latest run, which is about to be this one
    refreshSummarySoon();
  } catch (e) {
    $("taskStatus").textContent = "Could not start: " + e.message;
  } finally {
    button.disabled = false;
    setTimeout(() => { $("taskStatus").textContent = ""; }, 6000);
  }
});

// Enter runs the task, Shift+Enter adds a new line. Skip while an IME is composing or a run is already starting.
$("taskInput").addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter" || ev.shiftKey || ev.isComposing) return;
  ev.preventDefault();
  if (!$("newTask").querySelector("button[type=submit]").disabled) $("newTask").requestSubmit();
});

// Start from scratch (dev only): archives everything to rehearsals/, the SSE "reset" event then reloads the page.
async function postReset(force) {
  const r = await fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force }) });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}

$("reset").addEventListener("click", async () => {
  if (!confirm("Start from scratch? Every installed tool, the event log and the build workspaces move to rehearsals/ (nothing is deleted).")) return;
  let res = await postReset(false);
  if (res.status === 409 && String(res.body.detail).includes("unfinished")) {
    if (!confirm(`${res.body.detail}\n\nArchive anyway?`)) return;
    res = await postReset(true);
  }
  if (res.status !== 200) { banner("bad", `Start from scratch failed: ${res.body.detail || res.status}`); return; }
  location.reload();
});

// ---- wiring -------------------------------------------------------------------------------------

const TYPES = ["run_started", "plan", "gap", "study", "build", "test_run", "approval_requested", "approval_decided",
               "install", "call", "answer", "budget", "cap_hit", "rollback", "quarantine", "kill", "error", "run_finished"];
const es = new EventSource("/api/events");
// The log's own `error` events share a name with EventSource's connection error; only the former carry data.
for (const t of TYPES) es.addEventListener(t, (msg) => {
  if (msg instanceof MessageEvent) { apply(JSON.parse(msg.data)); schedule(); }
  else { $("live").className = "chip warn"; $("live").textContent = "reconnecting…"; }
});
es.addEventListener("reset", () => { es.close(); location.reload(); });  // the log was moved aside: replay the new one
es.onopen = () => { $("live").className = "chip ok"; $("live").textContent = "live"; };

loadConfig().catch(() => {});
loadSummary();
loadRegistry();
loadCreds().catch(() => {});
schedule();
setInterval(loadSummary, 20000);  // fallback poll, in case an event was missed
window.addEventListener("resize", renderSpendChart);  // the spend chart is drawn in pixels, not a stretched viewBox
