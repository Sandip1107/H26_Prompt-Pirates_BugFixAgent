// BugFix Agent frontend. All text from the server is inserted with textContent
// (via the el() helper), never innerHTML, so repository content cannot inject markup.

const $ = (sel) => document.querySelector(sel);
const state = { analysis: null, severity: "all", query: "", openIds: new Set() };
const SEVERITIES = ["high", "medium", "low", "info"];

/** Small DOM helper: el("div", {class: "x"}, "text", childNode) */
function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  kids.flat().forEach((c) => node.append(c instanceof Node ? c : document.createTextNode(c ?? "")));
  return node;
}

/** Calls the Flask backend. GET when no body is given, POST (JSON) otherwise. */
async function api(path, body) {
  const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new Error("Cannot reach the BugFix Agent server. Is app.py running?");
  }
  let data = {};
  try { data = await res.json(); } catch { /* non-JSON response */ }
  if (!res.ok) {
    // Flask always returns JSON errors. A bare 404 means a different server answered.
    if (res.status === 404 && !data.error) {
      throw new Error("API not found. Open the app at http://127.0.0.1:5000 after running `py app.py`, not through Live Server or a file.");
    }
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return data;
}

function showState(kind, message) {
  const box = $("#state-box");
  box.className = "state-box" + (kind === "error" ? " error" : "");
  box.replaceChildren(kind === "loading" ? el("span", { class: "spinner" }) : "", message);
  box.hidden = false;
}
const hideState = () => { $("#state-box").hidden = true; };

// ---------- Status pills ----------
async function loadStatus() {
  try {
    const s = await api("/api/status");
    const ai = $("#ai-status"), sb = $("#sandbox-status");
    ai.textContent = s.ai.configured ? `AI: ${s.ai.model}` : "AI: not configured";
    ai.className = "pill " + (s.ai.configured ? "ok" : "off");
    sb.textContent = s.sandbox.available ? "Sandbox: Docker ready" : "Sandbox: unavailable";
    sb.className = "pill " + (s.sandbox.available ? "ok" : "off");
    sb.title = s.sandbox.message || "";
    const notice = $("#ai-notice");
    notice.hidden = s.ai.configured;
    notice.textContent = s.ai.message || "";
  } catch (e) {
    $("#ai-status").textContent = "Server offline";
  }
}

// ---------- Analyze ----------
$("#analyze-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#analyze-btn");
  btn.disabled = true;
  $("#results").hidden = true;
  showState("loading", "Fetching repository and running static analysis\u2026");
  try {
    state.analysis = await api("/api/analyze", { url: $("#repo-url").value });
    state.severity = "all";
    state.query = "";
    state.openIds = new Set();
    $("#search").value = "";
    $("#verify-output").replaceChildren();
    hideState();
    renderAll();
    $("#results").hidden = false;
  } catch (e) {
    showState("error", e.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------- Rendering ----------
function renderAll() {
  renderRepo();
  renderFilters();
  renderFindings();
  const id = state.analysis.id;
  $("#dl-md").href = `/api/report/${id}/md`;
  $("#dl-json").href = `/api/report/${id}/json`;
}

function renderRepo() {
  const a = state.analysis, r = a.repo;
  const counts = SEVERITIES.map((s) => a.findings.filter((f) => f.severity === s).length);
  const langs = Object.keys(r.languages).slice(0, 5).join(", ") || "unknown";
  const stat = (v, label) => el("div", {}, el("b", {}, String(v ?? "-")), el("span", {}, label));
  $("#repo-card").replaceChildren(
    el("div", { class: "repo-head" },
      el("div", {}, el("h2", {}, `${r.owner}/${r.name}`), el("p", { class: "muted" }, r.description || "No description provided.")),
      el("a", { href: r.url, target: "_blank", rel: "noopener noreferrer" }, "View on GitHub")),
    el("div", { class: "meta-grid" },
      stat(a.files_scanned.length, "files scanned"), stat(a.findings.length, "findings"),
      stat(counts[0], "high"), stat(r.stars, "stars"), stat(r.license || "none", "license"),
      stat(r.branch, "default branch"), stat(langs, "top languages")),
    a.notes.length ? el("ul", { class: "note-list" }, a.notes.map((n) => el("li", {}, n))) : "");
}

function renderFilters() {
  const a = state.analysis, box = $("#severity-filters");
  const mk = (key, label, n) => el("button", {
    class: "chip", type: "button", "aria-pressed": String(state.severity === key),
    onclick: () => { state.severity = key; renderFilters(); renderFindings(); },
  }, `${label} (${n})`);
  box.replaceChildren(
    mk("all", "All", a.findings.length),
    ...SEVERITIES.map((s) => mk(s, s[0].toUpperCase() + s.slice(1), a.findings.filter((f) => f.severity === s).length)));
}

$("#search").addEventListener("input", (e) => {
  state.query = e.target.value.toLowerCase();
  renderFindings();
});

function renderFindings() {
  const a = state.analysis, list = $("#findings-list");
  const shown = a.findings.filter((f) =>
    (state.severity === "all" || f.severity === state.severity) &&
    (f.file + " " + f.title).toLowerCase().includes(state.query));
  if (!a.findings.length) {
    const msg = a.files_scanned.length
      ? "No issues matched the built-in rules in the scanned files. This does not prove the code is bug-free."
      : "No supported source files were found, so nothing could be analyzed.";
    return list.replaceChildren(el("div", { class: "empty" }, msg));
  }
  if (!shown.length) return list.replaceChildren(el("div", { class: "empty" }, "No findings match the current filter."));
  list.replaceChildren(...shown.map(findingCard));
}

function findingCard(f) {
  const aiHolder = el("div", { class: "block" });
  const card = el("details", {
      class: "finding",
      open: state.openIds.has(f.id) ? "" : null,
      // Remember which cards are open so filtering/re-rendering keeps them open.
      ontoggle: (e) => { e.target.open ? state.openIds.add(f.id) : state.openIds.delete(f.id); },
    },
    el("summary", {},
      el("span", { class: `sev sev-${f.severity}` }, f.severity),
      el("span", { class: "f-title" }, f.title),
      el("span", { class: "f-loc" }, f.file + (f.line ? `:${f.line}` : ""))),
    el("div", { class: "f-body" },
      el("div", { class: "block" },
        el("h4", {}, el("span", { class: "tag tag-evidence" }, "Evidence"), `Rule ${f.rule}`),
        f.evidence ? el("pre", {}, f.evidence) : el("p", { class: "muted small" }, "No line number available."),
        el("p", { class: "small" }, f.explanation)),
      aiHolder));
  renderAiBlock(aiHolder, f);
  return card;
}

function renderAiBlock(holder, f) {
  const a = state.analysis;
  a.fixes = a.fixes || {};
  a.decisions = a.decisions || {};
  const fix = a.fixes[f.id];
  const head = el("h4", {}, el("span", { class: "tag tag-ai" }, "AI recommendation"));

  // No recommendation yet: show a button that asks the backend (and so the model).
  if (!fix) {
    const msg = el("p", { class: "err" });
    const btn = el("button", {
      class: "btn small", type: "button",
      onclick: async () => {
        btn.disabled = true;
        btn.textContent = "Asking model\u2026";
        msg.textContent = "";
        try {
          a.fixes[f.id] = await api("/api/fix", { analysis_id: a.id, finding_id: f.id });
          renderAiBlock(holder, f);
        } catch (e) {
          msg.textContent = e.message;
          btn.disabled = false;
          btn.textContent = "Ask AI for a fix";
        }
      },
    }, "Ask AI for a fix");
    return holder.replaceChildren(head, el("p", { class: "muted small" }, "No recommendation requested yet."), btn, msg);
  }

  const decision = a.decisions[f.id] || "pending";
  const setDecision = async (d) => {
    try {
      await api("/api/decision", { analysis_id: a.id, finding_id: f.id, decision: d });
      a.decisions[f.id] = d;
      renderAiBlock(holder, f);
    } catch (e) {
      alert(e.message);
    }
  };

  holder.replaceChildren(head,
    el("p", { class: "small" }, fix.explanation),
    el("p", { class: "muted small" }, `Model: ${fix.model} \u00b7 confidence: ${fix.confidence}`),
    el("pre", { class: "del" }, fix.original_snippet || "(none)"),
    el("pre", { class: "add" }, fix.fixed_snippet || "(none)"),
    fix.caveats ? el("p", { class: "small muted" }, "Caveats: " + fix.caveats) : "",
    fix.snippet_matches_file ? "" : el("p", { class: "warn" },
      "The quoted original text was not found exactly once in the file, so this fix cannot be applied for testing. Review it manually."),
    el("div", { class: "decision" },
      el("button", { class: "btn small", type: "button", disabled: decision === "accepted" ? "" : null, onclick: () => setDecision("accepted") }, "Accept for testing"),
      el("button", { class: "btn small", type: "button", disabled: decision === "rejected" ? "" : null, onclick: () => setDecision("rejected") }, "Reject"),
      el("span", { class: "badge-decision" }, `Decision: ${decision}. Nothing is changed in your repository.`)));
}

// ---------- Verification ----------
$("#verify-btn").addEventListener("click", async () => {
  const btn = $("#verify-btn"), out = $("#verify-output");
  btn.disabled = true;
  out.replaceChildren(el("p", { class: "muted small" },
    el("span", { class: "spinner" }), "Downloading repo and running tests (can take a few minutes)\u2026"));
  try {
    const v = await api("/api/verify", { analysis_id: state.analysis.id });
    state.analysis.verification = v;
    renderVerification(v);
  } catch (e) {
    out.replaceChildren(el("p", { class: "err" }, e.message));
  } finally {
    btn.disabled = false;
  }
});

function renderRun(title, run) {
  return el("div", { class: "run" },
    el("div", {},
      el("span", { class: "tag tag-test" }, "Test result"), " ", title, ": ",
      el("span", { class: `run-status st-${run.status}` }, run.status.replace("_", " ")),
      ` (${run.suite}, ${run.seconds}s)`),
    el("pre", {}, run.output || "(no output)"));
}

function renderVerification(v) {
  const out = $("#verify-output");
  const parts = [el("p", { class: "muted small" }, `Isolation: ${v.isolation}`)];
  v.reasons.forEach((r) => parts.push(el("p", { class: "warn" }, r)));
  if (!v.executed) parts.unshift(el("p", { class: "warn" }, "No tests were executed."));
  v.baseline.forEach((r) => parts.push(renderRun("Unmodified repo", r)));
  v.with_fixes.forEach((r) => parts.push(renderRun(`With fixes ${v.applied_fixes.join(", ")}`, r)));
  v.skipped_fixes.forEach((s) => parts.push(el("p", { class: "warn" }, `${s.finding_id} not applied: ${s.reason}`)));
  out.replaceChildren(...parts);
}

loadStatus();