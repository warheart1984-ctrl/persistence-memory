/* Twin UI — read-only client for /api/jarvis/twin/*.
 *
 * Everything rendered comes from the gated endpoints: narration text is
 * already gate-checked server-side, and receipt.dropped carries only
 * {section, index, reason} — never the dropped text. All DOM writes use
 * textContent; nothing is injected as HTML. This page has no write
 * controls and never sees an API key.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const COMPONENT_NAMES = {
  V: "verified", P: "provenance", L: "lineage", W: "weight",
  S: "coverage", T: "temporal", C: "conflict health", N: "participation",
};

function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

function setStatus(msg) { $("status").textContent = msg; }

function showError(msg) {
  const box = $("error");
  box.textContent = msg;
  box.hidden = false;
}
function clearError() { $("error").hidden = true; }

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.status;
    try { detail = (await res.json()).detail || res.status; } catch (_) { /* keep status */ }
    const err = new Error(`HTTP ${res.status}: ${detail}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/* ---------- state panel ---------- */

function renderState(state) {
  const empty = state.record_count === 0;
  $("state-empty").hidden = !empty;
  $("state-body").hidden = empty;
  if (empty) return;

  $("coverage-index").textContent = state.coverage_index.toFixed(2);
  $("weakest").textContent =
    `${state.weakest_component} (${COMPONENT_NAMES[state.weakest_component] || ""})`;

  const comps = $("components");
  comps.textContent = "";
  for (const [k, v] of Object.entries(state.components)) {
    const li = document.createElement("li");
    li.append(el("span", `${k} ${COMPONENT_NAMES[k] || ""}`));
    const meter = el("span", "", "meter" + (k === state.weakest_component ? " weak" : ""));
    meter.setAttribute("role", "meter");
    meter.setAttribute("aria-valuemin", "0");
    meter.setAttribute("aria-valuemax", "1");
    meter.setAttribute("aria-valuenow", String(v));
    meter.setAttribute("aria-label", `${k} ${COMPONENT_NAMES[k] || ""}`);
    const fill = el("span");
    fill.style.width = `${Math.round(v * 100)}%`;
    meter.append(fill);
    li.append(meter, el("span", v.toFixed(2)));
    comps.append(li);
  }

  const projects = $("active-projects");
  projects.textContent = "";
  state.active_projects.forEach((t) => projects.append(el("li", t)));
  $("projects-empty").hidden = state.active_projects.length > 0;

  const acc = $("accomplishments");
  acc.textContent = "";
  state.recent_accomplishments.forEach((a) => {
    const li = document.createElement("li");
    li.append(el("span", a.summary || "(no summary)"));
    li.append(el("span", "", "cites"))
      .lastChild.append(el("code", a.record_id));
    acc.append(li);
  });
  $("accomplishments-empty").hidden = state.recent_accomplishments.length > 0;

  const risks = $("risks");
  risks.textContent = "";
  state.open_risks.forEach((r) => {
    const li = document.createElement("li");
    if (r.kind === "conflict") {
      li.append(el("span", "CONFLICT", "badge conflict"));
      li.append(el("span", `${r.subject}: ${r.record_ids.join(", ")}`));
    } else {
      li.append(el("span", "RISK", "badge risk"));
      li.append(el("span", r.text));
      li.append(el("span", "", "cites"))
        .lastChild.append(el("code", r.record_id));
    }
    risks.append(li);
  });
  $("risks-empty").hidden = state.open_risks.length > 0;

  const stale = $("stale");
  stale.textContent = "";
  state.stale_commitments.forEach((s) => {
    const li = document.createElement("li");
    li.append(el("span", `${s.subject || "(no subject)"} — idle ${s.days_since_update}d`));
    li.append(el("span", "", "cites"))
      .lastChild.append(el("code", s.record_id));
    stale.append(li);
  });
  $("stale-empty").hidden = state.stale_commitments.length > 0;
}

/* ---------- narration panel ---------- */

const SECTION_TITLES = {
  assessment: "Assessment", opportunity: "Opportunity", risk: "Risk",
  next_action: "Next action", explanation: "Explanation",
};

function renderNarration(narration) {
  const root = $("narration");
  root.textContent = "";
  for (const key of Object.keys(SECTION_TITLES)) {
    const sec = el("div", "", "narr-section");
    sec.append(el("h3", SECTION_TITLES[key]));
    const items = narration[key] || [];
    if (!items.length) {
      sec.append(el("p", "—", "muted"));
    } else {
      const ul = document.createElement("ul");
      items.forEach((it) => {
        const li = document.createElement("li");
        if (it.template) li.append(el("span", "TEMPLATE", "badge template"));
        li.append(el("span", it.text));
        const cites = el("span", "", "cites");
        (it.cites || []).forEach((c) => cites.append(el("code", c), " "));
        li.append(cites);
        ul.append(li);
      });
      sec.append(ul);
    }
    root.append(sec);
  }
}

/* ---------- receipt panel ---------- */

function renderReceipt(receipt) {
  const dl = $("receipt");
  dl.textContent = "";
  const rows = [
    ["schema", receipt.schema],
    ["provider", receipt.provider],
    ["model", receipt.model],
    ["fallback", receipt.fallback_used ? "yes" : "no"],
    ["latency", `${receipt.latency_ms} ms`],
    ["state digest", receipt.state_digest],
    ["input digest", receipt.twin_input_digest],
    ["output digest", receipt.final_output_digest],
    ["prompt digest", receipt.prompt_digest],
  ];
  rows.forEach(([k, v]) => {
    const dd = el("dd", String(v));
    if (k === "fallback" && receipt.fallback_used) {
      dd.prepend(el("span", "FALLBACK", "badge fallback"), " ");
    }
    dl.append(el("dt", k), dd);
  });

  const tbody = $("dropped").querySelector("tbody");
  tbody.textContent = "";
  const dropped = receipt.dropped || [];
  $("dropped").hidden = dropped.length === 0;
  $("dropped-empty").hidden = dropped.length > 0;
  dropped.forEach((d) => {
    const tr = document.createElement("tr");
    tr.append(el("td", d.section), el("td", d.reason));
    tbody.append(tr);
  });
}

/* ---------- loading ---------- */

async function load() {
  clearError();
  setStatus("Loading…");
  const provider = $("provider").value || "none";
  try {
    const [stateRes, narrRes] = await Promise.all([
      api("/api/jarvis/twin/state"),
      api(`/api/jarvis/twin/narration?provider=${encodeURIComponent(provider)}`),
    ]);
    renderState(stateRes.state);
    renderNarration(narrRes.narration);
    renderReceipt(narrRes.receipt);
    setStatus(`Updated ${stateRes.state.as_of || ""}`);
  } catch (e) {
    if (e.status === 404) {
      showError("Twin endpoints are disabled (404). Set JARVIS_TWIN_ENABLED " +
                "and JARVIS_TWIN_NARRATOR_ENABLED on the server.");
    } else {
      showError(`Request failed — ${e.message}`);
    }
    setStatus("Failed");
  }
}

async function loadProviders() {
  try {
    const res = await api("/api/jarvis/twin/providers");
    const sel = $("provider");
    sel.textContent = "";
    res.providers.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = p.model ? `${p.name} (${p.model})` : p.name;
      sel.append(opt);
    });
    sel.disabled = false;
  } catch (_) {
    /* picker stays on none */ void 0;
  }
}

$("refresh").addEventListener("click", load);
$("provider").addEventListener("change", load);
document.addEventListener("DOMContentLoaded", () => {
  loadProviders().then(load);
});
