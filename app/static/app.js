"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (text) => DOMPurify.sanitize(marked.parse(text || "", { gfm: true }));
// localStorage throws in some privacy modes; a lost preference must not break the page.
const store = {
  get(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* preference not persisted */ } },
};

const api = async (url, opts) => {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Request failed (${res.status})`);
  return res.json();
};

const EXAMPLES = [
  "Does semaglutide reduce heart failure events in patients with preserved ejection fraction?",
  "What is the evidence that gut microbiome composition predicts response to anti-PD-1 immunotherapy?",
  "Which mechanisms explain resistance to KRAS G12C inhibitors, and what combinations are being tested?",
  "Is TREM2 agonism a viable therapeutic strategy in Alzheimer's disease?",
];
const HINTS = {
  quick: "Quick answer: a few targeted searches and a short sourced answer, usually under a minute.",
  deep: "Deep research: a formal search strategy across databases, full-text reading, and a structured evidence report. Takes several minutes and uses considerably more of your Claude usage.",
};
const TOOL_LABELS = {
  search_pubmed: "PubMed", search_europe_pmc: "Europe PMC", search_openalex: "OpenAlex",
  search_clinical_trials: "ClinicalTrials.gov", get_citing_papers: "Citing papers", get_paper_details: "Paper details",
  get_full_text: "Full text", record_search_strategy: "Search strategy", Skill: "Skill", Read: "Skill reference",
};
const PANES_ORDER = ["sources", "outline", "strategy", "activity", "audit"];
const STRONG = new Set(["Meta-analysis", "Systematic review", "RCT", "Guideline"]);
const VERDICTS = { contradicted: "Contradicted by source", overstated: "Overstated", wrong_number: "Number differs", off_topic: "Source is off topic", not_in_abstract: "Not found in abstract" };

const START_TAB = PANES_ORDER.find((t) => t === new URLSearchParams(location.search).get("tab")) || "sources";
let current = null;          // the open thread
const tabs = {};             // turn id -> selected tab
const drafts = {};           // turn id -> streaming text

// ------------------------------------------------------------------ sidebar

async function loadHistory() {
  const threads = await api("/api/threads");
  $("#history").innerHTML = threads.map((t) => `
    <div class="hist ${current && current.id === t.id ? "active" : ""}">
      <button class="open" data-id="${t.id}" title="${esc(t.title)}">${esc(t.title)}</button>
      <button class="del" data-id="${t.id}" aria-label="Delete this search">×</button>
    </div>`).join("") || `<div class="empty">No searches yet.</div>`;
}

$("#history").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  if (btn.classList.contains("open")) return openThread(btn.dataset.id);
  if (btn.dataset.armed) {
    await api(`/api/threads/${btn.dataset.id}`, { method: "DELETE" }).catch((err) => alert(err.message));
    if (current && current.id === btn.dataset.id) showHome();
    return loadHistory();
  }
  btn.dataset.armed = "1"; btn.textContent = "Delete?"; btn.style.visibility = "visible";
  setTimeout(() => { delete btn.dataset.armed; btn.textContent = "×"; btn.style.visibility = ""; }, 3000);
});

function showHome() {
  current = null;
  outlineTurnId = null;
  toggleOutline(false);
  history.replaceState(null, "", location.pathname);
  $("#home").hidden = false; $("#thread").hidden = true;
  $("#question").focus();
  loadHistory();
}
$("#new-search").addEventListener("click", showHome);

// ------------------------------------------------------------------ asking

async function start(body) {
  const { thread_id, turn_id } = await api("/api/research", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  await openThread(thread_id);
  $(`#turn-${turn_id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

$("#ask").addEventListener("submit", (e) => {
  e.preventDefault();
  start({
    question: $("#question").value,
    mode: $("input[name=mode]:checked").value,
    hypotheses: $("#hypotheses").checked,
    audit: $("#audit").checked,
    year_from: Number($("#year-from").value) || null,
    year_to: Number($("#year-to").value) || null,
  }).catch((err) => alert(err.message));
});
$("#follow").addEventListener("submit", (e) => {
  e.preventDefault();
  const question = $("#follow-q").value;
  $("#follow-q").value = "";
  start({ question, thread_id: current.id, mode: "quick", audit: $("#audit").checked }).catch((err) => alert(err.message));
});
for (const el of [$("#question"), $("#follow-q")]) {
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) el.form.requestSubmit();
  });
}
const syncHint = () => { $("#mode-hint").textContent = HINTS[$("input[name=mode]:checked").value]; };
document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", syncHint));
$("#examples").innerHTML = EXAMPLES.map((q) => `<button type="button">${esc(q)}</button>`).join("");
$("#examples").addEventListener("click", (e) => {
  if (e.target.tagName === "BUTTON") { $("#question").value = e.target.textContent; $("#question").focus(); }
});

// ------------------------------------------------------------------ thread view

async function openThread(id) {
  current = await api(`/api/threads/${id}`);
  history.replaceState(null, "", `#${id}`);
  $("#home").hidden = true; $("#thread").hidden = false;
  $("#turns").innerHTML = "";
  for (const turn of current.turns) {
    const el = document.createElement("section");
    el.className = "turn"; el.id = `turn-${turn.id}`;
    $("#turns").append(el);
    renderTurn(turn);
    if (turn.live) listen(turn);
  }
  syncFollow();
  loadHistory();
}

function syncFollow() {
  const busy = current.turns.some((t) => t.live);
  $("#follow").hidden = busy || !current.turns.some((t) => t.status === "done");
}

function listen(turn) {
  let seen = 0;
  const threadId = current.id;
  const connect = () => {
    const es = new EventSource(`/api/runs/${turn.id}/events?start=${seen}`);
    es.onmessage = (msg) => {
      seen += 1;
      if (!current || current.id !== threadId) return es.close();
      const ev = JSON.parse(msg.data);
      if (ev.type === "delta") { drafts[turn.id] = (drafts[turn.id] || "") + ev.text; return scheduleDraft(turn); }
      if (ev.type === "text_reset") { drafts[turn.id] = ""; return; }
      if (ev.type === "tool" || ev.type === "tool_result" || ev.type === "status") {
        turn.activity.push({ ...ev, t: Math.round((Date.now() / 1000 - turn.started) * 10) / 10 });
        if (ev.type === "status") turn.statusText = ev.text;
      } else if (ev.type === "strategy") turn.strategy = ev.strategy;
      else if (ev.type === "final") { Object.assign(turn, ev.turn); tabs[turn.id] = "sources"; }
      else if (ev.type === "audit") turn.audit = ev.audit;
      else if (ev.type === "error") { turn.status = turn.status === "done" ? "done" : "error"; turn.error = ev.message; }
      else if (ev.type === "done") { es.close(); turn.live = false; syncFollow(); loadHistory(); }
      renderTurn(turn);
    };
    es.onerror = () => { es.close(); if (turn.live) setTimeout(connect, 1500); };
  };
  connect();
}

let draftTimer = null;
function scheduleDraft(turn) {
  if (draftTimer) return;
  draftTimer = setTimeout(() => {
    draftTimer = null;
    const box = $(`#turn-${turn.id} .draft`);
    if (box && turn.status === "running") box.innerHTML = md(drafts[turn.id]);
  }, 150);
}

// ------------------------------------------------------------------ rendering

const EXPORT_FORMATS = [
  ["pdf", "PDF", "Formatted PDF with clickable citations"],
  ["md", "Markdown", "Markdown with every citation linked to the paper"],
  ["html", "HTML", "Single self-contained page"],
  ["bib", "BibTeX", "Reference list for a citation manager"],
  ["ris", "RIS", "Reference list for EndNote, Zotero or Mendeley"],
];

function exportBar(turn, labelled = false) {
  const base = `/api/threads/${current.id}/turns/${turn.id}/export`;
  const links = EXPORT_FORMATS.map(([f, label, title]) =>
    `<a class="btn small" href="${base}?format=${f}" title="${esc(title)}" data-format="${f}">${label}</a>`).join("");
  const preview = `<a class="btn small" href="/api/threads/${current.id}/turns/${turn.id}/print" target="_blank" rel="noopener">Print view</a>`;
  return labelled ? `<span class="actions-label">Export this answer</span>${links}${preview}` : links;
}

function badges(ref) {
  const out = [];
  if (ref.retracted) out.push(`<span class="badge bad">Retracted</span>`);
  if (ref.design && ref.design !== "Preprint") out.push(`<span class="badge ${STRONG.has(ref.design) ? "strong" : ""}">${esc(ref.design)}</span>`);
  if (ref.preprint) out.push(`<span class="badge warn">Preprint, not peer reviewed</span>`);
  for (const s of ref.species || []) out.push(`<span class="badge">${esc(s)}</span>`);
  if (ref.cited_by != null) out.push(`<span class="badge">Cited by ${Number(ref.cited_by).toLocaleString()}</span>`);
  if (ref.full_text_read) out.push(`<span class="badge">Full text read</span>`);
  if (ref.from_memory) out.push(`<span class="badge warn">Not retrieved in this search</span>`);
  return out.join("");
}
const byline = (ref) => {
  const a = ref.authors || [];
  return `${esc(a.slice(0, 3).join(", "))}${a.length > 3 ? " et al." : ""}${a.length ? " · " : ""}${esc(ref.journal || "")} ${esc(ref.year || "")}`;
};

function sourcesPane(turn) {
  if (!turn.references.length) return `<div class="empty">${turn.status === "running" ? "Sources appear here once the report is written." : "This answer cites no sources."}</div>`;
  return turn.references.map((r) => `
    <div class="src" id="t${turn.id}-ref-${r.n}">
      <span class="num">${r.n}</span>
      <div class="body">
        <a class="title" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title || r.key)}</a>
        <div class="by">${byline(r)} · ${esc(r.key)}</div>
        <div>${badges(r)}</div>
        ${r.abstract ? `<details><summary>${r.kind === "trial" ? "Registry entry" : "Abstract"}</summary><p class="abs">${esc(r.abstract)}</p></details>` : ""}
      </div>
    </div>`).join("");
}

function strategyPane(turn) {
  const s = turn.strategy;
  if (!s) return `<div class="empty">Deep research records a formal search strategy here, with ready-to-paste queries for Web of Science, Scopus, Embase, CNKI and Wanfang.</div>`;
  const fw = Object.entries(s.framework || {}).map(([k, v]) => `<div class="kv"><b>${esc(k)}:</b> ${esc(v)}</div>`).join("");
  const dbs = s.databases.map((d, i) => `
    <div class="db">
      <div class="db-head">
        <strong>${esc(d.database)}</strong>
        ${d.hits != null ? `<span class="badge strong">${Number(d.hits).toLocaleString()} hits</span>` : ""}
        ${d.error ? `<span class="badge bad">query failed</span>` : ""}
        <button class="btn small" data-copy="${i}">Copy</button>
        <a class="btn small" href="${esc(d.url)}" target="_blank" rel="noopener">Open</a>
      </div>
      <pre>${esc(d.query)}</pre>
      <div class="note">${esc(d.note)}</div>
    </div>`).join("");
  return `${fw ? `<div class="db">${fw}</div>` : ""}${dbs}`;
}

function activityPane(turn) {
  const rows = turn.activity.filter((a) => a.type !== "tool_result" || a.summary).map((a) => {
    if (a.type === "status") return `<div class="act"><span class="t">${a.t}s</span><span class="what"><b>${esc(a.text)}</b></span></div>`;
    const label = TOOL_LABELS[a.name] || a.name;
    if (a.type === "tool_result") return `<div class="act ${a.is_error ? "err" : ""}"><span class="t"></span><span class="what">↳ ${esc(a.summary)}</span></div>`;
    const i = a.input || {};
    const detail = i.query || i.target || i.id || i.pmid || (i.pmids || []).join(", ") || (i.concepts ? i.concepts.map((c) => c.label).join(" · ") : "");
    return `<div class="act"><span class="t">${a.t}s</span><span class="what">${esc(label)} <span class="q">${esc(detail)}</span></span></div>`;
  });
  const log = (turn.search_log || []).length ? `<div class="kv" style="margin:0 0 8px">${turn.search_log.length} searches run; ${turn.records_seen ?? "?"} distinct records retrieved in this thread.</div>` : "";
  return log + (rows.join("") || `<div class="empty">No activity yet.</div>`);
}

function auditPane(turn) {
  const a = turn.audit;
  if (!a) return `<div class="empty">${turn.live ? "The audit runs after the report is written." : "No audit was run for this answer."}</div>`;
  if (a.error) return `<div class="banner bad">The audit could not be completed: ${esc(a.error)}</div>`;
  if (!a.issues.length) return `<div class="banner" style="background:var(--good-soft);color:var(--good)">An independent pass checked ${a.citations_checked} citations against their sources and found no problems.</div>`;
  return `<div class="kv" style="margin-bottom:10px">${a.citations_checked} citations checked, ${a.issues.length} flagged.</div>` + a.issues.map((it) => `
    <div class="issue">
      <span class="badge warn">${esc(VERDICTS[it.verdict] || it.verdict)}</span>
      ${it.n ? `<a class="cite" href="#ref-${it.n}">${it.n}</a>` : esc(it.citation)}
      <blockquote>${esc(it.claim)}</blockquote>
      <div>${esc(it.explanation)}</div>
    </div>`).join("");
}

const PANES = { sources: sourcesPane, outline: outlinePane, strategy: strategyPane, activity: activityPane, audit: auditPane };

function renderTurn(turn) {
  const el = $(`#turn-${turn.id}`);
  if (!el) return;
  const tab = tabs[turn.id] || (turn.status === "running" ? "activity" : START_TAB);
  const issues = turn.audit && turn.audit.issues ? turn.audit.issues.length : 0;
  const chips = [
    turn.mode === "deep" ? "Deep research" : "Quick answer",
    turn.hypotheses ? "with hypotheses" : "",
    turn.usage ? `${turn.usage.seconds}s` : "",
    turn.usage && turn.usage.cost_usd != null ? `≈ $${turn.usage.cost_usd.toFixed(2)} API-equivalent` : "",
  ].filter(Boolean).map((c) => `<span class="badge">${esc(c)}</span>`).join("");

  const problems = (turn.problems || []).length ? `
    <div class="banner warn"><b>Citation notes</b><ul>${turn.problems.map((p) => `<li><b>${esc(p.citation)}</b>: ${esc(p.problem)}</li>`).join("")}</ul></div>` : "";
  const error = turn.error && turn.status !== "done" ? `<div class="banner bad">${esc(turn.error)}</div>` : "";

  let body;
  if (turn.status === "running") {
    const last = [...turn.activity].reverse().find((a) => a.type === "tool");
    const now = last ? `${TOOL_LABELS[last.name] || last.name}${last.input && last.input.query ? `: ${last.input.query}` : ""}` : (turn.statusText || "Starting");
    body = `<div class="live"><span class="spin"></span><span>${esc(now).slice(0, 200)}</span>
              <button class="btn small" data-stop="${turn.id}" style="margin-left:auto">Stop</button></div>
            <div class="draft">${md(drafts[turn.id] || "")}</div>`;
  } else {
    body = turn.report ? md(turn.report) : `<div class="empty">No report was produced.</div>`;
  }
  const exports = turn.status === "done" ? exportBar(turn) : "";

  el.innerHTML = `
    <div class="turn-head"><h2>${esc(turn.question)}</h2><div class="meta">${chips}${exports}</div></div>
    <div class="cols">
      <div>${error}${problems}<article class="report">${body}</article>
        ${turn.status === "done" ? `<div class="actions">${exportBar(turn, true)}</div>` : ""}</div>
      <div class="side">
        <div class="tabs" role="tablist">
          ${[["sources", `Sources${turn.references.length ? ` (${turn.references.length})` : ""}`], ["outline", "Outline"], ["strategy", "Search strategy"], ["activity", "Activity"], ["audit", `Audit${issues ? ` (${issues})` : ""}`]]
            .map(([k, label]) => `<button role="tab" data-tab="${k}" aria-selected="${k === tab}">${label}</button>`).join("")}
        </div>
        <div class="pane">${PANES[tab](turn)}</div>
      </div>
    </div>`;

  // Turn the numbered links into citation chips, and mark the ones the audit questioned.
  const flagged = new Set(((turn.audit && turn.audit.issues) || []).map((i) => i.n));
  el.querySelectorAll('a[href^="#ref-"]').forEach((a) => {
    const n = Number(a.getAttribute("href").slice(5));
    a.className = `cite${flagged.has(n) ? " flagged" : ""}`;
    a.dataset.n = n; a.textContent = n;
  });
  el.querySelectorAll(".report a:not(.cite)").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
  markSections(el);
  scheduleOutline();
}

// ------------------------------------------------------------------ outline

const OUTLINE_PREF = "bdr.outline-pref";
const LONG_ANSWER_WORDS = 900;
let outlinePref = store.get(OUTLINE_PREF, "auto");
let outlineOpen = false;
let outlineEntries = [];
let outlineTurnId = null;

// Headings, plus the bold lead-ins the reports use to open a paragraph.
function markSections(turnEl) {
  const report = turnEl.querySelector(".report");
  if (!report) return;
  let n = 0;
  for (const node of report.querySelectorAll("h1, h2, h3, h4, p > strong:first-child")) {
    const lead = node.tagName === "STRONG";
    const anchor = lead ? node.parentElement : node;
    if (lead && anchor.firstChild !== node) continue;
    const text = node.textContent.trim().replace(/[.:,;]+$/, "");
    if (text.length < 4 || text.length > 140) continue;
    if (!anchor.id) anchor.id = `${turnEl.id}-s${++n}`;
    anchor.dataset.outline = text;
    anchor.dataset.level = lead ? 4 : Number(node.tagName[1]);
  }
}

function activeTurnEl() {
  const els = [...document.querySelectorAll(".turn")];
  if (els.length < 2) return els[0] || null;
  const mid = window.innerHeight / 2;
  return els.find((el) => {
    const r = el.getBoundingClientRect();
    return r.top <= mid && r.bottom >= mid;
  }) || els.reduce((best, el) => {
    const d = Math.abs(el.getBoundingClientRect().top);
    return !best || d < Math.abs(best.getBoundingClientRect().top) ? el : best;
  }, null);
}

function readOutline(turnEl) {
  if (!turnEl) return [];
  return [...turnEl.querySelectorAll("[data-outline]")].map((el) => ({
    id: el.id, text: el.dataset.outline, level: Number(el.dataset.level),
  }));
}

function currentSectionId() {
  let here = null;
  for (const entry of outlineEntries) {
    const el = document.getElementById(entry.id);
    if (el && el.getBoundingClientRect().top <= 140) here = entry.id;
  }
  return here || (outlineEntries[0] && outlineEntries[0].id);
}

function outlineListHTML(entries, filter = "") {
  const needle = filter.trim().toLowerCase();
  const shown = needle ? entries.filter((e) => e.text.toLowerCase().includes(needle)) : entries;
  if (!shown.length) {
    return `<div class="outline-empty">${entries.length ? "No section matches." : "This answer has no headings to navigate."}</div>`;
  }
  const here = needle ? null : currentSectionId();
  return shown.map((e) => `<a href="#${e.id}" class="lv${e.level}${e.id === here ? " here" : ""}">${esc(e.text)}</a>`).join("");
}

function outlinePane(turn) {
  const entries = readOutline(document.getElementById(`turn-${turn.id}`));
  if (!entries.length) {
    return `<div class="empty">${turn.status === "running" ? "Sections appear as the answer is written." : "This answer has no headings to navigate."}</div>`;
  }
  return `<nav class="outline-list">${outlineListHTML(entries)}</nav>`;
}

let outlineTimer = null;
function scheduleOutline() {
  if (outlineTimer) return;
  outlineTimer = requestAnimationFrame(() => {
    outlineTimer = null;
    refreshOutline();
  });
}

function refreshOutline() {
  const turnEl = activeTurnEl();
  if (!turnEl) return;
  const changed = turnEl.id !== outlineTurnId;
  outlineTurnId = turnEl.id;
  outlineEntries = readOutline(turnEl);
  if (changed && outlinePref !== "never") autoOpen(turnEl);
  if (outlineOpen) $("#outline-list").innerHTML = outlineListHTML(outlineEntries, $("#outline-filter").value);
}

function autoOpen(turnEl) {
  if (outlineOpen || outlinePref === "never") return;
  const words = (turnEl.querySelector(".report")?.textContent || "").split(/\s+/).length;
  if (outlinePref === "always" || words >= LONG_ANSWER_WORDS) toggleOutline(true);
}

function toggleOutline(open = !outlineOpen, focusFilter = false) {
  outlineOpen = open;
  $("#outline").hidden = !open;
  document.body.classList.toggle("outline-open", open);
  if (open) {
    outlineEntries = readOutline(activeTurnEl());
    $("#outline-list").innerHTML = outlineListHTML(outlineEntries, $("#outline-filter").value);
    if (focusFilter) $("#outline-filter").select();
  }
}

function jumpTo(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  el.classList.remove("jumped");
  void el.offsetWidth;
  el.classList.add("jumped");
}

$("#outline-list").addEventListener("click", (e) => {
  const link = e.target.closest("a");
  if (!link) return;
  e.preventDefault();
  jumpTo(link.getAttribute("href").slice(1));
});
$("#turns").addEventListener("click", (e) => {
  const link = e.target.closest(".pane .outline-list a");
  if (!link) return;
  e.preventDefault();
  jumpTo(link.getAttribute("href").slice(1));
}, true);
$("#outline-filter").addEventListener("input", (e) => {
  $("#outline-list").innerHTML = outlineListHTML(outlineEntries, e.target.value);
});
$("#outline-close").addEventListener("click", () => toggleOutline(false));
const prefSelect = $("#outline-pref");
prefSelect.value = outlinePref;
prefSelect.addEventListener("change", (e) => {
  outlinePref = e.target.value;
  store.set(OUTLINE_PREF, outlinePref);
});

const typing = (el) => el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.isContentEditable);
document.addEventListener("keydown", (e) => {
  if (e.key === "k" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); return toggleOutline(true, true); }
  if (e.key === "Escape" && outlineOpen) return toggleOutline(false);
  if (e.key.toLowerCase() === "o" && !e.metaKey && !e.ctrlKey && !e.altKey && !typing(e.target)) {
    e.preventDefault();
    toggleOutline();
  }
});
window.addEventListener("scroll", scheduleOutline, { passive: true });

const turnOf = (node) => {
  const el = node.closest(".turn");
  return el && current.turns.find((t) => `turn-${t.id}` === el.id);
};

// The PDF is rendered on demand and takes a few seconds, so fetch it with feedback
// rather than letting the browser sit on a pending navigation.
async function downloadPdf(link) {
  const label = link.textContent;
  link.setAttribute("aria-busy", "true");
  link.textContent = "Preparing…";
  try {
    const res = await fetch(link.href);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Export failed (${res.status})`);
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: "" });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (err) {
    alert(err.message);
  } finally {
    link.removeAttribute("aria-busy");
    link.textContent = label;
  }
}

$("#turns").addEventListener("click", (e) => {
  const pdf = e.target.closest('a[data-format="pdf"]');
  if (pdf) { e.preventDefault(); downloadPdf(pdf); return; }
  const turn = turnOf(e.target);
  if (!turn) return;
  const tabBtn = e.target.closest("[data-tab]");
  const cite = e.target.closest("a.cite");
  const copy = e.target.closest("[data-copy]");
  const stop = e.target.closest("[data-stop]");
  if (tabBtn) { tabs[turn.id] = tabBtn.dataset.tab; renderTurn(turn); }
  else if (cite) {
    e.preventDefault();
    tabs[turn.id] = "sources"; renderTurn(turn);
    const card = document.getElementById(`t${turn.id}-ref-${cite.dataset.n}`);
    if (card) { card.classList.add("hit"); card.scrollIntoView({ behavior: "smooth", block: "center" }); }
  } else if (copy) {
    navigator.clipboard.writeText(turn.strategy.databases[Number(copy.dataset.copy)].query);
    copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  } else if (stop) {
    stop.disabled = true; stop.textContent = "Stopping…";
    api(`/api/runs/${turn.id}/cancel`, { method: "POST" });
  }
});

// Hover preview for citations.
const pop = $("#pop");
$("#turns").addEventListener("mouseover", (e) => {
  const cite = e.target.closest("a.cite");
  const turn = cite && turnOf(cite);
  const ref = turn && turn.references.find((r) => r.n === Number(cite.dataset.n));
  if (!ref) return;
  pop.innerHTML = `<b>${esc(ref.title)}</b><div class="by">${byline(ref)}</div><div style="margin-top:6px">${badges(ref)}</div>`;
  pop.hidden = false;
  const box = cite.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - pop.offsetWidth - 12))}px`;
  pop.style.top = box.bottom + pop.offsetHeight + 12 > window.innerHeight ? `${box.top - pop.offsetHeight - 6}px` : `${box.bottom + 6}px`;
});
$("#turns").addEventListener("mouseout", (e) => { if (e.target.closest("a.cite")) pop.hidden = true; });

// ------------------------------------------------------------------ boot

syncHint();
if (location.hash.length > 1) openThread(location.hash.slice(1)).catch(showHome);
else loadHistory();
api("/api/health").then((h) => {
  const engine = h.third_party ? `${h.model} via ${new URL(h.base_url).host}` : h.model;
  $("#health").textContent = `${engine} · ${h.skills.length} skills · ${h.sources.join(", ")}`;
}).catch(() => {});
