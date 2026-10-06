// RAT — Repo Analysis Tool: application shell, state, rendering.

import { api } from "./api.js";
import { renderTimeline, renderTreemap, fmtInt, fmtK, fmtDate } from "./charts.js";

/* ---------------------------------------------------------------- utils */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

function toast(message, type = "info", ms = 4600) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

let loadingDepth = 0;
function setLoading(on) {
  loadingDepth = Math.max(0, loadingDepth + (on ? 1 : -1));
  $("#loading").classList.toggle("hidden", loadingDepth === 0);
}

const BUSY = new Set(["pending", "cloning", "loading", "parsing", "refreshing"]);

function growthClass(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }
function signed(v) { return `${v > 0 ? "+" : ""}${fmtInt(v)}`; }

/* ---------------------------------------------------------------- state */

const state = {
  repos: [],
  repoId: null,
  meta: null,
  ref: "HEAD",
  filters: { authors: [], tsFrom: 0, tsTo: null, mode: "range", commits: [], path: "" },
  dashboard: null,
  commitsList: null,
  commitPage: 1,
  commitSearch: "",
  selectedCommits: new Set(),
  tab: "dashboard",
  mergeSelection: new Set(),
  authorsData: null,
  pollTimer: null,
  lastErrorShown: {},
  ignoreHash: false,
};

/* ---------------------------------------------------------------- hash state */

function writeHash() {
  const p = new URLSearchParams();
  if (state.repoId) p.set("repo", state.repoId);
  if (state.ref && state.ref !== "HEAD") p.set("ref", state.ref);
  if (state.filters.path) p.set("path", state.filters.path);
  if (state.filters.tsFrom) p.set("from", state.filters.tsFrom);
  if (state.filters.tsTo) p.set("to", state.filters.tsTo);
  if (state.filters.authors.length) p.set("authors", state.filters.authors.join("|"));
  if (state.filters.mode === "manual") {
    p.set("mode", "manual");
    const hashes = [...state.selectedCommits];
    if (hashes.length <= 100) p.set("commits", hashes.join(","));
  }
  state.ignoreHash = true;
  location.hash = p.toString();
  setTimeout(() => { state.ignoreHash = false; }, 0);
}

function readHash() {
  const p = new URLSearchParams(location.hash.replace(/^#/, ""));
  const out = {};
  if (p.get("repo")) out.repoId = Number(p.get("repo"));
  if (p.get("ref")) out.ref = p.get("ref");
  if (p.get("path")) out.path = p.get("path");
  if (p.get("from")) out.tsFrom = Number(p.get("from"));
  if (p.get("to")) out.tsTo = Number(p.get("to"));
  if (p.get("authors")) out.authors = p.get("authors").split("|").filter(Boolean);
  if (p.get("mode") === "manual") { out.mode = "manual"; out.commits = (p.get("commits") || "").split(",").filter(Boolean); }
  return out;
}

/* ---------------------------------------------------------------- repos */

async function loadRepos({ silent = false } = {}) {
  const prev = new Map(state.repos.map((r) => [r.id, r]));
  const data = await api.listRepos();
  state.repos = data.repos;
  for (const repo of state.repos) {
    const before = prev.get(repo.id);
    if (repo.status === "error" && repo.error && state.lastErrorShown[repo.id] !== repo.error) {
      state.lastErrorShown[repo.id] = repo.error;
      toast(`${repo.name}: ${repo.error}`, "error", 9000);
    } else if (before && BUSY.has(before.status) && repo.status === "ready") {
      toast(`${repo.name} is ready`, "success");
    }
  }
  renderRepoList();
  syncPolling();
  if (!silent) {
    const active = state.repos.find((r) => r.id === state.repoId);
    if (active && active.status === "ready") return active;
  }
  return null;
}

function renderRepoList() {
  const box = $("#repo-list");
  if (!state.repos.length) {
    box.innerHTML = `<div class="sidebar-foot" style="padding:6px 4px">No repositories yet.</div>`;
    return;
  }
  box.innerHTML = state.repos.map((r) => {
    const busy = BUSY.has(r.status);
    const pct = r.commit_total ? Math.round((r.commit_parsed / r.commit_total) * 100) : 0;
    const badge = r.status === "ready" ? "" : `<span class="badge ${busy ? "busy" : "err"}">${esc(r.status)}</span>`;
    return `<div class="repo-item ${r.id === state.repoId ? "active" : ""}" data-repo="${r.id}">
      <div class="row1"><span class="name" title="${esc(r.name)}">${esc(r.name)}</span>${badge}</div>
      <div class="meta">
        <span>${fmtInt(r.commit_parsed)} commits</span>
        <span>${fmtInt(r.author_count)} authors</span>
        <span>${fmtInt(r.file_count)} files</span>
      </div>
      ${busy ? `<div class="progress"><div style="width:${pct}%"></div></div>
        <div class="meta"><span>${esc(r.status_detail || r.status)}</span></div>` : ""}
    </div>`;
  }).join("");
  $$(".repo-item", box).forEach((el) =>
    el.addEventListener("click", () => selectRepo(Number(el.dataset.repo))));
}

function syncPolling() {
  const anyBusy = state.repos.some((r) => BUSY.has(r.status));
  if (anyBusy && !state.pollTimer) {
    state.pollTimer = setInterval(async () => {
      try {
        const prevActive = state.repos.find((r) => r.id === state.repoId);
        const prevBusy = prevActive && BUSY.has(prevActive.status);
        await loadRepos({ silent: true });
        const active = state.repos.find((r) => r.id === state.repoId);
        if (active && prevBusy && active.status === "ready") {
          await loadMeta();
          await loadDashboard();
          if (state.tab === "authors") await loadAuthorsTab();
          if (state.tab === "compare") await loadCompareTab();
        }
        renderStatusBadge();
      } catch { /* poll errors are non-fatal */ }
    }, 1500);
  } else if (!anyBusy && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function selectRepo(id, { keepFilters = false } = {}) {
  if (!id) return;
  state.repoId = id;
  if (!keepFilters) {
    state.filters = { authors: [], tsFrom: 0, tsTo: null, mode: "range", commits: [], path: "" };
    state.selectedCommits = new Set();
    state.ref = "HEAD";
  }
  state.dashboard = null;
  state.authorsData = null;
  writeHash();
  await bootForRepo();
}

async function bootForRepo() {
  const repo = state.repos.find((r) => r.id === state.repoId);
  $("#filters").classList.remove("hidden");
  $("#hero").classList.add("hidden");
  renderStatusBadge();
  if (!repo) { setPage("hero"); return; }
  if (repo.status !== "ready") {
    setPage("none");
    return; // polling will pick it up
  }
  setLoading(true);
  try {
    await loadMeta();
    syncFilterInputs();
    await loadDashboard();
    if (state.tab === "authors") await loadAuthorsTab();
    if (state.tab === "compare") await loadCompareTab();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    setLoading(false);
  }
}

function renderStatusBadge() {
  const repo = state.repos.find((r) => r.id === state.repoId);
  const badge = $("#repo-status");
  const title = $("#repo-title");
  if (!repo) { badge.classList.add("hidden"); title.textContent = "No repository selected"; return; }
  title.textContent = repo.name;
  badge.classList.remove("hidden");
  badge.className = `badge ${repo.status === "ready" ? "ok" : BUSY.has(repo.status) ? "busy" : "err"}`;
  badge.textContent = repo.status === "ready"
    ? `ready · ${fmtInt(repo.commit_parsed)} commits`
    : (repo.status_detail || repo.status);
}

/* ---------------------------------------------------------------- meta / refs */

async function loadMeta() {
  state.meta = await api.meta(state.repoId);
  const known = new Set(state.meta.refs.map((r) => r.name));
  // keep hex-looking refs (commit hashes) even when unprepared — the 409
  // flow will offer to prepare them; plain names (stale branches) reset
  if (!known.has(state.ref) && !/^[0-9a-f]{7,40}$/i.test(state.ref)) state.ref = "HEAD";
  const sel = $("#ref-select");
  sel.innerHTML = state.meta.refs.slice(0, 400).map((r) => {
    const counts = r.commits != null ? ` · ${fmtK(r.commits)}` : (r.ready ? "" : " · not loaded");
    return `<option value="${esc(r.name)}" ${r.name === state.ref ? "selected" : ""}>${esc(r.name)}${counts}</option>`;
  }).join("") + `<option value="__custom__">custom hash…</option>`;
  const custom = state.ref && !known.has(state.ref);
  $("#ref-custom").classList.toggle("hidden", !custom);
  if (custom) {
    sel.value = "__custom__";
    $("#ref-custom").value = state.ref;
  }
}

function syncFilterInputs() {
  $("#ts-from").value = state.filters.tsFrom ? toLocalInput(state.filters.tsFrom) : "";
  $("#ts-to").value = state.filters.tsTo ? toLocalInput(state.filters.tsTo) : "";
  $$("#range-quick button").forEach((b) =>
    b.classList.toggle("active", Number(b.dataset.days) === state.filters._quickDays ?? false));
  $$("#commit-mode button").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === state.filters.mode));
  renderAuthorFilterButton();
  renderBreadcrumb();
}

function toLocalInput(ts) {
  const d = new Date(ts * 1000 - new Date().getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
}
function fromLocalInput(v) { return v ? Math.floor(new Date(v).getTime() / 1000) : null; }

/* ---------------------------------------------------------------- filters */

function apiFilters({ ignorePath = false, ignoreManual = false, forPicker = false } = {}) {
  const f = state.filters;
  const body = {
    ref: state.ref,
    ts_from: f.tsFrom || 0,
    authors: f.authors,
  };
  if (f.tsTo) body.ts_to = f.tsTo;
  if (!ignorePath && f.path) body.path = f.path;
  if (!ignoreManual && f.mode === "manual" && state.selectedCommits.size) {
    body.commits = [...state.selectedCommits];
  }
  if (!forPicker && f.mode !== "manual") body.commits = [];
  return body;
}

function renderAuthorFilterButton() {
  const btn = $("#author-filter-btn");
  const n = state.filters.authors.length;
  btn.textContent = n === 0 ? "All authors ▾" : `${n} author${n > 1 ? "s" : ""} ▾`;
  btn.classList.toggle("primary", n > 0);
}

function renderBreadcrumb() {
  const box = $("#path-breadcrumb");
  const path = state.filters.path;
  $("#btn-clear-path").classList.toggle("hidden", !path);
  if (!path) {
    box.innerHTML = `<span class="current">repo root</span>`;
    return;
  }
  const parts = path.split("/");
  let html = `<button data-path="">repo root</button>`;
  let acc = "";
  parts.forEach((part, i) => {
    acc = acc ? `${acc}/${part}` : part;
    html += `<span class="sep">/</span>`;
    html += i === parts.length - 1
      ? `<span class="current">${esc(part)}</span>`
      : `<button data-path="${esc(acc)}">${esc(part)}</button>`;
  });
  box.innerHTML = html;
  $$("button[data-path]", box).forEach((b) =>
    b.addEventListener("click", () => setPath(b.dataset.path)));
}

function setPath(path) {
  state.filters.path = path || "";
  writeHash();
  renderBreadcrumb();
  loadDashboard();
}

async function loadDashboard() {
  if (!state.repoId) return;
  setLoading(true);
  try {
    const data = await api.dashboard(state.repoId, apiFilters());
    state.dashboard = data;
    renderDashboard();
    await loadCommitsList();
  } catch (e) {
    if (e.status === 409) {
      await ensureRefFlow();
      return loadDashboard();
    }
    toast(e.message, "error");
  } finally {
    setLoading(false);
  }
}

async function ensureRefFlow() {
  toast(`Preparing reference “${state.ref}” — parsing commits, this can take a moment…`);
  try {
    await api.ensureRef(state.repoId, state.ref);
  } catch (e) {
    toast(e.message, "error");
    throw e;
  }
  // wait for the repo job to finish
  for (;;) {
    await new Promise((r) => setTimeout(r, 1200));
    const repos = (await api.listRepos()).repos;
    state.repos = repos;
    renderRepoList();
    const repo = repos.find((r) => r.id === state.repoId);
    if (!repo) throw new Error("repository disappeared");
    if (!BUSY.has(repo.status)) {
      if (repo.status === "ready") return true;
      throw new Error(repo.error || "failed to prepare reference");
    }
    renderStatusBadge();
  }
}

/* ---------------------------------------------------------------- dashboard rendering */

function setPage(which) {
  $("#hero").classList.toggle("hidden", which !== "hero");
  $("#page-dashboard").classList.toggle("hidden", which !== "dashboard");
  $("#page-authors").classList.toggle("hidden", which !== "authors");
  $("#page-compare").classList.toggle("hidden", which !== "compare");
  if (which !== "hero") $("#filters").classList.remove("hidden");
  else $("#filters").classList.add("hidden");
}

function renderDashboard() {
  const d = state.dashboard;
  if (!d) return;
  setPage("dashboard");
  renderKpis(d);
  renderScopePanel(d);
  $("#timeline-note").textContent = d.timeline.points.length
    ? `per ${d.timeline.bucket_seconds === 86400 ? "day" : d.timeline.bucket_seconds === 604800 ? "week" : "month"}`
    : "";
  renderTimeline($("#chart-timeline"), d.timeline.points, d.timeline.bucket_seconds);
  renderTreemap($("#chart-treemap"), d.tree, (path) => setPath(path));
  renderAuthorsTable(d);
  renderObjectTable($("#table-files"), d.top_files, "file");
  renderObjectTable($("#table-dirs"), d.top_dirs, "dir");
  renderSelectionInfo();
}

function kpi(k, v, sub = "", cls = "") {
  return `<div class="kpi"><div class="k">${esc(k)}</div><div class="v ${cls}">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
}

function renderKpis(d) {
  const s = d.summary;
  const m = d.repo_metrics;
  const range = s.commit_count
    ? `${fmtDate(s.ts_min)} → ${fmtDate(s.ts_max)}`
    : "no commits match the filters";
  $("#kpis").innerHTML = [
    kpi("Commit set |H|", fmtInt(s.commit_count), range, "accent"),
    kpi("Authors", fmtInt(s.authors)),
    kpi("Files touched", fmtInt(s.files_touched)),
    kpi("Added lines", `+${fmtInt(m.added)}`, "", "pos"),
    kpi("Removed lines", `−${fmtInt(m.removed)}`, "", "neg"),
    kpi("Growth δ", signed(m.growth), "", growthClass(m.growth)),
    kpi("Churn λ", fmtInt(m.churn)),
    kpi("Modifications n", fmtInt(m.modifications), `η ${(m.modification_frequency * 100).toFixed(1)}% · ρ ${fmtInt(Math.round(m.churn_rate))}/commit`),
  ].join("");
}

function renderScopePanel(d) {
  const host = $("#scope-host");
  if (!host) return;
  host.innerHTML = ""; // re-render in place: never stack duplicate cards
  if (!d.scope) return;
  const blocks = [];
  const row = (label, r) => `
    <tr><td><span class="scope-chip">${esc(label)}</span> <span class="path mono">${esc(r.path || "repo root")}</span></td>
      <td class="num pos">+${fmtInt(r.added)}</td><td class="num neg">−${fmtInt(r.removed)}</td>
      <td class="num ${growthClass(r.growth)}">${signed(r.growth)}</td><td class="num">${fmtInt(r.churn)}</td>
      <td class="num">${fmtInt(r.modifications)}</td>
      <td class="num">${(r.modification_frequency * 100).toFixed(1)}%</td>
      <td class="num">${r.churn_rate.toFixed(2)}</td></tr>`;
  if (d.scope.file) blocks.push(row("file", d.scope.file));
  if (d.scope.dir) blocks.push(row("directory", d.scope.dir));
  if (!blocks.length) return;
  const owners = (d.scope.authors || []).slice(0, 5).map((a) =>
    `<span class="chip">${esc(a.name)} ${(a.ownership * 100).toFixed(0)}%</span>`).join(" ");
  host.innerHTML = `<section class="card scope-card">
    <h3>Scope metrics <span class="hint">commits in H touching the selected path · clear the path filter to remove</span></h3>
    <table><thead><tr><th>Object</th><th class="num">l+</th><th class="num">l−</th><th class="num">δ</th><th class="num">λ</th>
      <th class="num">n</th><th class="num">η</th><th class="num">ρ</th></tr></thead>
      <tbody>${blocks.join("")}</tbody></table>
    <div style="margin-top:10px">${owners || '<span class="hint">no author churn in scope</span>'}</div>
  </section>`;
}

function ownerBar(authors, total) {
  if (!authors || !total) return "";
  const colors = ["#58a6ff", "#bc8cff", "#3fb950", "#d29922", "#f85149", "#39c5cf"];
  let acc = 0;
  const segs = authors.slice(0, 5).map((a, i) => {
    acc += a.churn;
    return `<span style="width:${((a.churn / total) * 100).toFixed(2)}%;background:${colors[i]}" title="${esc(a.name)}: ${(a.ownership * 100).toFixed(1)}%"></span>`;
  }).join("");
  return `<span class="bar">${segs}</span>`;
}

function renderAuthorsTable(d) {
  const rows = d.authors.slice(0, 25);
  if (!rows.length) { $("#table-authors").innerHTML = `<div class="hint">no authors in this commit set</div>`; return; }
  const total = rows.reduce((s, a) => s + a.churn, 0);
  $("#table-authors").innerHTML = `<table>
    <thead><tr><th>Author</th><th class="num">Commits</th><th class="num">Churn</th><th class="num">Growth</th><th>Ownership</th></tr></thead>
    <tbody>${rows.map((a) => `<tr>
      <td class="nowrap" title="${esc(a.name)}">${esc(a.name)}</td>
      <td class="num">${fmtInt(a.commits)}</td>
      <td class="num">${fmtInt(a.churn)}</td>
      <td class="num ${growthClass(a.growth)}">${signed(a.growth)}</td>
      <td class="nowrap">${ownerBar([a], d.repo_metrics.churn)} <span class="sub">${(a.ownership * 100).toFixed(1)}%</span></td>
    </tr>`).join("")}</tbody></table>
    <div class="hint" style="margin-top:8px">Total churn in H: ${fmtInt(total)}${d.authors.length > rows.length ? ` · showing top ${rows.length} of ${d.authors.length}` : ""}</div>`;
}

function renderObjectTable(host, rows, kind) {
  if (!rows.length) { host.innerHTML = `<div class="hint">no ${kind === "file" ? "files" : "directories"} with churn in this selection</div>`; return; }
  host.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>${kind === "file" ? "File" : "Directory"}</th>
      <th class="num">Churn λ</th><th class="num">l+</th><th class="num">l−</th><th class="num">δ</th>
      <th class="num">n</th><th class="num">η</th><th class="num">ρ</th><th></th></tr></thead>
    <tbody>${rows.map((r) => `<tr class="clickable" data-path="${esc(r.path)}" data-kind="${kind}">
      <td><span class="path">${esc(r.path)}</span></td>
      <td class="num">${fmtInt(r.churn)}</td>
      <td class="num pos">+${fmtInt(r.added)}</td>
      <td class="num neg">−${fmtInt(r.removed)}</td>
      <td class="num ${growthClass(r.growth)}">${signed(r.growth)}</td>
      <td class="num">${fmtInt(r.modifications)}</td>
      <td class="num">${(r.modification_frequency * 100).toFixed(1)}%</td>
      <td class="num">${r.churn_rate.toFixed(2)}</td>
      <td><button class="linkish" data-focus="${esc(r.path)}">focus</button></td>
    </tr>`).join("")}</tbody></table></div>`;
  $$("tr.clickable", host).forEach((tr) =>
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-focus]")) return;
      openObjectDrawer(tr.dataset.path);
    }));
  $$("[data-focus]", host).forEach((b) =>
    b.addEventListener("click", (ev) => { ev.stopPropagation(); setPath(b.dataset.focus); }));
}

/* ---------------------------------------------------------------- commits */

async function loadCommitsList() {
  if (!state.repoId) return;
  try {
    const data = await api.commits(state.repoId, {
      ...apiFilters({ forPicker: true, ignoreManual: true, ignorePath: true }),
      page: state.commitPage,
      per_page: 25,
      q: state.commitSearch,
    });
    state.commitsList = data;
    renderCommitsTable();
  } catch (e) {
    if (e.status !== 409) toast(e.message, "error");
  }
}

function renderCommitsTable() {
  const data = state.commitsList;
  const host = $("#table-commits");
  if (!data) { host.innerHTML = ""; return; }
  if (!data.commits.length) {
    host.innerHTML = `<div class="hint">no commits match the current author / period filters</div>`;
    $("#commit-pager").innerHTML = "";
    return;
  }
  host.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th></th><th>Commit</th><th>Author</th><th class="nowrap">When</th><th class="num">Files</th>
      <th class="num">+</th><th class="num">−</th><th class="num">λ</th></tr></thead>
    <tbody>${data.commits.map((c) => {
      const checked = state.selectedCommits.has(c.hash) ? "checked" : "";
      return `<tr class="clickable" data-hash="${c.hash}">
        <td><input type="checkbox" data-check="${c.hash}" ${checked}></td>
        <td><span class="mono">${c.hash.slice(0, 10)}</span><br><span class="sub">${esc((c.subject || "").slice(0, 70))}</span></td>
        <td class="nowrap">${esc(c.author)}</td>
        <td class="nowrap">${fmtDate(c.ts)}</td>
        <td class="num">${c.files_changed}</td>
        <td class="num pos">+${fmtInt(c.added)}</td>
        <td class="num neg">−${fmtInt(c.removed)}</td>
        <td class="num">${fmtInt(c.churn)}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
  $$("tr.clickable", host).forEach((tr) =>
    tr.addEventListener("click", (ev) => {
      if (ev.target.matches("input[type=checkbox]")) return;
      openCommitDrawer(tr.dataset.hash);
    }));
  $$("input[data-check]", host).forEach((cb) =>
    cb.addEventListener("change", () => {
      if (cb.checked) state.selectedCommits.add(cb.dataset.check);
      else state.selectedCommits.delete(cb.dataset.check);
      renderSelectionInfo();
    }));

  const pages = Math.max(1, Math.ceil(data.total / data.per_page));
  $("#commit-pager").innerHTML = `
    <span>${fmtInt(data.total)} commits · page ${data.page} / ${pages}</span>
    <button class="btn ghost small" data-page="prev" ${data.page <= 1 ? "disabled" : ""}>‹ prev</button>
    <button class="btn ghost small" data-page="next" ${data.page >= pages ? "disabled" : ""}>next ›</button>`;
  $$("#commit-pager [data-page]").forEach((b) =>
    b.addEventListener("click", () => {
      state.commitPage += b.dataset.page === "next" ? 1 : -1;
      loadCommitsList();
    }));
}

function renderSelectionInfo() {
  const n = state.selectedCommits.size;
  const mode = state.filters.mode;
  const info = $("#selection-info");
  if (mode === "manual") info.textContent = `commit set = manual selection (${fmtInt(n)} commits)`;
  else info.textContent = n ? `${fmtInt(n)} commits checked — press “Use selected commits” to apply` : "";
  $("#btn-apply-selection").disabled = n === 0;
  $("#btn-clear-selection").disabled = n === 0 && mode !== "manual";
  $$("#commit-mode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
}

async function selectAllFiltered() {
  const cap = 5000;
  const hashes = [];
  let page = 1;
  for (;;) {
    const data = await api.commits(state.repoId, {
      ...apiFilters({ forPicker: true, ignoreManual: true, ignorePath: true }),
      page, per_page: 200,
    });
    for (const c of data.commits) hashes.push(c.hash);
    if (hashes.length >= cap || page * 200 >= data.total) break;
    page++;
  }
  const trimmed = hashes.slice(0, cap);
  trimmed.forEach((h) => state.selectedCommits.add(h));
  if (hashes.length > cap) toast(`selection capped at ${fmtInt(cap)} commits`, "error");
  toast(`${fmtInt(trimmed.length)} commits selected`);
  renderCommitsTable();
  renderSelectionInfo();
}

/* ---------------------------------------------------------------- authors tab */

async function loadAuthorsTab() {
  if (!state.repoId) return;
  state.authorsData = await api.authors(state.repoId);
  renderAuthorsTab();
}

function renderAuthorsTab() {
  setPage("authors");
  const d = state.authorsData;
  if (!d) return;
  const rows = d.identities;
  $("#table-identities").innerHTML = rows.length ? `<div class="table-wrap"><table>
    <thead><tr><th></th><th>Identity</th><th>Email</th><th class="num">Commits</th><th class="num">Churn</th><th>Merged into</th></tr></thead>
    <tbody>${rows.map((r) => `<tr>
      <td><input type="checkbox" data-ident="${esc(r.key)}" ${state.mergeSelection.has(r.key) ? "checked" : ""}></td>
      <td>${esc(r.name)}</td><td class="mono">${esc(r.email)}</td>
      <td class="num">${fmtInt(r.commits)}</td><td class="num">${fmtInt(r.churn)}</td>
      <td>${r.canonical ? `<span class="chip">${esc(r.canonical)}</span>` : '<span class="hint">—</span>'}</td>
    </tr>`).join("")}</tbody></table></div>` : '<div class="hint">no authors</div>';
  $$("#table-identities input[data-ident]").forEach((cb) =>
    cb.addEventListener("change", () => {
      if (cb.checked) state.mergeSelection.add(cb.dataset.ident);
      else state.mergeSelection.delete(cb.dataset.ident);
      renderMergeState();
    }));

  const sugg = $("#suggestions");
  if (d.suggestions.length) {
    sugg.innerHTML = `<div class="hint" style="margin-bottom:6px">Suggestions (same email or name, not merged yet):</div>` +
      d.suggestions.slice(0, 8).map((s, i) => `<div class="suggestion">
        <span>${esc(s.reason)}</span><span class="keys">${esc(s.keys.join("  ·  "))}</span>
        <button class="btn ghost small" data-sugg="${i}">Merge these</button></div>`).join("");
    $$("[data-sugg]", sugg).forEach((b) =>
      b.addEventListener("click", () => {
        const s = d.suggestions[Number(b.dataset.sugg)];
        const names = s.keys.map((k) => k.split(" <")[0]);
        const name = names.sort((a, b2) =>
          names.filter((x) => x === b2).length - names.filter((x) => x === a).length)[0] || "merged author";
        doMerge(name, s.keys);
      }));
  } else sugg.innerHTML = "";

  const groups = Object.entries(d.groups);
  $("#table-groups").innerHTML = groups.length ? `<table>
    <thead><tr><th>Merged author</th><th>Identities</th><th class="num">Identities</th><th></th></tr></thead>
    <tbody>${groups.map(([canonical, keys]) => `<tr>
      <td><b>${esc(canonical)}</b></td>
      <td class="mono" style="font-size:11px">${esc(keys.join(", "))}</td>
      <td class="num">${keys.length}</td>
      <td><button class="btn ghost small" data-unmerge="${esc(canonical)}">Unmerge</button></td>
    </tr>`).join("")}</tbody></table>` : '<div class="hint">no manual merges yet — .mailmap merges are applied automatically at ingestion</div>';
  $$("[data-unmerge]").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        state.authorsData = await api.unmergeAuthors(state.repoId, b.dataset.unmerge);
        renderAuthorsTab();
        toast("authors unmerged", "success");
        if (state.dashboard) loadDashboard();
      } catch (e) { toast(e.message, "error"); }
    }));
  renderMergeState();
}

function renderMergeState() {
  const n = state.mergeSelection.size;
  $("#merge-count").textContent = n ? `${n} identities selected` : "select identities in the table to merge";
  $("#btn-merge-authors").disabled = n < 1;
}

async function doMerge(canonical, keys) {
  canonical = (canonical || "").trim();
  if (!canonical) { toast("enter a name for the merged author", "error"); return; }
  try {
    state.authorsData = await api.mergeAuthors(state.repoId, canonical, keys);
    state.mergeSelection.clear();
    renderAuthorsTab();
    toast(`merged ${keys.length} identities into “${canonical}”`, "success");
    if (state.dashboard) loadDashboard();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------------------------------------------------------------- compare tab */

async function loadCompareTab() {
  setPage("compare");
  const data = await api.compare();
  const rows = data.repos;
  $("#table-compare").innerHTML = rows.length ? `<div class="table-wrap"><table>
    <thead><tr><th>Repository</th><th>Source</th><th>Status</th><th class="num">Commits</th><th class="num">Authors</th>
      <th class="num">Files</th><th class="num">Added</th><th class="num">Removed</th><th class="num">Growth</th><th class="num">Churn</th><th class="nowrap">Span</th></tr></thead>
    <tbody>${rows.map((r) => `<tr class="clickable" data-repo="${r.id}">
      <td><b>${esc(r.name)}</b></td>
      <td class="mono" style="font-size:11px">${esc(r.source_type === "url" ? r.source : "zip upload")}</td>
      <td><span class="badge ${r.status === "ready" ? "ok" : BUSY.has(r.status) ? "busy" : "err"}">${esc(r.status)}</span></td>
      <td class="num">${fmtInt(r.commits)}</td><td class="num">${fmtInt(r.authors)}</td><td class="num">${fmtInt(r.files)}</td>
      <td class="num pos">+${fmtInt(r.added)}</td><td class="num neg">−${fmtInt(r.removed)}</td>
      <td class="num ${growthClass(r.growth)}">${signed(r.growth)}</td><td class="num">${fmtInt(r.churn)}</td>
      <td class="nowrap sub">${r.first_ts ? `${fmtDate(r.first_ts)} → ${fmtDate(r.last_ts)}` : "–"}</td>
    </tr>`).join("")}</tbody></table></div>` : '<div class="hint">no repositories ingested yet</div>';
  $$("#table-compare tr.clickable").forEach((tr) =>
    tr.addEventListener("click", async () => {
      await selectRepo(Number(tr.dataset.repo));
      switchTab("dashboard");
    }));
}

/* ---------------------------------------------------------------- drawers */

function closeDrawer() { $("#drawer").classList.add("hidden"); $("#drawer").innerHTML = ""; }

function openDrawer(html) {
  const drawer = $("#drawer");
  drawer.innerHTML = `<button class="btn ghost small close-x" id="drawer-close">✕</button>${html}`;
  drawer.classList.remove("hidden");
  $("#drawer-close").addEventListener("click", closeDrawer);
}

async function openObjectDrawer(path) {
  openDrawer(`<div class="hint">loading ${esc(path)}…</div>`);
  try {
    const d = await api.objectDetail(state.repoId, apiFilters());
    const row = d.file || d.dir;
    if (!row) { openDrawer(`<p>No changes recorded for <span class="mono">${esc(path)}</span> in the current selection.</p>`); return; }
    const kind = d.file ? "file" : "directory";
    const owners = d.authors.map((a) => `<tr>
        <td>${esc(a.name)}</td>
        <td class="num">${fmtInt(a.modifications)}</td>
        <td class="num">${fmtInt(a.churn)}</td>
        <td class="num">${(a.ownership * 100).toFixed(1)}%</td></tr>`).join("");
    const history = d.history.map((h) => `<tr class="clickable" data-hash="${h.hash}">
        <td class="mono">${h.hash.slice(0, 10)}</td><td class="nowrap">${fmtDate(h.ts)}</td>
        <td>${esc(h.author)}</td>
        <td class="num pos">+${fmtInt(h.added)}</td><td class="num neg">−${fmtInt(h.removed)}</td>
        <td class="sub">${esc((h.subject || "").slice(0, 40))}</td></tr>`).join("");
    openDrawer(`
      <h3>${esc(path)} <span class="chip">${kind}</span></h3>
      <div class="hint">metrics within the current commit set H · ${fmtInt(d.h_count)} commits</div>
      <div class="section"><h4>Metrics</h4>
        <dl class="kv">
          <dt>Added l+</dt><dd class="pos">+${fmtInt(row.added)}</dd>
          <dt>Removed l−</dt><dd class="neg">−${fmtInt(row.removed)}</dd>
          <dt>Growth δ</dt><dd class="${growthClass(row.growth)}">${signed(row.growth)}</dd>
          <dt>Churn λ</dt><dd>${fmtInt(row.churn)}</dd>
          <dt>Modifications n</dt><dd>${fmtInt(row.modifications)}</dd>
          <dt>Modification frequency η</dt><dd>${(row.modification_frequency * 100).toFixed(1)}%</dd>
          <dt>Churn rate ρ</dt><dd>${row.churn_rate.toFixed(2)} / commit</dd>
        </dl>
      </div>
      <div class="section"><h4>Ownership (churn per author)</h4>
        ${owners ? `<table><thead><tr><th>Author</th><th class="num">Modifications</th><th class="num">Churn</th><th class="num">Ownership</th></tr></thead><tbody>${owners}</tbody></table>` : '<div class="hint">no changes in selection</div>'}
      </div>
      <div class="section"><h4>Recent commits touching this object</h4>
        ${history ? `<div class="table-wrap"><table><tbody>${history}</tbody></table></div>` : '<div class="hint">none</div>'}
      </div>
      <div class="section">
        <button class="btn primary" id="drawer-focus">Focus this path</button>
      </div>`);
    $("#drawer-focus").addEventListener("click", () => { closeDrawer(); setPath(path); });
    $$("#drawer tr[data-hash]").forEach((tr) => tr.addEventListener("click", () => openCommitDrawer(tr.dataset.hash)));
  } catch (e) {
    openDrawer(`<p class="neg">${esc(e.message)}</p>`);
  }
}

async function openCommitDrawer(hash) {
  openDrawer(`<div class="hint">loading commit ${esc(hash.slice(0, 10))}…</div>`);
  try {
    const c = await api.commitDetail(state.repoId, hash);
    const files = c.files.map((f) => `<tr>
      <td><span class="path">${esc(f.path)}</span></td>
      <td class="num pos">+${fmtInt(f.added)}</td><td class="num neg">−${fmtInt(f.removed)}</td>
      <td class="num">${fmtInt(f.added + f.removed)}</td></tr>`).join("");
    openDrawer(`
      <h3 class="mono">${esc(c.hash)}</h3>
      <div class="hint">${fmtDate(c.ts, true)} · ${esc(c.author)}</div>
      <div class="section"><h4>Message</h4><div>${esc(c.subject)}</div></div>
      <div class="section"><h4>Totals</h4>
        <dl class="kv"><dt>Added</dt><dd class="pos">+${fmtInt(c.added)}</dd>
        <dt>Removed</dt><dd class="neg">−${fmtInt(c.removed)}</dd>
        <dt>Churn</dt><dd>${fmtInt(c.churn)}</dd>
        <dt>Files</dt><dd>${c.files.length}</dd></dl></div>
      <div class="section"><h4>Files changed</h4>
        ${files ? `<div class="table-wrap"><table><tbody>${files}</tbody></table></div>` : '<div class="hint">no measurable changes (binary or empty)</div>'}</div>`);
  } catch (e) {
    openDrawer(`<p class="neg">${esc(e.message)}</p>`);
  }
}

/* ---------------------------------------------------------------- modals */

function modal(html, { onMount } = {}) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-back"><div class="modal">${html}</div></div>`;
  const back = $(".modal-back", root);
  back.addEventListener("click", (ev) => { if (ev.target === back) closeModal(); });
  if (onMount) onMount(root);
  return root;
}
function closeModal() { $("#modal-root").innerHTML = ""; }

function modalConfirm(title, message, { danger = false, confirmLabel = "Confirm" } = {}) {
  return new Promise((resolve) => {
    modal(`<h3>${esc(title)}</h3><p>${esc(message)}</p>
      <div class="actions"><button class="btn ghost" id="m-cancel">Cancel</button>
      <button class="btn ${danger ? "danger" : "primary"}" id="m-ok">${esc(confirmLabel)}</button></div>`,
    {
      onMount() {
        $("#m-cancel").addEventListener("click", () => { closeModal(); resolve(false); });
        $("#m-ok").addEventListener("click", () => { closeModal(); resolve(true); });
      },
    });
  });
}

function addRepoModal() {
  modal(`
    <h3>Add a repository</h3>
    <div class="tabs" id="add-tabs">
      <button class="tab active" data-add="url">Clone URL</button>
      <button class="tab" data-add="zip">Upload ZIP</button>
    </div>
    <div id="add-url">
      <div class="field"><label>Remote URL</label>
        <input class="input" id="add-url-input" placeholder="https://github.com/redis/redis.git" spellcheck="false"></div>
    </div>
    <div id="add-zip" class="hidden">
      <div class="field"><label>Zip archive (must include the .git directory)</label>
        <input class="input" type="file" id="add-zip-input" accept=".zip"></div>
    </div>
    <div class="field"><label>Display name (optional)</label>
      <input class="input" id="add-name-input" placeholder="e.g. redis" spellcheck="false"></div>
    <div class="actions">
      <button class="btn ghost" id="add-cancel">Cancel</button>
      <button class="btn primary" id="add-go">Add repository</button>
    </div>`,
  {
    onMount() {
      $("#add-cancel").addEventListener("click", closeModal);
      $$("#add-tabs .tab").forEach((t) => t.addEventListener("click", () => {
        $$("#add-tabs .tab").forEach((x) => x.classList.toggle("active", x === t));
        $("#add-url").classList.toggle("hidden", t.dataset.add !== "url");
        $("#add-zip").classList.toggle("hidden", t.dataset.add !== "zip");
      }));
      $("#add-go").addEventListener("click", async () => {
        const name = $("#add-name-input").value.trim();
        const isUrl = !$("#add-url").classList.contains("hidden");
        const btn = $("#add-go");
        btn.disabled = true;
        btn.textContent = "Working…";
        try {
          let res;
          if (isUrl) {
            const url = $("#add-url-input").value.trim();
            if (!url) throw new Error("enter a repository URL");
            res = await api.addRepoUrl(url, name);
          } else {
            const file = $("#add-zip-input").files[0];
            if (!file) throw new Error("choose a .zip file");
            res = await api.addRepoZip(file, name);
          }
          closeModal();
          toast(`${res.repo.name}: ingestion started`, "success");
          await loadRepos({ silent: true });
          await selectRepo(res.repo.id);
        } catch (e) {
          toast(e.message, "error");
          btn.disabled = false;
          btn.textContent = "Add repository";
        }
      });
    },
  });
}

async function objectExplorerModal() {
  const path = state.filters.path;
  const renderList = (children, curPath, loading = false) => {
    if (loading) return `<div class="tree-row"><span class="k">loading…</span></div>`;
    if (!children.length) return `<div class="tree-row"><span class="k">no tracked changes below this directory</span></div>`;
    return children.map((c) => `<div class="tree-row" data-open="${esc(c.path)}" data-type="${c.type}">
        <span class="ic">${c.type === "dir" ? "▸" : "·"}</span>
        <span class="k">${esc(c.name)}${c.type === "dir" ? "/" : ""}</span>
        <span class="v">λ ${fmtK(c.churn)} · n ${fmtInt(c.modifications)}</span>
      </div>`).join("");
  };
  const root = modal(`
    <h3>Choose a file or directory</h3>
    <div class="inline" style="margin-bottom:10px">
      <div class="breadcrumb" id="exp-crumbs"></div>
      <input class="input" id="exp-search" placeholder="search paths…" spellcheck="false" style="width:220px">
    </div>
    <div class="tree-list" id="exp-list"><div class="tree-row"><span class="k">loading…</span></div></div>
    <div class="actions">
      <button class="btn ghost" id="exp-cancel">Cancel</button>
      <button class="btn primary" id="exp-use" disabled>Use this path</button>
    </div>`);
  let current = path;
  let chosen = path;

  const renderCrumbs = () => {
    const parts = current ? current.split("/") : [];
    let html = `<button data-goto="">repo root</button>`;
    let acc = "";
    parts.forEach((p, i) => {
      acc = acc ? `${acc}/${p}` : p;
      html += `<span class="sep">/</span>`;
      html += i === parts.length - 1
        ? `<span class="current">${esc(p)}</span>`
        : `<button data-goto="${esc(acc)}">${esc(p)}</button>`;
    });
    $("#exp-crumbs", root).innerHTML = html;
    $$("#exp-crumbs button", root).forEach((b) =>
      b.addEventListener("click", () => openDir(b.dataset.goto)));
  };

  const openDir = async (dir) => {
    current = dir;
    chosen = dir;
    $("#exp-use", root).disabled = !dir;
    renderCrumbs();
    $("#exp-list", root).innerHTML = renderList([], dir, true);
    try {
      const data = await api.tree(state.repoId, { ...apiFilters({ ignorePath: true }), path: dir });
      $("#exp-list", root).innerHTML = renderList(data.children, dir);
      $$("#exp-list .tree-row[data-open]", root).forEach((rowEl) =>
        rowEl.addEventListener("click", () => {
          if (rowEl.dataset.type === "dir") openDir(rowEl.dataset.open);
          else { chosen = rowEl.dataset.open; $("#exp-use", root).disabled = false; toast(`selected file ${chosen}`); }
        }));
    } catch (e) {
      $("#exp-list", root).innerHTML = `<div class="tree-row"><span class="k neg">${esc(e.message)}</span></div>`;
    }
  };

  $("#exp-cancel", root).addEventListener("click", closeModal);
  $("#exp-use", root).addEventListener("click", () => { closeModal(); setPath(chosen); });
  const search = debounce(async () => {
    const q = $("#exp-search", root).value.trim();
    if (q.length < 2) return openDir(current);
    try {
      const data = await api.searchObjects(state.repoId, q);
      $("#exp-list", root).innerHTML = data.results.length
        ? data.results.map((p) => `<div class="tree-row" data-pick="${esc(p)}">
            <span class="ic">·</span><span class="k">${esc(p)}</span></div>`).join("")
        : `<div class="tree-row"><span class="k">no matches</span></div>`;
      $$("#exp-list .tree-row[data-pick]", root).forEach((rowEl) =>
        rowEl.addEventListener("click", () => {
          chosen = rowEl.dataset.pick;
          $("#exp-use", root).disabled = false;
        }));
    } catch (e) { toast(e.message, "error"); }
  }, 280);
  $("#exp-search", root).addEventListener("input", search);
  await openDir(current);
}

/* ---------------------------------------------------------------- export */

function exportCsv() {
  const d = state.dashboard;
  if (!d) { toast("nothing to export", "error"); return; }
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [];
  lines.push(["section", "key", "value"].map(q).join(","));
  const repo = state.repos.find((r) => r.id === state.repoId);
  lines.push(["meta", "repository", q(repo ? repo.name : "")].join(","));
  lines.push(["meta", "ref", q(state.ref)].join(","));
  lines.push(["meta", "path", q(d.filters.path || "")].join(","));
  lines.push(["meta", "commit_set_size", d.summary.commit_count].join(","));
  const pushRow = (section, r) => lines.push([
    q(section), q(r.path), r.added, r.removed, r.growth, r.churn,
    r.modifications, r.modification_frequency.toFixed(6), r.churn_rate.toFixed(6),
  ].join(","));
  lines.push(["path", "added(+l)", "removed(-l)", "growth(delta)", "churn(lambda)", "modifications(n)", "mod_frequency(eta)", "churn_rate(rho)"].map(q).join(","));
  pushRow("repo", d.repo_metrics);
  d.top_files.forEach((r) => pushRow("file", r));
  d.top_dirs.forEach((r) => pushRow("dir", r));
  lines.push("");
  lines.push(["author", "commits", "added", "removed", "churn", "ownership"].map(q).join(","));
  d.authors.forEach((a) => lines.push([q(a.name), a.commits, a.added, a.removed, a.churn, a.ownership.toFixed(6)].join(",")));

  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `rat-${(repo ? repo.name : "repo").replace(/\W+/g, "_")}-metrics.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast("CSV exported", "success");
}

/* ---------------------------------------------------------------- tabs & events */

function switchTab(tab) {
  state.tab = tab;
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  if (tab === "dashboard") {
    if (state.dashboard) renderDashboard();
    else setPage(state.repoId ? "none" : "hero");
    if (state.dashboard) $("#filters").classList.remove("hidden");
  } else if (tab === "authors") {
    loadAuthorsTab().catch((e) => toast(e.message, "error"));
    $("#filters").classList.add("hidden");
  } else if (tab === "compare") {
    loadCompareTab().catch((e) => toast(e.message, "error"));
    $("#filters").classList.add("hidden");
  }
  if (!state.repoId && tab !== "compare") { setPage("hero"); }
}

function wireEvents() {
  $("#btn-add-repo").addEventListener("click", addRepoModal);
  $("#btn-add-repo-hero").addEventListener("click", addRepoModal);
  $("#btn-export").addEventListener("click", exportCsv);
  $("#btn-pick-path").addEventListener("click", objectExplorerModal);
  $("#btn-clear-path").addEventListener("click", () => setPath(""));

  $$("#tabs .tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  $("#btn-refresh").addEventListener("click", async () => {
    const repo = state.repos.find((r) => r.id === state.repoId);
    if (!repo) return;
    if (repo.source_type !== "url") { toast("refresh is for cloned repositories — use Rebuild for zip uploads", "error"); return; }
    try {
      await api.refreshRepo(repo.id);
      toast("fetching updates…");
      await loadRepos({ silent: true });
    } catch (e) { toast(e.message, "error"); }
  });
  $("#btn-rebuild").addEventListener("click", async () => {
    if (!state.repoId) return;
    if (!(await modalConfirm("Rebuild repository", "Re-parse the full history from scratch? Manual author merges are kept. This can take a while for large repositories.", { confirmLabel: "Rebuild" }))) return;
    try {
      await api.rebuildRepo(state.repoId);
      toast("rebuilding…");
      await loadRepos({ silent: true });
    } catch (e) { toast(e.message, "error"); }
  });
  $("#btn-delete").addEventListener("click", async () => {
    const repo = state.repos.find((r) => r.id === state.repoId);
    if (!repo) return;
    if (!(await modalConfirm("Delete repository", `Remove “${repo.name}” and all of its analysis data from disk?`, { danger: true, confirmLabel: "Delete" }))) return;
    try {
      await api.deleteRepo(repo.id);
      toast("repository deleted", "success");
      state.repoId = null;
      state.dashboard = null;
      state.meta = null;
      await loadRepos({ silent: true });
      if (state.repos.length) await selectRepo(state.repos[0].id);
      else { setPage("hero"); $("#filters").classList.add("hidden"); }
      writeHash();
    } catch (e) { toast(e.message, "error"); }
  });

  // refs
  $("#ref-select").addEventListener("change", async (ev) => {
    if (ev.target.value === "__custom__") { $("#ref-custom").classList.remove("hidden"); $("#ref-custom").focus(); return; }
    state.ref = ev.target.value;
    writeHash();
    loadDashboard();
  });
  $("#ref-custom-toggle").addEventListener("click", () => {
    const inp = $("#ref-custom");
    inp.classList.toggle("hidden");
    if (!inp.classList.contains("hidden")) inp.focus();
  });
  $("#ref-custom").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && ev.target.value.trim()) {
      state.ref = ev.target.value.trim();
      writeHash();
      loadDashboard();
    }
  });

  // date range
  $$("#range-quick button").forEach((b) =>
    b.addEventListener("click", () => {
      const days = Number(b.dataset.days);
      state.filters._quickDays = days;
      const anchor = (state.meta && state.meta.last_commit_ts) || Math.floor(Date.now() / 1000);
      state.filters.tsFrom = days ? anchor - days * 86400 : 0;
      state.filters.tsTo = null;
      syncFilterInputs();
      writeHash();
      loadDashboard();
    }));
  $("#ts-from").addEventListener("change", (ev) => {
    state.filters.tsFrom = fromLocalInput(ev.target.value) || 0;
    state.filters._quickDays = null;
    writeHash(); loadDashboard();
  });
  $("#ts-to").addEventListener("change", (ev) => {
    state.filters.tsTo = fromLocalInput(ev.target.value);
    state.filters._quickDays = null;
    writeHash(); loadDashboard();
  });

  // commit mode
  $$("#commit-mode button").forEach((b) =>
    b.addEventListener("click", () => {
      const mode = b.dataset.mode;
      if (mode === "manual" && state.selectedCommits.size === 0) {
        toast("check commits in the Commits panel first, then “Use selected commits”", "error");
        return;
      }
      state.filters.mode = mode;
      if (mode === "range") state.filters.commits = [];
      writeHash(); renderSelectionInfo(); loadDashboard();
    }));

  $("#commit-search").addEventListener("input", debounce((ev) => {
    state.commitSearch = ev.target.value.trim();
    state.commitPage = 1;
    loadCommitsList();
  }, 300));
  $("#btn-apply-selection").addEventListener("click", () => {
    if (!state.selectedCommits.size) return;
    state.filters.mode = "manual";
    writeHash(); renderSelectionInfo(); loadDashboard();
  });
  $("#btn-clear-selection").addEventListener("click", () => {
    state.selectedCommits.clear();
    if (state.filters.mode === "manual") state.filters.mode = "range";
    writeHash(); renderSelectionInfo(); loadDashboard();
    toast("commit selection cleared");
  });
  $("#btn-select-filtered").addEventListener("click", () => selectAllFiltered().catch((e) => toast(e.message, "error")));

  // author filter popover
  $("#author-filter-btn").addEventListener("click", async (ev) => {
    ev.stopPropagation();
    const pop = $("#author-filter-pop");
    if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
    pop.innerHTML = `<div class="item"><span class="k">loading authors…</span></div>`;
    pop.classList.remove("hidden");
    try {
      const data = await api.authors(state.repoId);
      const counts = new Map();
      for (const a of data.identities) {
        const name = a.canonical || a.key;
        counts.set(name, (counts.get(name) || 0) + a.commits);
      }
      const rows = [...counts.entries()].sort((a, b) => b[1] - a[1]);
      pop.innerHTML = `<div class="item"><input type="text" class="input" style="width:100%" placeholder="filter…" id="af-search"></div>` +
        rows.map(([name, n]) => `<label class="item"><input type="checkbox" data-author="${esc(name)}" ${state.filters.authors.includes(name) ? "checked" : ""}>
          <span class="k">${esc(name)}</span><span class="c">${fmtInt(n)}</span></label>`).join("") +
        `<div class="inline" style="margin-top:8px"><button class="btn ghost small" id="af-clear">Clear</button>
         <button class="btn primary small" id="af-apply">Apply</button></div>`;
      $("#af-search").addEventListener("input", (e) => {
        const q = e.target.value.toLowerCase();
        $$("label.item", pop).forEach((row) => {
          row.style.display = row.textContent.toLowerCase().includes(q) ? "" : "none";
        });
      });
      $("#af-clear").addEventListener("click", () => {
        $$("input[data-author]", pop).forEach((cb) => { cb.checked = false; });
      });
      $("#af-apply").addEventListener("click", () => {
        state.filters.authors = $$("input[data-author]:checked", pop).map((cb) => cb.dataset.author);
        pop.classList.add("hidden");
        renderAuthorFilterButton();
        writeHash();
        loadDashboard();
      });
    } catch (e) { pop.classList.add("hidden"); toast(e.message, "error"); }
  });
  document.addEventListener("click", (ev) => {
    const pop = $("#author-filter-pop");
    if (!pop || pop.classList.contains("hidden")) return;
    if (!ev.target.closest("#author-filter-pop") && !ev.target.closest("#author-filter-btn")) pop.classList.add("hidden");
  });

  // authors tab
  $("#btn-merge-authors").addEventListener("click", () =>
    doMerge($("#merge-name").value, [...state.mergeSelection]));

  // charts react to resize
  const ro = new ResizeObserver(debounce(() => {
    if (!state.dashboard || state.tab !== "dashboard") return;
    renderTimeline($("#chart-timeline"), state.dashboard.timeline.points, state.dashboard.timeline.bucket_seconds);
    renderTreemap($("#chart-treemap"), state.dashboard.tree, (p) => setPath(p));
  }, 200));
  ro.observe($("#page-dashboard"));

  window.addEventListener("hashchange", async () => {
    if (state.ignoreHash) return;
    const h = readHash();
    if (h.repoId && h.repoId !== state.repoId) {
      await selectRepo(h.repoId, { keepFilters: true });
      return;
    }
    if (!state.repoId) return;
    // same repository: adopt whatever the new hash changed (ref, path, time
    // range, authors, manual commit list) — keeps deep links shareable
    let dirty = false;
    const ref = h.ref || "HEAD";
    if (ref !== state.ref) { state.ref = ref; await loadMeta(); dirty = true; }
    const path = h.path || "";
    if (path !== state.filters.path) { state.filters.path = path; dirty = true; }
    const tsFrom = h.tsFrom || 0;
    const tsTo = h.tsTo || null;
    if (tsFrom !== state.filters.tsFrom) { state.filters.tsFrom = tsFrom; dirty = true; }
    if (tsTo !== state.filters.tsTo) { state.filters.tsTo = tsTo; dirty = true; }
    const authors = h.authors || [];
    if (authors.join("|") !== state.filters.authors.join("|")) { state.filters.authors = authors; dirty = true; }
    if (h.mode === "manual") {
      if (state.filters.mode !== "manual" || [...state.selectedCommits].join(",") !== h.commits.join(",")) {
        state.filters.mode = "manual";
        state.selectedCommits = new Set(h.commits);
        dirty = true;
      }
    } else if (state.filters.mode === "manual") {
      state.filters.mode = "range";
      state.selectedCommits.clear();
      dirty = true;
    }
    if (dirty) {
      syncFilterInputs();
      loadDashboard();
    }
  });
}

/* ---------------------------------------------------------------- boot */

async function boot() {
  wireEvents();
  try {
    await loadRepos({ silent: true });
  } catch (e) {
    toast("cannot reach the RAT backend: " + e.message, "error");
    return;
  }
  const h = readHash();
  if (h.repoId) state.repoId = h.repoId;
  if (h.ref) state.ref = h.ref;
  if (h.path) state.filters.path = h.path;
  if (h.tsFrom) state.filters.tsFrom = h.tsFrom;
  if (h.tsTo) state.filters.tsTo = h.tsTo;
  if (h.authors) state.filters.authors = h.authors;
  if (h.mode === "manual" && h.commits) {
    state.filters.mode = "manual";
    h.commits.forEach((x) => state.selectedCommits.add(x));
  }
  if (!state.repoId && state.repos.length) state.repoId = state.repos[0].id;
  const active = state.repos.find((r) => r.id === state.repoId);
  if (active) {
    await bootForRepo();
    if (state.tab === "dashboard" && state.dashboard) switchTab("dashboard");
  } else {
    setPage("hero");
  }
}

window.addEventListener("unhandledrejection", (ev) => {
  if (ev.reason && ev.reason.message) toast(ev.reason.message, "error");
});

boot();
