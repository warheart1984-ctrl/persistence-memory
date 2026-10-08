/* Read-only client. Narration is rendered only from the gated API response;
 * dropped sentence text never exists in the receipt or page. */
"use strict";

const $ = (id) => document.getElementById(id);
const COMPONENTS = {
  V: "Verified records", P: "Provenance", L: "Lineage", W: "Evidence weight",
  S: "Coverage", T: "Time signals", C: "Conflict health", N: "Participation",
};
const SECTIONS = {assessment:"Assessment", opportunity:"Opportunity", risk:"Risk", next_action:"Next action", explanation:"Explanation"};
let currentReceipt = null;
let highlightTimer;

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function text(id, value) { $(id).textContent = value == null || value === "" ? "—" : String(value); }
function status(label, kind="") { const n=$("status"); n.textContent=label; n.className=`status-pill ${kind}`.trim(); }
function failure(message, disabled=false) {
  $("loading").hidden=true; $("dashboard").hidden=true;
  $("disabled").hidden=!disabled;
  if (!disabled) { const box=$("error"); box.textContent=message; box.hidden=false; }
  status(disabled?"Disabled":"Unavailable", "fallback");
}
async function api(path) {
  const response = await fetch(path, {headers:{Accept:"application/json"}, cache:"no-store"});
  if (!response.ok) {
    let detail=response.statusText;
    try { const body=await response.json(); detail=body.detail||detail; } catch (_) { /* status text is enough */ }
    const error=new Error(`HTTP ${response.status}: ${detail}`); error.status=response.status; throw error;
  }
  return response.json();
}
function shortId(id) { return String(id).length>12 ? `${String(id).slice(0,8)}…${String(id).slice(-3)}` : String(id); }
function recordLink(id) {
  const a=el("a",shortId(id),"record-id record-link");
  a.href=`/api/jarvis/memory/${encodeURIComponent(id)}`;
  a.target="_blank"; a.rel="noopener"; a.setAttribute("aria-label",`Open record ${id}`);
  return a;
}
function renderAsOf(state) {
  const n=$("as-of");
  if (!state.as_of) { n.textContent="As of —"; return; }
  const date=new Date(state.as_of);
  n.dateTime=state.as_of;
  n.textContent=`As of ${new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",dateStyle:"medium",timeStyle:"short",timeZoneName:"short"}).format(date)}`;
}
function renderState(state) {
  text("tenant-label",`Tenant · ${state.identity||"—"}`); renderAsOf(state);
  text("disclaimer",state.disclaimer||"Measures how well-structured and evidenced the ledger is. It does not say whether any memory is true.");
  const index=Number(state.coverage_index)||0;
  text("coverage-index",index.toFixed(2));
  const gauge=document.querySelector(".gauge"); gauge.setAttribute("aria-valuenow",String(index));
  gauge.setAttribute("aria-valuetext",`${index.toFixed(2)} out of 1`); $("coverage-fill").style.width=`${Math.max(0,Math.min(1,index))*100}%`;

  const comps=$("components"); comps.replaceChildren();
  for (const key of Object.keys(COMPONENTS)) {
    const value=Number(state.components?.[key])||0, weak=key===state.weakest_component;
    const li=el("li",undefined,weak?"is-weak":""); li.dataset.fact=`components.${key}`;
    li.append(el("span",`${key} · ${COMPONENTS[key]}`));
    const meter=el("span",undefined,"meter"); meter.setAttribute("role","meter"); meter.setAttribute("aria-valuemin","0"); meter.setAttribute("aria-valuemax","1"); meter.setAttribute("aria-valuenow",String(value)); meter.setAttribute("aria-label",`${COMPONENTS[key]}: ${value.toFixed(2)}${weak?", weakest component":""}`);
    const fill=el("span"); fill.style.width=`${Math.max(0,Math.min(1,value))*100}%`; meter.append(fill);
    li.append(meter,el("span",value.toFixed(2),"component-value")); comps.append(li);
  }
  const weakest=$("weakest"); weakest.hidden=!state.weakest_component;
  weakest.textContent=state.weakest_component?`Weakest · ${state.weakest_component}`:"";

  const projects=$("active-projects"); projects.replaceChildren();
  (state.active_projects||[]).forEach((name,i)=>{const li=el("li",name);li.dataset.fact=`active_projects[${i}]`;projects.append(li);});
  $("project-count").textContent=String((state.active_projects||[]).length); $("projects-empty").hidden=!!state.active_projects?.length;

  const acc=$("accomplishments"); acc.replaceChildren();
  (state.recent_accomplishments||[]).forEach((item,i)=>{
    const li=el("li"); li.dataset.fact=`recent_accomplishments[${i}]`;
    li.append(el("div",item.subject||item.summary||"(No subject)","record-copy"));
    if(item.subject&&item.summary) li.append(el("div",item.summary,"record-copy microcopy"));
    const meta=el("div",undefined,"record-meta"); meta.append(recordLink(item.record_id)); li.append(meta); acc.append(li);
  });
  $("accomplishments-empty").hidden=!!state.recent_accomplishments?.length;

  const risks=$("risks"); risks.replaceChildren();
  (state.open_risks||[]).forEach((risk,i)=>{
    const li=el("li"); li.dataset.fact=`open_risks[${i}]`;
    if(risk.kind==="conflict"){
      li.append(el("span","CONFLICT · ","kind-badge conflict"),el("span",risk.subject||"Unresolved conflict","record-copy"));
      const meta=el("div",undefined,"record-meta"); (risk.record_ids||[]).forEach(id=>meta.append(recordLink(id))); li.append(meta);
    } else {
      li.append(el("span","RISK · ","kind-badge"),el("span",risk.text,"record-copy"));
      const meta=el("div",undefined,"record-meta"); meta.append(recordLink(risk.record_id)); li.append(meta);
    }
    risks.append(li);
  });
  $("risks-empty").hidden=!!state.open_risks?.length;

  const stale=$("stale"); stale.replaceChildren();
  (state.stale_commitments||[]).forEach((item,i)=>{
    const li=el("li");li.dataset.fact=`stale_commitments[${i}]`;
    li.append(el("div",item.subject||item.summary||"(No subject)","record-copy"));
    const meta=el("div",undefined,"record-meta");meta.append(el("span",`${item.days_since_update} days since update`),recordLink(item.record_id));li.append(meta);stale.append(li);
  });
  $("stale-empty").hidden=!!state.stale_commitments?.length;
  let mission=document.querySelector("[data-fact='recommended_mission']");
  if(!mission){mission=el("article",undefined,"card mission");mission.dataset.fact="recommended_mission";mission.append(el("span","RECOMMENDED MISSION","eyebrow"),el("p",state.recommended_mission));document.querySelector(".facts-column").append(mission);}
  else mission.querySelector("p").textContent=state.recommended_mission;
  const noEvidence=Number(state.record_count)===0;
  $("empty-ledger").hidden=!noEvidence;
  if(noEvidence){$("narration").hidden=true;}else{$("narration").hidden=false;}
}
function citeTarget(path) {
  if(path==="coverage_index"||path.startsWith("components.")||path==="weakest_component"||path.startsWith("recommended_mission")||["active_projects","recent_accomplishments","open_risks","stale_commitments"].some(k=>path===k||path.startsWith(k+"["))) return path;
  return null;
}
function highlight(path) {
  document.querySelectorAll("[data-fact].highlight").forEach(n=>n.classList.remove("highlight"));
  let selector;
  if(path==="coverage_index") selector="[data-fact='coverage_index']";
  else if(path.startsWith("components.")) selector=`[data-fact='components'] li[data-fact='${CSS.escape(path)}']`;
  else if(path==="weakest_component") selector="[data-fact='components']";
  else if(path.startsWith("recommended_mission")) selector="[data-fact='recommended_mission']";
  else {const m=path.match(/^(active_projects|recent_accomplishments|open_risks|stale_commitments)(?:\[(\d+)\])?/);if(m)selector=`[data-fact='${m[1]}']${m[2]?` li[data-fact='${CSS.escape(m[1]+"["+m[2]+"]")}']`:""}`;}
  const node=selector?document.querySelector(selector):null;if(!node)return;
  node.classList.add("highlight");node.scrollIntoView({behavior:"smooth",block:"center"});clearTimeout(highlightTimer);highlightTimer=setTimeout(()=>node.classList.remove("highlight"),2400);
}
function renderNarration(sections) {
  const root=$("narration");root.replaceChildren();
  for(const [key,title] of Object.entries(SECTIONS)){
    const section=el("section",undefined,"narr-section");section.append(el("h2",title));
    const card=el("div",undefined,"narr-card"),ul=document.createElement("ul"),items=sections?.[key]||[];
    if(!items.length)ul.append(el("li","No gated narration for this section yet.","muted"));
    items.forEach(item=>{
      const li=document.createElement("li");if(item.template)li.append(el("span","TEMPLATE","template-tag"));li.append(el("span",item.text,"narr-text"));
      const cites=el("div",undefined,"cites");(item.cites||[]).forEach(path=>{const button=el("button",path,"cite-chip");button.type="button";button.setAttribute("aria-label",`Highlight cited fact ${path}`);button.addEventListener("click",()=>highlight(path));button.addEventListener("focus",()=>highlight(path));cites.append(button);});li.append(cites);ul.append(li);
    });
    card.append(ul);section.append(card);root.append(section);
  }
}
function renderReceipt(receipt) {
  currentReceipt=receipt;
  const rows=[["Schema",receipt.schema],["Provider",receipt.provider],["Model",receipt.model],["Fallback used",receipt.fallback_used?"Yes":"No"],["Latency",`${receipt.latency_ms} ms`],["State digest",receipt.state_digest],["Twin input digest",receipt.twin_input_digest],["Prompt",receipt.prompt_digest],["Raw output digest",receipt.raw_output_digest],["Final output digest",receipt.final_output_digest]];
  const dl=$("receipt");dl.replaceChildren();rows.forEach(([label,value])=>{dl.append(el("dt",label),el("dd",value??"—"));});
  const drops=receipt.dropped||[];$("dropped-panel").hidden=drops.length===0;$("dropped-count").textContent=String(drops.length);
  const list=$("dropped-list");list.replaceChildren();drops.forEach(drop=>{const li=el("li",`${drop.section} · ${drop.reason}`);list.append(li);});
  if(receipt.fallback_used)status(drops.length?`Fallback · ${drops.length} dropped`:"Fallback", "fallback");
  else if(drops.length)status(`Gated · ${drops.length} dropped`,"warn");else status("Gated","gated");
}
async function loadProviders() {
  try {
    const result=await api("/api/jarvis/twin/providers"),select=$("provider");select.replaceChildren();
    (result.providers||[]).forEach(provider=>{const option=el("option",provider.model?`${provider.name} · ${provider.model}`:provider.name);option.value=provider.name;select.append(option);});
    select.value=[...select.options].some(option=>option.value==="none")?"none":(select.options[0]?.value||"");select.disabled=select.options.length===0;
  } catch (_) { /* the other endpoint supplies the useful disabled/error state */ }
}
async function load() {
  $("error").hidden=true;$("disabled").hidden=true;$("loading").hidden=false;status("Loading");$("refresh").disabled=true;
  const provider=$("provider").value||"none";
  try {
    const [stateResponse,narrationResponse]=await Promise.all([api("/api/jarvis/twin/state"),api(`/api/jarvis/twin/narration?provider=${encodeURIComponent(provider)}`)]);
    // Prefer the exact state snapshot cited by the narration response so the
    // displayed facts and citation highlights cannot drift between requests.
    const state=narrationResponse.state||stateResponse.state;renderState(state);renderNarration(narrationResponse.narration);renderReceipt(narrationResponse.receipt);
    $("dashboard").hidden=false;$("loading").hidden=true;
  } catch(error) {
    failure(error.status===404?"Twin is turned off on this server":"Unable to load the twin right now. Please refresh and try again.",error.status===404);
    if(error.status!==404){const box=$("error");box.append(el("div",error.message,"error-code"));}
  } finally {$("refresh").disabled=false;}
}
$("refresh").addEventListener("click",load);$("provider").addEventListener("change",load);
$("copy-receipt").addEventListener("click",async()=>{
  const live=$("copy-status");if(!currentReceipt)return;
  try{await navigator.clipboard.writeText(JSON.stringify(currentReceipt,null,2));live.textContent="Receipt JSON copied.";$("copy-receipt").textContent="Copied";setTimeout(()=>$("copy-receipt").textContent="Copy receipt JSON",1600);}
  catch(_){live.textContent="Clipboard access is unavailable in this browser.";}
});
/* --- Chat pane: governed turns, receipts visible, raw model text never shown --- */
const chatSession = (() => {
  try {
    let id = sessionStorage.getItem("twin-chat-session");
    if (!id) { id = crypto.randomUUID ? crypto.randomUUID() : `s-${Date.now()}`; sessionStorage.setItem("twin-chat-session", id); }
    return id;
  } catch (_) { return `s-${Date.now()}`; }
})();
let chatBusy = false;

async function apiPost(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: {Accept: "application/json", "Content-Type": "application/json"},
    body: JSON.stringify(body), cache: "no-store",
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { const b = await response.json(); detail = b.detail || detail; } catch (_) { /* status is enough */ }
    const error = new Error(`HTTP ${response.status}: ${detail}`); error.status = response.status; throw error;
  }
  return response.json();
}

function turnNode(role, text, metaNodes) {
  const li = el("li", undefined, `chat-turn ${role}`);
  const bubble = el("div", undefined, "bubble");
  bubble.append(el("span", text));
  if (metaNodes && metaNodes.length) {
    const meta = el("div", undefined, "chat-meta");
    metaNodes.forEach((n) => meta.append(n));
    bubble.append(meta);
  }
  li.append(bubble);
  return li;
}

function renderTurnReceipt(data) {
  const r = data.receipt;
  const meta = [];
  meta.push(el("span", `${r.backend}${r.model && r.model !== "none" ? ` · ${r.model}` : ""}`));
  meta.push(el("span", `${r.latency_ms} ms`));
  meta.push(el("span", `gate:${r.gate_mode}`, r.gate_dropped.length ? "warn-text" : ""));
  if (r.fallback_used) meta.push(el("span", "FALLBACK", "kind-badge"));
  if (data.degraded) meta.push(el("span", "DEGRADED", "kind-badge"));
  (r.recalled_ids || []).forEach((id) => meta.push(recordLink(id)));
  if (r.proposed_claims && r.proposed_claims.length) {
    const det = el("details", undefined, "turn-receipt");
    const sum = el("summary", undefined, "microcopy");
    sum.textContent = `${r.proposed_claims.length} proposed claim${r.proposed_claims.length === 1 ? "" : "s"}`;
    det.append(sum);
    const ul = document.createElement("ul");
    r.proposed_claims.forEach((p) => {
      const li = el("li");
      li.append(el("span", `${p.claim_type.toUpperCase()} `, "kind-badge"), el("span", p.content, "record-copy"));
      if (p.claim_type !== "decision" || p.attribution !== "user") li.append(el("span", " · not persisted", "muted"));
      ul.append(li);
    });
    det.append(ul);
    meta.push(det);
  }
  if (data.persist_receipt) {
    const pr = data.persist_receipt;
    const span = el("span", `persisted ${pr.persisted_ids.length}${pr.failures.length ? ` · ${pr.failures.length} refused` : ""}`, "microcopy");
    meta.push(span);
    (pr.persisted_ids || []).forEach((id) => meta.push(recordLink(id)));
  }
  const rc = el("details", undefined, "turn-receipt");
  const s = el("summary", undefined, "microcopy");
  s.textContent = `receipt ${shortId(r.receipt_digest)}`;
  const pre = el("pre", JSON.stringify({receipt: r, persist_receipt: data.persist_receipt}, null, 2), "receipt-json");
  rc.append(s, pre);
  meta.push(rc);
  return meta;
}

async function sendChat(evt) {
  evt.preventDefault();
  const input = $("chat-input"), msg = input.value.trim();
  if (!msg || chatBusy) return;
  chatBusy = true; $("chat-send").disabled = true;
  const stream = $("chat-stream");
  stream.append(turnNode("user", msg));
  input.value = "";
  const thinking = turnNode("assistant", "…");
  stream.append(thinking); stream.scrollTop = stream.scrollHeight;
  try {
    const data = await apiPost("/api/jarvis/twin/chat", {
      session_id: chatSession, message: msg,
      persist: $("chat-persist").checked,
    });
    thinking.replaceWith(turnNode("assistant", data.reply, renderTurnReceipt(data)));
    if (data.receipt.context_reset) $("chat-reset").hidden = false;
  } catch (error) {
    thinking.replaceWith(turnNode("assistant",
      error.status === 404 ? "Chat is dark on this server." :
      error.status === 409 ? "A turn is already in flight — wait and retry." :
      error.status === 403 ? "Persist is disabled on this server." :
      `Turn failed: ${error.message}`));
    if (error.status === 404) { $("chat-section").hidden = true; $("chat-disabled").hidden = false; }
  } finally {
    chatBusy = false; $("chat-send").disabled = false; stream.scrollTop = stream.scrollHeight;
    input.focus();
  }
}

async function probeChat() {
  const sessionEl = $("chat-session"); sessionEl.textContent = chatSession;
  try {
    await api(`/api/jarvis/twin/chat/sessions/${encodeURIComponent(chatSession)}/turns`);
    $("chat-section").hidden = false;
    $("chat-form").addEventListener("submit", sendChat);
  } catch (error) {
    if (error.status === 404) $("chat-disabled").hidden = false;
  }
}

document.addEventListener("DOMContentLoaded",()=>{ loadProviders().then(load); probeChat(); });
