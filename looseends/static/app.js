"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const state = { kind: "stale", center: null, detail: null, history: [], cy: null, lists: {} };

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const kepLink = (n) => `<a href="#kep=${n}" data-kep="${n}">KEP-${n}</a>`;
const people = (list) => (list || []).map((o) => "@" + esc(o.handle || o)).join(", ") || "no listed author";
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

/* Minimal, safe markdown: escape first, then bold, italics, lists, quotes, KEP links. */
function md(text) {
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)\*(?!\s)(.+?)\*(?=\W|$)/g, "$1<em>$2</em>")
    .replace(/KEP-(\d+)/g, (_, n) => kepLink(n));
  const out = [];
  let list = null;
  for (const line of String(text || "").split("\n")) {
    const li = line.match(/^\s*[-*]\s+(.*)/);
    if (li) { (list ||= []).push(`<li>${inline(li[1])}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    if (/^\s*>\s?/.test(line)) out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  return out.join("");
}

/* ---------- Summary and loose-end lists ---------- */
const EXPLAIN = {
  stale: "Open decisions that still depend on, or cite, a decision that was replaced, withdrawn or rejected.",
  broken_promises: "Decisions that promised a stage (alpha, beta, stable) by a release that has already shipped.",
  orphans: "Open decisions whose every author has had no involvement for 4 or more releases.",
};

async function loadSummary() {
  const s = await api("/api/summary");
  $("#status").innerHTML = `Kubernetes at ${esc(s.current_release)}, ${s.decisions} decisions: ` +
    `<strong>${s.stale}</strong> open ones rest on retired decisions, <strong>${s.broken_promises}</strong> release targets passed unmet, ` +
    `and <strong>${s.orphans}</strong> have owners who went quiet.`;
  for (const k of ["stale", "broken_promises", "orphans"]) $(`#n-${k}`).textContent = s[k];
  $("#mode").textContent = s.llm ? `Answering with ${s.model}` : "Answering with the built-in planner (no API key set)";
}

function dedupeByNumber(items) {
  const seen = new Map();
  for (const it of items) if (!seen.has(it.number) || it.releases_late > seen.get(it.number).releases_late) seen.set(it.number, it);
  return [...seen.values()];
}

async function loadKind(kind) {
  state.kind = kind;
  $$("#kinds button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.kind === kind)));
  $("#explain").textContent = EXPLAIN[kind];
  let items = state.lists[kind] || (state.lists[kind] = await api(`/api/loose-ends/${kind}`));
  if (kind === "broken_promises") items = dedupeByNumber(items);
  const ol = $("#items");
  ol.className = `items k-${kind}`;
  ol.innerHTML = items.map((it) => {
    let why, center = it.number;
    if (kind === "stale") {
      why = `<b>Rests on KEP-${it.old_number}</b>, ${esc(it.old_status)}${it.replaced_by.length ? `, replaced by KEP-${it.replaced_by[0]}` : ""}`;
      center = it.old_number;
    } else if (kind === "broken_promises") {
      why = `<b>Promised ${esc(it.promised)} by v1.${it.due}</b>, still ${esc(it.reached || "not started")}, ${it.releases_late} releases late`;
    } else {
      why = `<b>Authors quiet ${it.releases_quiet} releases</b>: ${people(it.authors)}`;
    }
    return `<li><button type="button" data-center="${center}" data-detail="${it.number}">
      <span class="num">KEP-${it.number}</span><span class="ttl">${esc(it.title)}</span><span class="why">${why}</span></button></li>`;
  }).join("") || `<li class="clean">Nothing here. Every decision in this view is in good shape.</li>`;
}

/* ---------- Thread view (graph) ---------- */
function nodeClass(n, center) {
  const cls = [n.retired ? "retired" : (n.active ? "open" : "done")];
  if (n.number === center) cls.push("center");
  return cls.join(" ");
}

function renderGraph(r, highlight) {
  const g = r.graph;
  const short = (t) => (t.length > 24 ? t.slice(0, 22) + "…" : t);
  const els = g.nodes.map((n) => ({ data: { id: `d${n.number}`, kep: n.number, label: `KEP-${n.number}\n${short(n.title)}`,
    level: n.number === g.center ? 10 : (g.hops[n.number] ? 9 - 2 * g.hops[n.number] : 7) }, classes: nodeClass(n, g.center) + (n.number === highlight ? " hl" : "") }));
  const gates = [...new Set(g.gates.map((x) => x.gate))];
  gates.forEach((name) => els.push({ data: { id: `g${name}`, label: name, level: 8 }, classes: "gate" }));
  g.gates.forEach((x, i) => {
    if (!els.find((e) => e.data.id === `d${x.other}`)) return;
    els.push({ data: { id: `ge${i}a`, source: `d${g.center}`, target: `g${x.gate}` }, classes: "gated" });
    els.push({ data: { id: `ge${i}b`, source: `d${x.other}`, target: `g${x.gate}` }, classes: "gated" });
  });
  const pathEdges = new Set();
  for (const a of r.affected) for (let i = 0; i + 1 < a.path.length; i++) pathEdges.add(`${a.path[i]}>${a.path[i + 1]}`);
  g.edges.forEach((e, i) => els.push({ data: { id: `e${i}`, source: `d${e.source}`, target: `d${e.target}` },
    classes: { DEPENDS_ON: "dep", CITES: "cite", REPLACES: "repl", LIKELY_RELATED: "sugg" }[e.type] + (pathEdges.has(`${e.source}>${e.target}`) ? " path" : "") }));

  const ink = css("--ink"), closed = css("--closed"), stale = css("--stale"), sugg = css("--sugg"), text = css("--text"),
    sheet = css("--sheet"), late = css("--late");
  if (state.cy) state.cy.destroy();
  state.cy = cytoscape({
    container: $("#cy"), elements: els, minZoom: 0.3, maxZoom: 2.5,
    layout: { name: "cose", animate: false, randomize: false, padding: 24, nodeDimensionsIncludeLabels: true,
      idealEdgeLength: () => 110, nodeRepulsion: () => 14000, edgeElasticity: () => 80, gravity: 0.6, numIter: 1500 },
    style: [
      { selector: "node", style: { "label": "data(label)", "text-wrap": "wrap", "text-max-width": 104, "font-family": "Public Sans, sans-serif",
        "font-size": 11.5, "color": text, "text-valign": "bottom", "text-margin-y": 6, "width": 18, "height": 18,
        "background-color": closed, "text-background-color": sheet, "text-background-opacity": 0.85, "text-background-padding": 2 } },
      { selector: "node.open", style: { "background-color": ink } },
      { selector: "node.retired", style: { "background-opacity": 0, "border-width": 2.5, "border-style": "dashed", "border-color": stale } },
      { selector: "node.center", style: { "width": 30, "height": 30, "font-size": 12, "font-weight": 600 } },
      { selector: "node.hl", style: { "border-width": 4, "border-color": late, "border-style": "solid" } },
      { selector: "node.gate", style: { "shape": "diamond", "width": 12, "height": 12, "background-color": late, "font-size": 9.5, "color": late } },
      { selector: "edge", style: { "width": 1.5, "curve-style": "bezier", "line-color": closed, "target-arrow-color": closed,
        "target-arrow-shape": "triangle", "arrow-scale": 0.8 } },
      { selector: "edge.dep", style: { "line-color": ink, "target-arrow-color": ink, "opacity": 0.75 } },
      { selector: "edge.cite", style: { "width": 1, "opacity": 0.6 } },
      { selector: "edge.repl", style: { "line-color": stale, "target-arrow-color": stale, "width": 2 } },
      { selector: "edge.sugg", style: { "line-color": sugg, "line-style": "dashed", "target-arrow-shape": "none" } },
      { selector: "edge.gated", style: { "line-color": late, "line-style": "dotted", "target-arrow-shape": "none", "width": 1.2, "opacity": 0.7 } },
      { selector: "edge.path", style: { "width": 3, "opacity": 1 } },
      { selector: "node:active", style: { "overlay-opacity": 0 } },
    ],
  });
  if (state.cy.zoom() > 1.1) { state.cy.zoom(1.1); state.cy.center(); }  // small graphs stay readable
  state.cy.on("tap", "node", (e) => { const k = e.target.data("kep"); if (k) select(k, k); });
}

async function select(center, detail = center) {
  state.center = Number(center); state.detail = Number(detail);
  history.replaceState(null, "", `#kep=${center}${detail !== center ? `&detail=${detail}` : ""}`);
  $$(".items button").forEach((b) => b.setAttribute("aria-current", String(Number(b.dataset.detail) === state.detail)));
  try {
    const [r, d] = await Promise.all([api(`/api/ripple/${center}`), api(`/api/decision/${detail}`)]);
    const open = r.affected.filter((a) => a.open).length;
    $("#stage-title").textContent = `What depends on KEP-${r.decision.number} ${r.decision.title}`;
    const bits = [`${open} open decision${open === 1 ? "" : "s"} would be affected if it changed`];
    if (r.shared_gate.length) bits.push(`${r.shared_gate.length} share${r.shared_gate.length === 1 ? "s" : ""} a feature gate with it`);
    if (r.suggested.length) bits.push(`${r.suggested.length} more look related but nobody declared it`);
    $("#stage-sub").textContent = bits.join("; ") + ".";
    renderGraph(r, state.detail !== state.center ? state.detail : null);
    renderDecision(d);
  } catch (err) {
    $("#stage-sub").textContent = `Couldn't load KEP-${center}: ${err.message}`;
  }
}

/* ---------- Decision panel ---------- */
function renderDecision(d) {
  const status = d.retired ? "retired" : d.open ? "open" : "";
  const flags = (d.loose_ends || []).map((f) => {
    const cls = /releases late/.test(f) ? "late" : /quiet/.test(f) ? "quiet" : "stale";
    return `<p class="flag ${cls}">${md(f).replace(/^<p>|<\/p>$/g, "")}</p>`;
  }).join("") || `<p class="clean">No loose ends found for this decision.</p>`;
  const force = d.in_force && d.in_force.in_force !== d.number
    ? `<h3>In force today</h3><p class="force">${kepLink(d.in_force.in_force)} ${esc(d.in_force.title)}<br><span class="meta">${d.in_force.chain.map(kepLink).join(" replaced by ")}</span></p>` : "";
  const experts = (d.experts || []).map((p) => `<li><span class="who">@${esc(p.handle)}</span> <span class="meta">active v1.${p.last_active}</span>
    <span class="why">${p.reasons.map(esc).join("; ")}</span></li>`).join("") || `<li class="clean">No active people found nearby.</li>`;
  const linkList = (label, arr) => arr && arr.length ? `<p class="meta">${label} ${arr.map(kepLink).join(", ")}</p>` : "";
  $("#p-decision").innerHTML = `
    <div class="dhead"><h2>KEP-${d.number}</h2><span class="chip ${status}">${esc(d.status)}${d.stage ? `, ${esc(d.stage)}` : ""}</span></div>
    <p class="dtitle">${esc(d.title)}</p>
    <p class="meta">Owned by ${esc(d.team)}, last worked on in v1.${d.last_release ?? "?"}. Authors: ${people(d.authors)}.</p>
    <h3>Loose ends</h3>${flags}
    ${force}
    <h3>Who could take over</h3><ul class="people">${experts}</ul>
    <h3>Links</h3>${linkList("Depends on", d.depends_on)}${linkList("Depended on by", d.depended_on_by)}${linkList("Replaced by", d.replaced_by)}
    ${d.feature_gates.length ? `<p class="meta">Feature gates: ${d.feature_gates.map(esc).join(", ")}</p>` : ""}
    <p class="meta"><a href="${esc(d.url)}" target="_blank" rel="noopener">Read the proposal on GitHub</a></p>
    ${d.summary ? `<h3>Summary</h3><p class="summary">${esc(d.summary)}</p>` : ""}
    <div class="actions">
      <button class="primary" type="button" data-act="draft">Draft a follow-up</button>
      <button class="btn" type="button" data-act="ask">Ask about this decision</button>
    </div>`;
}

/* ---------- Panels ---------- */
function showPanel(name) {
  $$("#panels button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.panel === name)));
  $$(".panel .pane").forEach((p) => (p.hidden = p.id !== `p-${name}`));
  if (name === "followups") loadFollowups();
  if (name === "benchmark") loadBenchmark();
}

async function askQuestion(q) {
  q = q.trim();
  const err = $("#ask-err");
  if (!q) { err.textContent = "Type a question first."; err.hidden = false; return; }
  err.hidden = true;
  showPanel("ask");
  $("#starters").hidden = true;
  const chat = $("#chat");
  chat.insertAdjacentHTML("beforeend", `<div class="msg q">${esc(q)}</div><div class="msg a thinking">Following the threads…</div>`);
  const slot = chat.lastElementChild;
  slot.scrollIntoView({ block: "end" });
  try {
    const res = await api("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, history: state.history.slice(-6) }) });
    state.history.push({ role: "user", content: q }, { role: "assistant", content: res.answer });
    const calls = res.trace.map((t) => `<li><code>${esc(t.tool)}(${esc(Object.entries(t.args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : v}`).join(", "))})</code>${t.ok === false ? ` failed: ${esc(t.error)}` : ""}</li>`).join("");
    slot.className = "msg a";
    slot.innerHTML = md(res.answer) + (calls ? `<details class="trace"><summary>How it answered (${res.trace.length} graph call${res.trace.length === 1 ? "" : "s"})</summary><ul>${calls}</ul></details>` : "");
    if (res.trace.some((t) => t.tool === "draft_followup" && t.ok)) refreshFollowupCount();
  } catch (e) {
    slot.className = "msg a";
    slot.innerHTML = `<p class="err">The question didn't go through: ${esc(e.message)}. Check the server is running and try again.</p>`;
  }
  slot.scrollIntoView({ block: "end" });
}

async function refreshFollowupCount() {
  const list = await api("/api/followups");
  const open = list.filter((f) => f.status === "draft").length;
  $("#n-fu").textContent = open ? open : "";
  return list;
}

async function loadFollowups() {
  const list = await refreshFollowupCount();
  $("#p-followups").innerHTML = `<p class="note">Follow-ups the agent drafted. They are stored in the graph and nothing is sent until a person approves them.</p>` +
    (list.map((f) => `<div class="fu"><div class="head"><strong>${esc(f.id)}: ${kepLink(f.kep)} to @${esc(f.assignee)}</strong><span class="chip ${esc(f.status)}">${esc(f.status)}</span></div>
      <p>${md(f.reason).replace(/^<p>|<\/p>$/g, "")}</p><div class="msgtext">${esc(f.message)}</div>
      ${f.status === "draft" ? `<div class="actions"><button class="primary" data-fu="${esc(f.id)}" data-status="approved">Approve</button><button class="btn" data-fu="${esc(f.id)}" data-status="dismissed">Dismiss</button></div>` : ""}</div>`).join("")
      || `<p class="clean">No follow-ups yet. Open a decision and choose "Draft a follow-up", or ask the agent to follow up on one.</p>`);
}

async function loadBenchmark() {
  const pane = $("#p-benchmark");
  if (pane.dataset.done) return;
  try {
    const b = await api("/api/benchmark");
    const methods = Object.entries(b.methods).sort((x, y) => y[1]["recall@10"] - x[1]["recall@10"]);
    const best = methods[0][0];
    const label = (name) => name.replace(/^graph: /, "Graph: ").replace(/^hybrid: /, "Hybrid: ").replace(/^text \(TF-IDF document search\)/, "Text search alone");
    pane.innerHTML = `<h3 style="margin-top:0">Finding links nobody wrote down</h3>
      <p class="note">We hid all ${b.hidden_links} declared links between ${b.sources} decisions, then asked each method to find them again among ${b.candidates_per_source} candidates. The bars show the share of hidden links found in each method's top 10.</p>
      <ul class="bars">${methods.map(([name, m]) => `<li class="${name === best ? "best" : ""}" tabindex="0">
        <span>${esc(label(name))}</span><span class="track"><span class="bar" style="display:block;width:${(m["recall@10"] * 100).toFixed(1)}%"></span></span>
        <span class="val">${Math.round(m["recall@10"] * 100)}%</span>
        <span class="tip">Top 1: ${Math.round(m["recall@1"] * 100)}%, top 5: ${Math.round(m["recall@5"] * 100)}%, top 10: ${Math.round(m["recall@10"] * 100)}%, MRR ${m.mrr.toFixed(2)}</span></li>`).join("")}</ul>
      <table class="bench"><caption class="sr">Benchmark results by method</caption><thead><tr><th scope="col">Method</th><th scope="col">Top 1</th><th scope="col">Top 5</th><th scope="col">Top 10</th><th scope="col">MRR</th></tr></thead>
      <tbody>${methods.map(([name, m]) => `<tr><td>${esc(label(name))}</td><td>${m["recall@1"].toFixed(2)}</td><td>${m["recall@5"].toFixed(2)}</td><td>${m["recall@10"].toFixed(2)}</td><td>${m.mrr.toFixed(2)}</td></tr>`).join("")}</tbody></table>
      <p class="note">Graph weights were fixed before testing. The hybrid's text weight was chosen by 2-fold cross-validation, so it is never tuned on the decisions it is scored on. Reproduce with <code>python -m looseends.benchmark</code>.</p>`;
    pane.dataset.done = "1";
  } catch (e) {
    pane.innerHTML = `<p class="clean">Benchmark results aren't available: ${esc(e.message)}</p>`;
  }
}

/* ---------- Search ---------- */
let searchTimer;
async function runSearch(q) {
  const ul = $("#results");
  q = q.trim();
  if (!q) { ul.hidden = true; return; }
  const direct = q.match(/^(?:kep[-\s]*)?(\d{1,5})$/i);
  const res = direct ? { results: [{ number: Number(direct[1]), title: "Open this decision" }] } : await api(`/api/search?q=${encodeURIComponent(q)}`);
  ul.innerHTML = res.results.map((r) => `<li><button type="button" data-kep="${r.number}">KEP-${r.number} ${esc(r.title)}</button></li>`).join("")
    || `<li class="clean" style="padding:6px 8px">No decisions match "${esc(q)}".</li>`;
  ul.hidden = false;
}

/* ---------- Wiring ---------- */
document.addEventListener("click", (e) => {
  const kind = e.target.closest("#kinds button");
  if (kind) return loadKind(kind.dataset.kind);
  const item = e.target.closest(".items button");
  if (item) { showPanel("decision"); return select(Number(item.dataset.center), Number(item.dataset.detail)); }
  const panel = e.target.closest("#panels button");
  if (panel) return showPanel(panel.dataset.panel);
  const kep = e.target.closest("[data-kep]");
  if (kep) { e.preventDefault(); $("#results").hidden = true; $("#q").value = ""; showPanel("decision"); return select(Number(kep.dataset.kep)); }
  const act = e.target.closest("[data-act]");
  if (act && act.dataset.act === "draft") return askQuestion(`Draft a follow-up for KEP-${state.detail}`);
  if (act && act.dataset.act === "ask") { showPanel("ask"); $("#ask").value = `What breaks if KEP-${state.detail} changes?`; return $("#ask").focus(); }
  const starter = e.target.closest(".starters button");
  if (starter) return askQuestion(starter.textContent);
  const fu = e.target.closest("[data-fu]");
  if (fu) return api(`/api/followups/${fu.dataset.fu}`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: fu.dataset.status }) }).then(loadFollowups);
  if (!e.target.closest(".search")) $("#results").hidden = true;
});
$("#askform").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#ask"); askQuestion(t.value); t.value = ""; });
$("#ask").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#askform").requestSubmit(); } });
$("#ask").addEventListener("input", () => ($("#ask-err").hidden = true));
$("#search").addEventListener("submit", (e) => { e.preventDefault(); const first = $("#results button"); if (first) first.click(); });
$("#q").addEventListener("input", (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch(e.target.value), 180); });
$("#q").addEventListener("keydown", (e) => { if (e.key === "Escape") $("#results").hidden = true; });

(async function start() {
  await loadSummary();
  await loadKind("stale");
  refreshFollowupCount();
  const m = location.hash.match(/kep=(\d+)(?:&detail=(\d+))?/);
  if (m) select(Number(m[1]), Number(m[2] || m[1]));
  else select(583, 5547);
})();
