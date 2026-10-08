// Owner: D. Same event log as the console (ui/static/index.html): approvals, kill and the
// budget meter are driven live off the SSE stream; runs/outputs come from /api/summary
// (scripts.evidence.summarize under the hood), refetched on the events that change it rather
// than reconstructed event-by-event in here — a deliberately lighter state model than the
// console's own build-by-build lab view. The one exception: every event is also kept per
// run_id (S.eventsByRun), so clicking an output can expand its full log without a server
// round trip — using the same per-event wording the console's own timeline uses.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const chip = (text, cls = "") => `<span class="chip ${cls}">${esc(text)}</span>`;
const tags = (xs, cls = "") => (xs || []).map((x) => `<span class="tag ${cls}">${esc(x)}</span>`).join("");
const clock = (ts) => new Date(ts).toLocaleTimeString([], { hour12: false });
const refused = (reason) => String(reason || "").startsWith("refused:");
const denied = (xs) => (xs?.length ? ` <span class="refused">egress refused:</span> ${tags(xs, "deny")}` : "");
const suite = (x) => (!x || x === "own" ? "" : x === "retest" ? chip("retest", "info") : chip(x.replace(/ tests$/, ""), "info"));

const STATUS_TONE = { ok: "ok", failed: "bad", killed: "bad", capped: "warn", unfinished: "info", running: "info" };
const STATUS_LABEL = { ok: "ok", failed: "failed", killed: "killed", capped: "capped", unfinished: "running" };
const CRED_LABELS = { telegram: "Telegram bot token", discord: "Discord bot token", elevenlabs: "ElevenLabs API key", apify: "Apify API token" };

const S = {
  config: null, summary: null, registry: [], versions: new Map(), creds: {}, pending: new Map(), budget: null, caps: [], lastId: -1,
  eventsByRun: new Map(), openRuns: new Set(), approvalRun: new Map(),
};

// ---- per-run full log: every event carrying a run_id, in the same wording the console uses ----

function logLine(e) {
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
    case "cap_hit": return `${chip("cap hit", "bad")} ${esc(d.limit)} = ${esc(d.value)} (max ${esc(d.max)})`;
    case "kill": return `${chip("kill", "bad")} by ${esc(d.by)}`;
    case "error": return `<span style="color:var(--bad)">${esc(d.message)}</span>`;
    case "run_finished": return chip(d.status, STATUS_TONE[d.status] || "");
    default: return null; // budget: shown live by the meters, not worth a line per turn
  }
}

function renderRunLog(runId) {
  const events = S.eventsByRun.get(runId) || [];
  const lines = events.map((e) => [e, logLine(e)]).filter(([, text]) => text != null);
  if (!lines.length) return `<div class="run-log"><div class="empty">No detail recorded for this run yet.</div></div>`;
  return `<div class="run-log">${lines.map(([e, text]) => `<div class="ev"><time>${clock(e.ts)}</time><div><span class="kind">${esc(e.type)}</span>${text}</div></div>`).join("")}</div>`;
}

async function j(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${url}: ${r.status}`);
  return r.status === 204 ? null : r.json();
}

// ---- loaders --------------------------------------------------------------------------------

async function loadConfig() {
  S.config = await j("/api/config");
  renderHeader();
  renderMeters();
}

async function loadSummary() {
  S.summary = await j("/api/summary");
  renderTiles();
  renderStatusChart();
  renderSpendChart();
  renderRuns();
}

async function loadRegistry() {
  S.registry = await j("/api/registry");
  renderRegistry();
}

async function loadCreds() {
  S.creds = await j("/api/credentials");
  renderCreds();
}

// ---- header -----------------------------------------------------------------------------

function renderHeader() {
  const c = S.config;
  if (!c) return;
  $("chips").innerHTML = (c.mode === "demo" ? chip("demo mode", "ok") : chip("dev mode"))
    + (c.fakes.length ? " " + chip("fake " + c.fakes.join(" + "), "warn") : "") + " " + chip("auth: " + c.auth)
    + (c.models === "cheap" ? " " + chip("models: cheap", "warn") : "");
  $("reset").hidden = c.mode !== "dev";
}

// ---- stat tiles: a hero number for a job that's just "one number" ----------------------------

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

// ---- runs by status: one reserved status color per bar, the label *is* the legend -----------

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

// ---- spend per run: one sequential hue, thin line, rounded cap, SVG <title> for the tooltip ----

function renderSpendChart() {
  const el = $("spendChart");
  const runs = (S.summary?.runs || []).filter((r) => r.usd > 0).slice(0, 20).reverse();
  if (runs.length < 2) { el.innerHTML = `<div class="empty">Not enough runs yet for a trend.</div>`; return; }
  const w = 560, h = 110, pad = 6;
  const max = Math.max(...runs.map((r) => r.usd), 0.01);
  const x = (i) => pad + (i * (w - 2 * pad)) / (runs.length - 1);
  const y = (v) => h - pad - (v / max) * (h - 2 * pad);
  const pts = runs.map((r, i) => [x(i), y(r.usd)]);
  const line = pts.map((p) => p.join(",")).join(" ");
  const area = `${pad},${h - pad} ${line} ${w - pad},${h - pad}`;
  const dots = pts.map(([px, py], i) => `<circle class="dot" cx="${px}" cy="${py}" r="3"><title>${esc(runs[i].task || runs[i].run_id)}: $${runs[i].usd.toFixed(3)}</title></circle>`).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <line class="baseline" x1="${pad}" y1="${h - pad}" x2="${w - pad}" y2="${h - pad}"/>
    <polygon class="area" points="${area}"/>
    <polyline class="line" points="${line}"/>
    ${dots}
  </svg>`;
}

// ---- outputs ------------------------------------------------------------------------------

function renderRuns() {
  const runs = S.summary?.runs || [];
  $("runsCount").textContent = runs.length;
  if (!runs.length) { $("runs").className = "muted"; $("runs").textContent = "No runs yet."; return; }
  $("runs").className = "";
  $("runs").innerHTML = runs.slice(0, 15).map((r) => {
    const tone = STATUS_TONE[r.status] || "";
    const open = S.openRuns.has(r.run_id);
    return `<details class="run-item" data-run="${esc(r.run_id)}"${open ? " open" : ""}>
      <summary>
        <div class="head">${chip(STATUS_LABEL[r.status] || r.status, tone)}${r.fake ? chip("fake", "warn") : ""}<span class="task">${esc(r.task || "(direct install or call)")}</span></div>
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

// ---- registry -------------------------------------------------------------------------------

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
  $("regEmpty").hidden = !!S.registry.length;
  const tbody = document.querySelector("#registry tbody");
  tbody.innerHTML = S.registry.map((e) => {
    const m = e.manifest, q = e.status === "quarantined", others = otherVersions(m);
    const rollback = others.length
      ? `<select aria-label="Version to roll back to">${others.map((v) => `<option value="${v}">v${v}</option>`).join("")}</select>
         <button data-rollback="${esc(m.name)}">Rollback</button>`
      : "";
    return `<tr class="${q ? "quarantined" : ""}">
      <td class="name">${esc(m.name)}@v${esc(m.version)}</td>
      <td>${q ? chip("quarantined", "bad") : chip("active", "ok")}</td>
      <td class="actions">
        ${rollback}
        ${q ? "" : `<button class="danger" data-quarantine="${esc(m.name)}">Quarantine</button>`}
      </td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll("[data-rollback]").forEach((b) => b.addEventListener("click", async () => {
    const version = Number(b.closest("td").querySelector("select").value);
    await registryAction(`Rollback of ${b.dataset.rollback} to v${version}`, `/api/registry/${encodeURIComponent(b.dataset.rollback)}/rollback`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version }) });
  }));
  tbody.querySelectorAll("[data-quarantine]").forEach((b) => b.addEventListener("click", async () => {
    await registryAction(`Quarantine of ${b.dataset.quarantine}`, `/api/registry/${encodeURIComponent(b.dataset.quarantine)}/quarantine`, { method: "POST" });
  }));
}

async function registryAction(what, url, opts) {
  try {
    await j(url, opts);
    banner("ok", `${what} done.`);
  } catch (e) {
    banner("bad", `${what} failed: ${e.message}`);
  }
  loadRegistry();
}

// ---- approvals ------------------------------------------------------------------------------

function renderApprovals() {
  $("apprCount").textContent = S.pending.size;
  if (!S.pending.size) { $("approvals").className = "muted"; $("approvals").textContent = "Nothing waiting for a decision."; return; }
  $("approvals").className = "";
  $("approvals").innerHTML = [...S.pending.values()].map((req) => `
    <div class="approval" data-id="${esc(req.id)}">
      <div class="head"><b class="mono">${esc(req.ref)}</b>${req.manifest?.permissions ? chip(`net: ${(req.manifest.permissions.network || []).join(", ") || "none"}`) : ""}</div>
      <div class="actions">
        <button class="ok" data-approve="${esc(req.id)}">Approve</button>
        <button class="danger" data-reject="${esc(req.id)}">Reject</button>
      </div>
    </div>`).join("");
  $("approvals").querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", () => decideApproval(b.dataset.approve, true)));
  $("approvals").querySelectorAll("[data-reject]").forEach((b) => b.addEventListener("click", () => decideApproval(b.dataset.reject, false)));
}

async function decideApproval(id, approved) {
  await j(`/api/approvals/${id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approved }) });
  S.pending.delete(id);
  renderApprovals();
}

// ---- budget meters (same component the console uses) -----------------------------------------

function renderMeters() {
  const lim = S.budget?.limits || S.config?.limits;
  if (!lim) return;
  const b = S.budget || { usd: 0, planner_turns: 0, gap_turns: 0, gaps: 0, minutes: 0 };
  const hit = new Set(S.caps.map((c) => c.limit));
  const meter = (key, label, value, shown, unit) => {
    const pct = Math.min(100, (value / lim[key]) * 100);
    const cls = hit.has(key) || value > lim[key] ? "hit" : pct >= 80 ? "warn" : "";
    return `<div class="meter ${cls}"><div class="label"><span>${label}</span><span>${Math.round(pct)}%</span></div>
      <div class="value">${shown} <small>/ ${unit}</small></div><div class="bar"><i style="width:${pct}%"></i></div></div>`;
  };
  const have = (key) => lim[key] != null;
  $("meters").innerHTML = meter("usd", "Spend", b.usd, "$" + Number(b.usd).toFixed(2), "$" + lim.usd.toFixed(2))
    + (have("planner_turns") ? meter("planner_turns", "Planner turns", b.planner_turns ?? 0, b.planner_turns ?? 0, lim.planner_turns) : "")
    + (have("turns_per_gap") ? meter("turns_per_gap", "Builder turns (gap)", b.gap_turns ?? 0, b.gap_turns ?? 0, lim.turns_per_gap) : "")
    + meter("gaps", "Gaps", b.gaps, b.gaps, lim.gaps)
    + meter("minutes", "Run time", b.minutes, Number(b.minutes).toFixed(1), lim.minutes + " min");
  $("caps").textContent = `· also capped: ${lim.repairs_per_gap} repairs per gap, ${lim.sandbox_seconds} s per sandbox run`;
}

// ---- credentials ------------------------------------------------------------------------------

function renderCreds() {
  $("creds").innerHTML = Object.entries(S.creds).map(([service, st]) => `
    <div class="cred-row-wrap">
      <div class="cred-row">
        <b>${esc(CRED_LABELS[service] || service)}</b>
        <span>${st.configured ? chip("configured", "ok") : chip("not set")}${st.remembered ? " " + chip("remembered", "warn") : ""}</span>
      </div>
      <form class="cred-form" data-service="${service}">
        <input type="password" placeholder="paste the key, nothing is shown back" autocomplete="off">
        <label><input type="checkbox"> remember</label>
        <button type="submit">Save</button>
        ${st.remembered ? `<button type="button" class="danger" data-forget="${service}">Forget</button>` : ""}
      </form>
    </div>`).join("");
  $("creds").querySelectorAll("form").forEach((f) => f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const value = f.querySelector("input[type=password]").value;
    const remember = f.querySelector("input[type=checkbox]").checked;
    if (!value) return;
    await j(`/api/credentials/${f.dataset.service}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value, remember }) });
    loadCreds();
  }));
  $("creds").querySelectorAll("[data-forget]").forEach((b) => b.addEventListener("click", async () => {
    await j(`/api/credentials/${b.dataset.forget}`, { method: "DELETE" });
    loadCreds();
  }));
}

// ---- live events (SSE) -------------------------------------------------------------------

let summaryTimer = null;
function refreshSummarySoon() {
  clearTimeout(summaryTimer);
  summaryTimer = setTimeout(loadSummary, 400);
}

function connectEvents() {
  const es = new EventSource("/api/events?offset=0");
  es.onopen = () => { $("live").textContent = "live"; $("live").className = "chip ok"; };
  es.onerror = () => { $("live").textContent = "reconnecting…"; $("live").className = "chip warn"; };
  es.addEventListener("reset", () => location.reload());

  for (const type of ["run_started", "plan", "gap", "study", "build", "test_run", "approval_requested", "approval_decided",
                       "install", "call", "answer", "budget", "cap_hit", "rollback", "quarantine", "kill", "error", "run_finished"]) {
    es.addEventListener(type, (ev) => onEvent(type, JSON.parse(ev.data)));
  }
}

function onEvent(type, e) {
  if (e.id <= S.lastId) return;
  S.lastId = e.id;
  const d = e.data;

  // approval_decided is written by whichever approver (console or dashboard) under its own
  // session, not the run's — re-attribute it to the run that asked, the same way
  // scripts.evidence.summarize does, via the request id approval_requested already recorded.
  const runId = type === "approval_decided" ? S.approvalRun.get(d.request_id) || e.run_id : e.run_id;
  if (runId) {
    if (!S.eventsByRun.has(runId)) S.eventsByRun.set(runId, []);
    S.eventsByRun.get(runId).push(e);
    if (S.openRuns.has(runId)) {
      const el = document.querySelector(`details.run-item[data-run="${CSS.escape(runId)}"] .run-log`);
      if (el) el.outerHTML = renderRunLog(runId);
    }
  }

  switch (type) {
    case "approval_requested":
      S.pending.set(d.id, d);
      S.approvalRun.set(d.id, e.run_id);
      renderApprovals();
      break;
    case "approval_decided":
      S.pending.delete(d.request_id);
      renderApprovals();
      break;
    case "budget":
      S.budget = d;
      renderMeters();
      break;
    case "cap_hit":
      S.caps.push(d);
      renderMeters();
      banner("bad", `Cap hit: ${esc(d.limit)} reached ${esc(d.value)} (max ${esc(d.max)}).`);
      break;
    case "kill":
      banner("bad", `Kill sent by ${esc(d.by)}.`);
      refreshSummarySoon();
      break;
    case "install":
      if (d.installed) {
        const [name, v] = d.ref.split("@v");
        if (!S.versions.has(name)) S.versions.set(name, new Set());
        S.versions.get(name).add(Number(v));
      }
      loadRegistry();
      refreshSummarySoon();
      break;
    case "rollback":
    case "quarantine":
      loadRegistry();
      break;
    case "run_started":
    case "run_finished":
    case "answer":
      refreshSummarySoon();
      break;
  }
}

function banner(cls, text) {
  const el = document.createElement("div");
  el.className = `banner ${cls}`;
  el.textContent = text;
  $("banners").hidden = false;
  $("banners").prepend(el);
  setTimeout(() => el.remove(), 15000);
}

// ---- new task -----------------------------------------------------------------------------

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
    $("taskStatus").textContent = "Started — see it below as it runs.";
    refreshSummarySoon();
  } catch (e) {
    $("taskStatus").textContent = "Could not start: " + e.message;
  } finally {
    button.disabled = false;
    setTimeout(() => { $("taskStatus").textContent = ""; }, 6000);
  }
});

// ---- wiring -------------------------------------------------------------------------------

$("kill").addEventListener("click", async () => {
  if (!$("kill").dataset.armed) {
    $("kill").dataset.armed = "1";
    $("kill").textContent = "Click again to confirm";
    setTimeout(() => { delete $("kill").dataset.armed; $("kill").textContent = "Kill switch"; }, 4000);
    return;
  }
  delete $("kill").dataset.armed;
  $("kill").textContent = "Kill switch";
  await j("/api/kill", { method: "POST" });
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

loadConfig();
loadSummary();
loadRegistry();
loadCreds();
connectEvents();
setInterval(loadSummary, 20000); // fallback poll, in case an event was missed
