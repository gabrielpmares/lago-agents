"use strict";

/* ================================================================ helpers */

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const icon = (name, style = "ph") => h("i", { class: `${style} ph-${name}`, "aria-hidden": "true" });
const setsEqual = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));
const byId = (id) => document.getElementById(id);
/* Like el.replaceChildren, but skips null/false (the DOM would print them as text). */
const fill = (el, ...children) => el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
const fmtPct = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const listFmt = new Intl.ListFormat(undefined, { style: "long", type: "conjunction" });

/* ================================================================ bridge */

const bridge = (() => {
  let seq = 0;
  const pending = new Map();
  window.lagoBridge = {
    resolve(id, ok, data) {
      const call = pending.get(id);
      if (!call) return;
      pending.delete(id);
      ok ? call.resolve(data) : call.reject(new Error(data));
    },
  };
  return {
    call(method, params = {}) {
      const id = ++seq;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        window.webkit.messageHandlers.lago.postMessage(JSON.stringify({ id, method, params }));
      });
    },
  };
})();

/* ================================================================ feedback */

/* kind: ok, error, or ring (something needs you; `onClick` takes you there). */
function toast(message, kind = "ok", onClick = null) {
  const iconName = { error: "warning-circle", ring: "bell-ringing" }[kind] || "check-circle";
  const el = onClick
    ? h("div", { class: `toast ${kind} clickable`, role: "button", tabindex: 0, onclick: () => { el.remove(); onClick(); },
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.remove(); onClick(); } } },
      icon(iconName, "ph-fill"), h("span", {}, message), icon("caret-right"))
    : h("div", { class: `toast ${kind}` }, icon(iconName, "ph-fill"), h("span", {}, message));
  document.querySelector(".toasts").append(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  }, kind === "error" ? 6000 : kind === "ring" ? 9000 : 3200);
}

async function runAction(button, work, successMessage) {
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  try {
    const res = await work();
    if (res && res.ok === false) toast(res.message || "That didn't work.", "error");
    else toast(successMessage || (res && res.message) || "Done.");
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
}

/* ================================================================ dialogs & menus */

function openDialog(build, { wide = false } = {}) {
  const dialog = byId("dialog");
  dialog.className = `dialog${wide ? " wide" : ""}`;
  dialog.replaceChildren();
  return new Promise((resolve) => {
    const close = (value) => {
      dialog.close();
      resolve(value);
    };
    dialog.append(build(close));
    dialog.onclose = () => resolve(undefined);
    dialog.onclick = (event) => { if (event.target === dialog) close(undefined); };
    dialog.showModal();
    const first = dialog.querySelector("[autofocus], input, select, button.primary");
    if (first) first.focus();
  });
}

function confirmDialog({ title, body, confirm = "Confirm", danger = false }) {
  return openDialog((close) => h("form", {
      method: "dialog",
      onsubmit: (event) => { event.preventDefault(); close(true); },
    },
    h("h2", {}, title),
    body ? h("p", { class: "desc" }, body) : null,
    h("div", { class: "dialog-actions" },
      h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Cancel"),
      h("button", { type: "submit", class: `btn ${danger ? "danger" : "primary"}`, autofocus: true }, confirm))));
}

function promptDialog({ title, label, value = "", placeholder = "", help = "", confirm = "Save", validate }) {
  return openDialog((close) => {
    const inputId = "prompt-input";
    const input = h("input", { id: inputId, class: "input", name: "value", value, placeholder,
      autocomplete: "off", spellcheck: "false", autofocus: true });
    const error = h("p", { class: "error", id: "prompt-error", hidden: true });
    return h("form", {
        onsubmit: (event) => {
          event.preventDefault();
          const text = input.value.trim();
          const problem = !text ? "This can't be empty." : validate ? validate(text) : null;
          if (problem) {
            error.textContent = problem;
            error.hidden = false;
            input.setAttribute("aria-invalid", "true");
            input.setAttribute("aria-describedby", "prompt-error");
            input.focus();
            return;
          }
          close(text);
        },
      },
      h("h2", {}, title),
      h("div", { class: "field" },
        h("label", { for: inputId }, label), input,
        help ? h("p", { class: "help" }, help) : null, error),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, confirm)));
  });
}

let openMenu = null;
function showMenu(x, y, items) {
  closeMenu();
  const menu = h("div", { class: "menu", role: "menu" },
    items.map((item) => h("button", {
      role: "menuitem", class: item.danger ? "danger" : "", disabled: item.disabled,
      onclick: () => { closeMenu(); item.action(); },
    }, icon(item.icon), item.label)));
  document.body.append(menu);
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, innerWidth - rect.width - 8)}px`;
  menu.style.top = `${Math.min(y, innerHeight - rect.height - 8)}px`;
  openMenu = menu;
  menu.querySelector("button:not([disabled])")?.focus();
}
function closeMenu() {
  if (openMenu) openMenu.remove();
  openMenu = null;
}
document.addEventListener("pointerdown", (event) => {
  if (openMenu && !openMenu.contains(event.target)) closeMenu();
});

/* ================================================================ shared view bits */

function pageHead(title, description, onRefresh) {
  return h("header", { class: "page-head" },
    h("div", {}, h("h1", {}, title), h("p", {}, description)),
    onRefresh ? h("button", { class: "btn icon ghost", "aria-label": "Refresh", title: "Refresh",
      onclick: onRefresh }, icon("arrow-clockwise")) : null);
}

const skeleton = (rows = 4) => h("div", { class: "skeleton", "aria-label": "Loading…" },
  Array.from({ length: rows }, () => h("span")));

function errorBox(error, retry) {
  return h("div", { class: "error-box", role: "alert" },
    h("p", {}, `Couldn't load this: ${error.message}`),
    h("button", { class: "btn small", style: "margin-top:10px", onclick: retry }, icon("arrow-clockwise"), "Try Again"));
}

/* ================================================================ agents: shared */

const STATUS_LABELS = {
  working: "Working",
  idle: "Idle",
  needs_approval: "Needs approval",
  needs_input: "Needs input",
  needs_you: "Waiting for you",
  waiting_on_team: "Waiting on team",
  over_budget: "Out of budget",
  agents_working: "Agents working",
  plan_waiting: "Plan to approve",
  solved: "Fully done",
  starting: "Starting",
  connecting: "Connecting",
  stopped: "Stopped",
  exited: "Crashed",
};
const NEEDS_ATTENTION = new Set(["needs_approval", "needs_input", "needs_you", "over_budget", "exited"]);
const RUNNING = (status) => status !== "stopped" && status !== "exited";
const dollars = (n) => `$${(n || 0).toFixed(2)}`;
/* Spend against its budgets (budgets.py), e.g. "$3.20 of $10.00 · session $12.40 of $40.00". */
const budgetText = (budgets) => (budgets || []).filter((b) => b.limit)
  .map((b) => `${b.kind === "session" ? "session " : ""}${dollars(b.spent)} of ${dollars(b.limit)}`).join(" · ");

/* Extend and Stop for an agent a budget stopped: Extend gives the budget another default
   amount and lets the agents it stopped carry on; Stop ends them and fails their tasks. */
function budgetButtons(agentId, after) {
  return [
    h("button", { class: "btn small primary", onclick: (e) => runAction(e.currentTarget, () => bridge.call("extend_budget", { agent_id: agentId })).then(after) },
      icon("coins"), "Extend Budget"),
    h("button", { class: "btn small", onclick: async (e) => {
      const button = e.currentTarget;
      const ok = await confirmDialog({ title: "Stop this work?", body: "Ends the agents this budget stopped. Their open tasks fail, so whoever handed them out hears about it.", confirm: "Stop", danger: true });
      if (ok) runAction(button, () => bridge.call("stop_over_budget", { agent_id: agentId })).then(after);
    } }, icon("stop"), "Stop")];
}
const MODELS = [["", "Definition default"], ["fable", "Fable"], ["opus", "Opus"], ["sonnet", "Sonnet"], ["haiku", "Haiku"]];
const PERMISSION_MODES = [["", "Team default"], ["acceptEdits", "Accept edits"], ["auto", "Auto"],
  ["manual", "Ask for everything"], ["plan", "Plan only"], ["bypassPermissions", "Bypass all (risky)"]];
const TEAM_ICONS = ["users-three", "megaphone", "code", "bank", "robot", "rocket-launch", "chart-line",
  "flask", "pen-nib", "shield-check", "globe", "briefcase", "lightbulb", "database", "cube"];

const pill = (status) => h("span", { class: `pill ${status}` }, STATUS_LABELS[status] || status);

function initials(name) {
  const words = name.split(/\s+/).filter((w) => /\p{L}/u.test(w));
  return (words.length > 1 ? words[0][0] + words[1][0] : name.slice(0, 2)).toUpperCase();
}

function humanize(definition) {
  const words = definition.split(/[-_]/).filter(Boolean);
  if (words.length > 2 && words[0].length <= 3) words.shift();
  return words.map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
}

function slug(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "agent";
}

function uniqueId(base, taken) {
  let id = slug(base);
  for (let n = 2; id in taken; n++) id = `${slug(base).slice(0, 36)}-${n}`;
  return id;
}

const relFmt = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "short" });
function ago(seconds) {
  if (!seconds) return "";
  const diff = Date.now() / 1000 - seconds;
  if (diff < 45) return "just now";
  if (diff < 3600) return relFmt.format(-Math.round(diff / 60), "minute");
  if (diff < 86400) return relFmt.format(-Math.round(diff / 3600), "hour");
  return relFmt.format(-Math.round(diff / 86400), "day");
}

function duration(seconds) {
  if (!seconds) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");

function agentName(id) {
  if (id === null || id === undefined) return "You";
  return state.config.agents[id]?.name || id;
}

function reportsOf(id) {
  return Object.entries(state.config.agents).filter(([, a]) => a.reports_to === id).map(([rid]) => rid);
}

function teamLead(teamId) {
  return Object.entries(state.config.agents).find(([, a]) => a.team === teamId && !a.reports_to)?.[0] ?? null;
}

function liveOf(id) {
  return state.live?.agents[id] || { status: "stopped", detail: "", open_tasks: 0, task: null };
}

/* ================================================================ state & router */

const state = {
  config: null,
  definitions: [],
  catalog: { agents: [], templates: [], folder: "" },
  swarmTeam: null,
  live: null,
  terminalOpen: (() => { try { return localStorage.getItem("lago.terminalOpen") === "1"; } catch { return false; } })(),
  lagoTerminal: false,   // Lago's page is a conversation; his terminal shows on request
  lagoRecording: false,
  showToolOutput: false,
};

/* Terminals stay out of the way until you ask to watch one; the choice is remembered. */
function toggleTerminal() {
  state.terminalOpen = !state.terminalOpen;
  try { localStorage.setItem("lago.terminalOpen", state.terminalOpen ? "1" : "0"); } catch { /* not kept: fine */ }
}

const views = {};
let currentView = null;
let viewToken = 0;
let viewCleanup = null;
/* The page's handler for the session in the terminal pane ending (the window closes the pane). */
let terminalEnded = null;
window.lagoTerminalEnded = (id) => terminalEnded?.(id);
const liveListeners = new Set();

/* Extras (features.py) and the pages that are theirs: off, a page leaves the menu and the router. */
const FEATURE_VIEWS = { map: "map", replay: "map", routines: "routines", people: "people", accounts: "budgets", phone: "phone" };
const featureOn = (id) => state.features?.[id] !== false;

function applyFeatures(features, list) {
  state.features = features || {};
  state.featureList = list || [];
  document.querySelectorAll(".nav a[data-view]").forEach((a) => {
    const feature = FEATURE_VIEWS[a.dataset.view];
    if (feature) a.closest("li").hidden = !featureOn(feature);
  });
}

/* Switch extras on and off. Saving re-reads which are on and redraws the page. */
function featuresDialog() {
  const chosen = Object.fromEntries((state.featureList || []).map((f) => [f.id, featureOn(f.id)]));
  return openDialog((close) => h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const ok = await runAction(e.submitter, async () => {
        for (const [id, on] of Object.entries(chosen)) {
          if (on !== featureOn(id)) await bridge.call("set_feature", { feature: id, on });
        }
        return { ok: true, message: "Saved." };
      });
      if (!ok) return;
      const init = await bridge.call("init");
      applyFeatures(init.features, init.feature_list);
      applyPersona(init.persona || "lago");
      refreshAccountChip();
      close(true);
      navigate();
    } },
    h("h2", {}, "Features"),
    h("p", { class: "desc" }, "Extras beyond the basics. Off, a feature's pages and settings leave the app and nothing of it runs."),
    (state.featureList || []).map((f) => h("label", { class: "switch-row feature-row" },
      h("button", { class: "switch", role: "switch", type: "button", "aria-checked": String(chosen[f.id]),
        onclick: (e) => { chosen[f.id] = !chosen[f.id]; e.currentTarget.setAttribute("aria-checked", String(chosen[f.id])); } }),
      h("span", {}, h("b", {}, f.name), h("small", { class: "help faint" }, f.about)))),
    h("div", { class: "dialog-actions" },
      h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Cancel"),
      h("button", { type: "submit", class: "btn primary" }, "Save"))));
}

function navigate() {
  const home = state.web && !(state.me?.owner || state.me?.admin) ? (featureOn("map") ? "map" : "tasks") : "orchestrator";
  const [name, param, sub, extra] = decodeURIComponent(location.hash.slice(1) || home).split("/");
  const off = FEATURE_VIEWS[name] && !featureOn(FEATURE_VIEWS[name]);
  const view = views[name] && !off ? name : home;
  closeMenu();
  if (viewCleanup) viewCleanup();
  viewCleanup = null;
  terminalEnded = null;
  liveListeners.clear();
  currentView = view;
  const token = ++viewToken;
  const navName = { agent: "overview", pipeline: "projects", pipelines: "projects", project: "projects", work: "tasks",
    teams: "swarm", sessions: "swarm", find: "swarm", import: "swarm", replay: "map" }[view] || view;
  document.querySelectorAll(".nav a").forEach((a) => {
    if (a.dataset.view === navName) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (!["agent", "work", "project", "orchestrator", "phone"].includes(view)) bridge.call("hide_terminal").catch(() => {});
  const main = byId("main");
  const container = h("div", { class: "view view-enter" });
  main.replaceChildren(container);
  main.scrollTop = 0;
  views[view](container, () => token === viewToken, param, sub, extra);
}

async function saveConfig(config) {
  await bridge.call("save_config", { config });
  state.config = withSwarm(config);
  state.definitions = await bridge.call("definitions");
}

/* The swarm team (the Conductor and the agents it woke) isn't in config.json: it's added
   from the live poll, so it stays current while the Conductor wakes agents. */
function withSwarm(config) {
  const swarmAgents = state.live?.swarm_agents || Object.fromEntries(Object.entries(state.config?.agents || {}).filter(([, a]) => a.swarm));
  const agents = Object.fromEntries(Object.entries(config.agents).filter(([, a]) => !a.swarm));
  const teams = Object.fromEntries(Object.entries(config.teams).filter(([, t]) => !t.swarm));
  return { ...config, teams: state.swarmTeam ? { swarm: state.swarmTeam, ...teams } : teams, agents: { ...agents, ...swarmAgents } };
}

/* A copy of the config without the swarm: what the Teams page edits. */
function userConfig() {
  const config = structuredClone(state.config);
  config.teams = Object.fromEntries(Object.entries(config.teams).filter(([, t]) => !t.swarm));
  config.agents = Object.fromEntries(Object.entries(config.agents).filter(([, a]) => !a.swarm));
  return config;
}

/* The overview is polled all the time: it feeds the attention badge and the views. */
/* An account ran out: say what Lago did, or ask you to log in to the other one. Each alert
   shows once. */
function showAccountAlert(alert) {
  if (!alert) return;
  let seen = null;
  try { seen = localStorage.getItem("lago.accountAlert"); } catch { /* shows again next launch */ }
  if (seen === alert.id || state.accountAlertShown === alert.id) return;
  if (alert.kind === "login" && byId("dialog").open) return;  // ask once the open dialog closes
  state.accountAlertShown = alert.id;
  try { localStorage.setItem("lago.accountAlert", alert.id); } catch { /* fine */ }
  refreshAccountChip();
  if (alert.kind === "login") {
    confirmDialog({ title: "An account ran out of usage", body: alert.text, confirm: "Log In Now" })
      .then((ok) => { if (ok) location.hash = `#accounts/login/${alert.account}`; });
  } else {
    toast(alert.text, alert.kind === "out" ? "error" : "ok");
  }
}

async function pollLive() {
  try {
    const live = await bridge.call("overview");
    showAccountAlert(live.account_alert);
    if (live.persona && live.persona !== state.persona) applyPersona(live.persona);  // "Hey Chip" switched him
    const changed = JSON.stringify(live) !== JSON.stringify(state.live);
    state.live = live;
    if (changed) state.config = withSwarm(state.config);
    const waiting = live.pipelines_waiting || 0;
    const pipelinesBadge = byId("pipelines-count");
    pipelinesBadge.hidden = waiting === 0;
    pipelinesBadge.textContent = waiting;
    pipelinesBadge.setAttribute("aria-label", `${waiting} pipeline${waiting === 1 ? "" : "s"} waiting for your approval`);
    const reports = live.reports_unseen || 0;
    const reportsBadge = byId("reports-count");
    reportsBadge.hidden = reports === 0;
    reportsBadge.textContent = reports;
    reportsBadge.setAttribute("aria-label", `${reports} new report${reports === 1 ? "" : "s"} from work sessions`);
    const attention = overviewAgents().filter(([id]) => NEEDS_ATTENTION.has(liveOf(id).status)).length;
    const badge = byId("attention-count");
    badge.hidden = attention === 0;
    badge.textContent = attention;
    badge.setAttribute("aria-label", `${attention} need attention`);
    syncLagoLink(live);
    renderTopbar();
    announceNew(live.notifications || []);
    if (changed) liveListeners.forEach((fn) => fn());
  } catch {
    /* the next poll tries again */
  }
}

/* ================================================================ top bar: session tabs and what needs you */

const NOTE_ICONS = { secret: "key", news: "newspaper", plan: "flow-arrow", permission: "lock-key", input: "chat-circle-dots", reply: "chat-text",
  crashed: "warning-octagon", budget: "coins", decided: "gavel", question: "question", gate: "eye", blocked: "prohibit", waiting: "hourglass",
  failed: "x-circle", done: "check-circle", solved: "seal-check", account: "user-switch" };
let lastTopbar = "";

/* The open work sessions as tabs (the one you're on is current, a dot marks one that needs
   you), and the bell with how many things need you. */
function renderTopbar() {
  const live = state.live;
  if (!live) return;
  const notes = live.notifications || [];
  const urgent = notes.filter((n) => n.urgent);
  const current = location.hash.match(/^#work\/(\d+)/)?.[1];
  const key = JSON.stringify([live.open_sessions, notes, current, byId("notifications").hidden]);
  if (key === lastTopbar) return;
  lastTopbar = key;
  const needing = new Set(urgent.map((n) => n.href));
  fill(byId("session-tabs"), (live.open_sessions || []).map((s) => h("a", {
      class: "session-tab", href: `#work/${s.id}`, "aria-current": String(s.id) === current ? "page" : null,
      title: `#${s.id} ${s.title} · ${projectName(s.project)} · ${STATUS_LABELS[s.status] || s.status}` },
    h("span", { class: "tab-dot", dataset: { status: s.status }, "aria-hidden": "true" }),
    h("span", { class: "tab-id" }, `#${s.id}`), h("span", { class: "tab-title" }, s.title),
    needing.has(`#work/${s.id}`) ? h("span", { class: "tab-alert", role: "img", "aria-label": "needs you" }) : null)));
  const bell = byId("bell");
  const count = byId("bell-count");
  count.hidden = !urgent.length;
  count.textContent = urgent.length;
  bell.classList.toggle("ringing", urgent.length > 0);
  bell.setAttribute("aria-label", urgent.length ? `${urgent.length} thing${urgent.length === 1 ? "" : "s"} need you` : "Nothing needs you");
  if (!byId("notifications").hidden) renderNotifications();
}

function renderNotifications() {
  const notes = state.live?.notifications || [];
  const urgent = notes.filter((n) => n.urgent);
  const finished = notes.filter((n) => !n.urgent);
  const item = (n) => h("li", { class: `note${n.urgent ? "" : " quiet"}` },
    h("a", { class: "note-main", href: n.href, onclick: (e) => { e.preventDefault(); followNote(n); } },
      icon(NOTE_ICONS[n.kind] || "info", "ph-fill"),
      h("span", { class: "note-body" },
        h("strong", {}, n.title),
        n.context ? h("span", { class: "note-context" }, n.context) : null,
        n.text ? h("span", { class: "note-text" }, n.text) : null,
        h("span", { class: "note-when" }, ago(n.at), n.for && n.for !== state.me?.login ? ` · for ${n.for}` : ""))),
    h("button", { type: "button", class: "note-dismiss", title: "Dismiss", "aria-label": `Dismiss: ${n.title}`,
      onclick: () => bridge.call("dismiss_notification", { key: n.key }).then(pollLive).catch((e) => toast(e.message, "error")) }, icon("x")));
  fill(byId("notifications"),
    h("div", { class: "notes-head" }, h("h2", {}, "Needs You"), urgent.length ? h("span", { class: "faint" }, urgent.length) : null),
    urgent.length ? h("ul", { class: "notes", role: "list" }, urgent.map(item))
      : h("p", { class: "notes-empty" }, icon("check-circle"), "Nothing needs you right now."),
    finished.length ? [h("div", { class: "notes-head" }, h("h2", {}, "Finished")), h("ul", { class: "notes", role: "list" }, finished.map(item))] : null);
}

function followNote(n) {
  closeNotifications();
  bridge.call("notification_opened", { key: n.key }).then(pollLive).catch(() => {});
  if (location.hash === n.href) navigate(); else location.hash = n.href;
}

function toggleNotifications() {
  const panel = byId("notifications");
  if (!panel.hidden) { closeNotifications(); return; }
  renderNotifications();
  panel.hidden = false;
  byId("bell").setAttribute("aria-expanded", "true");
  panel.querySelector("a, button")?.focus();
}

function closeNotifications() {
  const panel = byId("notifications");
  if (panel.hidden) return;
  panel.hidden = true;
  byId("bell").setAttribute("aria-expanded", "false");
}

/* Something new needs you: a toast that takes you there (none for what was already there
   when the app opened: the bell shows those). */
function announceNew(notes) {
  const urgent = notes.filter((n) => n.urgent);
  if (state.noteKeys) {
    for (const n of urgent.filter((n) => !state.noteKeys.has(n.key)).slice(0, 3)) toast(n.title, "ring", () => followNote(n));
  }
  state.noteKeys = new Set(urgent.map((n) => n.key));
}

byId("bell").addEventListener("click", toggleNotifications);
document.addEventListener("click", (e) => { if (!e.target.closest(".bell-wrap")) closeNotifications(); });

/* ================================================================ dialogs */

function taskDialog(agentId, { goal = false } = {}) {
  const who = agentName(agentId);
  return openDialog((close) => {
    const title = h("input", { id: "task-title", class: "input", placeholder: "Short title (optional)", autocomplete: "off" });
    const body = h("textarea", { id: "task-body", class: "input", rows: 8, autofocus: true,
      placeholder: goal ? "What should the team achieve? Include the context, inputs and what “done” looks like…"
        : "What should this agent do? Include inputs, expected output and where to write it…" });
    const error = h("p", { class: "error", hidden: true });
    return h("form", {
        onsubmit: (event) => {
          event.preventDefault();
          if (!body.value.trim()) { error.textContent = "Describe the task first."; error.hidden = false; body.focus(); return; }
          close({ title: title.value.trim(), body: body.value.trim() });
        },
      },
      h("h2", {}, goal ? `Give ${who} a Goal` : `Give ${who} a Task`),
      h("p", { class: "desc" }, goal
        ? "The lead plans the work and delegates to the team. Its session starts if it isn't running."
        : "It's tracked as a task. The agent's session starts if it isn't running."),
      h("div", { class: "field" }, h("label", { for: "task-title" }, "Title"), title),
      h("div", { class: "field" }, h("label", { for: "task-body" }, goal ? "Goal" : "Task"), body, error),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, icon("paper-plane-tilt"), goal ? "Send Goal" : "Send Task")));
  }, { wide: true });
}

async function giveTask(agentId, options) {
  const task = await taskDialog(agentId, options);
  if (!task) return;
  await runAction(null, () => bridge.call("give_task", { agent_id: agentId, title: task.title, body: task.body }));
  pollLive();
}

async function sendMessage(agentId) {
  const text = await openDialog((close) => {
    const body = h("textarea", { id: "msg-body", class: "input", rows: 5, autofocus: true, placeholder: "Extra context, a correction, an answer…" });
    return h("form", { onsubmit: (e) => { e.preventDefault(); if (body.value.trim()) close(body.value.trim()); } },
      h("h2", {}, `Message ${agentName(agentId)}`),
      h("p", { class: "desc" }, "Typed into its session. If it's busy, it arrives when the current step ends."),
      h("div", { class: "field" }, h("label", { for: "msg-body" }, "Message"), body),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, icon("paper-plane-tilt"), "Send")));
  }, { wide: true });
  if (text) await runAction(null, () => bridge.call("send_message", { agent_id: agentId, text }));
}

/* ================================================================ overview */

views.overview = (root, alive) => {
  const charts = !state.web || state.me?.owner || state.me?.admin; // spend and history cover every project
  const historySlot = h("div", {}, charts ? skeleton(4) : null);
  const loadHistory = async () => {
    if (!charts) return;
    try {
      const data = await bridge.call("dashboard");
      if (alive()) { state.dashboard = data; dashboardHistory(historySlot, data); render(); }
    } catch (error) { if (alive()) historySlot.replaceChildren(errorBox(error, loadHistory)); }
  };
  root.append(pageHead("Dashboard", "What your agents are doing now, and how the last two weeks went.", loadHistory));
  const cardsSlot = h("div");
  const body = h("div", {}, skeleton(6));
  root.append(body, historySlot, h("div", { class: "dash-teams" }), cardsSlot);
  statusCards(cardsSlot, alive);
  const teamsSlot = root.querySelector(".dash-teams");
  const render = () => renderOverview(body, teamsSlot);
  liveListeners.add(render);
  if (state.live) render();
  pollLive();
  loadHistory();
  const timer = setInterval(() => { if (alive()) loadHistory(); else clearInterval(timer); }, 60000);
};

function renderOverview(body, teamsSlot) {
  const focused = document.activeElement?.dataset?.agent;
  const live = state.live;
  const agents = overviewAgents();
  const count = (pred) => agents.filter(([id]) => pred(liveOf(id).status)).length;
  const working = agents.filter(([id]) => ["working", "waiting_on_team"].includes(liveOf(id).status));
  // Each open task's lead, then anyone else at work (a lead's own helpers show on its card).
  const leads = agents.filter(([, a]) => a.conductor && !a.closed);
  const inProgress = [...leads, ...working.filter(([id, a]) => !a.conductor)];
  const needing = agents.filter(([id]) => NEEDS_ATTENTION.has(liveOf(id).status));
  const month = state.dashboard?.month;
  const waiting = live.pipelines_waiting || 0;

  const now = h("section", { class: "dash-now", "aria-labelledby": "dash-now" },
    h("div", { class: "dash-head" }, h("h2", { id: "dash-now", class: "label" }, `In progress${inProgress.length ? ` (${inProgress.length})` : ""}`),
      h("span", { class: "dash-head-links" }, h("a", { class: "faint", href: "#map" }, "Live Map"),
        h("button", { type: "button", class: "btn small", onclick: () => newTaskDialog() }, icon("plus"), "New Task"))),
    inProgress.length ? h("div", { class: "dash-agents" }, inProgress.slice(0, 8).map(([id, a]) => agentCard(id, Boolean(a.conductor))))
      : h("p", { class: "faint dash-quiet" }, "Nothing open and nobody working. Start a task, or ask Lago."));

  const tiles = h("div", { class: "dash-tiles" },
    tile("users-three", working.length, "Agents working", `${count((s) => s === "idle")} idle · ${needing.length} need you`, needing.length > 0),
    tile("list-checks", live.open_tasks, "Tasks in progress", state.dashboard ? `${state.dashboard.blocked} blocked` : "open tasks"),
    tile("coins", month ? money(month.spent) : "…", "Spent this month", month ? (month.budget ? `of ${money(month.budget)} budget` : "No monthly budget") : ""),
    tile("seal-check", waiting + needing.length, "Waiting on you", waiting ? `${waiting} plan${waiting === 1 ? "" : "s"} to approve` : "Approvals and questions", waiting + needing.length > 0));

  const attention = needing.length ? h("div", { class: "attention", role: "list", "aria-label": "Needs your attention" },
    needing.map(([id, agent]) => {
      const l = liveOf(id);
      const why = {
        needs_approval: l.detail || "Waiting for permission to use a tool.",
        needs_input: "Claude Code is asking something in its terminal. Open it to answer.",
        needs_you: "Finished its turn and has an open task from you. It may be asking a question.",
        over_budget: l.detail || "It used its budget and stopped. Open it to extend the budget or stop it.",
        exited: l.detail || "The session crashed. Open it to restart.",
      }[l.status];
      return h("div", { class: "callout", role: "listitem" }, icon("warning-circle", "ph-fill"),
        h("p", {}, h("span", { class: "who" }, agent.name), ` · ${state.config.teams[agent.team]?.name || agent.team}. ${why}`),
        h("a", { class: "btn small", href: agent.conductor ? `#work/${agent.workspace}` : agent.orchestrator ? "#orchestrator" : `#agent/${id}` }, "Open"));
    })) : null;
  const pipelinesCallout = waiting ? h("div", { class: "attention" }, h("div", { class: "callout" }, icon("flow-arrow", "ph-fill"),
    h("p", {}, h("span", { class: "who" }, `${waiting} pipeline${waiting === 1 ? "" : "s"}`), " waiting for your approval. Nothing runs until you approve."),
    h("a", { class: "btn small", href: "#pipelines" }, "Review"))) : null;
  fill(body, now, tiles, pipelinesCallout, attention);

  const teams = Object.entries(state.config.teams).filter(([, team]) => !team.swarm);
  fill(teamsSlot, teams.length ? h("section", { "aria-labelledby": "dash-teams" },
    h("div", { class: "dash-head" }, h("h2", { id: "dash-teams", class: "label" }, "Teams"), h("a", { class: "faint", href: "#teams" }, "Org charts and settings")),
    h("div", { class: "dash-team-grid" }, teams.map(([teamId, team]) => teamCard(teamId, team)))) : null);
  if (focused) body.parentElement.querySelector(`[data-agent="${CSS.escape(focused)}"]`)?.focus();
}

/* What an activity line's actor did, before its text ("finished #366 Daily MCP news"). */
const ACTIVITY_VERBS = { "task.assigned": "was given ", "task.done": "finished ", "task.failed": "failed ",
  "agent.api_error": "hit an error: ", "routine.created": "made routine ", "session.closed": "closed ",
  "connector.added": "added connector ", "github.imported": "imported " };

const money = (n) => `$${(n || 0).toLocaleString(undefined, { minimumFractionDigits: n >= 100 ? 0 : 2, maximumFractionDigits: n >= 100 ? 0 : 2 })}`;

function tile(iconName, value, label, sub, warn = false) {
  return h("div", { class: `dash-tile${warn ? " warn" : ""}` },
    h("div", { class: "dash-tile-top" }, h("b", {}, value), icon(iconName)),
    h("span", { class: "dash-tile-label" }, label), h("span", { class: "dash-tile-sub" }, sub));
}

/* ---------------------------------------------------------------- dashboard charts */

const dayLabel = (key) => { const [, m, d] = key.split("-"); return `${Number(m)}/${Number(d)}`; };
const dayShort = (key) => `${new Date(`${key}T12:00:00`).toLocaleDateString(undefined, { weekday: "short" })} ${dayLabel(key)}`;
const dayLong = (key) => new Date(`${key}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });

function dashboardHistory(slot, data) {
  const total = data.done + data.failed + data.days.reduce((n, d) => n + d.other, 0);
  const spend = data.days.reduce((n, d) => n + d.cost, 0);
  const charts = h("div", { class: "dash-charts" },
    chartCard("Tasks", `${total} in ${data.days.length} days`,
      (w) => barChart(w, data.days, [["done", "Done"], ["failed", "Failed"], ["other", "Other"]], (v) => String(v)),
      [["done", "Done"], ["failed", "Failed"], ["other", "Cancelled or open"]],
      [["Day", (d) => dayShort(d.key)], ["Done", (d) => d.done], ["Failed", (d) => d.failed], ["Other", (d) => d.other]], data.days),
    chartCard("Spend", `${money(spend)} in ${data.days.length} days`,
      (w) => barChart(w, data.days, [["cost", "Spend"]], money), null,
      [["Day", (d) => dayShort(d.key)], ["Spend", (d) => money(d.cost)]], data.days),
    chartCard("Success rate", data.rate == null ? "No finished tasks yet" : `${Math.round(data.rate * 100)}% of finished tasks done`,
      (w) => lineChart(w, data.days, "rate"), null,
      [["Day", (d) => dayShort(d.key)], ["Done of finished", (d) => (d.rate == null ? "–" : `${Math.round(d.rate * 100)}%`)]], data.days));

  const activityList = data.recent_activity.length ? h("div", { class: "dash-list" }, data.recent_activity.map((r) =>
    h("div", { class: "dash-item" }, activityIcon(r.kind),
      h("span", { class: "dash-item-main truncate", title: r.text }, h("strong", {}, actorName(r.actor)), " ", ACTIVITY_VERBS[r.kind] || "", r.text),
      h("span", { class: "faint dash-when" }, ago(r.created_at)))))
    : h("p", { class: "faint" }, "Nothing yet.");
  const tone = { done: "done", failed: "failed", working: "running", sent: "running", queued: "draft", blocked: "draft", cancelled: "cancelled" };
  const tasksList = data.recent_tasks.length ? h("div", { class: "dash-list" }, data.recent_tasks.map((t) =>
    h("a", { class: "dash-item", href: "#tasks" }, h("span", { class: `pill p-${tone[t.status] || "draft"}` }, t.status),
      h("span", { class: "dash-item-main truncate", title: t.title }, t.title),
      h("span", { class: "faint dash-who truncate" }, agentName(t.to_agent)),
      h("span", { class: "faint dash-when" }, ago(t.updated_at)))))
    : h("p", { class: "faint" }, "No tasks yet.");
  slot.replaceChildren(charts, h("div", { class: "dash-columns" },
    h("section", { "aria-labelledby": "dash-activity" }, h("div", { class: "dash-head" }, h("h2", { id: "dash-activity", class: "label" }, "Recent activity"), h("a", { class: "faint", href: "#history" }, "History")), activityList),
    h("section", { "aria-labelledby": "dash-tasks" }, h("div", { class: "dash-head" }, h("h2", { id: "dash-tasks", class: "label" }, "Recent tasks"), h("a", { class: "faint", href: "#tasks" }, "All tasks")), tasksList)));
}

/* A chart with its legend (two or more series) and a table view of the same numbers. */
function chartCard(title, subtitle, draw, legend, columns, rows) {
  const plot = h("div", { class: "viz-plot" });
  // Drawn at its real width, so text stays its size and the height stays put.
  let drawn = 0;
  new ResizeObserver(([entry]) => {
    const width = Math.round(entry.contentRect.width);
    if (width && Math.abs(width - drawn) > 2) { drawn = width; plot.replaceChildren(draw(width)); }
  }).observe(plot);
  const table = h("table", { class: "viz-table", hidden: true },
    h("thead", {}, h("tr", {}, columns.map(([name]) => h("th", { scope: "col" }, name)))),
    h("tbody", {}, rows.map((row) => h("tr", {}, columns.map(([, cell]) => h("td", {}, cell(row)))))));
  const toggle = h("button", { type: "button", class: "btn small ghost viz-toggle", "aria-pressed": "false", onclick: () => {
    const showTable = table.hidden;
    table.hidden = !showTable; plot.hidden = showTable;
    toggle.setAttribute("aria-pressed", String(showTable));
    toggle.replaceChildren(icon(showTable ? "chart-bar" : "table"), showTable ? "Chart" : "Table");
  } }, icon("table"), "Table");
  return h("article", { class: "viz-card" },
    h("div", { class: "viz-top" }, h("div", {}, h("h3", {}, title), h("p", { class: "faint" }, subtitle)), toggle),
    plot, table,
    legend ? h("div", { class: "viz-legend" }, legend.map(([key, label]) => h("span", {}, h("i", { class: `viz-key viz-${key}` }), label))) : null);
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, v);
  for (const child of children.flat()) if (child) el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return el;
}

const VIZ = { h: 176, left: 34, right: 6, top: 10, bottom: 22 };

/* A clean axis top: the next round number at or above the largest value. */
function niceMax(max) {
  if (max <= 0) return 1;
  const step = 10 ** Math.floor(Math.log10(max));
  return [1, 2, 2.5, 5, 10].map((m) => m * step).find((v) => v >= max);
}

function axes(svg, w, days, top, format) {
  const { h: ht, left, right, top: pad, bottom } = VIZ;
  const plotH = ht - pad - bottom;
  for (const frac of [0, 0.5, 1]) {
    const y = pad + plotH * (1 - frac);
    svg.append(svgEl("line", { x1: left, x2: w - right, y1: y, y2: y, class: "viz-grid" }),
      svgEl("text", { x: left - 6, y: y + 3.5, "text-anchor": "end", class: "viz-tick" }, format(top * frac)));
  }
  const band = (w - left - right) / days.length;
  [0, Math.floor(days.length / 2), days.length - 1].forEach((i, n) =>
    svg.append(svgEl("text", { x: left + band * (i + 0.5), y: ht - 6, "text-anchor": ["start", "middle", "end"][n], class: "viz-tick" }, dayLabel(days[i].key))));
  return { band, plotH, y: (v) => pad + plotH * (1 - v / top) };
}

/* Stacked columns, first series at the baseline: 4px rounded top, 2px gaps between segments. */
function barChart(w, days, series, format) {
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${VIZ.h}`, width: w, height: VIZ.h, class: "viz", role: "img", "aria-label": series.map(([, l]) => l).join(", ") + " per day" });
  const totals = days.map((d) => series.reduce((n, [k]) => n + d[k], 0));
  const top = niceMax(Math.max(...totals));
  const fmt = (v) => (format === money ? `$${Math.round(v)}` : String(Math.round(v)));
  const { band, y } = axes(svg, w, days, top, fmt);
  const width = Math.min(18, band * 0.64);
  days.forEach((d, i) => {
    const x = VIZ.left + band * i + (band - width) / 2;
    let base = y(0);
    const parts = series.filter(([k]) => d[k] > 0);
    parts.forEach(([k], n) => {
      const height = Math.max(1, y(0) - y(d[k])) - (n ? 2 : 0);
      const topY = base - height - (n ? 2 : 0);
      const last = n === parts.length - 1;
      const r = last ? Math.min(4, height, width / 2) : 0;
      svg.append(svgEl("path", { class: `viz-mark viz-${series.length > 1 ? k : "single"}`,
        d: `M${x},${topY + height} V${topY + r} Q${x},${topY} ${x + r},${topY} H${x + width - r} Q${x + width},${topY} ${x + width},${topY + r} V${topY + height} Z` }));
      base = topY;
    });
    hoverBand(svg, VIZ.left + band * i, band, dayLong(d.key), series.map(([k, label]) => [label, format(d[k]), series.length > 1 ? k : "single"]));
  });
  return svg;
}

/* A 2px line with 8px dots; days with nothing finished leave a gap. */
function lineChart(w, days, key) {
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${VIZ.h}`, width: w, height: VIZ.h, class: "viz", role: "img", "aria-label": "Success rate per day" });
  const { band, y } = axes(svg, w, days, 1, (v) => `${Math.round(v * 100)}%`);
  const x = (i) => VIZ.left + band * (i + 0.5);
  let d = "";
  days.forEach((day, i) => { if (day[key] != null) d += `${d && days[i - 1]?.[key] != null ? "L" : "M"}${x(i)},${y(day[key])} `; });
  svg.append(svgEl("path", { d, class: "viz-line" }));
  days.forEach((day, i) => {
    if (day[key] != null) svg.append(svgEl("circle", { cx: x(i), cy: y(day[key]), r: 4, class: "viz-dot" }));
    hoverBand(svg, VIZ.left + band * i, band, dayLong(day.key),
      [["Done of finished", day[key] == null ? "nothing finished" : `${Math.round(day[key] * 100)}%`, "single"],
       ["Finished", String(day.done + day.failed), null]]);
  });
  return svg;
}

/* The whole day's column is the hover and focus target; one tooltip shows every series. */
function hoverBand(svg, x, width, title, rows) {
  const hit = svgEl("rect", { x, y: 0, width, height: VIZ.h - VIZ.bottom, class: "viz-hit", tabindex: "0",
    "aria-label": `${title}: ${rows.map(([l, v]) => `${l} ${v}`).join(", ")}` });
  const show = () => {
    const tip = vizTip();
    tip.replaceChildren(h("strong", {}, title), ...rows.map(([label, value, key]) =>
      h("div", { class: "viz-tip-row" }, key ? h("i", { class: `viz-line-key viz-${key}` }) : h("i", { class: "viz-line-key" }), h("b", {}, value), h("span", {}, label))));
    const box = hit.getBoundingClientRect();
    tip.hidden = false;
    const left = Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, box.left + box.width / 2 - tip.offsetWidth / 2));
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(8, box.top - tip.offsetHeight - 6)}px`;
    hit.classList.add("on");
  };
  const hide = () => { vizTip().hidden = true; hit.classList.remove("on"); };
  hit.addEventListener("pointerenter", show);
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("focus", show);
  hit.addEventListener("blur", hide);
  svg.append(hit);
}

function vizTip() {
  let tip = byId("viz-tip");
  if (!tip) { tip = h("div", { id: "viz-tip", class: "viz-tip", role: "tooltip", hidden: true }); document.body.append(tip); }
  return tip;
}

/* Standing "keep me posted" notes (statuscards.py): kept apart from the live re-render. */
async function statusCards(slot, alive) {
  let rows;
  try { rows = (await bridge.call("status_cards")).filter((c) => !c.summary); } catch { return; }
  if (!alive()) return;
  const reload = () => statusCards(slot, alive);
  const add = h("button", { class: "btn small", onclick: () => cardDialog(reload) }, icon("plus"), "New Card");
  if (!rows.length) {
    fill(slot, h("p", { class: "faint dash-quiet dash-cards-empty" }, icon("cards"),
      h("span", {}, "A status card follows a project or a topic and rewrites itself when something about it changes."), add));
    return;
  }
  fill(slot, h("section", { class: "dash-cards", "aria-labelledby": "dash-cards" },
    h("div", { class: "dash-head" }, h("h2", { id: "dash-cards", class: "label" }, "Status cards"), add),
    h("div", { class: "card-grid" }, rows.map((c) => h("article", { class: "status-card" },
      h("div", { class: "task-top" }, h("strong", {}, c.title),
        h("span", { class: "faint", style: "font-size:12px" }, c.generated_at ? `updated ${ago(c.generated_at)}` : "not written yet"),
        h("span", { style: "flex:1" }),
        h("button", { class: "btn small icon ghost", title: "Rewrite now", "aria-label": `Rewrite ${c.title} now`, onclick: (e) => runAction(e.currentTarget, () => bridge.call("refresh_status_card", { card_id: c.id }), "Rewritten.").then(reload) }, icon("arrow-clockwise")),
        h("button", { class: "btn small icon ghost", title: "Remove", "aria-label": `Remove ${c.title}`, onclick: (e) => runAction(e.currentTarget, () => bridge.call("archive_status_card", { card_id: c.id }), "Removed.").then(reload) }, icon("x"))),
      h("p", { class: "prewrap", style: "margin:6px 0 0" }, c.text || "Being written…"),
      h("p", { class: "faint", style: "font-size:12px;margin:6px 0 0" }, [c.project ? `Follows ${projectName(c.project)}` : "", c.query ? `“${c.query}”` : ""].filter(Boolean).join(" · "),
        c.error ? h("span", { class: "error" }, ` · last try failed: ${c.error}`) : null))))));
}

function cardDialog(done) {
  const title = h("input", { class: "input", required: true, placeholder: "e.g. Webstore launch" });
  const project = h("select", { class: "select" }, [["", "No project"], ...Object.entries(state.config.projects).map(([id, p]) => [id, p.name || id])].map(([v, l]) => h("option", { value: v }, l)));
  const query = h("input", { class: "input", placeholder: "words to follow, e.g. checkout payments" });
  return openDialog((close) => h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const ok = await runAction(e.submitter, async () => {
        const card = await bridge.call("add_status_card", { title: title.value.trim(), project: project.value, query: query.value.trim() });
        return bridge.call("refresh_status_card", { card_id: card.id });
      }, "Card added.");
      if (ok) { close(); done(); }
    } },
    h("h2", {}, "New Status Card"),
    h("p", { class: "desc" }, "Follow a project, a topic, or both. A cheap model rewrites the card from Lago's records only when they change."),
    h("div", { class: "field" }, h("label", {}, "Title"), title),
    h("div", { class: "field" }, h("label", {}, "Project"), project),
    h("div", { class: "field" }, h("label", {}, "Topic"), query),
    h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close() }, "Cancel"),
      h("button", { type: "submit", class: "btn primary" }, "Add"))));
}

function stat(value, label, warn = false) {
  return h("div", { class: `stat${warn && value ? " warn" : ""}` }, h("b", {}, value), h("span", {}, label));
}

/* One fixed team in a few lines: who's in it (running ones lit), and its lead's buttons. */
function teamCard(teamId, team) {
  const lead = teamLead(teamId);
  const members = Object.entries(state.config.agents).filter(([, a]) => a.team === teamId);
  const running = members.filter(([id]) => RUNNING(liveOf(id).status));
  const leadLive = lead ? liveOf(lead) : null;
  const shown = members.length > 8 ? 7 : 8; // one row: seven faces and "+N"
  return h("article", { class: "dash-team", "aria-labelledby": `team-${teamId}` },
    h("div", { class: "dash-team-head" },
      h("span", { class: "team-icon" }, icon(team.icon || "users-three")),
      h("div", { class: "dash-team-title" }, h("h3", { id: `team-${teamId}` }, team.name),
        h("span", { class: "faint mono truncate", title: team.cwd || "~" }, team.cwd || "~"),
        h("span", { class: "faint" }, `${members.length} agent${members.length === 1 ? "" : "s"}, `,
          running.length ? h("strong", { class: "dash-running" }, `${running.length} running`) : "none running"))),
    h("div", { class: "dash-team-people" },
      members.slice(0, shown).map(([id, a]) => h("a", { class: `mini-avatar${RUNNING(liveOf(id).status) ? " on" : ""}${id === lead ? " lead" : ""}`,
        href: `#agent/${id}`, dataset: { agent: id }, title: `${a.name}${id === lead ? " (lead)" : ""} · ${STATUS_LABELS[liveOf(id).status] || liveOf(id).status}` }, initials(a.name))),
      members.length > shown ? h("span", { class: "mini-avatar more" }, `+${members.length - shown}`) : null),
    h("div", { class: "dash-team-foot" },
      running.length ? h("button", { class: "btn small ghost", onclick: async (e) => {
        const ok = await confirmDialog({ title: `Stop ${team.name}?`, body: `Stops all ${running.length} running agent sessions in this team. Their conversations are kept and resume next time.`, confirm: "Stop Team", danger: true });
        if (ok) runAction(e.currentTarget, () => bridge.call("stop_team", { team_id: teamId })).then(pollLive);
      } }, icon("stop-circle"), "Stop") : null,
      lead && !RUNNING(leadLive.status) ? h("button", { class: "btn small", onclick: (e) =>
        runAction(e.currentTarget, () => bridge.call("start_team", { team_id: teamId })).then(pollLive) }, icon("play"), "Start Lead") : null,
      lead ? h("button", { class: "btn small primary", onclick: () => giveTask(lead, { goal: true }) }, icon("flag"), "Give Goal")
        : h("a", { class: "btn small", href: "#teams" }, "Pick a lead")));
}

/* The agents the overview shows, and its badge counts. Sleeping swarm agents (a finished
   pipeline step, even one that was killed) and closed sessions aren't part of the picture. */
function overviewAgents() {
  return Object.entries(state.config?.agents || {}).filter(([id, a]) => !a.swarm || (a.conductor ? !a.closed : isAwake(id)));
}

function isAwake(id) {
  const l = liveOf(id);
  return RUNNING(l.status) || l.open_tasks > 0;
}

function agentCard(id, isLead) {
  const agent = state.config.agents[id];
  const l = liveOf(id);
  let activity = l.detail;
  if (l.task) activity = `#${l.task.id} ${l.task.title}${l.detail ? ` · ${l.detail}` : ""}`;
  if (!activity) {
    activity = l.status === "stopped" ? (agent.conductor ? "Paused. Open the session to resume" : agent.reports_to ? "Starts on its first task" : "Not running")
      : l.status === "idle" ? `Idle ${ago(l.since) ? `since ${ago(l.since)}` : ""}` : STATUS_LABELS[l.status];
  }
  return h("a", { class: `agent-card${isLead ? " lead" : ""}`, href: agent.conductor ? `#work/${agent.workspace}` : `#agent/${id}`, dataset: { agent: id, status: l.status },
      "aria-label": `${agent.name}, ${STATUS_LABELS[l.status] || l.status}` },
    h("span", { class: "avatar" }, initials(agent.name)),
    h("span", { class: "agent-name truncate", title: agent.name }, agent.name, isLead && !agent.conductor ? icon("crown-simple", "ph-fill") : null),
    h("span", { class: "agent-line" }, pill(l.status), h("span", { class: "agent-activity truncate", title: activity }, activity)));
}

/* ================================================================ agent detail */

views.agent = (root, alive, agentId) => {
  const agent = state.config.agents[agentId];
  if (!agent) {
    root.append(h("div", { class: "empty" }, h("strong", {}, "This agent doesn't exist anymore."), h("a", { href: "#overview" }, "Back to Overview")));
    return;
  }
  const team = state.config.teams[agent.team] || {};
  const lead = agent.reports_to;
  const reports = reportsOf(agentId);
  let data = null;
  let tab = "activity";
  let terminalFor = null;
  let lastTabKey = "";

  const pillSlot = h("span");
  const toolbar = h("div", { class: "toolbar" });
  const needsSlot = h("div");
  const nowSlot = h("div");
  const browserSlot = h("div");
  browserWatch(browserSlot, agentId, alive);
  const tabBody = h("div", {}, skeleton(5));
  const tabs = h("div", { class: "tabs", role: "tablist" },
    [["activity", "Activity"], ["tasks", "Tasks"], ["setup", "Setup"]].map(([key, label]) => h("button", {
      role: "tab", type: "button", "aria-selected": String(key === tab), dataset: { tab: key },
      onclick: () => {
        tab = key;
        tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
        lastTabKey = "";
        renderTab();
      },
    }, label)));

  root.append(
    h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, h("a", { href: "#overview" }, "Overview"), icon("caret-right"),
      agent.swarm ? h("a", { href: "#swarm" }, team.name || agent.team) : h("span", {}, team.name || agent.team)),
    h("header", { class: "agent-head" },
      h("span", { class: "avatar" }, initials(agent.name)),
      h("div", {},
        h("h1", {}, agent.name, pillSlot),
        h("p", {}, agent.conductor ? "Picks agents for your requests and runs the plans you approve"
          : agent.definition ? h("span", { class: "mono" }, agent.definition) : "No agent definition", " · ",
          lead ? ["reports to ", h("a", { href: `#agent/${lead}` }, agentName(lead))] : agent.conductor ? "reports to you" : "leads the team, reports to you",
          reports.length ? [" · leads ", reports.map((rid, i) => [i ? ", " : "", h("a", { href: `#agent/${rid}` }, agentName(rid))])] : ""))),
    toolbar, needsSlot, nowSlot, browserSlot, tabs, tabBody);

  function renderToolbar() {
    const running = RUNNING(data.status);
    const act = (fn) => (e) => runAction(e.currentTarget, fn).then(() => { load(); pollLive(); });
    fill(toolbar,
      agent.conductor ? h("a", { class: "btn primary", href: `#work/${agent.workspace}` }, icon("hexagon"), "Open Task")
        : h("button", { class: "btn primary", onclick: () => giveTask(agentId, { goal: !lead }).then(load) }, icon(lead ? "paper-plane-tilt" : "flag"), lead ? "Give Task" : "Give Goal"),
      running ? h("button", { class: "btn", onclick: () => sendMessage(agentId).then(load) }, icon("chat-text"), "Message") : null,
      running ? h("button", { class: "btn", title: "Stop what it's doing now (Esc)", onclick: act(() => bridge.call("press", { agent_id: agentId, key: "interrupt" })) }, icon("hand"), "Interrupt") : null,
      running ? h("button", { class: "btn", "aria-pressed": String(state.terminalOpen), onclick: () => { toggleTerminal(); syncTerminal(true); renderToolbar(); } },
        icon("terminal-window"), state.terminalOpen ? "Hide Terminal" : "Watch Live") : null,
      running ? h("button", { class: "btn ghost", onclick: act(() => bridge.call("open_external", { agent_id: agentId })) }, icon("arrow-square-out"), "Open in Window") : null,
      !running ? h("button", { class: "btn", onclick: act(() => bridge.call("start_agent", { agent_id: agentId })) }, icon("play"), data.session_id ? "Resume" : "Start") : null,
      h("span", { style: "flex:1" }),
      h("button", { class: "btn icon ghost", "aria-label": "More actions", title: "More actions", onclick: (e) => {
        const r = e.currentTarget.getBoundingClientRect();
        showMenu(r.left - 150, r.bottom + 4, [
          { icon: "sparkle", label: "Start New Session", action: async () => {
            const ok = !running || await confirmDialog({ title: "Start a new session?", body: "Stops the running session. The next one starts with an empty conversation (the old one stays on disk).", confirm: "Start Fresh", danger: true });
            if (!ok) return;
            if (running) await bridge.call("stop_agent", { agent_id: agentId });
            await runAction(null, () => bridge.call("start_agent", { agent_id: agentId, fresh: true }));
            load(); pollLive();
          } },
          agent.swarm ? (agent.conductor ? { icon: "hexagon", label: "Task", action: () => { location.hash = `#work/${agent.workspace}`; } }
            : { icon: "scroll", label: "View Manifest", action: () => manifestDialog(agent.definition) })
            : { icon: "pencil-simple", label: "Edit in Teams", action: () => { location.hash = "#teams"; } },
          { icon: "stop", label: "Stop Session", danger: true, disabled: !running, action: async () => {
            const ok = await confirmDialog({ title: `Stop ${agent.name}?`, body: "Ends its session. The conversation is kept, and Resume picks it up again.", confirm: "Stop", danger: true });
            if (ok) { await runAction(null, () => bridge.call("stop_agent", { agent_id: agentId })); load(); pollLive(); }
          } },
        ]);
      } }, icon("dots-three")));
  }

  function renderNeeds() {
    const s = data.status;
    if (!NEEDS_ATTENTION.has(s)) { needsSlot.replaceChildren(); return; }
    const act = (key) => (e) => runAction(e.currentTarget, () => bridge.call("press", { agent_id: agentId, key })).then(load);
    const message = {
      needs_approval: data.detail || "It's asking permission to use a tool.",
      needs_input: "Claude Code is asking something before it can continue (for example, whether to trust this folder). Open the terminal and answer it there.",
      needs_you: "It finished its turn and still has an open task from you. It may be asking you a question: read its last message below, then reply with Message.",
      over_budget: data.detail || "It used its budget and stopped.",
      exited: data.detail || "The session crashed. Restart it to carry on.",
    }[s];
    const restart = (e) => runAction(e.currentTarget, () => bridge.call("start_agent", { agent_id: agentId })).then(() => { load(); pollLive(); });
    needsSlot.replaceChildren(h("div", { class: "needs", role: "alert" },
      h("div", { class: "needs-top" }, icon("warning-circle", "ph-fill"), h("p", {}, message),
        s === "needs_approval" ? [
          h("button", { class: "btn small primary", onclick: act("approve"), title: "Choose the highlighted answer (usually “Yes”)" }, icon("check"), "Approve"),
          h("button", { class: "btn small", onclick: act("deny") }, icon("x"), "Deny"),
          alwaysAllowButton(agentId, load)] : null,
        s === "needs_you" ? h("button", { class: "btn small primary", onclick: () => sendMessage(agentId).then(load) }, icon("chat-text"), "Reply") : null,
        s === "over_budget" ? budgetButtons(agentId, () => { load(); pollLive(); }) : null,
        s === "needs_input" ? h("button", { class: "btn small primary", onclick: () => { state.terminalOpen = true; syncTerminal(true); renderToolbar(); } }, icon("terminal-window"), "Show Terminal") : null,
        s === "exited" ? h("button", { class: "btn small primary", onclick: restart }, icon("arrow-clockwise"), "Restart") : null),
      data.screen ? h("pre", { class: "screen", "aria-label": "What its terminal shows" }, data.screen.replace(/\n{3,}/g, "\n\n").trim()) : null));
  }

  function renderNow() {
    const l = liveOf(agentId);
    const running = RUNNING(data.status);
    const parts = [];
    if (l.task) parts.push(h("span", {}, h("b", {}, `Task #${l.task.id}: `), l.task.title));
    if (data.detail && data.status !== "needs_approval") parts.push(h("span", { class: "muted" }, parts.length ? ` · ${data.detail}` : data.detail));
    const meta = [];
    if (running && data.started_at) meta.push(`up ${duration(data.started_at)}`);
    const spend = budgetText(data.budgets);
    if (spend) meta.push(spend);
    else if (data.cost != null) meta.push(`$${data.cost.toFixed(2)}`);
    nowSlot.replaceChildren(running || parts.length ? h("div", { class: "now" },
      icon(data.status === "working" ? "lightning" : "info"),
      h("div", { class: "truncate", style: "flex:1" }, parts.length ? parts : (running ? "Ready for work." : "Not running.")),
      meta.length ? h("span", { class: "faint num", style: "font-size:12.5px" }, meta.join(" · ")) : null) : "");
  }

  function renderTab() {
    const key = tab + JSON.stringify(tab === "activity" ? [data.events.length, data.events.at(-1), state.showToolOutput]
      : tab === "tasks" ? data.tasks : [data.session_id, data.model, data.cost, data.tokens]);
    if (key === lastTabKey) return;
    lastTabKey = key;
    if (tab === "activity") tabBody.replaceChildren(activityTab());
    else if (tab === "tasks") tabBody.replaceChildren(taskList(data.tasks, { perspective: agentId }));
    else tabBody.replaceChildren(setupTab());
  }

  function activityTab() {
    const events = data.events.filter((e) => state.showToolOutput || e.kind !== "result" || !e.ok).slice().reverse();
    const toggle = h("label", { class: "switch-row", style: "margin-bottom:10px" },
      h("button", { class: "switch", role: "switch", type: "button", "aria-checked": String(state.showToolOutput),
        onclick: () => { state.showToolOutput = !state.showToolOutput; lastTabKey = ""; renderTab(); } }),
      "Show tool output");
    if (!events.length) {
      return h("div", {}, h("div", { class: "empty" }, h("strong", {}, "Nothing yet."),
        data.session_id ? "Its activity shows up here as it works." : "Start it or give it a task."));
    }
    const icons = { prompt: "arrow-bend-down-right", say: "chat-text", tool: "wrench", result: "check" };
    return h("div", {}, toggle, h("div", { class: "timeline" }, events.slice(0, 250).map((e) => {
      const fail = e.kind === "result" && !e.ok;
      const isLagoPrompt = e.kind === "prompt" && e.text.startsWith("[Lago");
      return h("div", { class: `event ${e.kind}${fail ? " fail" : ""}${e.sub ? " sub" : ""}` },
        icon(fail ? "x-circle" : isLagoPrompt ? "tree-structure" : icons[e.kind]),
        h("div", { class: `text${e.kind === "say" || e.kind === "prompt" ? " clamp" : ""}`, title: e.kind === "tool" ? e.text : null,
          onclick: (ev) => ev.currentTarget.classList.remove("clamp") }, e.text),
        h("span", { class: "when" }, clock(e.at)));
    })));
  }

  function setupTab() {
    const entry = agent.swarm && !agent.conductor ? catalogEntry(agent.definition) : null;
    const def = entry ? { name: entry.id, description: entry.summary, path: entry.path, model: entry.model }
      : agent.swarm ? null : state.definitions.find((d) => d.name === agent.definition);
    const row = (label, value) => h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, label), h("span", {}, value)));
    return h("div", { class: "rows" },
      row("Agent definition", def ? [h("span", { class: "mono" }, def.name), h("div", { class: "def-desc" }, def.description), h("div", { class: "faint mono", style: "font-size:12px" }, def.path)]
        : agent.conductor ? "A task's lead (prompts/conductor.md)" : agent.definition ? `${agent.definition} (not found)` : "None: plain Claude Code with Lago's team instructions"),
      row("Works in", h("span", { class: "mono" }, data.cwd)),
      row("Model", data.model || agent.model || def?.model || "Default"),
      row("Permissions", PERMISSION_MODES.find(([v]) => v === (agent.permission_mode || ""))?.[1] || agent.permission_mode),
      row("Claude account", data.account_label),
      row("Session", data.session_id ? h("span", { class: "mono" }, data.session_id) : "None yet"),
      row("Usage", `${data.input_tokens.toLocaleString()} input · ${data.tokens.toLocaleString()} output tokens${data.cost != null ? ` · $${data.cost.toFixed(2)}` : ""}`),
      data.budgets?.length ? row("Budget", data.budgets.map((b) => `${b.kind === "session" ? "Work session" : "This conversation"}: ${dollars(b.spent)} of ${b.limit ? dollars(b.limit) : "no limit"}${b.paused ? " (stopped)" : ""}`).join(" · ")
        + " (at API prices)") : null);
  }

  function syncTerminal(force = false) {
    const want = RUNNING(data.status) && state.terminalOpen;
    if (want && (terminalFor !== agentId || force)) {
      terminalFor = agentId;
      bridge.call("show_terminal", { agent_id: agentId }).catch(() => {});
    } else if (!want && (terminalFor || force)) {
      terminalFor = null;
      bridge.call("hide_terminal").catch(() => {});
    }
  }

  let lastStatus = null;
  async function load() {
    try {
      const next = await bridge.call("agent_detail", { agent_id: agentId });
      if (document.visibilityState === "visible") bridge.call("mark_read", { items: [`agent:${agentId}`] }).catch(() => {});
      if (!alive()) return;
      data = next;
      pillSlot.replaceChildren(pill(data.status));
      if (data.status !== lastStatus) { renderToolbar(); syncTerminal(); lastStatus = data.status; }
      renderNeeds();
      renderNow();
      renderTab();
    } catch (error) {
      if (alive()) tabBody.replaceChildren(errorBox(error, load));
    }
  }

  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 2000);
  viewCleanup = () => { clearInterval(timer); bridge.call("hide_terminal").catch(() => {}); };
  load();
};

/* ================================================================ tasks */

function taskList(tasks, { perspective = null } = {}) {
  if (!tasks.length) return h("div", { class: "empty" }, h("strong", {}, "No tasks yet."), "Tasks appear when you or a lead hands out work.");
  return h("div", { class: "task-list" }, tasks.map((t) => {
    const open = ["queued", "blocked", "sent", "working"].includes(t.status);
    let direction;
    if (perspective && t.to_agent === perspective) direction = ["From ", agentLink(t.from_agent)];
    else if (perspective) direction = ["To ", agentLink(t.to_agent)];
    else direction = [agentLink(t.from_agent), " → ", agentLink(t.to_agent)];
    return h("article", { class: "task" },
      h("div", { class: "task-top" },
        h("span", { class: "faint num mono" }, `#${t.id}`),
        h("span", { class: "task-title" }, t.title),
        (t.labels || []).map((l) => h("span", { class: "chip" }, l)),
        h("span", { class: `task-status ${t.status}` }, t.status === "sent" ? "Waiting to start" : t.status === "blocked" ? `Waits on ${t.blocked_by.map((i) => `#${i}`).join(", ")}` : t.status),
        open ? h("button", { class: "btn small ghost", onclick: (e) => {
          const r = e.currentTarget.getBoundingClientRect();
          showMenu(r.left - 120, r.bottom + 4, [
            { icon: "check", label: "Mark Done", action: () => runAction(null, () => bridge.call("set_task", { task_id: t.id, status: "done" })).then(pollLive) },
            { icon: "x", label: "Cancel Task", danger: true, action: () => runAction(null, () => bridge.call("set_task", { task_id: t.id, status: "cancelled" })).then(pollLive) },
          ]);
        }, "aria-label": `Options for task ${t.id}` }, icon("dots-three")) : null),
      h("div", { class: "task-meta" }, direction, ` · ${ago(t.created_at)}`, t.status !== "queued" && !open ? ` · finished ${ago(t.updated_at)}` : ""),
      h("details", {}, h("summary", {}, "Instructions"), h("pre", {}, t.body)),
      t.result ? h("details", { open: !open && !perspective ? false : undefined }, h("summary", {}, "Result"), h("pre", {}, t.result)) : null,
      taskThread(t));
  }));
}

/* A task's comments (tickets.py): each one reaches the other side of the task. */
function taskThread(t) {
  const thread = h("div", { class: "thread" });
  const box = h("textarea", { class: "input", rows: 2, placeholder: t.to_agent ? `Comment to ${agentName(t.to_agent)}…` : "Comment…" });
  const closed = ["done", "failed"].includes(t.status);
  const send = (reopen) => (e) => runAction(e.currentTarget, () => bridge.call("comment_task", { task_id: t.id, text: box.value, reopen }))
    .then((ok) => { if (ok) { box.value = ""; load(); } });
  const load = async () => {
    const rows = await bridge.call("task_comments", { task_id: t.id }).catch(() => []);
    fill(thread, rows.map((c) => h("div", { class: "review-note" }, h("strong", {}, `${c.author === "you" ? "You" : agentName(c.author)} · ${ago(c.created_at)}: `), h("span", { class: "prewrap" }, c.text))));
  };
  const details = h("details", {}, h("summary", {}, `Comments${t.comments ? ` (${t.comments})` : ""}`), thread, box,
    h("div", { class: "controls-line", style: "margin:6px 0 0" },
      h("button", { class: "btn small", onclick: send(false) }, icon("chat-text"), closed ? "Comment" : "Send"),
      closed ? h("button", { class: "btn small ghost", title: "Send it back to the agent with your comment", onclick: send(true) }, icon("arrow-counter-clockwise"), "Reopen with Comment") : null));
  details.addEventListener("toggle", () => { if (details.open) load(); });
  return details;
}

function agentLink(id) {
  return id ? h("a", { href: `#agent/${id}` }, agentName(id)) : h("span", {}, "You");
}

views.tasks = (root, alive) => {
  let filter = "open";
  let mine = null;
  let handoffs = null;
  let lastKey = "";
  const body = h("div", {}, skeleton(5));
  const seg = h("div", { class: "segmented", role: "radiogroup", "aria-label": "Show" },
    [["open", "Open"], ["done", "Finished"], ["all", "All"]].map(([key, label]) => h("button", {
      type: "button", role: "radio", "aria-checked": String(key === filter),
      onclick: () => { filter = key; seg.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-checked", String(["open", "done", "all"][i] === filter))); lastKey = ""; render(); },
    }, label)));
  const handoffFold = h("details", { class: "fold" });
  const render = () => {
    if (!mine) return;
    // Stuck ones first: they need a decision.
    const shown = mine.filter((t) => filter === "all" || (filter === "open") === t.open)
      .sort((a, b) => Number(STUCK.has(b.standing?.state)) - Number(STUCK.has(a.standing?.state)));
    const openHandoffs = (handoffs || []).filter((t) => ["queued", "blocked", "sent", "working"].includes(t.status));
    const key = filter + JSON.stringify(shown) + JSON.stringify((handoffs || []).map((t) => [t.id, t.status, t.comments]));
    if (key === lastKey) return;
    lastKey = key;
    body.replaceChildren(shown.length ? h("div", { class: "my-tasks" }, shown.map((t) => (t.open && t.standing ? openRunCard(t, load) : myTaskRow(t))))
      : h("div", { class: "empty" }, h("strong", {}, filter === "open" ? "Nothing in progress." : "Nothing here yet."),
        "Press New Task (or Ctrl+N) and describe what you want done."));
    const wasOpen = handoffFold.open;
    fill(handoffFold, h("summary", {}, icon("arrows-left-right"), `Handed between agents${openHandoffs.length ? ` (${openHandoffs.length} open)` : ""}`,
      h("span", { class: "faint" }, "what leads gave their agents, with comments")),
      h("div", { class: "fold-body" }, taskList((handoffs || []).filter((t) => filter === "all" || (filter === "open") === ["queued", "blocked", "sent", "working"].includes(t.status)))));
    handoffFold.open = wasOpen;
  };
  const imported = h("div");
  let importedKey = "";
  const renderImported = (rows) => {
    const key = JSON.stringify(rows);
    if (key === importedKey) return;
    importedKey = key;
    const start = (t) => newTaskDialog({ text: `${t.title}\n\n${t.body}`.trim(), project: t.project, agent: t.agent,
      onStarted: () => bridge.call("imported_task_status", { task_id: t.id, status: "started" }).then(load) });
    fill(imported, rows.length ? h("section", { class: "drafts" }, h("h2", { class: "label" }, icon("download-simple"), ` Waiting from Paperclip (${rows.length})`),
      rows.map((t) => h("article", { class: "draft-row" },
        h("div", { class: "draft-row-top" }, h("strong", {}, t.title),
          h("span", { class: "faint", style: "font-size:12px" }, [t.ref, t.project ? projectName(t.project) : "", t.priority].filter(Boolean).join(" · ")),
          h("span", { style: "flex:1" }),
          h("button", { type: "button", class: "btn small primary", onclick: () => start(t) }, icon("play"), "Start"),
          h("button", { type: "button", class: "btn small ghost", onclick: (e) => runAction(e.currentTarget, () => bridge.call("imported_task_status", { task_id: t.id, status: "dismissed" })).then(load) }, "Dismiss")),
        t.body ? h("p", { class: "faint imported-body" }, t.body) : null))) : null);
  };
  const load = async () => {
    try {
      [mine, handoffs] = await Promise.all([bridge.call("my_tasks"), bridge.call("tasks")]);
      if (!state.web || state.me?.owner || state.me?.admin) bridge.call("imported_tasks").then((rows) => { if (alive()) renderImported(rows); }).catch(() => {});
      if (alive()) render();
    } catch (error) {
      if (alive()) body.replaceChildren(errorBox(error, load));
    }
  };
  root.append(pageHead("Tasks", "Everything you asked for, and where it stands."),
    h("div", { class: "controls-line" }, h("button", { class: "btn primary", onclick: () => newTaskDialog() }, icon("plus"), "New Task"), seg,
      h("button", { class: "btn ghost", title: "Mark every task as seen", onclick: (e) => runAction(e.currentTarget, () => bridge.call("mark_read", { items: (mine || []).filter((t) => t.unread).map((t) => t.item) })).then(load) }, icon("checks"), "Mark All Read")), imported, body, handoffFold);
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 3000);
  viewCleanup = () => clearInterval(timer);
  load();
};

/* Where an open run stands (open_tasks.py), as a pill, and which of those need a decision. */
const STANDING_LABELS = { working: ["Working", "running"], quiet: ["Gone quiet", "draft"], waiting_usage: ["Waiting on usage", "draft"],
  ready: ["Ready to carry on", "draft"], stopped: ["Stopped", "failed"], needs_you: ["Needs you", "draft"], queued: ["Not started", "cancelled"], blocked: ["Waiting on other tasks", "cancelled"] };
const STUCK = new Set(["quiet", "waiting_usage", "ready", "stopped", "needs_you"]);

/* An open run: where it honestly stands, its last words, and what you can do about it. */
function openRunCard(t, reload) {
  const s = t.standing;
  const [label, tone] = STANDING_LABELS[s.state] || [s.state, "cancelled"];
  const act = (fn, msg) => (e) => runAction(e.currentTarget, fn, msg).then(reload);
  return h("article", { class: `my-task run-card${STUCK.has(s.state) ? " stuck" : ""}${t.unread ? " unread" : ""}` },
    h("div", { class: "my-task-main" },
      h("a", { class: "my-task-title", href: t.href }, t.unread ? h("span", { class: "unread-dot", title: "Something happened since you last looked" }) : null, `#${t.id} ${t.title}`),
      h("span", { class: "row-sub" }, `${t.where} (one agent) · started ${ago(t.created_at)}`),
      h("p", { class: "run-says" }, icon(STUCK.has(s.state) ? "warning-circle" : "info", STUCK.has(s.state) ? "ph-fill" : "ph"), h("span", {}, s.says)),
      s.last ? h("p", { class: "run-last" }, h("span", { class: "faint" }, "Last words: "), s.last) : null,
      h("div", { class: "run-actions" },
        s.can_continue ? h("button", { class: "btn small primary", onclick: act(() => bridge.call("continue_task", { task_id: t.id }), "Carrying on.") }, icon("play"), "Continue") : null,
        h("button", { class: "btn small", onclick: () => abandonDialog(t, reload) }, icon("x-circle"), "Abandon"),
        h("button", { class: "btn small ghost", onclick: act(() => bridge.call("ask_lago_about_task", { task_id: t.id }), "Asked Lago.") }, icon("chat-circle-text"), "Ask Lago"))),
    h("span", { class: `pill p-${tone}` }, label));
}

function abandonDialog(t, done) {
  const reason = h("textarea", { class: "input", rows: 3, placeholder: "Optional: why, so Lago and the history know (e.g. not needed any more)" });
  return openDialog((close) => h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const ok = await runAction(e.submitter, () => bridge.call("abandon_task", { task_id: t.id, reason: reason.value.trim() }), "Task abandoned.");
      if (ok) { close(); done(); }
    } },
    h("h2", {}, `Abandon task #${t.id}?`),
    h("p", { class: "desc" }, `“${t.title}”. Its agent stops and the task closes as cancelled. Its work so far stays in its folder and in History.`),
    h("label", { class: "field" }, h("span", {}, "Reason"), reason),
    h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close() }, "Keep It"),
      h("button", { type: "submit", class: "btn danger" }, "Abandon"))));
}

/* One of your tasks: a work session or an agent's run, as one row. */
function myTaskRow(t) {
  const label = t.status === "closed" ? "Closed" : t.status === "done" ? "Done" : t.status === "failed" ? "Failed" : STATUS_LABELS[t.status] || t.status;
  const tone = t.status === "closed" || t.status === "stopped" ? "stopped" : t.status;
  return h("a", { class: `my-task${t.unread ? " unread" : ""}`, href: t.href },
    h("div", { class: "my-task-main" },
      h("span", { class: "my-task-title" }, t.unread ? h("span", { class: "unread-dot", title: "Something happened since you last looked" }) : null, t.title),
      h("span", { class: "row-sub" }, `${t.kind === "run" ? `${t.where} (one agent)` : t.where} · started ${ago(t.created_at)}`),
      t.last ? h("span", { class: "my-task-last" }, t.last) : null),
    h("span", { class: `pill ${tone}` }, label));
}

/* ================================================================ routines */

const REPEAT_LABELS = [["hourly", "Every hour"], ["daily", "Every day"], ["weekdays", "Every weekday"], ["weekly", "Every week"], ["minutes", "Every N minutes"], ["never", "Only when triggered"]];
const RUN_TONE = { running: "running", done: "done", failed: "failed", skipped: "draft", coalesced: "draft" };
const nextTime = (at) => new Date(at * 1000).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/* Everything that repeats on its own: agent routines, and the reminders Lago acts on. */
views.routines = (root, alive) => {
  const routinesSlot = h("div", {}, skeleton(3));
  const remindersSlot = h("div", {}, skeleton(2));
  const lagos = !state.web || state.me?.owner || state.me?.admin; // reminders are Lago's: admins only
  const load = () => { routineList(routinesSlot, alive, load); if (lagos) reminderList(remindersSlot, alive, load); };
  root.append(pageHead("Routines", "Jobs that run on their own: every day, every weekday, every hour. Make, change or delete them here.", load),
    h("div", { class: "controls-line" },
      h("button", { class: "btn primary", onclick: () => routineDialog(null, load) }, icon("plus"), "New Routine"),
      lagos ? h("button", { class: "btn", onclick: () => reminderDialog(null, load) }, icon("alarm"), "New Reminder for Lago") : null),
    h("h2", { class: "section-title" }, "Agent routines"),
    h("p", { class: "faint" }, "An agent does the job each time and Lago tells you the result."),
    routinesSlot,
    ...(lagos ? [h("h2", { class: "section-title" }, "Lago's reminders"),
      h("p", { class: "faint" }, "Lago gets the message when it comes due, then does what it says or reminds you."),
      remindersSlot] : []));
  load();
};

async function routineList(slot, alive, reload) {
  let rows;
  try { rows = await bridge.call("routines"); } catch (error) { if (alive()) slot.replaceChildren(errorBox(error, reload)); return; }
  if (!alive()) return;
  const act = (fn, msg) => (e) => runAction(e.currentTarget, fn, msg).then(reload);
  slot.replaceChildren(rows.length ? h("div", { class: "rows" }, rows.map((r) => h("div", { class: "row" },
    h("div", { class: "row-main" },
      h("span", { class: "row-title" }, r.title, " ", h("span", { class: `pill ${r.status === "active" ? "p-running" : "p-draft"}` }, r.status === "active" ? "Active" : "Paused")),
      h("span", { class: "row-sub" }, `${catalogEntry(r.agent)?.name || r.agent} · ${r.describe}${r.project ? ` · ${projectName(r.project)}` : ""}`,
        r.status === "active" && r.next_at ? ` · next ${nextTime(r.next_at)}` : "",
        r.only_if_changed ? " · only when the repository changed" : "", r.has_hook ? " · webhook" : ""),
      r.runs.length ? h("span", { class: "row-sub" }, "Recent: ", r.runs.map((run) => h("span", { class: `pill p-${RUN_TONE[run.status] || "draft"}`, title: `${run.trigger} · ${ago(run.created_at)}${run.reason ? ` · ${run.reason}` : ""}`, style: "margin-right:4px" }, run.status))) : null),
    h("div", { class: "row-actions" },
      h("button", { class: "btn small", onclick: act(() => bridge.call("run_routine", { routine_id: r.id }), "Started.") }, icon("play"), "Run Now"),
      h("button", { class: "btn small", onclick: () => routineDialog(r, reload) }, icon("pencil-simple"), "Edit"),
      h("button", { class: "btn small ghost", "aria-label": "More", onclick: (e) => {
        const box = e.currentTarget.getBoundingClientRect();
        showMenu(box.left, box.bottom + 4, [
          { label: r.status === "active" ? "Pause" : "Resume", icon: r.status === "active" ? "pause" : "play", action: () => runAction(null, () => bridge.call("routine_status", { routine_id: r.id, status: r.status === "active" ? "paused" : "active" })).then(reload) },
          { label: "Run History", icon: "clock-counter-clockwise", action: () => routineRunsDialog(r) },
          { label: r.has_hook ? "New Webhook Secret" : "Make a Webhook", icon: "plugs-connected", action: async () => {
            const { secret } = await bridge.call("routine_hook", { routine_id: r.id });
            openDialog((close) => h("div", {}, h("h2", {}, "Webhook"), h("p", { class: "desc" }, "POST to this address over Tailscale (the phone server must be on), with an optional JSON body. It's shown only now; making a new one stops the old."),
              h("pre", { class: "mono wrap" }, `https://<this computer's Tailscale address>:<phone port>/v1/hooks/${secret}`),
              h("div", { class: "dialog-actions" }, h("button", { class: "btn primary", onclick: () => close() }, "Done"))));
            reload();
          } },
          { label: "Delete", icon: "trash", danger: true, action: async () => {
            if (await confirmDialog({ title: `Delete “${r.title}”?`, body: "It stops for good. Its run history stays in History.", confirm: "Delete", danger: true }))
              runAction(null, () => bridge.call("routine_status", { routine_id: r.id, status: "archived" }), "Routine deleted.").then(reload);
          } },
        ]);
      } }, icon("dots-three"))))))
    : h("p", { class: "faint" }, "No routines yet. Make one here, or ask Lago (“every weekday at 9, have the web researcher check competitor prices”)."));
}

async function reminderList(slot, alive, reload) {
  let rows;
  try { rows = await bridge.call("reminders"); } catch (error) { if (alive()) slot.replaceChildren(errorBox(error, reload)); return; }
  if (!alive()) return;
  const act = (fn, msg) => (e) => runAction(e.currentTarget, fn, msg).then(reload);
  slot.replaceChildren(rows.length ? h("div", { class: "rows" }, rows.map((r) => h("div", { class: "row" },
    h("div", { class: "row-main" },
      h("span", { class: "row-title" }, icon(r.kind === "once" ? "alarm" : "repeat"), " ", r.text),
      h("span", { class: "row-sub" }, `${r.repeat} · next ${nextTime(r.next_at)}${r.last_at ? ` · last ${ago(r.last_at)}` : ""} · #${r.id}`)),
    h("div", { class: "row-actions" },
      h("button", { class: "btn small", onclick: act(() => bridge.call("run_reminder", { schedule_id: r.id }), "Sent to Lago.") }, icon("play"), "Run Now"),
      h("button", { class: "btn small", onclick: () => reminderDialog(r, reload) }, icon("pencil-simple"), "Edit"),
      h("button", { class: "btn small ghost", "aria-label": `Delete reminder ${r.id}`, title: "Delete", onclick: async (e) => {
        const button = e.currentTarget;
        if (!(await confirmDialog({ title: "Delete this reminder?", body: r.text, confirm: "Delete", danger: true }))) return;
        await runAction(button, () => bridge.call("cancel_schedule", { schedule_id: r.id }), "Reminder deleted.");
        reload();
      } }, icon("trash"))))))
    : h("p", { class: "faint" }, "No reminders. Ask Lago (“every morning at 9, check the deploy”) or make one here."));
}

/* The minutes field shows only while "Every N minutes" is picked. */
function onlyForMinutes(f) {
  const toggle = () => { f.everyField.hidden = f.repeat.value !== "minutes"; };
  f.repeat.addEventListener("change", toggle);
  toggle();
}

function reminderDialog(r, done) {
  const at = r ? new Date(r.next_at * 1000) : null;
  const pad = (n) => String(n).padStart(2, "0");
  const f = {};
  const field = (label, el, help) => h("label", { class: "field" }, h("span", {}, label), el, help ? h("small", { class: "faint" }, help) : null);
  const kinds = [["once", "Once"], ...REPEAT_LABELS.filter(([val]) => val !== "never")];
  const shown = openDialog((close) => h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const fields = { text: f.text.value.trim(), repeat: f.repeat.value, when: f.when.value.trim() };
      if (fields.repeat === "minutes") fields.every_minutes = Number(f.every.value);
      if (r && fields.when === `${pad(at.getHours())}:${pad(at.getMinutes())}`) delete fields.when; // unchanged: keep its date
      const ok = await runAction(e.submitter, () => bridge.call("save_reminder", { schedule_id: r?.id ?? null, ...fields }), r ? "Saved." : "Reminder created.");
      if (ok) { close(); done(); }
    } },
    h("h2", {}, r ? "Edit Reminder" : "New Reminder for Lago"),
    field("What Lago should do or remind you of", f.text = h("textarea", { class: "input", rows: 4, required: true, placeholder: "e.g. Run github-agent-builder in daily mode and tell me today's MCP news" }, r?.text || "")),
    field("Repeats", f.repeat = h("select", { class: "select" }, kinds.map(([val, lab]) => h("option", { value: val, selected: (r?.kind || "daily") === val }, lab)))),
    f.everyField = field("Every how many minutes", f.every = h("input", { class: "input num", type: "number", min: 5, value: r?.every_minutes || 60 })),
    field("Time", f.when = h("input", { class: "input", value: at ? `${pad(at.getHours())}:${pad(at.getMinutes())}` : "09:00", placeholder: "09:00 or 2026-11-01 08:30" }),
      "A time of day (the next one coming), or a date and time."),
    h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close() }, "Cancel"),
      h("button", { type: "submit", class: "btn primary" }, r ? "Save" : "Create"))), { wide: true });
  onlyForMinutes(f);
  return shown;
}

function routineDialog(r, done) {
  const agents = state.catalog.agents.filter((a) => a.enabled !== false);
  const v = r || { title: "", agent: agents[0]?.id || "", instructions: "", project: "", folder: "", repeat: "daily", every_minutes: 60, concurrency: "skip", catch_up: "once", only_if_changed: "", report: "lago" };
  const f = {};
  const field = (label, el, help) => h("label", { class: "field" }, h("span", {}, label), el, help ? h("small", { class: "faint" }, help) : null);
  const select = (key, options) => (f[key] = h("select", { class: "select" }, options.map(([val, lab]) => h("option", { value: val, selected: String(v[key] ?? "") === val }, lab))));
  const shown = openDialog((close) => h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const fields = { title: f.title.value.trim(), agent: f.agent.value, instructions: f.instructions.value.trim(), project: f.project.value,
        folder: f.project.value ? "" : f.folder.value.trim(), repeat: f.repeat.value, concurrency: f.concurrency.value, catch_up: f.catch_up.value,
        only_if_changed: f.only_if_changed.value, report: f.report.value };
      if (fields.repeat === "minutes") fields.every_minutes = Number(f.every.value);
      if (f.when.value.trim()) fields.when = f.when.value.trim();
      const ok = await runAction(e.submitter, () => bridge.call("save_routine", { routine_id: r?.id ?? null, ...fields }), r ? "Saved." : "Routine created.");
      if (ok) { close(); done(); }
    } },
    h("h2", {}, r ? `Edit “${r.title}”` : "New Routine"),
    field("Title", f.title = h("input", { class: "input", required: true, value: v.title, placeholder: "e.g. Competitor prices" })),
    field("Agent", select("agent", agents.map((a) => [a.id, a.name]))),
    field("What it does each time", f.instructions = h("textarea", { class: "input", rows: 4, required: true }, v.instructions)),
    field("Project", select("project", [["", "No project (a folder)"], ...Object.entries(state.config.projects).map(([id, p]) => [id, p.name || id])])),
    field("Folder (without a project)", f.folder = h("input", { class: "input mono", value: v.folder, placeholder: "~/lago-work/runs" })),
    field("Repeats", select("repeat", REPEAT_LABELS)),
    f.everyField = field("Every how many minutes", f.every = h("input", { class: "input num", type: "number", min: 5, value: v.every_minutes || 60 })),
    field("First time", f.when = h("input", { class: "input", placeholder: r ? "keep the current times" : "e.g. 09:00 or 2026-11-01 08:30" }), "It repeats from there. Empty: one interval from now."),
    h("details", {}, h("summary", { class: "faint" }, "Policies"), h("div", { style: "display:grid;gap:14px;margin-top:10px" },
      field("While the last run is still going", select("concurrency", [["skip", "Skip this run"], ["parallel", "Run another anyway"]])),
      field("Runs missed while the app was closed", select("catch_up", [["once", "Run once"], ["skip", "Skip them"]])),
      field("Only run when", select("only_if_changed", [["", "Always"], ["git", "The project's repository changed"]])),
      field("Results", select("report", [["lago", "Lago tells me each result"], ["failures", "Only tell me about failures"]])))),
    h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close() }, "Cancel"),
      h("button", { type: "submit", class: "btn primary" }, r ? "Save" : "Create"))), { wide: true });
  onlyForMinutes(f);
  return shown;
}

async function routineRunsDialog(r) {
  const runs = await bridge.call("routine_runs", { routine_id: r.id });
  openDialog((close) => h("div", {}, h("h2", {}, `Runs of “${r.title}”`),
    runs.length ? h("div", { class: "rows" }, runs.map((run) => h("div", { class: "row" }, h("div", { class: "row-main" },
      h("span", { class: "row-title" }, h("span", { class: `pill p-${RUN_TONE[run.status] || "draft"}` }, run.status), ` ${run.trigger}`, run.agent_id ? ` · ${run.agent_id}` : ""),
      h("span", { class: "row-sub" }, `${ago(run.created_at)}${run.reason ? ` · ${run.reason}` : ""}${run.coalesced ? ` · ${run.coalesced} skipped while it ran` : ""}${run.task_id ? ` · task #${run.task_id}` : ""}`)))))
      : h("p", { class: "faint" }, "No runs yet."),
    h("div", { class: "dialog-actions" }, h("button", { class: "btn", onclick: () => close() }, "Close"))), { wide: true });
}

/* ================================================================ history */

const ACTIVITY_ICONS = { task: "list-checks", plan: "flow-arrow", budget: "coins", decision: "gavel",
  config: "sliders", session: "hexagon", agent: "warning-circle", backup: "database", routine: "repeat", watchdog: "eye" };
const activityIcon = (kind) => icon(ACTIVITY_ICONS[kind.split(".")[0]] || "dot-outline");
const actorName = (id) => (id === "you" ? "You" : id === "system" ? "Lago Agents" : id === "app" ? "You (app)" : agentName(id));

function activityRows(rows) {
  if (!rows.length) return h("div", { class: "empty" }, h("strong", {}, "Nothing recorded yet."), "Tasks, plans, budgets, decisions and setting changes appear here as they happen.");
  return h("div", { class: "rows" }, rows.map((r) => h("div", { class: "row" },
    h("div", { class: "row-main" },
      h("span", { class: "row-title" }, activityIcon(r.kind), " ", linked(r.text.split("\n")[0])),
      h("span", { class: "row-sub" }, `${actorName(r.actor)} · ${r.kind}${r.target ? ` · ${r.target}` : ""} · ${ago(r.created_at)}`),
      r.text.includes("\n") ? h("details", {}, h("summary", { class: "faint" }, "More"), h("pre", { class: "mono wrap" }, r.text)) : null))));
}

function spendChart(days) {
  const max = Math.max(...days.map((d) => d.cost), 0.01);
  return h("figure", { class: "spend-chart" },
    h("figcaption", { class: "faint" }, `Per day, last ${days.length} days (most $${max.toFixed(2)})`),
    h("div", { class: "spend-bars", role: "img", "aria-label": "Spending per day" }, days.map((d) => h("div", {
      class: "spend-bar", title: `${d.key}: ${dollars(d.cost)}`, tabindex: "0", "aria-label": `${d.key}: ${dollars(d.cost)}` },
      h("span", { style: `height:${d.cost ? Math.max(2, (d.cost / max) * 100) : 0}%` })))));
}

function costRows(rows, label) {
  return rows.length ? h("div", { class: "rows" }, rows.map((r) => h("div", { class: "row" },
    h("div", { class: "row-main" }, h("span", { class: "row-title" }, label(r.key))),
    h("div", { class: "row-actions" }, h("span", { class: "mono" }, dollars(r.cost))))))
    : h("p", { class: "faint" }, "Nothing spent in this period.");
}

views.history = (root, alive, tab = "activity") => {
  const tabs = [["activity", "Activity", "clock-counter-clockwise"], ["spending", "Spending", "coins"],
    ["settings", "Setting Changes", "sliders"], ["backups", "Backups", "database"]];
  if (!tabs.some(([k]) => k === tab)) tab = "activity";
  const body = h("div", {}, skeleton(5));
  const seg = h("div", { class: "segmented", role: "radiogroup", "aria-label": "Show" }, tabs.map(([key, label, ic]) => h("button", {
    type: "button", role: "radio", "aria-checked": String(key === tab), onclick: () => { location.hash = `history/${key}`; } }, icon(ic), label)));
  const query = h("input", { type: "search", class: "input", style: "flex:1;max-width:560px", placeholder: "Search tasks, reports, plans and history…", "aria-label": "Search",
    onkeydown: (e) => { if (e.key === "Enter") runSearch(); } });
  const searchSlot = h("div");
  let kind = "";

  async function runSearch() {
    const q = query.value.trim();
    if (!q) { searchSlot.replaceChildren(); return; }
    try {
      const found = await bridge.call("search", { query: q });
      if (!alive()) return;
      const href = (r) => r.kind === "plan" ? `#pipeline/${r.id}` : r.workspace ? `#work/${r.workspace}` : r.kind === "task" ? "#tasks" : null;
      searchSlot.replaceChildren(h("details", { class: "fold", open: true },
        h("summary", {}, icon("magnifying-glass"), `${found.length} found for “${q}”`),
        found.length ? h("div", { class: "rows" }, found.map((r) => h("div", { class: "row" },
          h("div", { class: "row-main" },
            h("span", { class: "row-title" }, href(r) ? h("a", { href: href(r) }, `${r.kind} #${r.id} · ${r.title || ""}`) : `${r.kind} #${r.id} · ${r.title || ""}`),
            h("span", { class: "row-sub" }, `${r.snippet} · ${ago(r.at)}`)))))
          : h("p", { class: "fold-body faint" }, "Nothing matches every word.")));
    } catch (error) { if (alive()) searchSlot.replaceChildren(errorBox(error, runSearch)); }
  }

  const loaders = {
    async activity() {
      const filter = h("select", { class: "select", "aria-label": "Kind", onchange: (e) => { kind = e.target.value; load(); } },
        [["", "Everything"], ["session", "Your tasks"], ["task", "Agent hand-offs"], ["plan", "Plans"], ["decision", "Lago's decisions"],
          ["budget", "Budgets"], ["config", "Setting changes"], ["agent", "Crashes and errors"], ["routine", "Routines"], ["watchdog", "Watchdogs"]]
          .map(([v, l]) => h("option", { value: v, selected: v === kind }, l)));
      const rows = await bridge.call("activity", { kind, limit: 200 });
      return [h("div", { class: "controls-line" }, filter), activityRows(rows)];
    },
    async spending() {
      const s = await bridge.call("spending", { days: 30 });
      return [h("div", { class: "stats" }, stat(dollars(s.total), "last 30 days, at API prices"), stat(String(s.conversations), "agent conversations")),
        spendChart(s.by_day),
        h("h2", { class: "section-title" }, "By project"), costRows(s.by_project, (k) => (k.startsWith("(") ? k : projectName(k))),
        h("h2", { class: "section-title" }, "By agent"), costRows(s.by_agent, (k) => (k === "conductor" ? "Conductors" : catalogEntry(k)?.name || agentName(k))),
        s.by_session.length ? [h("h2", { class: "section-title" }, "Costliest tasks"), costRows(s.by_session, (k) => h("a", { href: `#work/${k}` }, `Task #${k}`))] : null,
        h("p", { class: "faint" }, "Lago's own conversations have no budget and aren't counted.")];
    },
    async settings() {
      const revs = await bridge.call("config_revisions");
      if (!revs.length) return h("div", { class: "empty" }, h("strong", {}, "No changes yet."), "Each time teams, projects or settings are saved, the change is kept here so you can undo it.");
      return h("div", { class: "rows" }, revs.map((r) => h("div", { class: "row" },
        h("div", { class: "row-main" }, h("span", { class: "row-title" }, r.changed.slice(0, 4).join(", ") + (r.changed.length > 4 ? ` and ${r.changed.length - 4} more` : "")),
          h("span", { class: "row-sub" }, `#${r.id} · by ${actorName(r.source)}${r.restored_from ? ` (undid #${r.restored_from})` : ""} · ${ago(r.created_at)}`)),
        h("div", { class: "row-actions" },
          h("button", { class: "btn small ghost", onclick: () => revisionDialog(r.id) }, icon("eye"), "Details"),
          h("button", { class: "btn small", onclick: async (e) => {
            const ok = await confirmDialog({ title: `Undo change #${r.id}?`, body: "Puts the settings back the way they were before it. Running agents pick it up when they restart. The undo is kept as a change too.", confirm: "Undo Change" });
            if (ok) runAction(e.currentTarget, () => bridge.call("restore_config", { revision_id: r.id }), "Undone.").then(async () => { state.config = (await bridge.call("init")).config; load(); });
          } }, icon("arrow-counter-clockwise"), "Undo")))));
    },
    async backups() {
      const list = await bridge.call("backups");
      return [h("div", { class: "controls-line" }, h("button", { class: "btn", onclick: (e) => runAction(e.currentTarget, () => bridge.call("backup_now"), "Backed up.").then(load) }, icon("database"), "Back Up Now"),
        h("span", { class: "faint" }, "A copy of tasks, sessions, plans and history is made each day; the newest seven are kept.")),
      list.length ? h("div", { class: "rows" }, list.map((b) => h("div", { class: "row" },
        h("div", { class: "row-main" }, h("span", { class: "row-title mono" }, b.name), h("span", { class: "row-sub" }, `${Math.round(b.size / 1024)} KB · ${ago(b.at)}`)),
        h("div", { class: "row-actions" }, h("button", { class: "btn small ghost danger", onclick: async (e) => {
          const ok = await confirmDialog({ title: "Restore this backup?", body: "Replaces tasks, sessions, plans and history with this copy. The current database is kept beside the backups first. Restart Lago Agents afterwards.", confirm: "Restore", danger: true });
          if (ok) runAction(e.currentTarget, () => bridge.call("restore_backup", { name: b.name }), "Restored. Restart Lago Agents.");
        } }, icon("clock-counter-clockwise"), "Restore")))))
        : h("p", { class: "faint" }, "No backups yet.")];
    },
  };

  async function load() {
    try {
      const content = await loaders[tab]();
      if (alive()) body.replaceChildren(...[content].flat(Infinity).filter(Boolean));
    } catch (error) { if (alive()) body.replaceChildren(errorBox(error, load)); }
  }

  root.append(pageHead("History", "What happened, what it cost, and every change, with a way back.", load),
    h("div", { class: "controls-line" }, query, h("button", { class: "btn", onclick: runSearch }, icon("magnifying-glass"), "Search")),
    searchSlot, h("div", { class: "controls-line" }, seg), body);
  load();
};

async function revisionDialog(id) {
  const rev = await bridge.call("config_revision", { revision_id: id });
  const show = (v) => (v === null || v === undefined ? "(none)" : JSON.stringify(v, null, 2));
  openDialog((close) => h("div", {}, h("h2", {}, `Change #${id}`),
    h("p", { class: "muted" }, `By ${actorName(rev.source)} · ${ago(rev.created_at)}`),
    ...rev.diff.map((d) => h("section", { class: "diff" }, h("h3", { class: "mono" }, d.path),
      h("div", { class: "diff-cols" }, h("div", {}, h("span", { class: "faint" }, "Before"), h("pre", { class: "mono wrap" }, show(d.before))),
        h("div", {}, h("span", { class: "faint" }, "After"), h("pre", { class: "mono wrap" }, show(d.after)))))),
    h("div", { class: "dialog-actions" }, h("button", { class: "btn", onclick: () => close() }, "Close"))), { wide: true });
}

/* ================================================================ teams builder */

/* Which subscription new sessions run on: Claude (Claude Code) or ChatGPT (Codex). */
async function providerSection(slot, alive) {
  let p;
  try { p = await bridge.call("providers"); } catch (error) { slot.replaceChildren(errorBox(error, () => providerSection(slot, alive))); return; }
  if (!alive()) return;
  const reload = () => providerSection(slot, alive);
  const limit = (l) => l?.primary ? `${Math.round(l.primary.used_percent)}% of its ${l.primary.window_minutes >= 10080 ? "weekly" : l.primary.window_minutes >= 1440 ? "daily" : `${Math.round(l.primary.window_minutes / 60)}-hour`} limit used`
    + (l.primary.resets_at ? `, resets ${new Date(l.primary.resets_at * 1000).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "") : "";
  const card = (id, title, logo, info, ok) => h("article", { class: `provider-row${p.current === id ? " on" : ""}` },
    icon(logo),
    h("div", { class: "row-main" }, h("span", { class: "row-title" }, title, p.current === id ? h("span", { class: "pill p-running", style: "margin-left:8px" }, "New sessions run here") : null),
      h("span", { class: "row-sub" }, info)),
    h("div", { class: "row-actions" },
      ok ? h("button", { class: "btn small ghost", onclick: (e) => runAction(e.currentTarget, () => bridge.call("test_provider", { provider: id }).then((r) => ({ ok: r.ok, message: r.message })), "Connected.") }, icon("plugs-connected"), "Test")
        : h("button", { class: "btn small", onclick: (e) => runAction(e.currentTarget, () => bridge.call("login_provider", { provider: id })) }, icon("sign-in"), "Log In"),
      p.current !== id ? h("button", { class: "btn small primary", disabled: !ok, onclick: (e) => runAction(e.currentTarget, () => bridge.call("set_provider", { provider: id })).then(reload) }, "Use for New Sessions") : null));
  const cl = p.claude, cx = p.codex;
  fill(slot, h("section", { class: "provider-section" },
    h("h2", { class: "section-title", style: "margin-top:0" }, "Subscription"),
    card("claude", "Claude", "asterisk", !cl.installed ? "Claude Code isn't installed." : cl.logged_in ? `Claude Code, logged in${cl.email ? ` as ${cl.email}` : ""}${cl.plan ? ` · ${cl.plan}` : ""}. Accounts and limits below.` : "Claude Code is installed but not logged in.", cl.logged_in),
    card("codex", "ChatGPT", "open-ai-logo", !cx.installed ? "Codex isn't installed (npm install -g @openai/codex)." : cx.logged_in ? [`Codex, logged in with ${cx.method || "ChatGPT"}`, limit(cx.limits) ? ` · ${limit(cx.limits)}` : "", ". Test checks the login still works."].join("") : "Codex is installed but not logged in.", cx.installed && cx.logged_in),
    h("p", { class: "faint", style: "font-size:12.5px;margin:6px 0 0" }, "Lago, leads and agents run on the subscription you pick; agents already running keep theirs until they restart. A catalog agent can name its own (runtime: claude or codex). Budgets count Claude conversations only (Codex runs are limited by your ChatGPT plan), and agent evaluations are graded by Claude.")));
}

/* Teams, swarm agents and skills as one archive (portability.py). */
function exportDialog() {
  const config = userConfig();
  const picked = { teams: new Set(), catalog: new Set() };
  const box = (kind, id, label) => h("label", { class: "q-choice" }, h("input", { type: "checkbox", onchange: (e) => { e.target.checked ? picked[kind].add(id) : picked[kind].delete(id); } }), label);
  const skills = h("input", { class: "input mono", placeholder: "skill names, comma-separated (from claude-skills/)" });
  return openDialog((close) => h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const ok = await runAction(e.submitter, () => bridge.call("export_team", { teams: [...picked.teams], catalog: [...picked.catalog], skills: csv(skills.value) }));
      if (ok) close();
    } },
    h("h2", {}, "Export"),
    h("p", { class: "desc" }, "One archive with the teams, agents (and their tests), the definitions they build on and the skills you pick. It goes to ~/lago-work/exports/. Project folders and accounts stay out."),
    h("fieldset", { class: "q-field" }, h("legend", {}, "Teams"), h("div", { class: "q-choices" }, Object.entries(config.teams).map(([id, t]) => box("teams", id, t.name)))),
    h("details", {}, h("summary", { class: "faint" }, "Swarm agents"), h("div", { class: "q-choices", style: "margin-top:8px" }, state.catalog.agents.map((a) => box("catalog", a.id, a.name)))),
    h("div", { class: "field" }, h("label", {}, "Skills"), skills),
    h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close() }, "Cancel"),
      h("button", { type: "submit", class: "btn primary" }, icon("export"), "Export"))), { wide: true });
}

async function importDialog() {
  const path = await promptDialog({ title: "Import", label: "Archive (.tar.gz)", placeholder: "~/Downloads/lago-team.tar.gz", confirm: "Preview" });
  if (!path) return;
  let plan;
  try { plan = await bridge.call("preview_import", { path }); } catch (error) { toast(error.message, "error"); return; }
  const list = (title, items, note) => items.length ? h("details", { open: title === "New" || undefined }, h("summary", {}, `${title} (${items.length})${note ? ` · ${note}` : ""}`), h("ul", { class: "mono", style: "font-size:12.5px" }, items.map((i) => h("li", {}, i)))) : null;
  const ok = await openDialog((close) => h("div", {}, h("h2", {}, "Import this archive?"),
    h("p", { class: "desc" }, `Teams: ${plan.teams.join(", ") || "none"} (${plan.teams_new.length} new). Agents in them: ${plan.agents_new.length} new. Read new prompts and skills before you rely on them: they come from whoever made the archive.`),
    list("New", plan.new), list("Already yours", plan.same, "unchanged"), list("Different from yours", plan.different, "yours are kept"),
    h("div", { class: "dialog-actions" }, h("button", { class: "btn ghost", onclick: () => close(false) }, "Cancel"),
      h("button", { class: "btn primary", disabled: !plan.new.length && !plan.teams_new.length, onclick: () => close(true) }, "Import"))), { wide: true });
  if (ok) runAction(null, () => bridge.call("apply_import", { path })).then(async () => { state.config = (await bridge.call("init")).config; refreshCatalog(); navigate(); });
}

async function skillFetchDialog() {
  const repo = h("input", { class: "input mono", required: true, placeholder: "https://github.com/owner/repo" });
  const path = h("input", { class: "input mono", placeholder: "folder of the skill, if the repo has several" });
  return openDialog((close) => h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const ok = await runAction(e.submitter, () => bridge.call("fetch_skill", { repo: repo.value.trim(), path: path.value.trim() }));
      if (ok) { close(); navigate(); }
    } },
    h("h2", {}, "Add a Skill from a Repository"),
    h("p", { class: "desc" }, "It's fetched at its current commit and checked against the Agent Skills spec. Nothing is installed until you've read it and press Install."),
    h("div", { class: "field" }, h("label", {}, "Repository"), repo),
    h("div", { class: "field" }, h("label", {}, "Path (optional)"), path),
    h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close() }, "Cancel"),
      h("button", { type: "submit", class: "btn primary" }, "Fetch"))));
}

/* Agents, Teams and other Claude sessions: one place, three tabs. */
function agentsTabs(active) {
  return h("nav", { class: "segmented page-tabs", "aria-label": "Agents" },
    [["swarm", "Agents", "users-three"], ["find", "Find on GitHub", "compass"], ["import", "Import", "download-simple"], ["teams", "Fixed Teams", "tree-structure"], ["sessions", "Other Claude Sessions", "terminal-window"]]
      .map(([view, label, ic]) => h("a", { href: `#${view}`, role: "tab", "aria-selected": String(view === active), class: view === active ? "on" : "" }, icon(ic), label)));
}

function orgChart(config) {
  const node = (id) => {
    const a = config.agents[id];
    const kids = Object.keys(config.agents).filter((k) => config.agents[k].reports_to === id);
    return h("li", {}, h("span", { class: "org-node" }, h("a", { href: `#agent/${id}` }, a.name),
      h("span", { class: "faint" }, ` ${humanize(a.definition || "")}`)),
      kids.length ? h("ul", {}, kids.map(node)) : null);
  };
  const teams = Object.entries(config.teams).filter(([, t]) => !t.swarm);
  return h("div", { class: "fold-body org-chart" }, teams.map(([tid, t]) => {
    const roots = Object.keys(config.agents).filter((k) => config.agents[k].team === tid && !config.agents[k].reports_to);
    return h("section", {}, h("h3", {}, t.name), roots.length ? h("ul", { class: "org-tree" }, roots.map(node)) : h("p", { class: "faint" }, "No agents yet."));
  }));
}

views.teams = (root, alive) => {
  let draft = userConfig();
  const dirty = () => JSON.stringify(draft) !== JSON.stringify(userConfig());
  const body = h("div");
  const bar = h("div", { class: "controls-line" });

  function render() {
    renderBar();
    const teams = Object.entries(draft.teams);
    body.replaceChildren(...(teams.length ? teams.map(([id, team]) => teamEditor(id, team))
      : [h("div", { class: "empty" }, h("strong", {}, "No teams yet."), "Create one to start building your organization.")]));
  }

  function renderBar() {
    fill(bar,
      h("button", { class: "btn", onclick: addTeam }, icon("plus"), "New Team"),
      h("button", { class: "btn ghost", onclick: () => exportDialog() }, icon("export"), "Export"),
      h("button", { class: "btn ghost", onclick: () => importDialog() }, icon("download-simple"), "Import"),
      h("span", { style: "flex:1" }),
      dirty() ? h("span", { class: "muted" }, "Unsaved changes") : null,
      dirty() ? h("button", { class: "btn ghost", onclick: () => { draft = userConfig(); render(); } }, "Discard") : null,
      h("button", { class: "btn primary", disabled: !dirty(), onclick: save }, icon("floppy-disk"), "Save Changes"));
  }

  async function save(e) {
    const ok = await runAction(e.currentTarget, () => saveConfig(structuredClone(draft)), "Saved. Running agents pick up changes when they restart.");
    if (ok) { draft = userConfig(); render(); }
  }

  function update(fn) { fn(); render(); }
  /* Text fields change the draft without re-rendering, so the next click still lands. */
  function edit(fn) { fn(); renderBar(); }

  async function addTeam() {
    const name = await promptDialog({ title: "New Team", label: "Name", placeholder: "e.g. Research", confirm: "Create" });
    if (!name) return;
    const id = uniqueId(name, { ...draft.teams, swarm: 1, conductor: 1 });
    update(() => { draft.teams[id] = { name, icon: "users-three", cwd: "~/projects", description: "" }; });
  }

  function teamEditor(teamId, team) {
    const members = Object.entries(draft.agents).filter(([, a]) => a.team === teamId);
    const field = (label, control) => h("div", { class: "field" }, h("label", { for: control.id }, label), control);
    const nameInput = h("input", { id: `t-${teamId}-name`, class: "input", value: team.name, oninput: (e) => edit(() => { team.name = e.target.value.trim() || team.name; }) });
    const cwdInput = h("input", { id: `t-${teamId}-cwd`, class: "input mono", value: team.cwd || "", placeholder: "~/projects/…", spellcheck: "false",
      oninput: (e) => edit(() => { team.cwd = e.target.value.trim(); }) });
    const iconSelect = h("select", { id: `t-${teamId}-icon`, class: "select", onchange: (e) => update(() => { team.icon = e.target.value; }) },
      TEAM_ICONS.map((i) => h("option", { value: i, selected: i === team.icon }, humanize(i))));

    const table = h("div", { class: "table-wrap" }, h("table", { class: "member-table" },
      h("colgroup", {}, h("col", { style: "width:20%" }), h("col", { style: "width:25%" }), h("col", { style: "width:19%" }), h("col", { style: "width:17%" }), h("col", { style: "width:15%" }), h("col", { style: "width:44px" })),
      h("thead", {}, h("tr", {}, ["Name", "Agent definition", "Reports to", "Model", "Permissions", ""].map((c) => h("th", { style: "padding:10px 8px" }, c)))),
      h("tbody", {}, members.length ? members.map(([aid, a]) => memberRow(teamId, aid, a, members))
        : h("tr", {}, h("td", { colspan: 6, class: "muted", style: "padding:14px" }, "No agents yet. Add them from your agent definitions.")))));

    return h("section", { class: "section", "aria-label": team.name },
      h("div", { class: "section-head" }, h("span", { class: "team-icon", style: "color:var(--accent)" }, icon(team.icon || "users-three")),
        h("h2", {}, team.name), h("span", { class: "faint mono", style: "font-size:12px" }, teamId), h("span", { class: "spacer" }),
        h("button", { class: "btn small", onclick: () => addMembers(teamId) }, icon("user-plus"), "Add Agents"),
        h("button", { class: "btn small ghost danger", onclick: () => deleteTeam(teamId) }, icon("trash"), "Delete Team")),
      h("div", { class: "team-form" }, field("Name", nameInput), field("Project folder", cwdInput), field("Icon", iconSelect)),
      table);
  }

  function memberRow(teamId, aid, a, members) {
    const defOptions = [h("option", { value: "" }, "None (plain Claude Code)"),
      ...state.definitions.map((d) => h("option", { value: d.name, selected: d.name === a.definition }, d.name))];
    if (a.definition && !state.definitions.some((d) => d.name === a.definition)) defOptions.push(h("option", { value: a.definition, selected: true }, `${a.definition} (missing)`));
    const def = state.definitions.find((d) => d.name === a.definition);
    const reportOptions = [h("option", { value: "" }, "No one: team lead"),
      ...members.filter(([id]) => id !== aid && !reportsTo(id, aid)).map(([id, m]) => h("option", { value: id, selected: id === a.reports_to }, m.name))];
    return h("tr", {},
      h("td", {}, h("input", { class: "input", value: a.name, "aria-label": "Agent name", oninput: (e) => edit(() => { a.name = e.target.value.trim() || a.name; }) }),
        h("div", { class: "def-desc mono" }, aid)),
      h("td", {}, h("select", { class: "select", "aria-label": `Definition for ${a.name}`, onchange: (e) => update(() => { a.definition = e.target.value; }) }, defOptions),
        def ? h("div", { class: "def-desc truncate", title: def.description }, def.description) : null),
      h("td", {}, h("select", { class: "select", "aria-label": `Who ${a.name} reports to`, onchange: (e) => update(() => { a.reports_to = e.target.value || null; }) }, reportOptions)),
      h("td", {}, h("select", { class: "select", "aria-label": `Model for ${a.name}`, onchange: (e) => update(() => { if (e.target.value) a.model = e.target.value; else delete a.model; }) },
        MODELS.map(([v, l]) => h("option", { value: v, selected: v === (a.model || "") }, v === "" && def?.model ? `Default (${def.model})` : l)))),
      h("td", {}, h("select", { class: "select", "aria-label": `Permissions for ${a.name}`, onchange: (e) => update(() => { if (e.target.value) a.permission_mode = e.target.value; else delete a.permission_mode; }) },
        PERMISSION_MODES.map(([v, l]) => h("option", { value: v, selected: v === (a.permission_mode || "") }, l)))),
      h("td", {}, h("button", { class: "btn icon small ghost danger", "aria-label": `Remove ${a.name}`, title: "Remove", onclick: () => removeMember(aid) }, icon("trash"))));
  }

  /* true when `id` reports (directly or not) to `ancestor`, so it can't become its lead */
  function reportsTo(id, ancestor) {
    for (let cursor = draft.agents[id]?.reports_to; cursor; cursor = draft.agents[cursor]?.reports_to) {
      if (cursor === ancestor) return true;
    }
    return false;
  }

  function removeMember(aid) {
    update(() => {
      const lead = draft.agents[aid].reports_to || null;
      for (const a of Object.values(draft.agents)) if (a.reports_to === aid) a.reports_to = lead;
      delete draft.agents[aid];
    });
  }

  async function deleteTeam(teamId) {
    const ok = await confirmDialog({ title: `Delete ${draft.teams[teamId].name}?`, body: "Removes the team and its agents from Lago Agents. Agent definition files and project folders are not touched.", confirm: "Delete Team", danger: true });
    if (!ok) return;
    update(() => {
      for (const [aid, a] of Object.entries(draft.agents)) if (a.team === teamId) delete draft.agents[aid];
      delete draft.teams[teamId];
    });
  }

  async function addMembers(teamId) {
    const members = Object.entries(draft.agents).filter(([, a]) => a.team === teamId);
    const lead = members.find(([, a]) => !a.reports_to)?.[0];
    const picked = await openDialog((close) => {
      const search = h("input", { class: "input", placeholder: "Filter…", "aria-label": "Filter definitions", autofocus: true,
        oninput: () => list.querySelectorAll(".app-option").forEach((o) => { o.hidden = !o.textContent.toLowerCase().includes(search.value.toLowerCase()); }) });
      const list = h("div", { class: "app-list" },
        h("label", { class: "app-option" }, h("input", { type: "checkbox", name: "def", value: "" }), icon("sparkle"),
          h("span", {}, h("span", { class: "row-title" }, "Plain agent"), h("div", { class: "def-desc" }, "Claude Code with no definition, only Lago's team instructions."))),
        state.definitions.map((d) => h("label", { class: "app-option" }, h("input", { type: "checkbox", name: "def", value: d.name }), icon("robot"),
          h("span", { style: "min-width:0" }, h("span", { class: "row-title mono" }, d.name), h("div", { class: "def-desc truncate" }, d.description)))));
      const reportsTo = h("select", { id: "add-reports", class: "select" },
        !lead ? h("option", { value: "" }, "No one: the first becomes the lead") : null,
        members.map(([id, m]) => h("option", { value: id, selected: id === lead }, m.name)));
      return h("form", { onsubmit: (e) => {
          e.preventDefault();
          const defs = [...list.querySelectorAll("input:checked")].map((i) => i.value);
          close(defs.length ? { defs, reportsTo: reportsTo.value || null } : undefined);
        } },
        h("h2", {}, `Add Agents to ${draft.teams[teamId].name}`),
        h("p", { class: "desc" }, "Pick agent definitions from ~/.claude/agents and the team's project folder."),
        search, list,
        h("div", { class: "field" }, h("label", { for: "add-reports" }, "They report to"), reportsTo),
        h("div", { class: "dialog-actions" },
          h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
          h("button", { type: "submit", class: "btn primary" }, "Add")));
    }, { wide: true });
    if (!picked) return;
    update(() => {
      let reportsToId = picked.reportsTo;
      for (const def of picked.defs) {
        const name = def ? humanize(def) : "Agent";
        const id = uniqueId(`${teamId}-${def || "agent"}`, draft.agents);
        draft.agents[id] = { name, team: teamId, definition: def, reports_to: reportsToId };
        if (!reportsToId) reportsToId = id; // the first one became the lead
      }
    });
  }

  const chart = h("details", { class: "fold" }, h("summary", {}, icon("tree-structure"), "Org Chart", h("span", { class: "faint" }, "who reports to whom, as saved")));
  chart.addEventListener("toggle", () => { if (chart.open) fill(chart, chart.firstChild, orgChart(userConfig())); });
  root.append(pageHead("Agents", "Fixed teams: a lead and the agents who report to it, always the same people. Most work doesn't need one: tasks pick agents for themselves."), agentsTabs("teams"), chart, bar, body);
  viewCleanup = () => {};
  render();
};

/* ================================================================ other sessions */

views.sessions = (root, alive) => {
  const body = h("div", {}, skeleton(3));
  const load = async () => {
    try {
      const sessions = await bridge.call("sessions");
      if (!alive()) return;
      body.replaceChildren(sessions.length ? h("div", { class: "rows" }, sessions.map((s) => h("div", { class: "row" },
        h("div", { class: "row-main" }, h("span", { class: "row-title" }, s.project),
          h("span", { class: "row-sub mono truncate" }, s.cwd),
          h("span", { class: "row-sub" }, `PID ${s.pid} · started ${ago(s.started)}${s.args ? ` · ${s.args}` : ""}`)),
        h("div", { class: "row-actions" }, h("button", { class: "btn small ghost danger", onclick: async (e) => {
          const ok = await confirmDialog({ title: `Stop the session in ${s.project}?`, body: "Ends that Claude Code process. If it's the session you're typing in, it closes too.", confirm: "Stop Session", danger: true });
          if (ok) runAction(e.currentTarget, () => bridge.call("stop_session", { pid: s.pid })).then(load);
        } }, icon("stop"), "Stop")))))
        : h("div", { class: "empty" }, h("strong", {}, "No other Claude Code sessions are running."), "Sessions you start yourself in a terminal show up here."));
    } catch (error) {
      if (alive()) body.replaceChildren(errorBox(error, load));
    }
  };
  root.append(pageHead("Agents", "Claude Code sessions running on this computer outside Lago Agents. They can't be given work, but you can see and stop them.", load), agentsTabs("sessions"), body);
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 5000);
  viewCleanup = () => clearInterval(timer);
  load();
};

/* ================================================================ accounts */

const LIMIT_LABELS = { session: "Session (5 hours)", week: "Week" };
const compact = (n) => new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n || 0);

function limitMeter(limit) {
  const label = LIMIT_LABELS[limit.window] + (limit.scope && limit.scope !== "all models" ? ` · ${limit.scope}` : "");
  const level = limit.percent >= 90 ? "high" : limit.percent >= 70 ? "mid" : "";
  return h("div", { class: "limit" },
    h("div", { class: "limit-head" }, h("span", {}, label), h("b", { class: "num" }, `${limit.percent}%`)),
    h("div", { class: `meter ${level}`, role: "meter", "aria-label": label, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(limit.percent) },
      h("span", { style: `width:${Math.min(100, limit.percent)}%` })),
    limit.resets ? h("span", { class: "row-sub" }, `Resets ${limit.resets}`) : null);
}

function accountCard(account, actions) {
  const { status, usage, lago } = account;
  const tokens = (w) => (lago[w] ? `${compact(lago[w].input)} in · ${compact(lago[w].output)} out · ${lago[w].messages} calls` : "None");
  const activity = (w) => (usage.activity?.[w] ? `${usage.activity[w].requests.toLocaleString()} requests · ${usage.activity[w].sessions} sessions` : "-");
  return h("section", { class: `account${account.active ? " active" : ""}` },
    h("div", { class: "section-head" },
      icon("user-circle"),
      h("h2", {}, account.label),
      account.active ? h("span", { class: "pill working" }, "New sessions use this") : null,
      status.logged_in ? h("span", { class: "row-sub" }, [status.email, status.plan && status.plan.toUpperCase()].filter(Boolean).join(" · "))
        : h("span", { class: "status fail" }, "Not logged in"),
      h("span", { class: "spacer" }),
      actions),
    status.logged_in ? h("div", { class: "account-body" },
      h("div", { class: "limits" },
        usage.limits?.length ? usage.limits.map(limitMeter)
          : h("p", { class: "muted" }, usage.error ? `Couldn't read the limits: ${usage.error}` : "Reading the limits…"),
        usage.fetched_at ? h("span", { class: "faint", style: "font-size:12px" },
          `From Claude Code's /usage, ${ago(usage.fetched_at)}${usage.refreshing ? " · updating" : ""}`) : null),
      h("div", { class: "rows" },
        h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "Lago sessions today"), h("span", { class: "num" }, tokens("today")))),
        h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "Lago sessions, last 7 days"), h("span", { class: "num" }, tokens("week")))),
        h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "Agents running on it"), h("span", { class: "num" }, String(account.running)))),
        h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "This machine, last 24 hours (all accounts)"), h("span", { class: "num" }, activity("24h")))),
        h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "This machine, last 7 days (all accounts)"), h("span", { class: "num" }, activity("7d"))))))
      : h("p", { class: "muted" }, "Log in to use this account. Claude Code opens your browser to sign in; the login is kept in ",
          h("span", { class: "mono" }, account.folder), "."));
}

views.accounts = (root, alive, action, actionAccount) => {
  const body = h("div", {}, skeleton(4));
  const termBar = h("div");
  let terminal = null;

  terminalEnded = (id) => {
    if (terminal?.id !== id) return;
    terminal = null;
    renderTermBar();
    load(true);  // the login just finished: show the account's new status
  };

  function renderTermBar() {
    fill(termBar, terminal ? h("div", { class: "now" }, icon("terminal-window"),
      h("div", { style: "flex:1" }, h("b", {}, terminal.label), h("span", { class: "muted" }, " · in the terminal on the right. It closes when you're done.")),
      h("button", { class: "btn small ghost", onclick: () => { terminal = null; bridge.call("hide_terminal").catch(() => {}); renderTermBar(); } }, "Hide")) : null);
  }

  async function login(account, button) {
    try {
      if (button) button.disabled = true;
      const res = await bridge.call("login_account", { account_id: account.id });
      toast(res.message);
      terminal = { id: res.terminal, label: `Logging in to ${account.label}` };
      setTimeout(() => bridge.call("show_terminal", { agent_id: res.terminal }).catch(() => {}), 400);
      renderTermBar();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  function actionsFor(account) {
    const use = account.active || !account.status.logged_in ? null
      : h("button", { class: "btn small primary", onclick: (e) => runAction(e.currentTarget, () => bridge.call("set_active_account", { account_id: account.id })).then(() => { load(); refreshAccountChip(); }) },
          icon("user-switch"), "Use for New Sessions");
    const loginButton = h("button", { class: `btn small${account.status.logged_in ? " ghost" : " primary"}`, onclick: (e) => login(account, e.currentTarget) },
      icon("sign-in"), account.status.logged_in ? "Log In Again" : "Log In");
    const more = h("button", { class: "btn small icon ghost", "aria-label": `More for ${account.label}`, onclick: (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      showMenu(r.left, r.bottom + 4, [
        { icon: "pencil-simple", label: "Rename", action: async () => {
          const label = await promptDialog({ title: "Rename account", label: "Name", value: account.label, confirm: "Rename" });
          if (label) runAction(null, () => bridge.call("rename_account", { account_id: account.id, label })).then(load);
        } },
        { icon: "trash", label: "Remove", danger: true, disabled: account.id === "main", action: async () => {
          const ok = await confirmDialog({ title: `Remove ${account.label}?`, body: "Deletes its login from this machine. Your agents, skills and session history stay (they're shared with the main account).", confirm: "Remove", danger: true });
          if (ok) runAction(null, () => bridge.call("remove_account", { account_id: account.id })).then(() => { load(); refreshAccountChip(); });
        } },
      ]);
    } }, icon("dots-three"));
    return [use, loginButton, more];
  }

  /* Weekly or monthly caps (budgets.windows): kept while you edit, like the budgets. */
  let windowForm = null;
  function windowSettings(windows) {
    if (windowForm?.contains(document.activeElement)) return windowForm;
    let rows = windows.map((w) => ({ ...w }));
    const scopes = [["all", "Everything"], ...Object.entries(state.config.projects).map(([id, p]) => [`project:${id}`, `Project: ${p.name || id}`]),
      ...state.catalog.agents.map((a) => [`agent:${a.id}`, `Agent: ${a.name}`])];
    const list = h("div", { class: "rows" });
    const render = () => fill(list, rows.length ? rows.map((w, i) => h("div", { class: "row" },
      h("div", { class: "row-main", style: "grid-auto-flow:column;justify-content:start;gap:10px;align-items:center" },
        h("select", { class: "select", "aria-label": "Covers", onchange: (e) => { w.scope = e.target.value; } },
          (scopes.some(([v]) => v === w.scope) ? scopes : [[w.scope, w.scope], ...scopes]).map(([v, l]) => h("option", { value: v, selected: v === w.scope }, l))),
        h("select", { class: "select", "aria-label": "Window", onchange: (e) => { w.window = e.target.value; } },
          [["week", "per week"], ["month", "per month"]].map(([v, l]) => h("option", { value: v, selected: v === w.window }, l))),
        h("input", { class: "input num", type: "number", min: "1", step: "1", value: String(w.limit), style: "max-width:110px", "aria-label": "Limit in dollars", oninput: (e) => { w.limit = e.target.value; } }),
        w.spent !== undefined ? h("span", { class: "faint" }, `${dollars(w.spent)} spent so far${w.extended ? ` (extended by ${dollars(w.extended)})` : ""}`) : null),
      h("div", { class: "row-actions" }, h("button", { type: "button", class: "btn small ghost", "aria-label": "Remove", onclick: () => { rows.splice(i, 1); render(); } }, icon("trash")))))
      : h("p", { class: "faint" }, "No windows: only the per-conversation and per-session budgets apply."));
    render();
    windowForm = h("form", { class: "section", style: "margin-top:24px", onsubmit: (e) => {
        e.preventDefault();
        runAction(e.submitter, () => bridge.call("save_budget_windows", { windows: rows.map(({ scope, window, limit }) => ({ scope, window, limit })) })).then(() => load());
      } },
      h("div", { class: "section-head" }, h("h2", {}, "Weekly and Monthly Budgets")),
      h("p", { class: "desc" }, "Caps on everything agents spend in a week or a month, overall, for one project or for one agent. When one is used up, the agents it covers stop and wait for you, like the budgets above, until you extend it or the next week or month starts."),
      list,
      h("div", { class: "controls-line", style: "margin-top:12px" },
        h("button", { type: "button", class: "btn small", onclick: () => { rows.push({ scope: "all", window: "month", limit: 100 }); render(); } }, icon("plus"), "Add Window"),
        h("button", { class: "btn primary", type: "submit" }, icon("floppy-disk"), "Save Windows")));
    return windowForm;
  }

  /* The default budgets (budgets.py): kept while you type, since the page reloads every minute. */
  let budgetForm = null;
  function budgetSettings(budgets, warnAt) {
    if (budgetForm?.contains(document.activeElement)) return budgetForm;
    const field = (id, label, value, help) => h("div", { class: "field" }, h("label", { for: id }, label),
      h("input", { id, class: "input num", type: "number", min: "0", step: "0.5", value: String(value), style: "max-width:140px" }),
      h("p", { class: "help" }, help));
    budgetForm = h("form", { class: "section", style: "margin-top:24px", onsubmit: (e) => {
        e.preventDefault();
        runAction(e.submitter, () => bridge.call("save_budgets", { agent: byId("budget-agent").value || 0, session: byId("budget-session").value || 0 }));
      } },
      h("div", { class: "section-head" }, h("h2", {}, "Budgets")),
      h("p", { class: "desc" }, `What an agent may spend before it stops and waits for you, priced at Anthropic's API rates (plans don't bill in dollars, but this weighs models and caching the way their limits do). You're told at ${Math.round(warnAt * 100)}%; at the limit it stops mid-turn, shows in the bell and on your phone, and carries on only when you extend it. Lago himself has no budget. 0 means no limit.`),
      field("budget-agent", "Per agent conversation ($)", budgets.agent, "One plan step, one run Lago started, or one team member's conversation."),
      field("budget-session", "Per task ($)", budgets.session, "Everything in one task: its lead and every agent it wakes."),
      h("button", { class: "btn primary", type: "submit" }, icon("floppy-disk"), "Save Budgets"));
    return budgetForm;
  }

  let pendingLogin = action === "login" ? actionAccount : null;
  async function load(refresh = false) {
    try {
      const { accounts: list, auto_switch: autoSwitch, switch_at: switchAt, budgets, warn_at: warnAt, windows } = await bridge.call("accounts", { refresh });
      if (!alive()) return;
      if (pendingLogin) {
        const account = list.find((a) => a.id === pendingLogin);
        pendingLogin = null;
        history.replaceState(null, "", "#accounts");
        if (account) login(account);
      }
      fill(body,
        h("label", { class: "switch-row", style: "margin-bottom:18px" },
          h("button", { class: "switch", role: "switch", type: "button", "aria-checked": String(autoSwitch), onclick: (e) => {
            const on = e.currentTarget.getAttribute("aria-checked") !== "true";
            e.currentTarget.setAttribute("aria-checked", String(on));
            runAction(null, () => bridge.call("set_auto_switch", { on }));
          } }),
          `Switch accounts automatically: new sessions move at ${switchAt}% of a limit, and an agent that hits a limit resumes on the other account`),
        list.map((account) => accountCard(account, actionsFor(account))),
        budgetSettings(budgets, warnAt),
        windowSettings(windows || []),
        h("p", { class: "faint", style: "font-size:12.5px;margin-top:24px" },
          "Claude plans have a 5-hour session limit and weekly limits; there is no daily limit. ",
          "Limits come from Claude Code's /usage and count everything on that account (other machines and claude.ai too). ",
          "Lago's own token counts add up its agents' sessions per account. Running agents keep their account until restarted."));
    } catch (error) {
      if (alive()) body.replaceChildren(errorBox(error, () => load()));
    }
  }

  const add = h("button", { class: "btn", onclick: async () => {
    const label = await promptDialog({ title: "Add a Claude account", label: "Name", placeholder: "Work",
      help: "Its own folder keeps its login; agents, skills, plugins and history are shared with the main account.", confirm: "Add" });
    if (!label) return;
    try {
      const res = await bridge.call("add_account", { label });
      toast(res.message);
      await load();
    } catch (error) {
      toast(error.message, "error");
    }
  } }, icon("plus"), "Add Account");

  const providerSlot = h("div");
  providerSection(providerSlot, alive);
  root.append(pageHead("Accounts & Budgets", "The subscriptions your agents run on, their limits, and what agents may spend.", () => load(true)),
    providerSlot, h("h2", { class: "section-title" }, "Claude accounts"),
    h("div", { class: "controls-line" }, add), termBar, body);
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 60000);
  viewCleanup = () => { clearInterval(timer); bridge.call("hide_terminal").catch(() => {}); };
  load();
};

/* ================================================================ phone */

/* Your phone: the Lago Agents Android app reaches this computer over Tailscale (remote.py).
   Set its password, switch the server on, pair a phone with a QR code, remove phones. */
function qrSvg(text) {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const n = qr.getModuleCount();
  const quiet = 4;
  let path = "";
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (qr.isDark(r, c)) path += `M${c + quiet},${r + quiet}h1v1h-1z`;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${n + 2 * quiet} ${n + 2 * quiet}`);
  svg.setAttribute("class", "qr");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Pairing QR code");
  svg.setAttribute("shape-rendering", "crispEdges");
  const bg = document.createElementNS(ns, "rect");
  bg.setAttribute("width", "100%"); bg.setAttribute("height", "100%"); bg.setAttribute("fill", "#fff");
  const fg = document.createElementNS(ns, "path");
  fg.setAttribute("d", path); fg.setAttribute("fill", "#000");
  svg.append(bg, fg);
  return svg;
}

function phonePasswordDialog(isSet) {
  return openDialog((close) => {
    const first = h("input", { id: "phone-pw", class: "input", type: "password", autocomplete: "new-password", autofocus: true, minlength: 10 });
    const again = h("input", { id: "phone-pw2", class: "input", type: "password", autocomplete: "new-password" });
    const error = h("p", { class: "error", hidden: true });
    return h("form", { onsubmit: (e) => {
        e.preventDefault();
        const problem = first.value.length < 10 ? "Use at least 10 characters." : first.value !== again.value ? "The two don't match." : null;
        if (problem) { error.textContent = problem; error.hidden = false; first.focus(); return; }
        close(first.value);
      } },
      h("h2", {}, isSet ? "Change the phone password" : "Set a phone password"),
      h("p", { class: "desc" }, "Your phone asks for it when you pair it and whenever its sign-in lapses. Use a different one from your Linux password: it's stored here only as a scrypt hash."),
      h("div", { class: "field" }, h("label", { for: "phone-pw" }, "Password (10 characters or more)"), first),
      h("div", { class: "field" }, h("label", { for: "phone-pw2" }, "Again"), again, error),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, icon("lock-key"), "Save Password")));
  });
}

function pairDialog(info, onClosed) {
  let timer = null;
  const left = h("span", { class: "num" });
  const tick = () => {
    const s = Math.max(0, Math.round(info.expires - Date.now() / 1000));
    left.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  tick();
  timer = setInterval(tick, 1000);
  return openDialog((close) => h("form", { method: "dialog", onsubmit: (e) => { e.preventDefault(); close(true); } },
    h("h2", {}, "Pair a phone"),
    h("p", { class: "desc" }, "Open Lago Agents on your phone (with Tailscale on) and scan this code. Then enter the phone password there."),
    h("div", { class: "pair-box" },
      qrSvg(info.qr),
      h("div", { class: "pair-facts" },
        h("span", { class: "label" }, "Or type it in"),
        h("div", { class: "rows" },
          h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "Address"), h("span", { class: "mono" }, info.url))),
          h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "Pairing code"), h("span", { class: "pair-code mono" }, info.code)))),
        h("p", { class: "faint", style: "font-size:12.5px" }, "Valid for ", left, ", once. The phone checks this computer's certificate: ",
          h("span", { class: "mono", style: "word-break:break-all" }, info.fingerprint)))),
    info.restart ? h("p", { class: "callout" }, icon("warning"), h("span", {}, "Phone access is off, so the phone can't reach this computer yet. Switch it on first.")) : null,
    h("div", { class: "dialog-actions" }, h("button", { type: "submit", class: "btn primary" }, "Done"))), { wide: true })
    .finally(() => { clearInterval(timer); onClosed(); });
}

views.phone = (root, alive) => {
  const body = h("div", {}, skeleton(4));
  const termBar = h("div");
  let terminal = null;

  terminalEnded = (id) => {
    if (terminal?.id !== id) return;
    terminal = null;
    renderTermBar();
    load();  // the build finished: show the published version
  };

  function renderTermBar() {
    fill(termBar, terminal ? h("div", { class: "now" }, icon("terminal-window"),
      h("div", { style: "flex:1" }, h("b", {}, "Building the phone app"), h("span", { class: "muted" }, " · in the terminal on the right. Phones update when it says Published.")),
      h("button", { class: "btn small ghost", onclick: () => { terminal = null; bridge.call("hide_terminal").catch(() => {}); renderTermBar(); } }, "Hide")) : null);
  }

  async function publish(button) {
    const notes = await noteDialog({ title: "Publish an app update", desc: "Builds the Android app with the next version number and sends it to your paired phones, which install it over the app they have.",
      label: "What changed (optional; otherwise the commit subjects since the last release)", confirm: "Build and Publish" });
    if (notes === undefined) return;
    try {
      if (button) button.disabled = true;
      const res = await bridge.call("remote_publish_app", { notes });
      toast(res.message);
      terminal = { id: res.terminal };
      setTimeout(() => bridge.call("show_terminal", { agent_id: res.terminal }).catch(() => {}), 400);
      renderTermBar();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function load() {
    try {
      const s = await bridge.call("remote_status");
      if (!alive()) return;
      const on = s.service.active;
      const reachable = Boolean(s.tailscale_ip);
      const next = !s.password_set ? "Step 1 of 3: set the phone password (top right of Phone access)."
        : !on ? "Step 2 of 3: switch phone access on."
        : !s.devices.length ? "Step 3 of 3: press Pair a Phone. It shows the QR code to scan with the app." : null;
      fill(body,
        next && reachable ? h("div", { class: "callout" }, icon("info"), h("p", {}, next)) : null,
        !reachable ? h("div", { class: "callout" }, icon("warning"),
          h("p", {}, "Tailscale isn't up on this computer. Phone access only listens on your Tailscale network, so start it (", h("span", { class: "mono" }, "sudo tailscale up"), ") and install Tailscale on your phone with the same account.")) : null,
        h("section", { class: "account" },
          h("div", { class: "section-head" }, icon("shield-check"), h("h2", {}, "Phone access"),
            h("span", { class: `status${on ? " on" : s.service.enabled ? " fail" : ""}` }, on ? "On" : s.service.enabled ? "Starting or failed" : "Off"),
            h("span", { class: "spacer" }),
            h("button", { class: "btn small ghost", onclick: async () => {
              const pw = await phonePasswordDialog(s.password_set);
              if (pw) runAction(null, () => bridge.call("remote_set_password", { password: pw })).then(load);
            } }, icon("lock-key"), s.password_set ? "Change Password" : "Set Password"),
            h("button", { class: `btn small${on ? " ghost" : " primary"}`, disabled: !s.password_set, title: s.password_set ? null : "Set the phone password first",
              onclick: (e) => runAction(e.currentTarget, () => bridge.call("remote_set_enabled", { on: !on })).then(() => setTimeout(load, 800)) },
              icon(on ? "power" : "play"), on ? "Switch Off" : "Switch On")),
          h("div", { class: "rows" },
            h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "Address (Tailscale only)"),
              h("span", { class: "mono" }, s.url || "Waiting for Tailscale"), s.tailscale_name ? h("span", { class: "row-sub mono" }, `Also https://${s.tailscale_name}:${s.port}`) : null)),
            h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "Certificate fingerprint (SHA-256)"),
              h("span", { class: "mono", style: "word-break:break-all" }, s.fingerprint || "Made when you first pair a phone"))),
            h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-sub" }, "Password"),
              h("span", {}, s.password_set ? "Set" : "Not set yet"))))),
        h("section", { class: "account" },
          h("div", { class: "section-head" }, icon("device-mobile"), h("h2", {}, "Phones"),
            h("span", { class: "spacer" }),
            h("button", { class: "btn small primary", disabled: !s.password_set, onclick: async (e) => {
              try {
                const info = await bridge.call("remote_pair");
                pairDialog(info, () => { bridge.call("remote_cancel_pair").catch(() => {}); load(); });
              } catch (error) { toast(error.message, "error"); }
            } }, icon("qr-code"), "Pair a Phone")),
          s.devices.length ? h("div", { class: "rows" }, s.devices.map((d) => h("div", { class: "row" },
            h("div", { class: "row-main" }, h("span", { class: "row-title" }, d.name),
              h("span", { class: "row-sub" }, `${d.signed_in ? "Signed in" : "Signed out"} · last seen ${ago(d.last_seen) || "never"}${d.last_ip ? ` from ${d.last_ip}` : ""} · paired ${ago(d.paired_at)}`),
              d.app_version ? h("span", { class: `row-sub${s.app && d.app_version < s.app.code ? " warn-text" : ""}` },
                s.app && d.app_version < s.app.code ? `App version ${d.app_version}, updating to ${s.app.code}` : `App version ${d.app_version}${s.app && d.app_version >= s.app.code ? " (up to date)" : ""}`) : null),
            h("div", { class: "row-actions" }, h("button", { class: "btn small ghost danger", onclick: async (e) => {
              const ok = await confirmDialog({ title: `Remove ${d.name}?`, body: "It's signed out at once and has to be paired again, with a new code, to come back.", confirm: "Remove", danger: true });
              if (ok) runAction(e.currentTarget, () => bridge.call("remote_revoke", { device_id: d.id })).then(load);
            } }, icon("trash"), "Remove")))))
            : h("p", { class: "muted" }, "No phones yet. Pair one to see and steer your agents from anywhere.")),
        h("section", { class: "account" },
          h("div", { class: "section-head" }, icon("package"), h("h2", {}, "Phone app"),
            s.app ? h("span", { class: "row-sub" }, `Published ${s.app.name} · ${ago(s.app.at)}`) : h("span", { class: "row-sub" }, "Nothing published yet"),
            h("span", { class: "spacer" }),
            h("button", { class: "btn small primary", onclick: (e) => publish(e.currentTarget) }, icon("upload-simple"), "Publish Update")),
          s.app?.notes ? h("pre", { class: "mono phone-log", style: "padding:0 4px 8px" }, s.app.notes) : null,
          h("p", { class: "faint", style: "font-size:12.5px" },
            "Changes to these pages reach the phone at once, with no update: it shows them from this computer. ",
            "Publish an update when the Android app itself changed (", h("span", { class: "mono" }, "android/"), "). ",
            "Phones download it over Tailscale, check it's signed with your key, and install it over the app they have.")),
        s.log.length ? h("details", { class: "fold" }, h("summary", {}, "Recent sign-ins and refusals"),
          h("pre", { class: "mono phone-log" }, s.log.join("\n"))) : null,
        h("p", { class: "faint", style: "font-size:12.5px;margin-top:24px" },
          "The phone runs no agents: it shows these pages from this computer and mirrors the terminals. ",
          "Traffic stays inside Tailscale and is encrypted twice (WireGuard, then TLS pinned to this computer's certificate). ",
          "Five wrong passwords lock that address out for a minute, doubling each time."));
    } catch (error) {
      if (alive()) body.replaceChildren(errorBox(error, load));
    }
  }

  root.append(pageHead("Phone", "Use Lago Agents from your Android phone, anywhere, through your Tailscale network.", load), termBar, body);
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 10000);
  viewCleanup = () => { clearInterval(timer); bridge.call("hide_terminal").catch(() => {}); };
  load();
};

/* The sidebar chip: which account new sessions start on, and its session limit. */
async function refreshAccountChip() {
  if (!featureOn("budgets")) { byId("account-chip").hidden = true; return; }  // it opens Accounts & Budgets
  try {
    const account = await bridge.call("active_account");
    const session = account.limits.find((l) => l.window === "session");
    const week = account.limits.find((l) => l.window === "week");
    fill(byId("account-chip"), icon("user-circle"), h("span", { class: "truncate" }, account.label),
      session ? h("span", { class: "faint num", title: "Session · week" }, `${session.percent}%${week ? ` · ${week.percent}%` : ""}`) : null);
    byId("account-chip").hidden = false;
  } catch {
    byId("account-chip").hidden = true;
  }
}

/* ================================================================ swarm: shared */

const DOMAIN_ICONS = { development: "code", quality: "seal-check", product: "lightbulb", marketing: "megaphone",
  ops: "gear-six", meta: "graph", design: "pen-nib", data: "database", general: "cube" };
const PIPELINE_LABELS = { draft: "Waiting for you", changes_requested: "Changes requested", approved: "Approved",
  running: "Running", done: "Done", failed: "Failed", rejected: "Rejected", cancelled: "Cancelled" };
const STEP_LABELS = { pending: "Pending", running: "Running", reviewing: "In review", done: "Done", failed: "Failed", skipped: "Skipped" };
const OPEN_PIPELINE = new Set(["draft", "changes_requested", "approved", "running"]);

const catalogEntry = (id) => state.catalog.agents.find((a) => a.id === id);
const projectName = (id) => id ? state.config.projects?.[id]?.name || id : "No project";
/* Breadcrumb link to a session's project, or to Projects for a session that has none. */
const projectCrumb = (id) => id ? h("a", { href: `#project/${id}` }, projectName(id)) : h("a", { href: "#projects" }, projectName(id));
const areaName = (projectId, areaId) => state.config.projects?.[projectId]?.areas?.[areaId]?.name || areaId;
const pipelinePill = (status) => h("span", { class: `pill p-${status}` }, PIPELINE_LABELS[status] || status);
const csv = (text) => text.split(",").map((s) => s.trim()).filter(Boolean);
const lines = (text) => text.split("\n").map((s) => s.trim()).filter(Boolean);
const githubUrl = (repo) => (repo ? `https://github.com/${repo.replace(/^(https?:\/\/)?(www\.)?github\.com\//, "").replace(/\.git$/, "")}` : "");

async function refreshCatalog() {
  state.catalog = await bridge.call("catalog");
}

function openUrl(url) {
  bridge.call("open_url", { url }).catch((error) => toast(error.message, "error"));
}

function manifestDialog(id) {
  const e = catalogEntry(id);
  if (!e) { toast(`There's no manifest for ${id}.`, "error"); return Promise.resolve(); }
  const prose = (label, text) => (text ? [h("dt", {}, label), h("dd", {}, text)] : null);
  return openDialog((close) => h("form", { method: "dialog", onsubmit: (ev) => { ev.preventDefault(); close(); } },
    h("div", { class: "manifest-head" },
      h("span", { class: "team-icon" }, icon(DOMAIN_ICONS[e.domain] || "cube")),
      h("div", { style: "flex:1;min-width:0" }, h("h2", {}, e.name), h("p", { class: "faint mono", style: "font-size:12px" }, `${e.id} · ${e.domain}`)),
      h("span", { class: `pill ${e.enabled ? "working" : "stopped"}` }, e.enabled ? "On" : "Off")),
    h("p", {}, e.summary),
    h("dl", { class: "manifest-prose" }, prose("Requires", e.requires), prose("Use when", e.use_when), prose("Avoid when", e.avoid_when)),
    h("div", {}, h("h3", { class: "label" }, "Inputs"),
      e.inputs.length ? h("div", { class: "io-list" }, e.inputs.map((i) => h("div", { class: "io" },
        h("span", { class: "mono" }, i.name), i.required ? h("span", { class: "badge warn" }, "required") : h("span", { class: "badge" }, "optional"),
        h("span", { class: "muted" }, i.description)))) : h("p", { class: "muted" }, "None.")),
    h("div", {}, h("h3", { class: "label" }, "Outputs"),
      e.outputs.length ? h("div", { class: "io-list" }, e.outputs.map((o) => h("div", { class: "io" },
        h("span", { class: "mono" }, o.name), h("span", { class: "muted" }, o.description)))) : h("p", { class: "muted" }, "Not declared.")),
    h("div", { class: "manifest-runs" },
      h("span", {}, h("b", {}, "Runs with "), e.extends ? [h("span", { class: "mono" }, e.extends), e.missing_base ? h("span", { class: "danger-text" }, " (not found!)") : ""] : "its own instructions",
        e.extra_instructions && e.extends ? " + extra instructions" : ""),
      h("span", {}, h("b", {}, "Model "), e.model || "default"),
      e.tools ? h("span", {}, h("b", {}, "Tools "), e.tools.join(", ")) : null,
      e.tags.length ? h("span", { class: "chips" }, e.tags.map((t) => h("span", { class: "chip" }, t))) : null,
      h("span", { class: "faint mono", style: "font-size:11.5px" }, e.path)),
    h("div", { class: "dialog-actions" }, h("button", { type: "submit", class: "btn primary" }, "Close"))), { wide: true });
}

/* Work always starts from a project: pick one if we don't know which. */
async function startWork() {
  const projects = Object.entries(state.config.projects || {});
  if (projects.length === 1) { location.hash = `#project/${projects[0][0]}`; return; }
  const picked = await openDialog((close) => h("form", { method: "dialog", onsubmit: (e) => e.preventDefault() },
    h("h2", {}, "Which project?"),
    projects.length ? h("div", { class: "pick-list" }, projects.map(([id, p]) => h("button", { type: "button", class: "pick", onclick: () => close(id) },
      h("span", { class: "team-icon" }, icon(p.icon || "cube")), h("span", { class: "row-title" }, p.name || id))))
      : h("p", { class: "muted" }, "No projects yet."),
    h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"))));
  if (picked) location.hash = `#project/${picked}`;
}

/* Your Linux password, sent once to be checked by sudo; Lago never stores it. */
function passwordDialog({ title, body, confirm }) {
  return openDialog((close) => {
    const input = h("input", { id: "pw-input", class: "input", type: "password", autocomplete: "current-password", autofocus: true });
    return h("form", { onsubmit: (e) => { e.preventDefault(); if (input.value) close(input.value); } },
      h("h2", {}, title),
      h("p", { class: "desc" }, body),
      h("div", { class: "field" }, h("label", { for: "pw-input" }, "Your password (the one you use for sudo)"), input,
        h("p", { class: "help" }, "Checked by sudo on this machine and never stored. Asked once until you close the app.")),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: "btn danger" }, icon("lock-key-open"), confirm)));
  });
}

/* The password, unless you already gave it since the app opened: "" then, null if you cancel. */
async function askPassword(options) {
  if (await bridge.call("bypass_unlocked").catch(() => false)) return "";
  return (await passwordDialog(options)) || null;
}

const unrestrictedPill = () => h("span", { class: "pill unrestricted", title: "Opened with your password: the lead and its agents run without permission checks" }, "Unrestricted");

function noteDialog({ title, desc, label, confirm, required = false, danger = false, placeholder = "" }) {
  return openDialog((close) => {
    const note = h("textarea", { id: "note-body", class: "input", rows: 5, autofocus: true, placeholder });
    const error = h("p", { class: "error", hidden: true });
    return h("form", { onsubmit: (e) => {
        e.preventDefault();
        if (required && !note.value.trim()) { error.textContent = "This can't be empty."; error.hidden = false; note.focus(); return; }
        close(note.value.trim());
      } },
      h("h2", {}, title), desc ? h("p", { class: "desc" }, desc) : null,
      h("div", { class: "field" }, h("label", { for: "note-body" }, label), note, error),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: `btn ${danger ? "danger" : "primary"}` }, confirm)));
  }, { wide: true });
}

/* ================================================================ pipelines: shared */

const DECISIONS = {
  approve: { title: "Approve and Run?", desc: "The lead starts waking agents for the steps. You can still cancel it.", label: "Note for the lead (optional)", confirm: "Approve and Run" },
  changes: { title: "Request Changes", desc: "The lead revises the plan and brings it back for approval.", label: "What should change?", confirm: "Send", required: true },
  reject: { title: "Reject this plan?", desc: "The lead won't run it.", label: "Why (optional)", confirm: "Reject", danger: true },
};

async function decidePipeline(pipeline, decision, button) {
  const note = await noteDialog(DECISIONS[decision]);
  if (note === undefined) return;
  await runAction(button, () => bridge.call("decide_pipeline", { pipeline_id: pipeline.id, decision, note }));
  pollLive();
}

function pipelineActions(p, reload) {
  const waiting = p.status === "draft" || p.status === "changes_requested";
  const decide = (decision) => (e) => decidePipeline(p, decision, e.currentTarget).then(reload);
  return [
    p.status === "draft" ? h("button", { class: "btn primary", onclick: decide("approve") }, icon("check"), "Approve and Run") : null,
    waiting ? h("button", { class: "btn", onclick: decide("changes") }, icon("pencil-simple"), "Request Changes") : null,
    waiting ? h("button", { class: "btn ghost danger", onclick: decide("reject") }, icon("x"), "Reject") : null,
    p.status === "approved" || p.status === "running" ? h("button", { class: "btn ghost danger", onclick: async (e) => {
      const ok = await confirmDialog({ title: "Cancel this plan?", body: "Running steps are cancelled and their agents go back to sleep. Finished work stays.", confirm: "Cancel Plan", danger: true });
      if (ok) { await runAction(e.currentTarget, () => bridge.call("cancel_pipeline", { pipeline_id: p.id })); reload(); pollLive(); }
    } }, icon("stop-circle"), "Cancel Plan") : null,
  ];
}

/* A work session's documents (documents.py): revisions, what changed, and comments that go to its Conductor. */
async function documentsFold(slot, workspaceId, alive, open = false) {
  let docs;
  try { docs = await bridge.call("session_documents", { workspace_id: workspaceId }); } catch { return; }
  if (!alive() || !docs.length) { slot.replaceChildren(); return; }
  fill(slot, h("details", { class: "fold", style: "margin-bottom:16px", open: open || undefined },
    h("summary", {}, icon("files"), `Documents (${docs.length})`, h("span", { class: "faint" }, "plans and files this session shared, with every revision")),
    h("div", { class: "rows" }, docs.map((d) => h("div", { class: "row" },
      h("div", { class: "row-main" }, h("span", { class: "row-title" }, d.title),
        h("span", { class: "row-sub" }, `${d.revisions || 0} revision${d.revisions === 1 ? "" : "s"}${d.changed_at ? ` · changed ${ago(d.changed_at)}` : ""}${d.comments ? ` · ${d.comments} comment${d.comments === 1 ? "" : "s"}` : ""}`)),
      h("div", { class: "row-actions" }, h("button", { class: "btn small", onclick: () => documentDialog(d.id, null, () => documentsFold(slot, workspaceId, alive, true)) }, icon("eye"), "Read")))))));
}

/* What the agent's browser shows, live (browser_live.py): shown only while it has one open. */
function browserWatch(slot, agentId, alive) {
  let timer = null;
  const img = h("img", { class: "browser-shot", alt: "What the agent's browser shows" });
  const where = h("span", { class: "row-sub mono browser-url" });
  const card = h("section", { class: "browser-live" },
    h("div", { class: "browser-bar" }, icon("browser"), h("b", {}, "Its browser, live"), where), img);
  const tick = async () => {
    if (!alive()) return;
    let v = { live: false };
    try { v = await bridge.call("browser_view", { agent_id: agentId }); } catch { /* not allowed or gone */ }
    if (!alive()) return;
    if (v.live) {
      if (!card.isConnected) slot.replaceChildren(card);
      where.textContent = v.title ? `${v.title} · ${v.url}` : v.url || "blank page";
      if (v.image) img.src = v.image;
    } else if (card.isConnected) slot.replaceChildren();
    timer = setTimeout(tick, v.live && !document.hidden ? 1200 : 5000);
  };
  tick();
}

/* Standing tool permissions (toolrules.py): approve what an agent asks now, and from now on. */
function alwaysAllowButton(agentId, after) {
  return h("button", { class: "btn small ghost", title: "Approve, and don't ask again for calls like this one",
    onclick: async (e) => {
      const button = e.currentTarget;
      let sug;
      try { sug = await bridge.call("tool_rule_suggestion", { agent_id: agentId }); } catch (error) { toast(error.message, "error"); return; }
      if (!sug) { toast("Can't tell which tool it's asking about. Approve it this time.", "error"); return; }
      const rule = await toolRuleDialog({ ...sug, decision: "allow" }, { title: "Always allow this?", confirm: "Approve and Always Allow" });
      if (!rule) return;
      await runAction(button, () => bridge.call("allow_always", { agent_id: agentId, tool: rule.tool, pattern: rule.pattern, agent: rule.agent, project: rule.project }));
      after?.();
    } }, icon("check-square-offset"), "Always Allow…");
}

/* Edit one rule: which agent, tool and calls, and what happens. Resolves the rule, or null. */
function toolRuleDialog(rule, { title = "New rule", confirm = "Save Rule", projects = null } = {}) {
  const r = { agent: "*", project: "", tool: "", pattern: "*", decision: "allow", ...rule };
  return openDialog((close) => {
    const field = (label, input, hint) => h("label", { class: "field" }, h("span", {}, label), input, hint ? h("small", { class: "faint" }, hint) : null);
    const tool = h("input", { class: "input mono", value: r.tool, placeholder: "Bash, Edit, WebFetch, mcp__github__*" });
    const pattern = h("input", { class: "input mono", value: r.pattern, placeholder: "npm test*  (or * for anything)" });
    const agent = h("select", { class: "select" }, [...new Set(["*", r.agent])].map((v) => h("option", { value: v, selected: v === r.agent }, v === "*" ? "Every agent" : r.agent_name && v === r.agent ? `${r.agent_name} (${v})` : v)));
    const decision = h("select", { class: "select" }, [["allow", "Always allow"], ["ask", "Always ask me"], ["deny", "Never allow"]].map(([v, l]) => h("option", { value: v, selected: v === r.decision }, l)));
    const project = projects ? h("select", { class: "select" }, [["", "Any project"], ...Object.entries(projects)].map(([v, l]) => h("option", { value: v, selected: v === r.project }, l))) : null;
    return h("form", { onsubmit: (e) => { e.preventDefault(); close({ ...r, tool: tool.value.trim(), pattern: pattern.value.trim() || "*", agent: agent.value, decision: decision.value, project: project ? project.value : r.project }); } },
      h("h2", {}, title),
      r.subject ? h("p", { class: "faint" }, "It's asking to run: ", h("span", { class: "mono" }, r.subject.length > 200 ? `${r.subject.slice(0, 200)}…` : r.subject)) : null,
      field("Tool", tool),
      field("Calls it covers", pattern, "What the call is about: its command, file or web address. * matches anything."),
      field("Agent", agent, r.agent !== "*" && r.agent_name ? "For this agent, and every copy of it the swarm wakes." : null),
      project ? field("Project", project) : null,
      field("What happens", decision),
      h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close(null) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, confirm)));
  });
}

views.permissions = (root, alive) => {
  const body = h("div", {}, skeleton(3));
  root.append(pageHead("Permissions", "Secrets agents ask for, and standing answers to their permission questions, so you aren't asked the same thing twice."), body);
  const load = async () => {
    let data, asks;
    try { [data, asks] = await Promise.all([bridge.call("tool_rules"), bridge.call("secret_requests")]); } catch (error) { if (alive()) body.replaceChildren(errorBox(error, load)); return; }
    if (!alive()) return;
    const label = { allow: "Always allow", ask: "Always ask", deny: "Never" };
    const add = async (e) => {
      const rule = await toolRuleDialog({}, { projects: data.projects });
      if (rule) runAction(e?.currentTarget, () => bridge.call("add_tool_rule", { tool: rule.tool, decision: rule.decision, agent: rule.agent, project: rule.project, pattern: rule.pattern })).then(load);
    };
    const giveDialog = (req) => openDialog((close) => {
      const value = h("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: "Paste the value", autofocus: true });
      const all = h("input", { type: "checkbox" });
      return h("form", { onsubmit: (e) => { e.preventDefault(); if (value.value) close({ value: value.value, for_all: all.checked }); } },
        h("h2", {}, `Give ${req.env}`),
        h("p", { class: "desc" }, `${req.agent_name} needs it: `, h("q", {}, req.reason)),
        h("label", { class: "field" }, h("span", {}, "Value"), value,
          h("small", { class: "faint" }, "It goes to this computer's keyring, never into the agent's conversation, and is hidden wherever it would show on screen.")),
        req.project ? h("label", { class: "q-choice" }, all, ` For every project, not only ${data.projects[req.project] || req.project}`) : null,
        h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close(null) }, "Cancel"),
          h("button", { type: "submit", class: "btn primary" }, icon("key"), "Give It")));
    });
    body.replaceChildren(
      asks.pending.length ? h("section", { class: "section" },
        h("div", { class: "section-head" }, h("h2", {}, `Agents asking for a secret (${asks.pending.length})`)),
        h("div", { class: "rows" }, asks.pending.map((r) => h("div", { class: "row" },
          h("div", { class: "row-main" }, h("span", { class: "row-title" }, h("span", { class: "mono" }, r.env), ` for ${r.agent_name}`),
            h("span", { class: "row-sub" }, `${r.reason} · ${ago(r.created_at)}${r.project ? ` · ${data.projects[r.project] || r.project}` : ""}`)),
          h("div", { class: "row-actions" },
            h("button", { class: "btn small primary", onclick: async (e) => {
              const button = e.currentTarget;
              const answer = await giveDialog(r);
              if (answer) runAction(button, () => bridge.call("give_secret", { request_id: r.id, ...answer })).then(load);
            } }, icon("key"), "Give…"),
            h("button", { class: "btn small ghost", onclick: (e) => runAction(e.currentTarget, () => bridge.call("decline_secret", { request_id: r.id })).then(load) }, "Decline")))))) : null,
      h("div", { class: "section-head" }, h("h2", {}, "Standing rules")),
      h("div", { class: "controls-line", style: "margin-bottom:14px" }, h("button", { class: "btn primary", onclick: add }, icon("plus"), "New Rule")),
      data.rules.length ? h("div", { class: "rows" }, data.rules.map((r) => h("div", { class: "row" },
        h("div", { class: "row-main" },
          h("span", { class: "row-title" }, h("span", { class: `pill ${r.decision === "allow" ? "p-done" : r.decision === "deny" ? "p-failed" : "p-draft"}` }, label[r.decision]), " ",
            h("span", { class: "mono" }, r.tool), r.pattern && r.pattern !== "*" ? h("span", { class: "mono faint" }, ` ${r.pattern}`) : null),
          h("span", { class: "row-sub" }, `${r.agent === "*" ? "Every agent" : r.agent}${r.project ? ` · ${data.projects[r.project] || r.project}` : ""} · used ${r.hits} time${r.hits === 1 ? "" : "s"}${r.last_hit ? `, last ${ago(r.last_hit)}` : ""}`)),
        h("div", { class: "row-actions" },
          h("button", { class: "btn small ghost", onclick: async (e) => {
            const rule = await toolRuleDialog(r, { title: "Change rule", projects: data.projects });
            if (!rule) return;
            await runAction(e.currentTarget, async () => { if (rule.agent !== r.agent || rule.project !== r.project || rule.tool !== r.tool || rule.pattern !== r.pattern) await bridge.call("remove_tool_rule", { rule_id: r.id }); return bridge.call("add_tool_rule", { tool: rule.tool, decision: rule.decision, agent: rule.agent, project: rule.project, pattern: rule.pattern }); });
            load();
          } }, "Change"),
          h("button", { class: "btn small ghost danger", onclick: (e) => runAction(e.currentTarget, () => bridge.call("remove_tool_rule", { rule_id: r.id })).then(load) }, "Remove")))))
        : h("div", { class: "empty" }, h("p", {}, "No rules yet. When an agent asks permission, press Always Allow… to make one, or add one here.")));
  };
  load();
};

/* Connectors (connectors.py): hosted MCP servers agents can use, and Lago Agents as an MCP
   server for Claude Code, Codex and other assistants. */
views.connectors = (root, alive, tab = "browse") => {
  const body = h("div", {}, skeleton(4));
  root.append(pageHead("Connectors", "Apps your agents can use through MCP, and Lago Agents as a connector for your other assistants."),
    h("nav", { class: "segmented page-tabs", "aria-label": "Connectors" },
      [["browse", "Browse", "storefront"], ["review", "Review", "shield-check"]]
        .map(([t, label, ic]) => h("a", { href: `#connectors/${t}`, role: "tab", "aria-selected": String(t === tab), class: t === tab ? "on" : "" }, icon(ic), label))),
    body);
  if (tab === "review") return connectorReview(body, alive);
  let filter = "all", query = "";
  const list = h("div");
  const load = async () => {
    let data;
    try { data = await bridge.call("connectors"); } catch (error) { if (alive()) body.replaceChildren(errorBox(error, load)); return; }
    if (!alive()) return;
    const render = () => {
      const q = query.trim().toLowerCase();
      const hit = (c) => !q || `${c.name} ${c.description || ""}`.toLowerCase().includes(q);
      const mine = data.installed.filter(hit);
      const rest = filter === "installed" ? [] : data.catalog.filter((c) => !c.installed && hit(c));
      fill(list, mine.length || rest.length
        ? h("div", { class: "connector-list" }, mine.map((c) => installedCard(c)), rest.map((c) => catalogCard(c)))
        : h("div", { class: "empty" }, h("p", {}, filter === "installed" && !q ? "No connectors yet. Pick one under All." : "Nothing matches.")));
    };
    const tile = (c) => c.icon
      ? h("span", { class: `connector-tile logo${c.icon.endsWith(".png") ? " full" : ""}`, "aria-hidden": "true" }, h("img", { src: c.icon, alt: "" }))
      : h("span", { class: "connector-tile", "aria-hidden": "true" }, (c.name || "?").slice(0, 1).toUpperCase());
    const authLabel = { oauth: "Sign in with your account", token: "Uses a token", none: "No sign-in", client: "Needs your own OAuth client" };
    const catalogCard = (c) => h("div", { class: "connector-card" },
      h("div", { class: "connector-head" }, tile(c),
        h("div", { class: "row-main" }, h("span", { class: "row-title" }, c.name), h("span", { class: "row-sub" }, c.description, c.client ? " Needs your own OAuth client." : "")),
        h("button", { class: "btn", onclick: (e) => connect(e.currentTarget, c) }, "Connect")));
    const connect = async (button, c) => {
      let id = c.id;
      await runAction(button, async () => { const res = await bridge.call("add_connector", { catalog_id: c.id }); id = res.id; return { ok: true, message: `Added ${c.name}.` }; });
      await load();
      // The row just added, from a fresh listing: this render's `data` is from before it existed.
      const added = (await bridge.call("connectors")).installed.find((x) => x.id === id);
      if (c.auth !== "none" && added) signIn(null, added);
    };
    // A provider that doesn't let apps register themselves (Google, Slack...): you create an
    // OAuth client once in its console, and give its ID and secret here.
    const askClient = (c) => openDialog((close) => {
      const idInput = h("input", { class: "input mono", autocomplete: "off", placeholder: "Client ID", autofocus: true });
      const secretInput = h("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: "Client secret" });
      return h("form", { onsubmit: (e) => { e.preventDefault(); if (idInput.value.trim() && secretInput.value.trim()) close({ client_id: idInput.value.trim(), client_secret: secretInput.value.trim() }); } },
        h("h2", {}, `Connect ${c.name}`),
        h("p", { class: "desc" }, c.client.help),
        h("p", { class: "desc" }, h("button", { type: "button", class: "linklike", onclick: () => openUrl(c.client.console) }, "Open the developer console"), "."),
        h("label", { class: "field" }, h("span", {}, "Redirect URI to add there"),
          h("input", { class: "input mono", readonly: true, value: c.client.redirect, onclick: (e) => e.currentTarget.select() })),
        h("label", { class: "field" }, h("span", {}, "Client ID"), idInput),
        h("label", { class: "field" }, h("span", {}, "Client secret"), secretInput,
          h("small", { class: "faint" }, "Both go to this computer's keyring." + (c.client.group === "google" ? " Your other Google connectors reuse them." : ""))),
        h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close(null) }, "Cancel"),
          h("button", { type: "submit", class: "btn primary" }, icon("sign-in"), "Sign In")));
    }, { wide: true });
    const signIn = async (button, c) => {
      if (c.client && !c.client.known) {
        const client = await askClient(c);
        if (!client) return;
        toast(`Sign in to ${c.name} in your browser. Lago waits up to 5 minutes.`);
        await runAction(button, () => bridge.call("connector_sign_in", { connector_id: c.id, ...client }));
      } else if (c.auth === "token") {
        const token = await openDialog((close) => {
          const input = h("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: "Paste the token", autofocus: true });
          const entry = data.catalog.find((x) => x.id === (c.catalog || c.id));
          return h("form", { onsubmit: (e) => { e.preventDefault(); if (input.value.trim()) close(input.value.trim()); } },
            h("h2", {}, `Connect ${c.name}`),
            entry?.token_help ? h("p", { class: "desc" }, entry.token_help) : null,
            h("label", { class: "field" }, h("span", {}, "Token"), input,
              h("small", { class: "faint" }, "It goes to this computer's keyring. Agents never see it: Lago passes their requests on.")),
            h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close(null) }, "Cancel"),
              h("button", { type: "submit", class: "btn primary" }, icon("key"), "Connect")));
        });
        if (token) await runAction(button, () => bridge.call("connector_sign_in", { connector_id: c.id, token }));
      } else {
        toast(`Sign in to ${c.name} in your browser. Lago waits up to 5 minutes.`);
        await runAction(button, () => bridge.call("connector_sign_in", { connector_id: c.id }));
      }
      load();
    };
    const scope = (c) => c.projects?.length ? c.projects.map((p) => data.projects[p] || p).join(", ") : "Every project";
    const installedCard = (c) => h("div", { class: "connector-card" },
      h("div", { class: "connector-head" }, tile(c),
        h("div", { class: "row-main" },
          h("span", { class: "row-title" }, c.name, c.custom ? h("span", { class: "badge", style: "margin-left:8px" }, "Custom") : null),
          h("span", { class: "row-sub" }, c.description)),
        c.signed_in ? h("span", { class: "status on" }, "Connected")
          : h("button", { class: "btn primary", onclick: (e) => signIn(e.currentTarget, c) }, "Sign In"),
        h("button", { class: "btn icon ghost", "aria-label": `${c.name} options`, title: "Options", onclick: (e) => {
          const r = e.currentTarget.getBoundingClientRect();
          showMenu(r.left, r.bottom + 4, [
            { icon: "wrench", label: "Tools…", disabled: !c.signed_in, action: () => toolsDialog(c).then(load) },
            { icon: "sliders-horizontal", label: "Who Uses It…", action: () => accessDialog(c).then(load) },
            c.auth !== "none" ? { icon: "arrow-clockwise", label: c.signed_in ? "Sign In Again" : "Sign In", action: () => signIn(null, c) } : null,
            c.auth !== "none" && c.signed_in ? { icon: "sign-out", label: "Sign Out", action: () => runAction(null, () => bridge.call("connector_sign_out", { connector_id: c.id })).then(load) } : null,
            { icon: "trash", label: "Remove", danger: true, action: async () => {
              if (await confirmDialog({ title: `Remove ${c.name}?`, body: "Agents stop getting it when their sessions next start, and its sign-in is deleted.", confirm: "Remove", danger: true }))
                runAction(null, () => bridge.call("remove_connector", { connector_id: c.id })).then(load);
            } },
          ].filter(Boolean));
        } }, icon("dots-three"))),
      h("div", { class: "connector-meta row-sub" },
        `${authLabel[c.client ? "client" : c.auth] || ""} · ${scope(c)}${c.lago ? " · Lago too" : ""}${c.ask ? " · asks before each use" : ""}${c.blocked_tools?.length ? ` · ${c.blocked_tools.length} tool${c.blocked_tools.length === 1 ? "" : "s"} off` : ""}`));
    const accessDialog = (c) => openDialog((close) => {
      const boxes = Object.entries(data.projects).map(([pid, name]) => [pid, h("input", { type: "checkbox", checked: c.projects?.includes(pid) || undefined })]);
      const lago = h("input", { type: "checkbox", checked: c.lago || undefined });
      const ask = h("input", { type: "checkbox", checked: c.ask || undefined });
      return h("form", { onsubmit: async (e) => {
          e.preventDefault();
          const ok = await runAction(e.submitter, () => bridge.call("update_connector", { connector_id: c.id,
            projects: boxes.filter(([, b]) => b.checked).map(([pid]) => pid), lago: lago.checked, ask: ask.checked }), "Saved. Agents get it when their sessions next start; Lago restarts for it once he's idle.");
          if (ok) close(true);
        } },
        h("h2", {}, `Who uses ${c.name}`),
        h("p", { class: "desc" }, "Agents working on these projects get it. Pick none for every project."),
        h("div", { class: "connector-checks" }, boxes.length ? boxes.map(([pid, b]) => h("label", { class: "q-choice" }, b, ` ${data.projects[pid]}`)) : h("span", { class: "faint" }, "No projects yet: every agent gets it.")),
        h("label", { class: "q-choice", style: "margin-top:14px" }, lago, " Lago uses it too (its tools cost him a few tokens on every turn)"),
        h("label", { class: "q-choice", style: "margin-top:8px" }, ask, " Agents ask before each use"),
        h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Cancel"),
          h("button", { type: "submit", class: "btn primary" }, "Save")));
    });
    const toolsDialog = async (c) => {
      let found;
      try { found = await bridge.call("connector_tools", { connector_id: c.id }); } catch (error) { toast(error.message, "error"); return; }
      return openDialog((close) => {
        const boxes = found.tools.map((t) => [t.name, h("input", { type: "checkbox", checked: t.on || undefined })]);
        return h("form", { onsubmit: async (e) => {
            e.preventDefault();
            const ok = await runAction(e.submitter, () => bridge.call("update_connector", { connector_id: c.id,
              blocked_tools: boxes.filter(([, b]) => !b.checked).map(([name]) => name) }), "Saved. Sessions see the change when they next start.");
            if (ok) close(true);
          } },
          h("h2", {}, `${c.name} tools (${found.tools.length})`),
          h("p", { class: "desc" }, "Switch off what agents shouldn't do. Fewer tools also cost fewer tokens."),
          h("div", { class: "rows connector-tools" }, found.tools.map((t, i) => h("label", { class: "row" },
            h("div", { class: "row-main" }, h("span", { class: "row-title mono" }, t.name, t.read_only ? h("span", { class: "badge", style: "margin-left:8px" }, "Read-only") : null),
              t.description ? h("span", { class: "row-sub" }, t.description) : null),
            boxes[i][1]))),
          h("div", { class: "dialog-actions" },
            h("button", { type: "button", class: "btn ghost", style: "margin-right:auto", onclick: () => boxes.forEach(([n, b], i) => { b.checked = found.tools[i].read_only; }) }, "Only Read-Only"),
            h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Cancel"),
            h("button", { type: "submit", class: "btn primary" }, "Save")));
      }, { wide: true });
    };
    const customDialog = () => openDialog((close) => {
      const name = h("input", { class: "input", placeholder: "Airtable", autofocus: true });
      const url = h("input", { class: "input mono", placeholder: "https://mcp.example.com/mcp" });
      const command = h("input", { class: "input mono", placeholder: "npx -y @example/mcp-server" });
      const auth = h("select", { class: "input" }, [["oauth", "Sign in through the browser"], ["token", "A token I paste"], ["none", "No sign-in"]].map(([v, l]) => h("option", { value: v }, l)));
      const error = h("p", { class: "error", hidden: true });
      return h("form", { onsubmit: async (e) => {
          e.preventDefault();
          try {
            const res = await bridge.call("add_connector", { name: name.value, url: url.value, command: command.value, auth: command.value.trim() ? "none" : auth.value });
            close(res);
          } catch (err) { error.textContent = err.message; error.hidden = false; }
        } },
        h("h2", {}, "Custom connector"),
        h("p", { class: "desc" }, "Any MCP server: a hosted one by its URL (Streamable HTTP), or a program on this computer."),
        h("label", { class: "field" }, h("span", {}, "Name"), name),
        h("label", { class: "field" }, h("span", {}, "Server URL"), url),
        h("label", { class: "field" }, h("span", {}, "Sign-in"), auth),
        h("label", { class: "field" }, h("span", {}, "Or a command that starts it"), command),
        error,
        h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close(null) }, "Cancel"),
          h("button", { type: "submit", class: "btn primary" }, "Add")));
    });
    const a = data.assistants;
    const assistantRow = (which, label) => h("div", { class: "row" },
      h("div", { class: "row-main" }, h("span", { class: "row-title" }, label),
        h("span", { class: "row-sub" }, !a[which].available ? "Not installed on this computer" : a[which].connected ? `Has Lago Agents as “${a.name}”` : "Not connected")),
      h("div", { class: "row-actions" }, !a[which].available ? null : a[which].connected
        ? h("button", { class: "btn small ghost", onclick: (e) => runAction(e.currentTarget, () => bridge.call("connect_assistant", { which, on: false })).then(load) }, "Remove")
        : h("button", { class: "btn small primary", onclick: (e) => runAction(e.currentTarget, () => bridge.call("connect_assistant", { which, on: true })).then(load) }, "Connect")));
    const setupDialog = () => openDialog((close) => h("form", { onsubmit: (e) => { e.preventDefault(); close(); } },
      h("h2", {}, "Use Lago Agents from another assistant"),
      h("p", { class: "desc" }, "It runs on this computer over stdio. Assistants get a small tool set: list projects and work sessions, open one, message it, read reports, search, and tell Lago something. It never opens sessions without permission checks."),
      Object.entries(a.snippets).map(([label, text]) => h("div", { class: "field" }, h("label", {}, label), h("pre", { class: "connector-snippet mono" }, text))),
      h("div", { class: "dialog-actions" }, h("button", { type: "submit", class: "btn primary" }, "Done"))), { wide: true });
    body.replaceChildren(
      h("div", { class: "connector-card connector-self" },
        h("div", { class: "connector-head" }, h("span", { class: "connector-tile accent", "aria-hidden": "true" }, icon("plugs-connected")),
          h("div", { class: "row-main" }, h("span", { class: "row-title" }, "Assistant Connection (MCP)"),
            h("span", { class: "row-sub" }, "Use Lago Agents from Claude Code, Codex, OpenCode or another assistant.")),
          h("button", { class: "btn", onclick: setupDialog }, "Setup")),
        h("div", { class: "rows" }, assistantRow("claude", "Claude Code"), assistantRow("codex", "Codex"))),
      h("div", { class: "controls-line", style: "margin:20px 0 14px" },
        h("label", { class: "search" }, icon("magnifying-glass"), h("input", { class: "input", type: "search", placeholder: "Search connectors…", value: query,
          oninput: (e) => { query = e.target.value; render(); } })),
        h("div", { class: "segmented", role: "radiogroup", "aria-label": "Show" },
          [["all", "All"], ["installed", `Installed (${data.installed.length})`]].map(([v, l]) => h("button", { type: "button", role: "radio", "aria-checked": String(filter === v),
            onclick: (e) => { filter = v; e.currentTarget.parentElement.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b === e.currentTarget))); render(); } }, l))),
        h("button", { class: "btn ghost", style: "margin-left:auto", onclick: async () => { const res = await customDialog(); if (res) { await load(); const c = (await bridge.call("connectors")).installed.find((x) => x.id === res.id); if (c && c.auth !== "none") signIn(null, c); } } }, icon("plus"), "Custom Connector")),
      list);
    render();
  };
  load();
};

async function connectorReview(body, alive) {
  let rows;
  try { rows = await bridge.call("connector_calls"); } catch (error) { if (alive()) body.replaceChildren(errorBox(error, () => connectorReview(body, alive))); return; }
  if (!alive()) return;
  const counts = {};
  rows.forEach((r) => { const name = r.text.split(":")[0]; counts[name] = (counts[name] || 0) + 1; });
  body.replaceChildren(
    h("p", { class: "desc", style: "margin-bottom:14px" }, "Every tool call agents made through a connector, newest first. Switch tools off under Browse → a connector's Tools…"),
    Object.keys(counts).length ? h("div", { class: "controls-line", style: "margin-bottom:14px;flex-wrap:wrap" },
      Object.entries(counts).map(([name, n]) => h("span", { class: "badge" }, `${name}: ${n}`))) : "",
    rows.length ? h("div", { class: "rows" }, rows.map((r) => h("div", { class: "row" },
      h("div", { class: "row-main" }, h("span", { class: "row-title mono" }, r.text), h("span", { class: "row-sub" }, `${r.actor} · ${ago(r.created_at)}`)),
      r.actor && /^w\d+/.test(r.actor) ? h("a", { class: "btn small ghost", href: `#agent/${r.actor}` }, "Agent") : null)))
      : h("div", { class: "empty" }, h("p", {}, "No connector calls yet.")));
}

/* What a work session changed (changes.py): its files with lines added and removed, each file's diff. */
async function changesFold(slot, workspaceId, alive, open = false) {
  let c;
  try { c = await bridge.call("session_changes", { workspace_id: workspaceId }); } catch { return; }
  if (!alive()) return;
  const branchFiles = c.branches.reduce((n, b) => n + b.files.length, 0);
  if (!c.files.length && !branchFiles) { slot.replaceChildren(); return; }
  const total = (files) => files.reduce((a, f) => [a[0] + f.added, a[1] + f.removed], [0, 0]);
  const [added, removed] = total(c.files);
  const fileRow = (f, branch) => h("div", { class: "row change-row" },
    h("div", { class: "row-main" }, h("span", { class: "row-title mono" }, f.path),
      h("span", { class: "row-sub" }, h("span", { class: `change-status s-${f.status || "modified"}` }, f.status || "modified"),
        f.binary ? " · binary file" : h("span", {}, " · ", h("span", { class: "add" }, `+${f.added}`), " ", h("span", { class: "del" }, `−${f.removed}`)))),
    f.binary ? null : h("div", { class: "row-actions" }, h("button", { class: "btn small", onclick: () => diffDialog(workspaceId, f.path, branch) }, icon("eye"), "See Changes")));
  fill(slot, h("details", { class: "fold", style: "margin-bottom:16px", open: open || undefined },
    h("summary", {}, icon("git-diff"), `Changes (${c.files.length + branchFiles} file${c.files.length + branchFiles === 1 ? "" : "s"})`,
      h("span", { class: "faint" }, c.files.length ? `+${added} −${removed} in the folder${c.full ? " since this session started" : ""}` : "on the steps' own branches")),
    c.note ? h("p", { class: "faint", style: "font-size:12.5px" }, c.note) : null,
    h("div", { class: "rows" }, c.files.map((f) => fileRow(f))),
    c.more ? h("p", { class: "faint" }, `and ${c.more} more files`) : null,
    c.branches.filter((b) => b.files.length).map((b) => h("div", { style: "margin-top:12px" },
      h("p", { class: "row-sub" }, icon("git-branch"), " Branch ", h("span", { class: "mono" }, b.branch), b.merged ? " · merged" : " · not merged yet"),
      h("div", { class: "rows" }, b.files.map((f) => fileRow(f, b.branch))))),
    h("div", { class: "controls-line", style: "margin-top:10px" },
      h("button", { class: "btn small ghost", onclick: () => changesFold(slot, workspaceId, alive, true) }, icon("arrow-clockwise"), "Refresh"))));
}

async function diffDialog(workspaceId, path, branch) {
  let d;
  try { d = await bridge.call("session_change_diff", { workspace_id: workspaceId, path, branch: branch || null }); } catch (error) { toast(error.message, "error"); return; }
  return openDialog((close) => h("form", { onsubmit: (e) => { e.preventDefault(); close(); } },
    h("h2", { class: "mono", style: "font-size:15px;word-break:break-all" }, path),
    branch ? h("p", { class: "faint" }, `On the branch ${branch}`) : null,
    d.diff ? h("pre", { class: "doc-diff" }, d.diff.split("\n").map((l) => h("span", { class: l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "" }, l + "\n")))
      : h("p", { class: "faint" }, "No changes in this file any more."),
    h("div", { class: "dialog-actions" }, h("button", { type: "submit", class: "btn primary" }, "Close"))), { wide: true });
}

async function documentDialog(docId, number, after) {
  let doc;
  try { doc = await bridge.call("document", { doc_id: docId, number }); } catch (error) { toast(error.message, "error"); return; }
  let mode = "text";
  const view = h("div");
  const render = () => fill(view, mode === "text"
    ? h("div", { class: "doc-text" }, markdown(doc.revision.text))
    : h("pre", { class: "doc-diff" }, doc.diff.split("\n").map((l) => h("span", { class: l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : "" }, l + "\n"))));
  render();
  const comment = h("textarea", { class: "input", rows: 2, placeholder: "Comment for the lead. Select text above first to quote it." });
  let quote = "";
  const quoting = h("p", { class: "faint", style: "font-size:12.5px;margin:8px 0 0", hidden: true });
  view.addEventListener("mouseup", () => {
    const picked = String(window.getSelection() || "").trim();
    if (!picked) return;
    quote = picked.slice(0, 1000);
    quoting.hidden = false;
    fill(quoting, "Quoting: ", h("q", {}, quote.length > 140 ? `${quote.slice(0, 140)}…` : quote), " ",
      h("button", { type: "button", class: "linklike", onclick: () => { quote = ""; quoting.hidden = true; } }, "Clear"));
  });
  const latest = doc.revision.number;
  return openDialog((close) => h("div", {},
    h("h2", {}, doc.title),
    h("p", { class: "faint mono", style: "font-size:12px" }, doc.path),
    h("div", { class: "controls-line" },
      h("select", { class: "select", "aria-label": "Revision", onchange: (e) => { close(); documentDialog(docId, Number(e.target.value), after); } },
        Array.from({ length: latest }, (_, i) => latest - i).map((n) => h("option", { value: n, selected: n === doc.revision.number }, `Revision ${n}${n === latest && number === null ? " (latest)" : ""}`))),
      h("div", { class: "segmented", role: "radiogroup" }, [["text", "Text"], ["diff", "What Changed"]].map(([k, l]) => h("button", { type: "button", role: "radio", "aria-checked": String(k === mode),
        onclick: (e) => { mode = k; e.currentTarget.parentElement.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b === e.currentTarget))); render(); } }, l)))),
    view,
    doc.comments.length ? h("details", {}, h("summary", {}, `Comments (${doc.comments.length})`), doc.comments.map((c) => h("div", { class: "review-note" },
      h("strong", {}, `Revision ${c.revision} · ${ago(c.created_at)}: `), c.quote ? h("q", { class: "faint" }, c.quote) : null, h("p", { class: "prewrap", style: "margin:2px 0 0" }, c.text)))) : null,
    quoting,
    h("div", { class: "field", style: "margin-top:12px" }, comment),
    h("div", { class: "dialog-actions" }, h("button", { class: "btn ghost", onclick: () => close() }, "Close"),
      h("button", { class: "btn primary", onclick: async (e) => {
        const ok = await runAction(e.currentTarget, () => bridge.call("comment_document", { doc_id: docId, text: comment.value, quote }));
        if (ok) { close(); after?.(); }
      } }, icon("chat-text"), "Send Comment"))), { wide: true });
}

/* A Conductor's question with choices (questions.py): answered with a click, sent back as a message. */
function questionForm(event, form, after) {
  const picks = {};
  const notes = h("input", { class: "input", placeholder: "Anything to add (optional)" });
  const fields = form.questions.map((q) => {
    if (!q.choices.length) {
      const input = h("textarea", { class: "input", rows: 2, oninput: (e) => { picks[q.id] = e.target.value; } });
      return h("fieldset", { class: "q-field" }, h("legend", {}, q.prompt), input);
    }
    picks[q.id] = q.multi ? [] : q.recommended || "";
    return h("fieldset", { class: "q-field" }, h("legend", {}, q.prompt, q.multi ? h("span", { class: "faint" }, " (pick any)") : null),
      h("div", { class: "q-choices" }, q.choices.map((c) => h("label", { class: "q-choice" },
        h("input", { type: q.multi ? "checkbox" : "radio", name: `q-${event.id}-${q.id}`, value: c, checked: c === q.recommended || undefined,
          onchange: (e) => {
            if (q.multi) picks[q.id] = e.target.checked ? [...picks[q.id], c] : picks[q.id].filter((x) => x !== c);
            else picks[q.id] = c;
          } }),
        h("span", {}, c), c === q.recommended ? h("span", { class: "badge" }, "Recommended") : null))));
  });
  return h("form", { class: "q-form", onsubmit: async (e) => {
      e.preventDefault();
      const ok = await runAction(e.submitter, () => bridge.call("answer_question", { event_id: event.id, answers: picks, note: notes.value }), "Sent to the lead.");
      if (ok) after();
    } }, fields, notes,
    h("div", { class: "controls-line", style: "margin:8px 0 0" }, h("button", { class: "btn small primary", type: "submit" }, icon("paper-plane-tilt"), "Send Answer")));
}

function stepItem(s, i) {
  const entry = catalogEntry(s.agent);
  const mark = { done: icon("check"), failed: icon("x"), skipped: icon("minus"), running: icon("circle-notch"), reviewing: icon("eye") }[s.status] || String(i + 1);
  const inputs = Object.entries(s.inputs);
  return h("li", { class: `step s-${s.status}` },
    h("span", { class: "step-num", "aria-hidden": "true" }, mark),
    h("div", { class: "step-body" },
      h("div", { class: "task-top" },
        h("span", { class: "task-title" }, s.title),
        s.gate ? h("span", { class: "badge warn", title: "The lead stops here and shows you the result before continuing" }, "Check-in") : null,
        s.attempts > 1 ? h("span", { class: "faint", style: "font-size:12px" }, `attempt ${s.attempts}`) : null,
        h("span", { class: `task-status ${s.status === "pending" || s.status === "skipped" ? "" : ["running", "reviewing"].includes(s.status) ? "working" : s.status}` },
          s.status === "reviewing" && s.review ? `In review by ${catalogEntry(s.reviewers[s.review.index])?.name || s.reviewers[s.review.index]}` : STEP_LABELS[s.status] || s.status)),
      h("div", { class: "task-meta" },
        h("button", { type: "button", class: "linklike", onclick: () => manifestDialog(s.agent) }, entry?.name || s.agent),
        s.task_id && state.config.agents[s.agent_id] ? [" · ", h("a", { href: `#agent/${s.agent_id}` }, "open its terminal")] : null,
        s.after.length ? ` · after ${s.after.join(", ")}` : "",
        s.task_id && (s.status === "done" || s.status === "failed") ? thumbs(s.task_id, entry?.name || s.agent) : null),
      s.skills?.length ? h("div", { class: "chips" }, s.skills.map((k) => h("span", { class: "chip skill", title: "Skill" }, icon("lightning"), k))) : null,
      s.reviewers?.length ? h("div", { class: "chips" }, s.reviewers.map((r) => h("span", { class: "chip", title: "Reviews this step before it counts as done" }, icon("eye"), catalogEntry(r)?.name || r))) : null,
      s.criteria?.length ? h("details", {}, h("summary", {}, `Acceptance criteria (${s.criteria.length})`), h("ol", { class: "criteria" }, s.criteria.map((c) => h("li", {}, c)))) : null,
      s.reviews?.length ? h("details", {}, h("summary", {}, `Reviews (${s.reviews.length})`), s.reviews.map((r) => h("div", { class: "review-note" },
        h("strong", {}, `${catalogEntry(r.reviewer)?.name || r.reviewer}, round ${r.round}: `, r.verdict === "approved" ? "approved" : "asked for changes"), h("pre", {}, r.text)))) : null,
      h("details", {}, h("summary", {}, "Details"),
        inputs.length ? h("dl", { class: "inputs" }, inputs.map(([k, v]) => [h("dt", { class: "mono" }, k), h("dd", {}, v)])) : null,
        h("pre", {}, s.task)),
      s.result ? h("details", { open: s.status === "failed" ? true : undefined }, h("summary", {}, "Result"), h("pre", {}, s.result)) : null));
}

const stepDots = (p) => h("span", { class: "step-dots", "aria-hidden": "true" }, p.steps.map((s) => h("span", { class: `dot s-${s.status}` })));

/* ================================================================ swarm catalog */

/* "What does this do?" (ghcatalog.explain): Lago reads the repository and says what it does,
   its reward and risks, whether you should use it, and where it fits your projects. */
async function explainDialog(c, onImport) {
  const body = h("div", {}, h("p", { class: "muted" }, "Reading the repository and your projects…"), skeleton(5));
  const verdicts = { use: ["Worth using", "solved"], try: ["Worth a careful try", "waiting_on_team"], skip: ["Skip it for now", "over_budget"] };
  const fill = (d) => {
    const [label, tone] = verdicts[d.verdict] || verdicts.try;
    const section = (title, ...content) => h("section", { style: "margin-top:14px" }, h("h3", { class: "label" }, title), ...content);
    body.replaceChildren(...[
      h("div", { class: "controls-line" }, h("span", { class: `pill ${tone}` }, label), h("span", { class: "faint", style: "font-size:12px" }, `Risk: ${d.risk} · read at commit ${d.sha.slice(0, 7)}`)),
      h("p", { style: "margin-top:8px" }, d.why),
      section("What it does", h("p", {}, d.what)),
      section("What you'd gain", h("p", {}, d.reward)),
      section("Risks", h("ul", {}, d.risks.map((r) => h("li", {}, r)))),
      section("Where it fits your projects", d.fits.length
        ? h("ul", {}, d.fits.map((f) => h("li", {}, h("a", { href: `#project/${f.project}` }, f.name), `: ${f.how}`)))
        : h("p", { class: "muted" }, "None of your projects needs it right now.")),
      d.similar.length ? section("You already have", h("p", {}, d.similar.map((id) => h("a", { href: `#swarm/${id}` }, id)).flatMap((el, i) => (i ? [", ", el] : [el])),
        " does much of this.")) : null].filter(Boolean));
  };
  const load = (refresh) => bridge.call("explain_github_agent", { repo: c.repo, refresh }).then(fill)
    .catch((error) => body.replaceChildren(errorBox(error, () => load(refresh))));
  load(false);
  const go = await openDialog((close) => h("form", { method: "dialog", onsubmit: (e) => { e.preventDefault(); close(true); } },
    h("h2", {}, c.repo),
    body,
    h("div", { class: "dialog-actions" },
      h("button", { type: "button", class: "btn ghost", title: "Ask again, e.g. after your projects changed", onclick: () => { body.replaceChildren(skeleton(5)); load(true); } }, icon("arrow-clockwise"), "Ask Again"),
      h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Close"),
      c.have ? null : h("button", { type: "button", class: "btn primary", onclick: () => close(true) }, icon("download-simple"), "Import"))), { wide: true });
  if (go) onImport();
}

/* Lago's own list of agents on GitHub (ghcatalog.py): open-source licensed, gaining stars
   lately, by topic, in plain words. Importing goes through the GitHub Agent Builder's checks
   and a draft you approve on the Agents tab. */
/* Bringing a setup over from Paperclip (paperclip_import.py): choose the company package it
   exported, see what's in it and what can't come over, tick what to import. */
views.import = (root, alive) => {
  const body = h("div", { class: "import-flow" });
  root.append(pageHead("Agents", "Bring the agents, skills, routines, projects and open tasks you built in Paperclip."), agentsTabs("import"), body);
  const zipInput = h("input", { type: "file", accept: ".zip,application/zip", hidden: true, onchange: () => { const file = zipInput.files[0]; zipInput.value = ""; if (file) readZip(file); } });
  const folder = h("input", { class: "input mono", placeholder: "~/acme-export", spellcheck: "false", "aria-label": "Exported folder" });
  const repo = h("input", { class: "input mono", placeholder: "owner/repo", spellcheck: "false", "aria-label": "GitHub repository" });
  const status = h("p", { class: "faint", "aria-live": "polite" });

  const stage = async (button, params) => {
    let token = null;
    const ok = await runAction(button, async () => { token = (await bridge.call("paperclip_stage", params)).token; return { ok: true, message: "Package read: tick what to import." }; });
    if (ok && token && alive()) showPreview(token);
  };
  const readZip = (file) => {
    const reader = new FileReader();
    status.textContent = `Reading ${file.name}…`;
    reader.onload = () => { status.textContent = ""; stage(null, { zip: reader.result }); };
    reader.onerror = () => { status.textContent = "That file couldn't be read."; };
    reader.readAsDataURL(file);
  };

  const choose = () => fill(body,
    h("section", { class: "import-card" },
      h("h2", {}, icon("export"), "1. Export your company from Paperclip"),
      h("p", {}, "In Paperclip, open your company's settings, then ", h("strong", {}, "Export"), ". Include agents, skills, projects and tasks, and download the zip."),
      h("p", { class: "faint" }, "From a terminal instead: ", h("code", {}, "paperclipai company export <company-id> --out ./my-company --include company,agents,projects,tasks,skills --expand-referenced-skills"))),
    h("section", { class: "import-card" },
      h("h2", {}, icon("download-simple"), "2. Bring it here"),
      h("div", { class: "import-sources" },
        h("div", {}, h("button", { type: "button", class: "btn primary", onclick: () => zipInput.click() }, icon("file-zip"), "Choose the Zip…"), zipInput),
        h("form", { class: "import-line", onsubmit: (e) => { e.preventDefault(); stage(e.submitter, { folder: folder.value }); } },
          h("label", {}, "Or the exported folder"), folder, h("button", { type: "submit", class: "btn" }, "Open")),
        h("form", { class: "import-line", onsubmit: (e) => { e.preventDefault(); stage(e.submitter, { github: repo.value }); } },
          h("label", {}, "Or a GitHub repository"), repo, h("button", { type: "submit", class: "btn" }, "Fetch"))),
      status,
      h("p", { class: "faint" }, "Nothing is imported until you've seen what's in it and ticked what you want.")));

  function showPreview(token) {
    body.replaceChildren(skeleton(4));
    bridge.call("paperclip_preview", { token }).then((p) => { if (alive()) renderPreview(token, p); }, (error) => alive() && body.replaceChildren(errorBox(error, choose)));
  }

  function renderPreview(token, p) {
    const picked = { agents: new Set(), skills: new Set(), routines: new Set(), tasks: new Set(), projects: new Set() };
    const folders = {};
    const box = (kind, item) => {
      const usable = !item.blocked;
      if (usable) picked[kind].add(item.slug);
      return h("input", { type: "checkbox", checked: usable, disabled: !usable, "aria-label": `Import ${item.name}`,
        onchange: (e) => { e.target.checked ? picked[kind].add(item.slug) : picked[kind].delete(item.slug); } });
    };
    const row = (kind, item, extra) => h("label", { class: `import-item${item.blocked ? " blocked" : ""}` }, box(kind, item),
      h("span", { class: "import-text" }, h("strong", {}, item.name),
        item.note ? h("small", { class: "faint" }, item.note) : null,
        item.blocked ? h("small", { class: "import-why" }, icon("prohibit"), item.blocked) : null,
        item.exists ? h("small", { class: "faint" }, "You already have one with this name: it comes in under a new one.") : null),
      extra || null);
    const section = (kind, title, ic, items, extra) => items.length ? h("section", { class: "import-group" },
      h("h3", {}, icon(ic), `${title} (${items.length})`), items.map((item) => row(kind, item, extra?.(item)))) : null;
    const projectFolder = (item) => {
      folders[item.slug] = item.folder;
      return h("span", { class: "import-folder", onclick: (e) => e.preventDefault() },
        h("input", { class: "input mono", value: item.folder, "aria-label": `Folder for ${item.name}`, oninput: (e) => { folders[item.slug] = e.target.value; } }),
        h("small", { class: "faint" }, item.repo ? `Cloned from ${item.repo} if the folder doesn't exist yet.` : "Made empty if it doesn't exist yet."));
    };
    const total = ["agents", "skills", "projects", "routines", "tasks"].reduce((n, k) => n + p[k].length, 0);
    fill(body,
      h("section", { class: "import-card" },
        h("h2", {}, icon("buildings"), p.company.name, h("span", { class: "faint" }, ` · ${total} things found`)),
        p.company.description ? h("p", { class: "faint" }, p.company.description) : null,
        section("agents", "Agents", "robot", p.agents),
        section("skills", "Skills", "lightning", p.skills),
        section("projects", "Projects", "folders", p.projects, projectFolder),
        section("routines", "Routines", "repeat", p.routines),
        section("tasks", "Open tasks", "list-checks", p.tasks),
        h("details", { class: "fold" }, h("summary", {}, icon("info"), "What doesn't come over"),
          h("ul", { class: "import-left" }, p.left_out.map((line) => h("li", {}, line)))),
        h("p", { class: "faint" }, "Routines arrive switched off. Open tasks wait on the Tasks page until you start them."),
        h("div", { class: "toolbar" },
          h("button", { type: "button", class: "btn ghost", onclick: choose }, "Choose Another"),
          h("span", { style: "flex:1" }),
          h("button", { type: "button", class: "btn primary", onclick: async (e) => {
            let result = null;
            const projects = Object.fromEntries([...picked.projects].map((slug) => [slug, folders[slug]]));
            const ok = await runAction(e.currentTarget, async () => {
              result = await bridge.call("paperclip_apply", { token, agents: [...picked.agents], skills: [...picked.skills], projects,
                routines: [...picked.routines], tasks: [...picked.tasks] });
              return { ok: true, message: result.done.length ? `Imported ${result.done.length} from Paperclip.` : "Nothing was imported." };
            });
            if (ok && result && alive()) { await refreshCatalog(); state.config = (await bridge.call("init")).config; renderDone(result); }
          } }, icon("download-simple"), "Import"))));
  }

  function renderDone(result) {
    fill(body, h("section", { class: "import-card" },
      h("h2", {}, icon("check-circle", "ph-fill"), result.done.length ? `Imported ${result.done.length} thing${result.done.length === 1 ? "" : "s"}` : "Nothing was imported"),
      result.done.length ? h("ul", { class: "import-left" }, result.done.map((line) => h("li", {}, line))) : null,
      result.skipped.length ? [h("h3", {}, "Skipped"), h("ul", { class: "import-left" }, result.skipped.map((line) => h("li", {}, line)))] : null,
      h("div", { class: "toolbar" },
        h("a", { class: "btn", href: "#swarm" }, icon("users-three"), "Agents"),
        h("a", { class: "btn", href: "#routines" }, icon("repeat"), "Routines"),
        h("a", { class: "btn", href: "#tasks" }, icon("list-checks"), "Tasks"),
        h("span", { style: "flex:1" }),
        h("button", { type: "button", class: "btn ghost", onclick: choose }, "Import Another"))));
  }
  choose();
};

views.find = (root, alive) => {
  let topic = "";
  let query = "";
  let timer = null;
  const body = h("div", {}, skeleton());
  const controls = h("div", { class: "controls-line", style: "margin:4px 0 14px" });
  const status = h("p", { class: "faint", style: "font-size:12px;margin:0 0 10px" });
  let careful = false;
  root.append(pageHead("Agents", "Agents other people published on GitHub, gaining the most stars lately. Only projects with an open-source license are listed.",
      async (e) => { await runAction(e.currentTarget, () => bridge.call("refresh_github_agents")); }),
    agentsTabs("find"), controls, status, body);

  const card = (c, kinds) => {
    const s = c.summary || {};
    const growth = c.held ? "new: too early to rank" : c.week ? `+${c.week} stars this week` : "";
    return h("article", { class: "draft-row" },
      h("div", { class: "draft-row-top" },
        h("button", { type: "button", class: "linklike", style: "font-weight:600", onclick: () => openUrl(githubUrl(c.repo)) }, c.repo),
        h("span", { class: "pill" }, kinds[c.kind] || c.kind),
        h("span", { class: "faint", style: "font-size:12px" }, [`★ ${c.stars}`, growth, c.license + (c.copyleft ? " (copyleft)" : "")].filter(Boolean).join(" · ")),
        h("span", { style: "flex:1" }),
        h("button", { class: "btn small ghost", title: "Lago reads the repository and tells you what it does, its risk and reward, and where it fits your projects",
          onclick: () => explainDialog(c, () => importIt(c)) }, icon("question"), "What does this do?"),
        c.have ? haveButton(c.have) : h("button", { class: "btn small", onclick: async (e) => {
          const risky = s.risk === "high" ? " It's marked high risk: it can run commands, change data or send things." : "";
          const ok = await confirmDialog({ title: `Import ${c.repo}?`,
            body: careful
              ? "Careful import: the GitHub Agent Builder checks it for security problems and tests it in a throwaway sandbox. If it passes, a draft appears on the Agents tab for you to approve." + risky
              : "Lago fetches it, checks its files, and installs its agents, skills or MCP server. You can switch any of them off on the Agents tab." + risky,
            confirm: careful ? "Check and Draft" : "Import" });
          if (ok) await runAction(e.currentTarget, () => bridge.call("import_github_agent", { repo: c.repo })).then((done) => { if (done) { if (!careful) refreshCatalog(); load(); } });
        } }, icon("download-simple"), "Import")),
      c.have && (c.have.agents.length || c.have.skills.length) ? h("p", { class: "faint", style: "font-size:12px;margin:2px 0 4px" }, icon("check-circle"), " Yours: ",
        [...c.have.agents.map((id) => h("a", { href: `#swarm/${id}` }, id)), ...c.have.skills.map((n) => h("span", {}, `skill ${n}`))]
          .flatMap((el, i) => (i ? [", ", el] : [el]))) : null,
      h("p", { class: "muted draft-line" }, s.what || c.description || "No description yet."),
      s.what ? h("p", { class: "faint", style: "font-size:12px;margin:4px 0 0" }, `Needs: ${s.needs} · Produces: ${s.produces} · Risk: ${s.risk}`) : null);
  };

  const importIt = (c) => runAction(null, () => bridge.call("import_github_agent", { repo: c.repo })).then((done) => { if (done) { if (!careful) refreshCatalog(); load(); } });

  // What you already have of a repo, instead of an Import button.
  const haveButton = (have) => have.agents.length || have.skills.length ? h("span", { class: "pill solved" }, icon("check"), "Imported")
    : have.draft ? h("a", { class: "btn small", href: "#swarm" }, icon("hourglass"), "Draft Waiting")
    : h("span", { class: "pill working" }, "Importing…");

  async function load() {
    let data;
    try { data = await bridge.call("github_agents", { topic, query }); } catch (error) { if (alive()) body.replaceChildren(errorBox(error, load)); return; }
    if (!alive()) return;
    if (!controls.childElementCount) {
      controls.append(
        h("label", { class: "search" }, icon("magnifying-glass"), h("input", { class: "input", type: "search", placeholder: "Describe the task, e.g. query my Postgres database…",
          oninput: (e) => { query = e.target.value; clearTimeout(timer); timer = setTimeout(load, 300); } })),
        h("select", { class: "select", "aria-label": "Topic", onchange: (e) => { topic = e.target.value; load(); } },
          [["", "All topics"], ...Object.entries(data.topics)].map(([v, l]) => h("option", { value: v }, l))),
        h("label", { class: "check", style: "margin-left:auto", title: "Test each agent in a throwaway sandbox and approve a draft before it's installed" },
          h("input", { type: "checkbox", checked: data.careful || undefined,
            onchange: async (e) => { const on = e.target.checked; if (await runAction(null, () => bridge.call("set_careful_import", { on }))) careful = on; else e.target.checked = !on; } }),
          " Careful import"));
    }
    careful = data.careful;
    status.textContent = data.refreshed_at ? `${data.listed} agents listed · updated ${ago(data.refreshed_at)} · ${query.trim() ? "best matches first" : "most stars gained lately first"}` : "";
    body.replaceChildren(data.cards.length ? h("div", { class: "rows" }, data.cards.map((c) => card(c, data.kinds)))
      : h("div", { class: "empty" }, h("p", {}, data.listed ? "Nothing matches." : "The list is empty: it fills on its daily refresh, or press refresh above.")));
  }
  load();
};

views.swarm = (root, alive, agentId) => {
  if (agentId) { evaluationPage(root, alive, agentId); return; }
  let query = "";
  let domain = "";
  let show = "all";
  let order = "domain";
  const controls = h("div", { class: "controls-line" });
  const draftsSlot = h("div");
  const skillDraftsSlot = h("div");
  const grid = h("div");
  const extras = h("div");
  root.append(pageHead("Agents", "Every agent your tasks can use and the skills and plugins they have. Switch off the ones that shouldn't be used.",
      async () => { await refreshCatalog(); if (alive()) { renderControls(); render(); } }),
    agentsTabs("swarm"),
    h("div", { class: "hint-line" }, icon("info"), "Agents sleep until a task needs them. ", h("button", { type: "button", class: "linklike", onclick: () => newTaskDialog() }, "Start a new task.")),
    draftsSlot, skillDraftsSlot, controls, grid, extras);

  // Skills fetched from a repository (skill_sources.py), waiting for your review.
  async function loadSkillDrafts() {
    let rows;
    try { rows = await bridge.call("skill_drafts"); } catch { return; }
    if (!alive()) return;
    if (!rows.length) { skillDraftsSlot.replaceChildren(); return; }
    fill(skillDraftsSlot, h("section", { class: "drafts" }, h("h2", { class: "label" }, icon("lightning"), ` New Skills to Review (${rows.length})`),
      rows.map((d) => h("article", { class: "draft-card" },
        h("div", { class: "task-top" }, h("strong", {}, d.name), h("span", { class: "faint mono", style: "font-size:12px" }, `${d.repo} @ ${d.commit.slice(0, 10)}`)),
        d.warnings.length ? h("p", { class: "muted" }, d.warnings.join(" ")) : null,
        Object.entries(d.texts).map(([name, text]) => h("details", {}, h("summary", {}, name), h("pre", { class: "draft-file" }, text))),
        h("div", { class: "controls-line", style: "margin-top:8px" },
          h("button", { class: "btn small primary", onclick: (e) => runAction(e.currentTarget, () => bridge.call("decide_skill", { draft_id: d.id, approve: true })).then(() => { refreshCatalog(); navigate(); }) }, icon("check"), "Install"),
          h("button", { class: "btn small ghost", onclick: (e) => runAction(e.currentTarget, () => bridge.call("decide_skill", { draft_id: d.id, approve: false })).then(navigate) }, "Reject"))))));
  }

  // Agents the github-agent-builder made from a repo, waiting for you: nothing is installed
  // until you approve one.
  async function loadDrafts() {
    let res;
    try { res = await bridge.call("agent_drafts"); } catch { return; }
    if (!alive()) return;
    const recent = (d) => Date.now() / 1000 - (d.updated_at || d.created_at) < 86400;
    const shown = res.drafts.filter((d) => !["rejected", "dismissed"].includes(d.status) && (d.status !== "installed" || recent(d)));
    if (!shown.length) { draftsSlot.replaceChildren(); return; }
    const waiting = shown.filter((d) => d.status === "ready");
    const tone = { ready: "draft", installing: "running", installed: "done", failed: "failed" };
    const label = { ready: "Waiting for you", installing: "Installing…", installed: "Installed", failed: "Install failed" };
    const fileLabel = (d, name) => ({ "agent.md": "Manifest and prompt", "install.sh": `Install script (runs in ~/lago-work/tools/${d.agent_id})`, "mcp.json": "MCP server launch", "tests.json": "Its tests" }[name] || name);
    const origin = (d) => !d.repo ? `Proposed by ${agentName(d.from_agent || "") || "an agent"}: no agent fit the job.`
      : ["Built on ", h("button", { type: "button", class: "linklike", onclick: () => openUrl(githubUrl(d.repo.split("@")[0])) }, d.repo.split("@")[0])];
    // Everything long (why it was drafted, its files, the install log) sits behind one fold.
    const details = (d) => h("details", { class: "draft-more" }, h("summary", {}, "Details"),
      h("p", { class: "prewrap", style: "margin:6px 0" }, d.summary),
      d.sandbox_report ? h("p", { class: "faint mono", style: "font-size:12px" }, `Sandbox report: ${d.sandbox_report}`) : null,
      Object.entries(d.files).map(([name, text]) => h("details", {}, h("summary", {}, fileLabel(d, name)), h("pre", { class: "draft-file" }, text))),
      d.install_log ? h("details", {}, h("summary", {}, "Install log"), h("pre", { class: "draft-file" }, d.install_log)) : null);
    const row = (d) => h("article", { class: `draft-row s-${d.status}` },
      h("div", { class: "draft-row-top" },
        h("strong", {}, d.name), h("span", { class: "faint mono", style: "font-size:12px" }, d.agent_id),
        h("span", { class: `pill p-${tone[d.status] || "running"}` }, label[d.status] || d.status),
        h("span", { style: "flex:1" }),
        d.status === "ready" ? [
          h("button", { class: "btn primary small", onclick: async (e) => {
            const ok = await confirmDialog({ title: `Install ${d.name}?`, body: d.files["install.sh"]
              ? "Its install script runs now, inside ~/lago-work/tools. Read it first (Details). Then the agent joins the swarm and its tests run once."
              : "The agent joins the swarm and its tests run once.", confirm: "Approve and Install" });
            if (ok && await runAction(e.currentTarget, () => bridge.call("decide_agent_draft", { draft_id: d.id, approve: true }))) loadDrafts();
          } }, icon("check"), "Approve"),
          h("button", { class: "btn ghost small", onclick: async (e) => {
            if (await runAction(e.currentTarget, () => bridge.call("decide_agent_draft", { draft_id: d.id, approve: false }))) loadDrafts();
          } }, "Reject")] : null,
        ["failed", "installed"].includes(d.status) ? h("button", { class: "btn ghost small", title: "Hide it from this list",
          onclick: (e) => runAction(e.currentTarget, () => bridge.call("dismiss_agent_draft", { draft_id: d.id }), "Hidden.").then(loadDrafts) }, "Dismiss") : null),
      h("p", { class: "muted draft-line" }, d.agent_summary),
      h("p", { class: "faint draft-line", style: "font-size:12.5px" }, origin(d)),
      d.notes ? h("p", { class: d.status === "failed" ? "error draft-line" : "faint draft-line", style: "font-size:12.5px" }, d.notes) : null,
      details(d));
    fill(draftsSlot, h("details", { class: "fold", style: "margin-bottom:16px", open: waiting.length ? true : undefined },
      h("summary", {}, icon("github-logo"), waiting.length ? `New Agents to Approve (${waiting.length})` : `New Agents (${shown.length})`,
        h("span", { class: "faint" }, waiting.length ? "nothing is installed until you approve" : "recently installed or failed")),
      h("div", { class: "fold-body draft-list" }, shown.map(row))));
    if (shown.some((d) => d.status === "installing")) setTimeout(() => { if (alive()) { loadDrafts(); refreshCatalog().then(() => { if (alive()) render(); }).catch(() => {}); } }, 4000);
  }

  loadDrafts();
  loadSkillDrafts();

  function renderControls() {
    const domains = [...new Set(state.catalog.agents.map((a) => a.domain))].sort();
    const segment = (options, current, set) => h("div", { class: "segmented", role: "radiogroup" },
      options.map(([key, label]) => h("button", { type: "button", role: "radio", "aria-checked": String(key === current),
        onclick: () => { set(key); renderControls(); render(); } }, label)));
    fill(controls,
      h("label", { class: "search agents-search" }, icon("magnifying-glass"), h("input", { class: "input", type: "search", placeholder: "Search purpose, inputs, tags…",
        "aria-label": "Search agents", value: query, oninput: (e) => { query = e.target.value; render(); } })),
      h("select", { class: "select", "aria-label": "Domain", onchange: (e) => { domain = e.target.value; render(); } },
        [["", "All domains"], ...domains.map((d) => [d, humanize(d)])].map(([v, l]) => h("option", { value: v, selected: v === domain }, l))),
      segment([["all", "Any"], ["on", "On"], ["off", "Off"]], show, (v) => { show = v; }),
      segment([["domain", "By Domain"], ["rank", "Ranked"]], order, (v) => { order = v; }));
  }

  function matches(e) {
    if (domain && e.domain !== domain) return false;
    if (show === "on" && !e.enabled) return false;
    if (show === "off" && e.enabled) return false;
    if (!query.trim()) return true;
    const text = [e.id, e.name, e.summary, e.use_when, e.tags.join(" "), e.inputs.map((i) => i.name).join(" ")].join(" ").toLowerCase();
    return query.toLowerCase().split(/\s+/).filter(Boolean).every((w) => text.includes(w));
  }

  function render() {
    const shown = state.catalog.agents.filter(matches);
    const byDomain = {};
    for (const e of shown) (byDomain[e.domain] ||= []).push(e);
    const on = state.catalog.agents.filter((a) => a.enabled).length;
    fill(grid, h("p", { class: "faint", style: "margin:-4px 0 16px;font-size:12.5px", title: `Manifests live in ${state.catalog.folder} (format in catalog/README.md).` },
        `${on} of ${state.catalog.agents.length} agents on${shown.length !== state.catalog.agents.length ? `, ${shown.length} shown` : ""}.`),
      shown.length && order === "rank" ? h("div", { class: "catalog-grid" },
          [...shown].sort((x, y) => (x.eval?.rank || 999) - (y.eval?.rank || 999)).map(agentTile))
      : shown.length ? Object.keys(byDomain).sort().map((d) => h("section", { class: "catalog-group", "aria-label": humanize(d) },
        h("h2", { class: "catalog-domain" }, icon(DOMAIN_ICONS[d] || "cube"), humanize(d), h("span", { class: "faint" }, byDomain[d].length)),
        h("div", { class: "catalog-grid" }, byDomain[d].map(agentTile))))
        : h("div", { class: "empty" }, h("strong", {}, "No agents match."), "Try other words or filters."));
    const skills = state.catalog.skills || [];
    const plugins = state.catalog.plugins || [];
    fill(extras,
      h("details", { class: "fold" }, h("summary", {}, icon("lightning"), `Skills (${skills.length})`, h("span", { class: "faint" }, "The lead assigns them to steps")),
        h("div", { class: "controls-line", style: "padding:0 14px" }, h("button", { class: "btn small", onclick: () => skillFetchDialog() }, icon("git-branch"), "Add from a Repository")),
        h("div", { class: "rows" }, skills.map((k) => h("div", { class: "row" }, h("div", { class: "row-main" },
          h("span", { class: "row-title mono" }, k.name, h("span", { class: "badge", style: "margin-left:8px" }, k.source)),
          h("span", { class: "muted", style: "font-size:13px" }, k.description)))))),
      h("details", { class: "fold" }, h("summary", {}, icon("plugs-connected"), `Plugins (${plugins.length})`, h("span", { class: "faint" }, "On for every agent")),
        h("div", { class: "rows" }, plugins.map((pl) => h("div", { class: "row" }, h("div", { class: "row-main" },
          h("span", { class: "row-title" }, pl.name, h("span", { class: "faint mono", style: "margin-left:8px;font-size:12px" }, pl.version)),
          h("span", { class: "muted", style: "font-size:13px" }, pl.description),
          pl.skills.length || pl.commands.length ? h("span", { class: "chips" }, pl.skills.map((k) => h("span", { class: "chip skill" }, k)), pl.commands.map((c) => h("span", { class: "chip" }, `/${c}`))) : null))))),
      state.catalog.templates.length ? h("details", { class: "fold" }, h("summary", {}, icon("flow-arrow"), `Plan Templates (${state.catalog.templates.length})`, h("span", { class: "faint" }, "Proven shapes the lead starts from")),
        h("div", { class: "rows" }, state.catalog.templates.map((t) => h("div", { class: "row" },
          h("div", { class: "row-main" }, h("span", { class: "row-title" }, t.name, h("span", { class: "faint mono", style: "font-size:12px;margin-left:8px" }, t.id)),
            h("span", { class: "muted" }, t.summary),
            h("span", { class: "chips" }, t.agents.map((a) => h("button", { type: "button", class: `chip link${catalogEntry(a)?.enabled === false ? " off" : ""}`, onclick: () => manifestDialog(a) }, a)))))))) : null);
  }

  function agentTile(e) {
    const required = e.inputs.filter((i) => i.required);
    return h("article", { class: `catalog-card${e.enabled ? "" : " off"}` },
      h("div", { class: "catalog-top" },
        h("button", { type: "button", class: "catalog-name", onclick: () => manifestDialog(e.id), title: "Read the manifest" }, e.name),
        h("button", { class: "switch", role: "switch", type: "button", "aria-checked": String(e.enabled),
          "aria-label": `${e.name}: ${e.enabled ? "on" : "off"}`, title: e.enabled ? "On: tasks can use it" : "Off: tasks can't see it",
          onclick: async (ev) => {
            const next = !e.enabled;
            if (await runAction(ev.currentTarget, () => bridge.call("set_swarm_agent", { agent_id: e.id, enabled: next }))) {
              e.enabled = next;
              render();
            }
          } })),
      h("p", { class: "catalog-summary" }, e.summary),
      scoreBadge(e),
      h("div", { class: "chips" },
        required.map((i) => h("span", { class: "chip", title: `Required input: ${i.description}` }, icon("arrow-right"), i.name)),
        e.outputs.map((o) => h("span", { class: "chip out", title: `Output: ${o.description}` }, icon("arrow-left"), o.name)),
        e.missing_base ? h("span", { class: "chip danger", title: `Extends ${e.extends}, which wasn't found` }, "definition missing") : null));
  }

  renderControls();
  render();
  refreshCatalog().then(() => { if (alive()) { renderControls(); render(); } }).catch(() => {});
};

/* ================================================================ agent evaluations */

/* Your thumbs on one task an agent did. It replaces the Conductor's or Lago's verdict. */
function thumbs(taskId, name, after) {
  const rate = (verdict) => async (ev) => {
    const button = ev.currentTarget;
    let reason = "";
    if (verdict === "down") {
      reason = await promptDialog({ title: `What did ${name} get wrong?`, label: "Reason",
        placeholder: "e.g. it skipped the edge cases the task asked for", confirm: "Thumbs Down",
        help: "The agent's prompt is rewritten from these reasons, for you to approve." });
      if (reason === undefined || reason === null) return;
    }
    if (await runAction(button, () => bridge.call("rate_task", { task_id: taskId, verdict, reason }))) after?.();
  };
  return h("span", { class: "thumbs" },
    h("button", { type: "button", class: "btn icon ghost small", title: "It did a good job", "aria-label": `Thumbs up for ${name}`, onclick: rate("up") }, icon("thumbs-up")),
    h("button", { type: "button", class: "btn icon ghost small", title: "It did a poor job", "aria-label": `Thumbs down for ${name}`, onclick: rate("down") }, icon("thumbs-down")));
}

/* Your verdict on a session's whole result: every agent that did a step shares it. */
function judgeButtons(workspaceId, after) {
  const judge = (accepted) => async (ev) => {
    const button = ev.currentTarget;
    let reason = "";
    if (!accepted) {
      reason = await promptDialog({ title: "What's wrong with the result?", label: "Reason",
        placeholder: "e.g. the export still drops the last row", confirm: "Not Right",
        help: "The steps that failed or were rated down lose points, and their agents learn from this." });
      if (reason === undefined || reason === null) return;
    }
    if (await runAction(button, () => bridge.call("judge_session", { workspace_id: workspaceId, accepted, reason }))) after?.();
  };
  return h("span", { class: "thumbs" },
    h("button", { type: "button", class: "btn small", title: "The result is right: the agents that did it earn points", onclick: judge(true) }, icon("thumbs-up"), "Accept"),
    h("button", { type: "button", class: "btn small", title: "The result is wrong: the steps that failed lose points", onclick: judge(false) }, icon("thumbs-down"), "Not Right"));
}

function scoreBadge(e) {
  const c = e.eval;
  if (!c) return null;
  const rated = c.ups + c.downs + c.tests_run;
  const tone = !rated ? "" : c.score >= 70 ? " good" : c.score < 45 ? " bad" : " mid";
  return h("a", { class: `score-line${tone}`, href: `#swarm/${e.id}`, title: "Ratings, tests and improvements" },
    h("b", {}, rated ? `${c.score}` : "–"), rated ? `#${c.rank}` : "Not rated yet",
    rated ? h("span", { class: "faint" }, `${c.ups} up · ${c.downs} down${c.tests_run ? ` · tests ${c.tests_passed}/${c.tests_run}` : ""}`) : null,
    c.stars ? h("span", { class: "faint", title: `Built on ${c.source}` }, icon("star"), c.stars.toLocaleString()) : null);
}

const RATER_LABELS = { human: "You", conductor: "Conductor", lago: "Lago", outcome: "Whole result", pick: "Test drive" };
const PROPOSAL_LABELS = { preparing: "Filling the suite", drafting: "Drafting", testing: "Testing", ready: "Passed: ready for you", approved: "Approved", rejected: "Rejected", failed: "Didn't pass", reverted: "Reverted" };
const GATE_LABELS = { better: "Better", held: "Held", dropped: "Dropped", fails: "Fails", "not run": "Not run" };

/* Publishing an agent (publish.py): the exact files that go public, what the scrub removed,
   and the words you check before anything leaves the computer. */
async function publishDialog(agentId, name) {
  let d;
  try { d = await bridge.call("prepare_publish", { agent_id: agentId }); } catch (error) { toast(error.message, "error"); return; }
  const removed = [...new Set(d.findings.filter((f) => f.action === "removed").map((f) => `${f.kind} (${f.text})`))];
  const check = [...new Set(d.findings.filter((f) => f.action === "check").map((f) => f.text))];
  let checked = !check.length;
  const go = h("button", { type: "submit", class: "btn primary", disabled: !checked || undefined }, d.published_at ? "Publish Update" : "Publish Publicly");
  const ok = await openDialog((close) => h("form", { method: "dialog", onsubmit: (e) => { e.preventDefault(); close(true); } },
    h("h2", {}, `Publish ${name} to GitHub?`),
    h("p", { class: "desc" }, d.published_at
      ? `This pushes the changes to github.com/${d.repo}, which is public.`
      : `This creates the public repository github.com/${d.repo} with the files below, under an MIT license, tagged lago-agent so other people's Lago can find it.`),
    removed.length ? h("p", { class: "muted" }, `Removed from the copy: ${removed.join(", ")}.`) : h("p", { class: "muted" }, "Nothing private was found to remove."),
    check.length ? h("div", { class: "needs", role: "alert", style: "margin:10px 0" },
      h("p", {}, `Check these words in the files: ${check.join(", ")}. They may say what you work on, or point to folders only your computer has.`),
      h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => { checked = e.target.checked; go.disabled = !checked; } }), " I've read them and they can be public")) : null,
    Object.entries(d.files).map(([file, text]) => h("details", {}, h("summary", {}, file), h("pre", { class: "draft-file" }, text))),
    h("div", { class: "dialog-actions" },
      h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Cancel"), go)), { wide: true });
  if (ok) await runAction(null, () => bridge.call("publish_agent", { agent_id: agentId, checked }));
}

function evaluationPage(root, alive, agentId) {
  const body = h("div", {}, skeleton(6));
  const entry = catalogEntry(agentId);
  root.append(h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, h("a", { href: "#swarm" }, "Swarm"), icon("caret-right"), h("span", {}, entry?.name || agentId)),
    pageHead(`${entry?.name || agentId}: Evaluation`, "How well it works: ratings from the lead, Lago and you, your tests, and prompt improvements waiting for you.", () => load()),
    body);

  async function load() {
    let d;
    try { d = await bridge.call("agent_evaluation", { agent_id: agentId }); } catch (error) { fill(body, errorBox(error, load)); return; }
    if (alive()) render(d);
  }

  function render(d) {
    const c = d.card;
    const open = d.proposals.filter((p) => ["drafting", "testing", "ready"].includes(p.status));
    fill(body,
      h("div", { class: "score-head" },
        h("div", { class: "score-big" }, h("b", {}, c.ups + c.downs + c.tests_run ? c.score : "–"), h("span", { class: "faint" }, "/ 100")),
        h("div", { class: "row-main" },
          h("span", { class: "row-title" }, `Rank #${c.rank} of ${state.catalog.agents.length}`),
          h("span", { class: "muted" }, `${c.ups} thumbs up, ${c.downs} down on the current prompt (${c.version})`
            + (c.tests ? ` · tests: ${c.tests_run ? `${c.tests_passed} of ${c.tests_run} passed, ${c.test_score}/100` : `${c.tests} not run on this prompt`}` : " · no tests yet")),
          h("span", { class: "faint" }, c.source ? `Starts from ${c.prior}/100: built on ${c.source}${c.stars ? ` (${c.stars.toLocaleString()} stars)` : ""}.` : "Starts from 50/100 until it has ratings.",
            d.improve_after ? ` A better prompt is drafted after ${d.improve_after} thumbs down.` : " Automatic improvement is off.")),
        h("div", { class: "row-actions" },
          h("button", { class: "btn", disabled: !d.tests.length || undefined, title: d.tests.length ? "Run every test on the current prompt" : "Add a test first",
            onclick: async (e) => { if (await runAction(e.currentTarget, () => bridge.call("run_agent_tests", { agent_id: agentId }))) load(); } }, icon("play"), "Run Tests"),
          h("button", { class: "btn", disabled: open.length ? true : undefined, title: open.length ? "An improvement is already in progress" : "Ask the agent-designer for a better prompt now",
            onclick: async (e) => { if (await runAction(e.currentTarget, () => bridge.call("improve_agent", { agent_id: agentId }))) load(); } }, icon("magic-wand"), "Improve Now"),
          h("button", { class: "btn", title: "Share it on GitHub for other people: you read the scrubbed files first",
            onclick: () => publishDialog(agentId, entry?.name || agentId) }, icon("github-logo"), "Publish to GitHub"))),
      d.proposals.length ? h("section", {}, h("h2", { class: "label" }, "Prompt Improvements"), h("div", { class: "rows" }, d.proposals.map(proposalRow))) : null,
      h("section", {}, h("h2", { class: "label" }, `Ratings (${d.ratings.length})`),
        d.ratings.length ? h("div", { class: "rows" }, d.ratings.map((r) => h("div", { class: "row" },
          h("div", { class: "row-main" },
            h("span", { class: "row-title" }, icon(r.verdict > 0 ? "thumbs-up" : "thumbs-down", "ph-fill"), " ", RATER_LABELS[r.rater] || r.rater,
              h("span", { class: "faint", style: "font-weight:400" }, ` · Task #${r.task_id} ${r.title || ""} · ${ago(r.created_at)}`),
              r.current ? null : h("span", { class: "badge", style: "margin-left:8px", title: "Rated an older prompt: counts half" }, "older prompt")),
            r.reason ? h("span", { class: "muted" }, r.reason) : null),
          r.rater === "human" ? null : h("div", { class: "row-actions" }, h("span", { class: "faint", style: "font-size:12px" }, "Your verdict:"), thumbs(r.task_id, entry?.name || agentId, load)))))
        : h("p", { class: "muted" }, "No ratings yet. The lead rates each step it checks, and Lago each agent it runs. You can rate steps on the plan page.")),
      testsSection(d));
  }

  function proposalRow(p) {
    const g = p.gate;
    const tone = { ready: "draft", approved: "done", failed: "failed", rejected: "failed", reverted: "failed" }[p.status] || "running";
    return h("div", { class: "row", style: "grid-template-columns:1fr" },
      h("div", { class: "row-main" },
        h("span", { class: "row-title" }, `#${p.id} `, h("span", { class: `pill p-${tone}` }, PROPOSAL_LABELS[p.status] || p.status),
          h("span", { class: "faint", style: "font-weight:400" }, ` ${p.reason} · ${ago(p.created_at)}${p.attempts > 1 ? ` · attempt ${p.attempts}` : ""}`),
          g?.old != null || g?.new != null ? h("span", { class: "badge", style: "margin-left:8px" }, `Suite: ${g.old ?? "–"} → ${g.new ?? "–"}`) : null),
        p.notes ? h("p", { class: "muted", style: "white-space:pre-wrap;margin:4px 0" }, p.notes) : null,
        g ? h("details", { open: p.status === "ready" || p.status === "failed" ? true : undefined }, h("summary", {}, g.passed ? "Regression check: passed" : "Regression check: not passed"),
          g.problems.length ? h("ul", { class: "gate-problems" }, g.problems.map((x) => h("li", {}, x))) : null,
          h("table", { class: "gate-table" },
            h("thead", {}, h("tr", {}, h("th", {}, "Test"), h("th", {}, "Current"), h("th", {}, "Proposed"), h("th", {}, ""))),
            h("tbody", {}, g.tests.map((t) => h("tr", {},
              h("td", {}, t.name, t.kind === "fix" ? h("span", { class: "badge", style: "margin-left:6px" }, "fix") : null),
              h("td", {}, t.old ?? "–"), h("td", {}, t.new ?? "–"),
              h("td", { class: `gate-${t.verdict.replace(" ", "-")}` }, GATE_LABELS[t.verdict] || t.verdict)))))) : null,
        p.new_prompt ? h("details", {}, h("summary", {}, "Compare the prompts"),
          h("div", { class: "prompt-compare" },
            h("div", {}, h("h3", { class: "label" }, `Current (${p.old_version})`), h("pre", {}, p.old_prompt)),
            h("div", {}, h("h3", { class: "label" }, `Proposed (${p.new_version})`), h("pre", {}, p.new_prompt)))) : null,
        h("div", { class: "row-actions", style: "margin-top:8px" },
          p.status === "ready" ? h("button", { class: "btn primary small", onclick: async (e) => {
            if (await runAction(e.currentTarget, () => bridge.call("decide_proposal", { proposal_id: p.id, approve: true }))) { await refreshCatalog(); load(); }
          } }, icon("check"), "Use the New Prompt") : null,
          ["preparing", "drafting", "testing", "ready"].includes(p.status) ? h("button", { class: "btn ghost danger small", onclick: async (e) => {
            if (await runAction(e.currentTarget, () => bridge.call("decide_proposal", { proposal_id: p.id, approve: false }))) load();
          } }, icon("x"), p.status === "ready" ? "Keep the Current One" : "Stop") : null,
          p.status === "approved" ? h("button", { class: "btn ghost small", onclick: async (e) => {
            if (await confirmDialog({ title: "Go back to the previous prompt?", body: "The agent runs with the prompt it had before this change.", confirm: "Revert" })
              && await runAction(e.currentTarget, () => bridge.call("revert_proposal", { proposal_id: p.id }))) { await refreshCatalog(); load(); }
          } }, icon("arrow-counter-clockwise"), "Revert") : null)));
  }

  function testsSection(d) {
    const latest = {};
    for (const r of d.test_runs) if (!latest[r.test_id]) latest[r.test_id] = r;
    const save = async (button, tests) => {
      if (await runAction(button, () => bridge.call("save_agent_tests", { agent_id: agentId, tests }))) load();
    };
    return h("section", {},
      h("h2", { class: "label" }, `Tests (${d.tests.length})`,
        h("button", { class: "btn small", style: "margin-left:12px", onclick: async (e) => {
          const t = await testDialog();
          if (t) save(e.currentTarget, [...d.tests, t]);
        } }, icon("plus"), "Add Test"),
        d.tests.filter((t) => t.kind !== "fix").length < d.min_tests ? h("button", { class: "btn small", style: "margin-left:8px", title: "Draft tests from the agent's manifest",
          onclick: async (e) => { if (await runAction(e.currentTarget, () => bridge.call("draft_agent_tests", { agent_id: agentId }))) load(); } }, icon("sparkle"), "Draft Tests") : null),
      h("p", { class: "faint", style: "margin:0 0 8px;font-size:12.5px" },
        `Every prompt change must pass this whole suite before you see it: each test runs ${d.repeats}× on the current and the proposed prompt, and the new one must pass all, drop on none, and score at least as well. `,
        "Regression tests are the agent's everyday job; fix tests come from its thumbs down. The designer only sees the fix tests."),
      d.tests.length ? h("div", { class: "rows" }, d.tests.map((t, i) => {
        const r = latest[t.id];
        return h("div", { class: "row" },
          h("div", { class: "row-main" },
            h("span", { class: "row-title" }, t.name,
              h("span", { class: "badge", style: "margin-left:8px" }, t.kind === "fix" ? "fix" : "regression"),
              t.origin === "drafted" ? h("span", { class: "badge", style: "margin-left:4px", title: "Drafted by a model: worth reading over" }, "drafted") : null),
            h("span", { class: "muted" }, `Must have: ${t.criteria}`),
            r ? h("span", { class: "faint" }, r.status === "graded" ? `Last run ${ago(r.updated_at)} on ${r.version}: ${Math.round(r.score * 100)}/100. ${r.notes}` : `Last run: ${r.status}${r.notes ? `. ${r.notes}` : ""}`) : h("span", { class: "faint" }, "Not run yet.")),
          h("div", { class: "row-actions" },
            h("button", { class: "btn icon ghost small", "aria-label": `Edit ${t.name}`, onclick: async (e) => {
              const edited = await testDialog(t);
              if (edited) save(e.currentTarget, d.tests.map((x, j) => (j === i ? { ...edited, id: t.id } : x)));
            } }, icon("pencil-simple")),
            h("button", { class: "btn icon ghost small danger", "aria-label": `Delete ${t.name}`, onclick: async (e) => {
              if (await confirmDialog({ title: "Delete this test?", body: t.name, confirm: "Delete", danger: true })) save(e.currentTarget, d.tests.filter((_, j) => j !== i));
            } }, icon("trash"))));
      })) : h("p", { class: "muted" }, "No tests yet. A test is a sample task and what a good result must have; it runs the agent for real in a scratch folder, and a cheap model grades the result."));
  }

  function testDialog(t = {}) {
    return openDialog((close) => {
      const name = h("input", { class: "input", value: t.name || "", placeholder: "e.g. Finds the login endpoint", autofocus: true });
      const task = h("textarea", { class: "input", rows: 5, placeholder: "The task exactly as the agent would get it" }, t.task || "");
      const inputs = h("textarea", { class: "input mono", rows: 3, placeholder: "name = value, one per line (its manifest's inputs)" },
        Object.entries(t.inputs || {}).map(([k, v]) => `${k} = ${v}`).join("\n"));
      const criteria = h("textarea", { class: "input", rows: 4, placeholder: "What a good result must have, e.g. names the endpoint and its parameters, cites evidence, under 1 page" }, t.criteria || "");
      const kind = h("select", { class: "input" },
        h("option", { value: "regression", selected: t.kind !== "fix" || undefined }, "Regression: part of its everyday job"),
        h("option", { value: "fix", selected: t.kind === "fix" || undefined }, "Fix: a failure it must not repeat (the designer sees it)"));
      const field = (label, el) => h("label", { class: "field" }, h("span", { class: "label" }, label), el);
      return h("form", { method: "dialog", onsubmit: (ev) => {
          ev.preventDefault();
          if (!task.value.trim() || !criteria.value.trim()) { toast("A test needs a task and what a good result must have.", "error"); return; }
          const parsed = Object.fromEntries(lines(inputs.value).map((l) => l.split(/\s*=\s*/)).filter((p) => p.length > 1).map(([k, ...v]) => [k, v.join("=")]));
          close({ ...t, name: name.value.trim(), task: task.value.trim(), inputs: parsed, criteria: criteria.value.trim(), kind: kind.value, origin: t.origin === "drafted" ? "edited" : t.origin || "you" });
        } },
        h("h2", {}, t.task ? "Edit Test" : "Add a Test"),
        field("Name", name), field("Kind", kind), field("Task", task), field("Inputs", inputs), field("A good result must have", criteria),
        h("div", { class: "dialog-actions" },
          h("button", { type: "button", class: "btn ghost", onclick: () => close(null) }, "Cancel"),
          h("button", { type: "submit", class: "btn primary" }, "Save Test")));
    }, { wide: true });
  }

  load();
}

/* ================================================================ pipelines */

views.pipelines = (root, alive) => {
  let filter = "open";
  let list = null;
  let lastKey = "";
  const body = h("div", {}, skeleton(4));
  const seg = h("div", { class: "segmented", role: "radiogroup", "aria-label": "Show" },
    [["open", "Open"], ["closed", "Finished"], ["all", "All"]].map(([key, label]) => h("button", {
      type: "button", role: "radio", "aria-checked": String(key === filter),
      onclick: (e) => { filter = key; seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b === e.currentTarget))); lastKey = ""; render(); },
    }, label)));
  root.append(h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, h("a", { href: "#projects" }, "Projects"), icon("caret-right"), h("span", {}, "All Plans")),
    pageHead("All Plans", "Every plan a lead proposed, in every project."),
    h("div", { class: "controls-line" }, seg), body);

  function render() {
    if (!list) return;
    const shown = list.filter((p) => filter === "all" || (filter === "open") === OPEN_PIPELINE.has(p.status));
    const key = filter + JSON.stringify(shown);
    if (key === lastKey) return;
    lastKey = key;
    if (!shown.length) {
      body.replaceChildren(h("div", { class: "empty" }, h("strong", {}, filter === "open" ? "No open plans." : "No plans here."),
        "Tasks propose plans here for you to approve."));
      return;
    }
    body.replaceChildren(h("div", { class: "task-list" }, shown.map((p) => {
      const done = p.steps.filter((s) => s.status === "done" || s.status === "skipped").length;
      return h("a", { class: `pipeline-row${p.status === "draft" ? " waiting" : ""}`, href: p.workspace ? `#work/${p.workspace}` : `#pipeline/${p.id}` },
        h("span", { class: "faint num mono" }, `#${p.id}`),
        h("span", { class: "pipeline-main" }, h("span", { class: "task-title" }, p.title),
          h("span", { class: "task-meta" }, projectName(p.project), p.area ? ` · ${areaName(p.project, p.area)}` : "",
            ` · ${done}/${p.steps.length} steps · ${ago(p.updated_at)}`)),
        stepDots(p), pipelinePill(p.status));
    })));
  }
  const load = async () => {
    try {
      list = await bridge.call("pipelines");
      if (alive()) render();
    } catch (error) {
      if (alive()) body.replaceChildren(errorBox(error, load));
    }
  };
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 3000);
  viewCleanup = () => clearInterval(timer);
  load();
};

views.pipeline = (root, alive, id) => {
  let data = null;
  let lastKey = "";
  const crumbs = h("nav", { class: "crumbs", "aria-label": "Breadcrumb" });
  const head = h("div");
  const toolbar = h("div", { class: "toolbar" });
  const stepsEl = h("div", {}, skeleton(4));
  root.append(crumbs, head, toolbar, stepsEl);

  function render() {
    const key = JSON.stringify(data);
    if (key === lastKey) return;
    lastKey = key;
    fill(crumbs, h("a", { href: "#projects" }, "Projects"), icon("caret-right"), projectCrumb(data.project),
      data.workspace ? [icon("caret-right"), h("a", { href: `#work/${data.workspace}` }, `Session #${data.workspace}`)] : null,
      icon("caret-right"), h("span", {}, `Plan #${data.id}`));
    fill(head,
      h("header", { class: "agent-head" },
        h("span", { class: "avatar" }, icon("flow-arrow")),
        h("div", {}, h("h1", {}, data.title, pipelinePill(data.status)),
          h("p", {}, projectName(data.project), data.area ? ` · ${areaName(data.project, data.area)}` : "",
            ` · ${data.steps.length} steps · proposed ${ago(data.created_at)}`))),
      pipelineNotes(data));
    fill(toolbar, pipelineActions(data, load), h("span", { style: "flex:1" }),
      featureOn("map") && PLAN_FINISHED.includes(data.status) && data.steps.some((st) => st.task_id)
        ? h("a", { class: "btn ghost", href: `#replay/${data.id}` }, icon("play-circle"), "Replay") : null,
      data.workspace ? h("a", { class: "btn ghost", href: `#work/${data.workspace}` }, icon("hexagon"), "Task") : null);
    fill(stepsEl, h("ol", { class: "steps", "aria-label": "Steps" }, data.steps.map(stepItem)));
  }

  async function load() {
    try {
      const next = await bridge.call("pipeline", { pipeline_id: Number(id) });
      if (!alive()) return;
      data = next;
      render();
    } catch (error) {
      if (alive()) stepsEl.replaceChildren(errorBox(error, load));
    }
  }
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 3000);
  viewCleanup = () => clearInterval(timer);
  load();
};

function pipelineNotes(p) {
  return [
    p.status === "draft" ? h("div", { class: "needs" }, h("div", { class: "needs-top" }, icon("warning-circle", "ph-fill"),
      h("p", {}, "The lead is waiting for your decision. No agent wakes until you approve."))) : null,
    h("section", { class: "goal-box" }, h("h2", { class: "label" }, "Goal"), h("p", { class: "prewrap" }, p.goal)),
    p.feedback ? h("section", { class: "goal-box" }, h("h2", { class: "label" }, p.status === "changes_requested" ? "Changes you asked for" : "Your note"), h("p", { class: "prewrap" }, p.feedback)) : null,
    p.summary ? h("section", { class: "goal-box done" }, h("h2", { class: "label" }, "Lead's summary"), h("p", { class: "prewrap" }, p.summary)) : null,
  ];
}

/* ================================================================ projects */

views.projects = (root, alive) => {
  const grid = h("div", {}, skeleton(3));
  const free = h("div", {});
  root.append(pageHead("Projects", "Open a project to work on it."),
    h("div", { class: "controls-line" }, h("span", { style: "flex:1" }),
      h("a", { class: "btn ghost", href: "#pipelines" }, icon("flow-arrow"), "All Plans"),
      h("button", { class: "btn", onclick: newFreeSession }, icon("hexagon"), "Work Without a Project"),
      h("button", { class: "btn", onclick: newProject }, icon("plus"), "New Project")),
    grid, free);
  let lastKey = "";
  async function load() {
    try {
      const [cards, sessions] = await Promise.all([bridge.call("projects"), bridge.call("free_sessions")]);
      if (!alive()) return;
      fill(free, sessions.length ? h("section", { class: "free-sessions" }, h("h2", { class: "label" }, "Outside projects"),
        sessions.map((s) => h("a", { class: "pipeline-row compact", href: `#work/${s.id}` },
          h("span", { class: "pipeline-main" }, h("span", { class: "task-title" }, s.title),
            h("span", { class: "task-meta mono" }, `${s.folder} · #${s.id}`)), pill(s.live)))) : null);
      const key = JSON.stringify(cards);
      if (key === lastKey) return;
      lastKey = key;
      fill(grid, cards.length ? h("div", { class: "project-grid" }, cards.map((c) => h("a", { class: "project-card", href: `#project/${c.id}` },
        h("div", { class: "project-card-top" }, h("span", { class: "team-icon" }, icon(c.icon || "cube")),
          h("span", { class: "project-card-name" }, c.name),
          c.waiting ? h("span", { class: "pill needs_you" }, `${c.waiting} waiting`) : c.active_sessions ? h("span", { class: "pill working" }, `${c.active_sessions} active`) : null),
        h("p", { class: "project-card-desc" }, c.description || h("span", { class: "mono faint" }, c.cwd)),
        h("div", { class: "project-card-meta" },
          c.areas ? h("span", {}, icon("squares-four"), `${c.areas} sections`) : null,
          c.services ? h("span", {}, icon("hard-drives"), `${c.services} services`) : null,
          c.repo ? h("span", {}, icon("github-logo"), "GitHub") : null,
          c.boards ? h("span", {}, icon("kanban"), `${c.boards} board${c.boards === 1 ? "" : "s"}`) : null,
          c.open_sessions ? h("span", {}, icon("hexagon"), `${c.open_sessions} open session${c.open_sessions === 1 ? "" : "s"}`) : null))))
        : h("div", { class: "empty" }, h("strong", {}, "No projects yet."), "Add one so tasks know where it lives and what it's connected to."));
    } catch (error) {
      if (alive()) grid.replaceChildren(errorBox(error, load));
    }
  }
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 5000);
  viewCleanup = () => clearInterval(timer);
  load();
};

/* A work session that belongs to no project: in a folder you name, or a new one of its own. */
async function newFreeSession() {
  let unrestricted = false;
  const result = await openDialog((close) => {
    const text = h("textarea", { id: "fs-text", class: "input", rows: 3, autofocus: true, placeholder: "What do you want to do? Optional: leave it empty and the lead asks." });
    const folder = h("input", { id: "fs-folder", class: "input mono", placeholder: "~/lago-work/session-N (a new folder)", spellcheck: "false" });
    return h("form", { onsubmit: (e) => { e.preventDefault(); close({ text: text.value.trim(), folder: folder.value.trim() }); } },
      h("h2", {}, "Work Without a Project"),
      h("p", { class: "desc" }, "A lead and any agents it wakes, working in one folder. Nothing project-specific: no services, servers or boards."),
      h("div", { class: "field" }, h("label", { for: "fs-text" }, "Task"), text),
      h("div", { class: "field" }, h("label", { for: "fs-folder" }, "Folder (optional)"), folder),
      h("label", { class: "switch-row" }, h("button", { class: "switch danger", role: "switch", type: "button", "aria-checked": "false",
        onclick: (e) => { unrestricted = !unrestricted; e.currentTarget.setAttribute("aria-checked", String(unrestricted)); } }), "Skip permissions"),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, icon("play"), "Start")));
  });
  if (!result) return;
  let password = "";
  if (unrestricted) {
    password = await askPassword({ title: "Start without permission checks?", body: "The lead and the agents it wakes will run every command without asking.", confirm: "Start Unrestricted" });
    if (password === null) return;
  }
  let opened = null;
  await runAction(null, async () => (opened = await bridge.call("open_workspace", { project: "", text: result.text, folder: result.folder, unrestricted, password })));
  if (opened?.workspace) { await pollLive(); location.hash = `#work/${opened.workspace}`; }
}

async function newProject() {
  const result = await openDialog((close) => {
    const name = h("input", { id: "np-name", class: "input", placeholder: "e.g. Webstore", autofocus: true, autocomplete: "off" });
    const folder = h("input", { id: "np-cwd", class: "input mono", placeholder: "~/projects/…", spellcheck: "false" });
    const error = h("p", { class: "error", hidden: true });
    return h("form", { onsubmit: (e) => {
        e.preventDefault();
        if (!name.value.trim() || !folder.value.trim()) { error.textContent = "Give it a name and a folder."; error.hidden = false; return; }
        close({ name: name.value.trim(), cwd: folder.value.trim() });
      } },
      h("h2", {}, "New Project"),
      h("p", { class: "desc" }, "You can connect its sections, services, links and GitHub in Settings next."),
      h("div", { class: "field" }, h("label", { for: "np-name" }, "Name"), name),
      h("div", { class: "field" }, h("label", { for: "np-cwd" }, "Folder"), folder, error),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, "Create")));
  });
  if (!result) return;
  const projects = structuredClone(state.config.projects || {});
  const id = uniqueId(result.name, projects);
  projects[id] = { name: result.name, icon: "cube", cwd: result.cwd, repo: "", branch: "main", description: "", stack: "",
    context: [], vars: {}, agents: [], areas: {}, links: [], services: [], commands: {}, github: { repos: [], boards: [] }, mcp_servers: {} };
  if (await runAction(null, () => bridge.call("save_projects", { projects }))) {
    state.config.projects = projects;
    location.hash = `#project/${id}/settings`;
  }
}

/* ---------------------------------------------------------------- one project */

const projectTabs = {};

views.project = (root, alive, pid, sub, extra) => {
  if (sub === "server" && extra) serverChoice[pid] = extra;
  const project = state.config.projects?.[pid];
  if (!project) {
    root.append(h("div", { class: "empty" }, h("strong", {}, "This project doesn't exist."), h("a", { href: "#projects" }, "Back to Projects")));
    return;
  }
  if (sub === "settings") { projectSettings(root, alive, pid); return; }
  let tab = ["work", "services", "github", "server"].includes(sub) ? sub : projectTabs[pid] || "work";
  let tabTimer = null;
  let tabCleanup = null;
  const hasGithub = Boolean(project.repo || project.github?.repos?.length || project.github?.boards?.length
    || Object.values(project.areas || {}).some((a) => a.repo));
  const links = [
    project.repo ? { name: "GitHub", url: githubUrl(project.repo), icon: "github-logo" } : null,
    ...(project.links || []).map((l) => ({ ...l, icon: "globe" })),
  ].filter(Boolean);
  const localSite = projectServers(project).find((s) => s.role === "local" && s.site);
  const tabBody = h("div");
  const tabs = h("div", { class: "tabs", role: "tablist" },
    [["work", "Work"], ["services", `Services${project.services?.length ? ` (${project.services.length})` : ""}`], hasGithub ? ["github", "GitHub"] : null,
      projectServers(project).length ? ["server", projectServers(project).length > 1 ? "Servers" : "Server"] : null].filter(Boolean)
      .map(([key, label]) => h("button", { role: "tab", type: "button", "aria-selected": String(key === tab), dataset: { tab: key },
        onclick: () => { tab = key; projectTabs[pid] = key; tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab))); renderTab(); } }, label)));

  root.append(
    h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, h("a", { href: "#projects" }, "Projects"), icon("caret-right"), h("span", {}, project.name || pid)),
    h("header", { class: "agent-head" },
      h("span", { class: "avatar big-icon" }, icon(project.icon || "cube")),
      h("div", {}, h("h1", {}, project.name || pid),
        h("p", {}, project.description || h("span", { class: "mono" }, project.cwd)),
        links.length ? h("div", { class: "link-chips" }, links.map((l) => h("button", { type: "button", class: "chip link", onclick: () => openUrl(l.url), title: l.url }, icon(l.icon), l.name))) : null),
      h("div", { class: "head-actions" },
        localSite ? h("button", { class: "btn primary", title: `Open ${localSite.site} (starts the local server if it's stopped)`,
          onclick: (e) => openSite(e.currentTarget, pid, localSite) }, icon("play"), "Test Locally") : null,
        h("a", { class: "btn ghost", href: `#project/${pid}/settings` }, icon("gear-six"), "Settings"))),
    tabs, tabBody);

  function renderTab() {
    if (tab !== "server") bridge.call("hide_terminal").catch(() => {});
    if (tabCleanup) tabCleanup();
    tabCleanup = null;
    clearInterval(tabTimer);
    tabBody.replaceChildren();
    if (tab === "services") tabCleanup = servicesPanel(tabBody, pid, project, alive);
    else if (tab === "github") tabCleanup = githubPanel(tabBody, pid, project, alive);
    else if (tab === "server" && projectServers(project).length) tabCleanup = serverPanel(tabBody, pid, project, alive);
    else tabCleanup = workPanel(tabBody, pid, project, alive);
  }
  viewCleanup = () => { if (tabCleanup) tabCleanup(); };
  renderTab();
};

function workPanel(root, pid, project, alive) {
  let selected = null;
  const areas = Object.entries(project.areas || {});
  const composer = h("div");
  const summaryEl = h("div");
  const goalsEl = h("div");
  const sessionsEl = h("div", {}, skeleton(2));
  const tile = (key, name, sub, iconName) => h("button", { type: "button", class: "area-tile", "aria-pressed": "false", dataset: { area: key },
    onclick: (e) => {
      selected = key;
      root.querySelectorAll(".area-tile").forEach((t) => t.setAttribute("aria-pressed", String(t === e.currentTarget)));
      renderComposer(name);
    } }, h("span", { class: "area-name" }, iconName ? icon(iconName) : null, name), sub ? h("span", { class: "area-sub" }, sub) : null);
  root.append(summaryEl,
    h("section", { class: "work-start" },
      h("h2", {}, "What do you want to work on?"),
      h("p", { class: "muted" }, "Pick a section. Its lead looks around, asks what you need, and chooses the agents, skills and plugins for it."),
      h("div", { class: "area-grid" },
        areas.map(([id, a]) => tile(id, a.name || id, (a.paths || []).join(", ") || a.description, null)),
        tile("", "Whole project", "Anything that spans sections", "squares-four")),
      composer),
    goalsEl,
    sessionsEl);
  loadGoals();
  loadSummary();

  /* The project's summary card (statuscards.py), written the first time it's opened. */
  function loadSummary(refresh = false) {
    const fold = h("details", { class: "fold", style: "margin:0 0 16px" },
      h("summary", {}, icon("article"), "Where It Stands", h("span", { class: "faint" }, "a short summary, rewritten when the project's work changes")),
      h("div", { class: "fold-body" }, h("p", { class: "faint" }, "Writing it…")));
    const show = async () => {
      let card;
      try { card = await bridge.call("project_summary", { project: pid, refresh }); } catch (error) { fill(fold.lastChild, errorBox(error, () => loadSummary())); return; }
      if (!alive()) return;
      fill(fold.lastChild, h("p", { class: "prewrap", style: "margin:0" }, card.text || "Nothing yet."),
        h("p", { class: "faint", style: "font-size:12px;margin:8px 0 0" }, card.generated_at ? `Written ${ago(card.generated_at)}. ` : "",
          h("button", { type: "button", class: "linklike", onclick: () => { refresh = true; fill(fold.lastChild, h("p", { class: "faint" }, "Rewriting…")); show(); } }, "Rewrite now")));
    };
    fold.addEventListener("toggle", function once() { if (fold.open) { fold.removeEventListener("toggle", once); show(); } });
    fill(summaryEl, fold);
    if (refresh) { fold.open = true; }
  }

  async function loadGoals(open = false) {
    let rows;
    try { rows = await bridge.call("goals", { project: pid, include_closed: true }); } catch { return; }
    if (!alive()) return;
    const active = rows.filter((g) => g.status === "active");
    const byParent = (parent) => rows.filter((g) => (g.parent_id ?? null) === parent);
    const row = (g, depth) => [h("div", { class: "row", style: depth ? `padding-left:${depth * 22}px` : "" },
      h("div", { class: "row-main" },
        h("span", { class: "row-title" }, g.title, g.status !== "active" ? h("span", { class: `pill ${g.status === "achieved" ? "p-done" : "p-draft"}`, style: "margin-left:8px" }, g.status === "achieved" ? "Achieved" : "Dropped") : null),
        h("span", { class: "row-sub" }, [g.target ? `by ${g.target}` : "", `${g.counts.open} open · ${g.counts.solved} solved · ${g.counts.closed} closed sessions`].filter(Boolean).join(" · "))),
      g.status === "active" ? h("div", { class: "row-actions" },
        h("button", { class: "btn small ghost", onclick: (e) => runAction(e.currentTarget, () => bridge.call("save_goal", { goal_id: g.id, status: "achieved" }), "Marked achieved.").then(() => loadGoals(true)) }, icon("check"), "Achieved"),
        h("button", { class: "btn small ghost", "aria-label": "Add a sub-goal", title: "Add a sub-goal", onclick: () => goalDialog(g.id) }, icon("plus"))) : null),
      ...byParent(g.id).map((c) => row(c, depth + 1))];
    fill(goalsEl, h("details", { class: "fold", style: "margin-bottom:16px", open: open || undefined },
      h("summary", {}, icon("target"), `Goals${active.length ? ` (${active.length})` : ""}`, h("span", { class: "faint" }, "what this project works toward")),
      h("div", { class: "fold-body" },
        rows.length ? h("div", { class: "rows" }, byParent(null).map((g) => row(g, 0))) : h("p", { class: "faint" }, "No goals yet. Tasks can be linked to one, and their leads see why the work matters."),
        h("div", { class: "controls-line", style: "margin-top:10px" }, h("button", { class: "btn small", onclick: () => goalDialog(null) }, icon("plus"), "New Goal")))));
  }

  async function goalDialog(parent) {
    const title = await promptDialog({ title: parent ? "New sub-goal" : "New goal", label: "Goal", placeholder: "e.g. First 10 paying customers", confirm: "Add" });
    if (!title) return;
    runAction(null, () => bridge.call("save_goal", { title, project: pid, parent_id: parent }), "Goal added.").then(() => loadGoals(true));
  }

  function renderComposer(name) {
    let unrestricted = false;
    const text = h("textarea", { class: "input", rows: 3, "aria-label": "What do you want to do?",
      placeholder: `What do you want to do${selected ? ` in ${name}` : ""}? Optional: leave it empty and the lead asks.` });
    const warning = h("p", { class: "unrestricted-note", hidden: true },
      icon("warning"), "The lead and every agent it wakes run with --dangerously-skip-permissions: no approvals, full shell. It can SSH into this project's servers and change them when you ask. Your password is required.");
    const toggle = h("label", { class: "switch-row" },
      h("button", { class: "switch danger", role: "switch", type: "button", "aria-checked": "false",
        onclick: (e) => { unrestricted = !unrestricted; e.currentTarget.setAttribute("aria-checked", String(unrestricted)); warning.hidden = !unrestricted; } }),
      "Skip permissions");
    const start = async (e) => {
      const button = e.currentTarget;
      let password = "";
      if (unrestricted) {
        password = await askPassword({ title: "Start without permission checks?",
          body: `The lead for ${name || "this project"} and the agents it wakes will run every command without asking, including over SSH on this project's servers.`,
          confirm: "Start Unrestricted" });
        if (password === null) return;
      }
      button.disabled = true;
      try {
        const res = await bridge.call("open_workspace", { project: pid, area: selected || "", text: text.value.trim(), unrestricted, password });
        await pollLive();
        location.hash = `#work/${res.workspace}`;
      } catch (error) {
        toast(error.message, "error");
        button.disabled = false;
      }
    };
    fill(composer, h("div", { class: "composer" }, text, warning,
      h("div", { class: "composer-actions" }, toggle,
        h("span", { class: "faint", style: "font-size:12.5px" }, `Opens a new terminal with a lead in ${project.cwd}`),
        h("button", { class: "btn ghost", onclick: () => { selected = null; root.querySelectorAll(".area-tile").forEach((t) => t.setAttribute("aria-pressed", "false")); composer.replaceChildren(); } }, "Cancel"),
        h("button", { class: "btn primary", onclick: start }, icon("play"), "Start"))));
    text.focus();
  }

  let lastKey = "";
  async function load() {
    try {
      const data = await bridge.call("project", { project_id: pid });
      if (!alive()) return;
      const key = JSON.stringify(data.sessions);
      if (key === lastKey) return;
      lastKey = key;
      const open = data.sessions.filter((s) => s.status === "open");
      const closed = data.sessions.filter((s) => s.status !== "open");
      fill(sessionsEl,
        h("h2", { class: "section-title" }, "Tasks"),
        open.length ? h("div", { class: "task-list" }, open.map(sessionRow))
          : h("p", { class: "muted" }, "None open. Pick a section above to start one."),
        closed.length ? h("details", { class: "fold" }, h("summary", {}, icon("clock-counter-clockwise"), `Closed sessions (${closed.length})`),
          h("div", { class: "task-list" }, closed.map(sessionRow))) : null);
    } catch (error) {
      if (alive()) sessionsEl.replaceChildren(errorBox(error, load));
    }
  }
  function sessionRow(s) {
    return h("a", { class: `pipeline-row${s.waiting ? " waiting" : ""}`, href: `#work/${s.id}` },
      h("span", { class: "team-icon small" }, icon("hexagon")),
      h("span", { class: "pipeline-main" }, h("span", { class: "task-title" }, s.title),
        h("span", { class: "task-meta" }, s.area ? areaName(pid, s.area) : "Whole project", ` · started ${ago(s.created_at)}`,
          s.pipeline ? [" · ", h("span", {}, s.pipeline.title)] : "")),
      s.unrestricted ? unrestrictedPill() : null,
      s.waiting ? h("span", { class: "pill needs_you" }, "Plan waiting for you") : s.pipeline ? pipelinePill(s.pipeline.status) : null,
      s.status === "open" ? pill(s.live) : h("span", { class: "pill stopped" }, "Closed"));
  }
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 4000);
  load();
  return () => clearInterval(timer);
}

function servicesPanel(root, pid, project, alive) {
  const list = project.services || [];
  const commands = project.commands || {};
  const body = h("div", {}, skeleton(Math.max(1, Math.min(list.length, 4))));
  const checkedSlot = h("span");
  root.append(
    h("div", { class: "controls-line" },
      h("p", { class: "muted", style: "flex:1" }, "What this project runs: local servers, containers and live sites. Agents get these addresses with their tasks. ", checkedSlot),
      commands.start_all ? h("button", { class: "btn", onclick: (e) => runCommand(e, "start_all", "Start everything?") }, icon("play"), "Start All") : null,
      commands.stop_all ? h("button", { class: "btn ghost danger", onclick: (e) => runCommand(e, "stop_all", "Stop everything?") }, icon("stop"), "Stop All") : null,
      h("a", { class: "btn ghost", href: `#project/${pid}/settings` }, icon("gear-six"), "Edit")),
    body);
  if (!list.length) {
    body.replaceChildren(h("div", { class: "empty" }, h("strong", {}, "No services yet."), h("a", { href: `#project/${pid}/settings` }, "Add them in Settings.")));
    return () => {};
  }
  let statuses = {};
  async function runCommand(e, which, title) {
    const ok = await confirmDialog({ title, body: `Runs \`${commands[which]}\` in ${project.cwd}.`, confirm: "Run" });
    if (ok) runAction(e.currentTarget, () => bridge.call("project_command", { project_id: pid, which })).then(() => setTimeout(load, 2500));
  }
  function render() {
    fill(body, h("div", { class: "rows svc-rows" }, list.map((s) => {
      const st = statuses[s.id] || { status: "checking", detail: "checking…" };
      const address = s.url || (s.port ? `http://localhost:${s.port}` : "");
      return h("div", { class: "row" },
        h("span", { class: `svc-dot ${st.status}`, title: st.detail, "aria-label": st.status }),
        h("div", { class: "row-main" },
          h("span", { class: "row-title" }, s.name || s.id, s.area ? h("span", { class: "badge", style: "margin-left:8px" }, areaName(pid, s.area)) : null),
          h("span", { class: "row-sub" }, st.detail, address ? [" · ", h("button", { type: "button", class: "linklike mono", onclick: () => openUrl(address) }, address)] : "",
            s.container ? ` · container ${s.container}` : "")),
        h("div", { class: "row-actions" },
          s.start && st.status !== "up" ? h("button", { class: "btn small", onclick: (e) => runAction(e.currentTarget, () => bridge.call("service_action", { project_id: pid, service_id: s.id, action: "start" })).then(() => setTimeout(load, 2500)) }, icon("play"), "Start") : null,
          s.stop && st.status === "up" ? h("button", { class: "btn small ghost", onclick: (e) => runAction(e.currentTarget, () => bridge.call("service_action", { project_id: pid, service_id: s.id, action: "stop" })).then(() => setTimeout(load, 2000)) }, icon("stop"), "Stop") : null,
          s.logs || s.start || s.stop ? h("button", { class: "btn small ghost", onclick: () => showLogs(s) }, icon("scroll"), "Logs") : null));
    })));
  }
  async function showLogs(s) {
    try {
      const log = await bridge.call("service_logs", { project_id: pid, service_id: s.id });
      openDialog((close) => h("form", { method: "dialog", onsubmit: (e) => { e.preventDefault(); close(); } },
        h("h2", {}, `${s.name} logs`), h("p", { class: "desc mono" }, log.path || "No log yet."),
        h("pre", { class: "screen log" }, log.text || "(empty)"),
        h("div", { class: "dialog-actions" }, h("button", { type: "submit", class: "btn primary" }, "Close"))), { wide: true });
    } catch (error) { toast(error.message, "error"); }
  }
  async function load() {
    try {
      statuses = await bridge.call("services_status", { project_id: pid });
      if (!alive()) return;
      fill(checkedSlot, freshness(statuses._cache, "Checked"));
      render();
    } catch (error) {
      if (alive()) body.replaceChildren(errorBox(error, load));
    }
  }
  render();
  load();
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 6000);
  return () => clearInterval(timer);
}

const gb = (bytes) => `${(bytes / 1e9).toFixed(1)} GB`;

/* How fresh cached data is: "Updated 2 min ago · refreshing…". */
function freshness(meta, label = "Updated") {
  if (!meta?.fetched_at) return null;
  return h("span", { class: `freshness${meta.error ? " error" : ""}`, title: meta.error ? `Couldn't refresh: ${meta.error}` : null },
    meta.refreshing ? icon("arrows-clockwise") : null,
    `${label} ${ago(meta.fetched_at)}`, meta.refreshing ? " · refreshing…" : meta.error ? " · couldn't refresh" : "");
}
const REFRESH_CHECK_MS = 4000;
/* A project's servers; an older single `server` setting counts as production. */
const projectServers = (project) => (project.servers
  || (project.server?.ssh ? [{ id: "production", role: "production", ...project.server }] : [])).filter((s) => s.ssh);

const serverChoice = {};

/* Open a server's site; the backend starts a stopped local server and waits for it first. */
function openSite(button, pid, srv) {
  if (srv.start) toast(`Opening ${srv.site}. If the local server is stopped it starts first, which can take a minute.`);
  return runAction(button, () => bridge.call("server_open_site", { project_id: pid, server_id: srv.id }));
}

function serverPanel(root, pid, project, alive) {
  const list = projectServers(project);
  let current = list.find((s) => s.id === serverChoice[pid]) || list[0];
  let cleanup = null;
  const picker = h("div", { class: "controls-line" });
  const holder = h("div");
  root.append(list.length > 1 ? picker : "", holder);
  function renderPicker() {
    fill(picker, h("div", { class: "segmented", role: "radiogroup", "aria-label": "Server" }, list.map((s) =>
      h("button", { type: "button", role: "radio", "aria-checked": String(s.id === current.id),
        onclick: () => { current = s; serverChoice[pid] = s.id; renderPicker(); show(); } },
        icon(s.role === "local" ? "laptop" : "cloud"), s.name || s.id))));
  }
  function show() {
    if (cleanup) cleanup();
    holder.replaceChildren();
    cleanup = oneServer(holder, pid, project, current, alive);
  }
  renderPicker();
  show();
  return () => { if (cleanup) cleanup(); };
}

function oneServer(root, pid, project, srv, alive) {
  const sid = srv.id;
  const local = srv.role === "local";
  let data = null;
  let terminal = null;
  const actions = h("div", { class: "controls-line" });
  const termBar = h("div");
  const body = h("div", {}, skeleton(4));
  root.append(actions, termBar, body);

  terminalEnded = (id) => {
    if (terminal?.id !== id) return;
    terminal = null;
    renderTermBar();
    load(true);
  };
  function showTerminal(id, label) {
    terminal = { id, label };
    bridge.call("show_terminal", { agent_id: id }).catch(() => {});
    renderTermBar();
  }
  function renderTermBar() {
    fill(termBar, terminal ? h("div", { class: "now" }, icon("terminal-window"),
      h("div", { style: "flex:1" }, h("b", {}, terminal.label), h("span", { class: "muted" }, " · in the terminal on the right. Type into it directly.")),
      h("button", { class: "btn small ghost", onclick: () => { terminal = null; bridge.call("hide_terminal").catch(() => {}); renderTermBar(); } }, "Hide")) : null);
  }

  function renderActions() {
    const blocked = !local && data?.git?.changed?.length > 0;
    fill(actions,
      h("p", { class: "muted", style: "flex:1" }, srv.name ? `${srv.name} · ` : "", h("span", { class: "mono" }, `ssh ${srv.ssh}`), srv.path ? h("span", { class: "mono" }, `:${srv.path}`) : null),
      srv.site ? h("button", { class: "btn", onclick: (e) => openSite(e.currentTarget, pid, srv) }, icon("globe"), "Open Site") : null,
      h("button", { class: "btn", onclick: (e) => runAction(e.currentTarget, async () => {
        const res = await bridge.call("server_connect", { project_id: pid, server_id: sid });
        setTimeout(() => showTerminal(res.terminal, `SSH to ${srv.ssh}`), 400);
        return res;
      }) }, icon("terminal-window"), "Connect"),
      srv.deploy ? h("button", { class: "btn primary", disabled: blocked, title: blocked ? "The server has uncommitted changes. Commit them first." : null, onclick: deploy }, icon("rocket-launch"), "Deploy") : null,
      h("button", { class: "btn ghost icon", "aria-label": "Refresh", title: "Refresh", onclick: () => load(true) }, icon("arrow-clockwise")));
  }

  async function deploy(e) {
    const button = e.currentTarget;
    let preview;
    try { preview = await bridge.call("server_deploy_preview", { project_id: pid, server_id: sid }); } catch (error) { toast(error.message, "error"); return; }
    const commits = data?.compare?.to_deploy || [];
    const ok = await openDialog((close) => h("form", { onsubmit: (ev) => { ev.preventDefault(); close(true); } },
      h("h2", {}, `Deploy ${project.name} to ${srv.ssh}?`),
      h("p", { class: "desc" }, local ? "Rebuilds and restarts the local server's services from your working copy, uncommitted changes included."
        : commits.length ? `${commits.length} commit${commits.length === 1 ? "" : "s"} on GitHub will go live:` : "The server already has everything on GitHub. This rebuilds and restarts the services anyway."),
      !local && commits.length ? h("div", { class: "rows" }, commits.map((c) => h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", {}, h("span", { class: "mono faint" }, c.sha), " ", c.subject))))) : null,
      !local && data?.compare?.local_unpushed?.length ? h("p", { class: "error" }, `${data.compare.local_unpushed.length} local commit(s) aren't pushed, so they won't deploy.`) : null,
      h("details", {}, h("summary", {}, "What runs on the server"), h("pre", { class: "screen" }, preview.script)),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, icon("rocket-launch"), "Deploy"))), { wide: true });
    if (!ok) return;
    await runAction(button, async () => {
      const res = await bridge.call("server_deploy", { project_id: pid, server_id: sid });
      setTimeout(() => showTerminal(res.terminal, `Deploying to ${srv.ssh}`), 400);
      return res;
    });
  }

  function render() {
    const d = data;
    const diskPct = Math.round((d.disk.used / d.disk.total) * 100);
    const memUsed = d.memory.total - d.memory.available;
    const running = d.containers.filter((c) => c.State === "running").length;
    const cache = d.docker_df.find((x) => x.Type === "Build Cache");
    const c = d.compare;
    const g = d.git;
    fill(body,
      h("div", { class: "stats" },
        stat(`${Math.floor(d.uptime / 86400)}d`, "up"),
        stat(d.load[0].toFixed(2), "load (1 min)"),
        stat(`${Math.round((memUsed / d.memory.total) * 100)}%`, `memory of ${gb(d.memory.total)}`),
        stat(`${diskPct}%`, `disk, ${gb(d.disk.free)} free`, diskPct >= 80),
        stat(`${running}/${d.containers.length}`, "containers running", running < d.containers.length),
        stat(d.failed_units.length, "failed services", true)),
      diskPct >= 80 && cache ? h("div", { class: "callout" }, icon("warning-circle", "ph-fill"),
        h("p", {}, `Disk is ${diskPct}% full. Docker's build cache holds ${cache.Reclaimable} that can be freed with `, h("span", { class: "mono" }, "docker builder prune"), ".")) : null,
      d.failed_units.length ? h("div", { class: "error-box" }, `Failed: ${d.failed_units.join(", ")}`) : null,

      local ? h("div", { class: "callout calm" }, icon("laptop"),
        h("p", {}, "This server runs your working copy (mounted at ", h("span", { class: "mono" }, srv.path), "): edits apply at once. Deploy rebuilds its containers and restarts services. Its data started as a snapshot of production; Lightning uses FakeWallet and emails aren't sent.")) : null,
      g && !local ? h("section", { class: "section" },
        h("div", { class: "section-head" }, h("h2", {}, "Code on the Server")),
        h("p", {}, h("span", { class: "mono" }, `${g.branch} @ ${g.head.slice(0, 7)}`), ` · ${g.subject} · ${ago(Date.parse(g.date) / 1000)}`),
        g.changed.length ? h("div", { class: "error-box", style: "margin-top:10px" },
          h("p", {}, h("b", {}, `${g.changed.length} uncommitted change(s) to tracked files on the server. `), "Deploying is blocked so they aren't overwritten. Bring them into the repository first."),
          h("pre", { class: "mono" }, g.changed.join("\n"))) : null,
        c?.server_only ? h("p", { class: "error" }, "The server is on a commit your local repository doesn't have. Pull or fetch locally.") : null,
        c ? h("div", { class: "code-state" },
          c.to_deploy.length ? [h("p", {}, icon("arrow-up"), h("b", {}, `${c.to_deploy.length} commit${c.to_deploy.length === 1 ? "" : "s"} on GitHub not deployed yet`)),
            h("ul", {}, c.to_deploy.map((x) => h("li", {}, h("span", { class: "mono faint" }, x.sha), " ", x.subject)))]
            : !c.server_only ? h("p", {}, icon("check"), "Deployed code matches GitHub.") : null,
          c.local_unpushed.length ? h("p", { class: "muted" }, icon("upload-simple"), `${c.local_unpushed.length} local commit(s) not pushed to GitHub.`) : null) : null,
        g.untracked.length ? h("details", { class: "fold" }, h("summary", {}, `Untracked files on the server (${g.untracked.length})`, h("span", { class: "faint" }, "not in git: data, backups, caches")),
          h("pre", { class: "mono fold-pre" }, g.untracked.join("\n"))) : null) : null,

      d.drift.length ? h("section", { class: "section" },
        h("div", { class: "section-head" }, h("h2", {}, "Server vs Your Repository")),
        d.drift.map((w) => h("div", { class: "drift" },
          h("p", {}, h("b", {}, w.name), " ", h("span", { class: "mono faint" }, w.remote), ": ",
            `${w.server_count} on the server, ${w.both} also in your repository (${w.local.join(", ")}).`),
          w.server_only.length ? h("details", { class: "fold" }, h("summary", {}, icon("warning-circle"), `${w.server_only.length} only on the server`, h("span", { class: "faint" }, "not in git: production is ahead here")),
            h("div", { class: "chips fold-body" }, w.server_only.map((n) => h("span", { class: "chip" }, n)))) : null,
          w.local_only.length ? h("details", { class: "fold" }, h("summary", {}, `${w.local_only.length} only in your repository`),
            h("div", { class: "chips fold-body" }, w.local_only.map((n) => h("span", { class: "chip" }, n)))) : null))) : null,

      h("section", { class: "section" },
        h("div", { class: "section-head" }, h("h2", {}, "Containers")),
        h("div", { class: "rows" }, d.containers.map((ct) => h("div", { class: "row" },
          h("span", { class: `svc-dot ${ct.State === "running" ? "up" : "down"}` }),
          h("div", { class: "row-main" }, h("span", { class: "row-title" }, ct.Names), h("span", { class: "row-sub" }, `${ct.Status} · ${ct.Image}`))))),
        h("p", { class: "faint", style: "font-size:12.5px;margin-top:8px" }, `${d.running_units} systemd services running on ${d.host} · `, freshness(d.cache, "checked"))));
  }

  let recheck = null;
  async function load(refresh = false) {
    if (refresh) body.replaceChildren(skeleton(4));
    clearTimeout(recheck);
    try {
      data = await bridge.call("server_status", { project_id: pid, server_id: sid, refresh });
      if (!alive()) return;
      renderActions();
      render();
      if (data.cache?.refreshing) recheck = setTimeout(() => { if (alive()) load(); }, REFRESH_CHECK_MS);
    } catch (error) {
      if (alive()) { renderActions(); body.replaceChildren(errorBox(error, () => load(true))); }
    }
  }
  renderActions();
  load();
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 30000);
  return () => { clearInterval(timer); clearTimeout(recheck); terminalEnded = null; bridge.call("hide_terminal").catch(() => {}); };
}

function githubPanel(root, pid, project, alive) {
  const boards = project.github?.boards || [];
  let view = boards.length ? `board:0` : "repos";
  let showDone = false;
  let search = "";
  let sprint = "";
  let person = "";
  const seg = h("div", { class: "segmented", role: "radiogroup", "aria-label": "GitHub view" });
  const filters = h("div", { class: "controls-line" });
  const body = h("div");
  root.append(h("div", { class: "controls-line" }, seg, h("span", { style: "flex:1" }),
    h("button", { class: "btn ghost", onclick: () => load(true) }, icon("arrow-clockwise"), "Refresh")), filters, body);

  function renderSeg() {
    fill(seg, [...boards.map((b, i) => [`board:${i}`, b.name || `Board #${b.number}`]), ["repos", "Pull Requests & Issues"]].map(([key, label]) =>
      h("button", { type: "button", role: "radio", "aria-checked": String(key === view), onclick: () => { view = key; renderSeg(); load(); } }, label)));
  }

  let board = null;
  let recheck = null;
  const stamp = h("span");
  /* quiet: a background refresh finished; update in place without a loading state. */
  async function load(refresh = false, quiet = false) {
    const shown = view;
    clearTimeout(recheck);
    if (!quiet) { body.replaceChildren(skeleton(4)); filters.replaceChildren(); }
    try {
      let refreshing = false;
      if (view === "repos") {
        const repos = await bridge.call("github_repos", { project_id: pid, refresh });
        if (!alive() || shown !== view) return;
        renderRepos(repos);
        refreshing = repos.some((r) => r.cache?.refreshing);
      } else {
        const b = boards[Number(view.split(":")[1])];
        board = await bridge.call("github_board", { project_id: pid, owner: b.owner, number: b.number, refresh });
        if (!alive() || shown !== view) return;
        if (!quiet) renderFilters();
        fill(stamp, freshness(board.cache, "Synced"));
        if (board.cache && !board.cache.refreshing) stamp.title = `${board.last_changed ?? 0} item(s) changed in the last sync`;
        renderBoard();
        refreshing = board.cache?.refreshing;
      }
      if (refreshing) recheck = setTimeout(() => { if (alive()) load(false, true); }, REFRESH_CHECK_MS);
    } catch (error) {
      if (alive() && !quiet) body.replaceChildren(errorBox(error, () => load(true)));
    }
  }

  function renderFilters() {
    const sprints = [...new Set(board.items.map((i) => i.sprint).filter(Boolean))].sort();
    const people = [...new Set(board.items.flatMap((i) => i.assignees))].sort();
    fill(filters,
      h("label", { class: "search" }, icon("magnifying-glass"), h("input", { class: "input", type: "search", placeholder: "Filter items…", value: search,
        "aria-label": "Filter items", oninput: (e) => { search = e.target.value; renderBoard(); } })),
      sprints.length ? h("select", { class: "select", "aria-label": "Sprint", onchange: (e) => { sprint = e.target.value; renderBoard(); } },
        h("option", { value: "" }, "All sprints"), sprints.map((s) => h("option", { value: s, selected: s === sprint }, s))) : null,
      people.length ? h("select", { class: "select", "aria-label": "Assignee", onchange: (e) => { person = e.target.value; renderBoard(); } },
        h("option", { value: "" }, "Everyone"), people.map((p) => h("option", { value: p, selected: p === person }, p))) : null,
      h("label", { class: "switch-row" }, h("button", { class: "switch", role: "switch", type: "button", "aria-checked": String(showDone),
        onclick: (e) => { showDone = !showDone; e.currentTarget.setAttribute("aria-checked", String(showDone)); renderBoard(); } }), "Show Done"),
      h("button", { type: "button", class: "linklike", onclick: () => openUrl(board.url) }, "Open on GitHub"),
      stamp);
  }

  function renderBoard() {
    const words = search.toLowerCase().split(/\s+/).filter(Boolean);
    const items = board.items.filter((i) => (!sprint || i.sprint === sprint) && (!person || i.assignees.includes(person))
      && words.every((w) => `${i.title} ${i.repo} ${i.labels.join(" ")}`.toLowerCase().includes(w)));
    const columns = [...board.statuses, ...new Set(items.map((i) => i.status).filter((s) => !board.statuses.includes(s)))]
      .filter((c) => showDone || c.toLowerCase() !== "done");
    fill(body, h("div", { class: "kanban" }, columns.map((col) => {
      const mine = items.filter((i) => i.status === col);
      return h("section", { class: "kan-col", "aria-label": col },
        h("h3", {}, col, h("span", { class: "faint" }, mine.length)),
        h("div", { class: "kan-items" }, mine.map((i) => h("button", { type: "button", class: "kan-card", onclick: () => itemDialog(i) },
          h("span", { class: "kan-title" }, i.title),
          h("span", { class: "kan-meta" },
            i.number ? h("span", { class: "mono" }, `${i.repo.split("/")[1] || i.repo}#${i.number}`) : h("span", {}, "Draft"),
            i.priority ? h("span", { class: `prio ${i.priority.toLowerCase()}` }, i.priority) : null,
            i.assignees.length ? h("span", {}, i.assignees.join(", ")) : null)))));
    })));
  }

  function renderRepos(repos) {
    fill(body, repos.length ? repos.map((r) => h("section", { class: "repo-block" },
      h("div", { class: "section-head" }, icon("github-logo"), h("h2", {}, r.repo), freshness(r.cache), h("span", { class: "spacer" }),
        h("button", { type: "button", class: "linklike", onclick: () => openUrl(githubUrl(r.repo)) }, "Open on GitHub")),
      r.error ? h("div", { class: "error-box" }, r.error) : [
        h("h3", { class: "label" }, `Pull requests (${r.prs.length})`),
        r.prs.length ? h("div", { class: "rows" }, r.prs.map((p) => h("div", { class: "row" }, h("div", { class: "row-main" },
          h("button", { type: "button", class: "linklike row-title", onclick: () => openUrl(p.url) }, `#${p.number} ${p.title}`),
          h("span", { class: "row-sub" }, `${p.headRefName} · ${p.author} · ${ago(Date.parse(p.updatedAt) / 1000)}${p.isDraft ? " · draft" : ""}`)),
          h("div", { class: "row-actions" }, h("button", { class: "btn small ghost", onclick: () => mergeDialog(r.repo, p.number) }, icon("git-merge"), "Merge…"))))) : h("p", { class: "muted" }, "None open."),
        h("h3", { class: "label", style: "margin-top:14px" }, `Issues (${r.issues.length})`),
        r.issues.length ? h("div", { class: "rows" }, r.issues.map((i) => h("div", { class: "row" },
          h("div", { class: "row-main" },
            h("button", { type: "button", class: "linklike row-title", onclick: () => openUrl(i.url) }, `#${i.number} ${i.title}`),
            h("span", { class: "row-sub" }, [i.labels.join(", "), i.assignees.join(", "), ago(Date.parse(i.updatedAt) / 1000)].filter(Boolean).join(" · "))),
          h("div", { class: "row-actions" }, h("button", { class: "btn small", onclick: () => workOn(pid, project, { title: i.title, url: i.url, repo: r.repo, body: "" }) }, icon("hexagon"), "Work on This"))))) : h("p", { class: "muted" }, "None open."),
        r.runs.length ? [h("h3", { class: "label", style: "margin-top:14px" }, "Recent CI runs"),
          h("div", { class: "rows" }, r.runs.map((run) => h("div", { class: "row" },
            h("span", { class: `svc-dot ${run.conclusion === "success" ? "up" : run.conclusion === "failure" ? "down" : "unknown"}` }),
            h("div", { class: "row-main" }, h("button", { type: "button", class: "linklike row-title", onclick: () => openUrl(run.url) }, run.displayTitle),
              h("span", { class: "row-sub" }, `${run.workflowName} · ${run.headBranch} · ${run.conclusion || run.status}`)))))] : null,
      ])) : h("div", { class: "empty" }, h("strong", {}, "No repositories set."), h("a", { href: `#project/${pid}/settings` }, "Add them in Settings.")));
  }

  function itemDialog(i) {
    openDialog((close) => h("form", { method: "dialog", onsubmit: (e) => { e.preventDefault(); close(); } },
      h("h2", {}, i.title),
      h("div", { class: "chips" }, h("span", { class: "chip" }, i.status),
        i.number ? h("span", { class: "chip" }, `${i.repo}#${i.number}`) : null,
        i.priority ? h("span", { class: "chip" }, i.priority) : null, i.sprint ? h("span", { class: "chip" }, i.sprint) : null,
        i.assignees.map((a) => h("span", { class: "chip" }, `@${a}`)), i.labels.map((l) => h("span", { class: "chip" }, l))),
      i.body ? h("div", { class: "item-body prewrap" }, i.body) : h("p", { class: "muted" }, "No description."),
      h("div", { class: "dialog-actions" },
        i.url ? h("button", { type: "button", class: "btn ghost", onclick: () => openUrl(i.url) }, icon("arrow-square-out"), "Open on GitHub") : null,
        h("button", { type: "button", class: "btn primary", onclick: () => { close(); workOn(pid, project, i); } }, icon("hexagon"), "Work on This"))), { wide: true });
  }

  renderSeg();
  load();
  return () => clearTimeout(recheck);
}

/* Open a work session on a GitHub item, in the section its repository belongs to. */
/* Merging under the project's policy (pull_requests.py): shows what passes before it merges. */
async function mergeDialog(repo, number) {
  let s;
  try { s = await bridge.call("pr_check", { repo, number }); } catch (error) { toast(error.message, "error"); return; }
  const ok = await openDialog((close) => h("div", {}, h("h2", {}, `Merge ${repo}#${number}?`),
    h("p", { class: "desc" }, s.title),
    h("ul", { class: "criteria" },
      h("li", {}, s.failing.length ? `Checks not green: ${s.failing.join(", ")}` : `${s.checks} checks green`),
      h("li", {}, `GitHub review: ${s.review_decision || "none"}`),
      s.reviews.map((r) => h("li", {}, `${r.reviewer}: ${r.verdict} on ${r.head.slice(0, 10)}${r.head === s.head ? "" : " (an older commit)"}`))),
    s.allowed ? h("p", { class: "muted" }, "Everything the project's merge policy asks for passes.")
      : h("p", { class: "error" }, `It can't be merged yet: ${s.reasons.join("; ")}.`),
    h("div", { class: "dialog-actions" }, h("button", { class: "btn ghost", onclick: () => close(false) }, "Cancel"),
      h("button", { class: "btn ghost", onclick: () => { close(false); openUrl(s.url); } }, "Open on GitHub"),
      h("button", { class: "btn primary", disabled: !s.allowed, onclick: () => close(true) }, icon("git-merge"), "Merge"))));
  if (ok) runAction(null, () => bridge.call("merge_pr", { repo, number }));
}

async function workOn(pid, project, item) {
  const areas = Object.entries(project.areas || {});
  const guess = areas.find(([, a]) => a.repo && item.repo && a.repo.toLowerCase().endsWith(item.repo.toLowerCase().split("/").pop()))?.[0] || "";
  const area = await openDialog((close) => {
    const select = h("select", { id: "wo-area", class: "select" }, h("option", { value: "" }, "Whole project"),
      areas.map(([id, a]) => h("option", { value: id, selected: id === guess }, a.name || id)));
    return h("form", { onsubmit: (e) => { e.preventDefault(); close({ area: select.value }); } },
      h("h2", {}, "Work on This"), h("p", { class: "desc" }, item.title),
      h("div", { class: "field" }, h("label", { for: "wo-area" }, "Section"), select),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, icon("play"), "Start Session")));
  });
  if (!area) return;
  const text = `Let's work on this GitHub item: ${item.title}\n${item.url || ""}\n\n${(item.body || "").slice(0, 4000)}`.trim();
  try {
    const res = await bridge.call("open_workspace", { project: pid, area: area.area, text });
    await pollLive();
    location.hash = `#work/${res.workspace}`;
  } catch (error) { toast(error.message, "error"); }
}

/* ---------------------------------------------------------------- project settings */

function projectSettings(root, alive, pid) {
  let draft = structuredClone(state.config.projects[pid]);
  const saved = () => state.config.projects[pid];
  const dirty = () => JSON.stringify(draft) !== JSON.stringify(saved());
  const bar = h("div", { class: "controls-line sticky-bar" });
  const body = h("div");
  const secretsEl = h("div");
  const openFolds = new Set(["basics"]);
  root.append(
    h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, h("a", { href: "#projects" }, "Projects"), icon("caret-right"),
      h("a", { href: `#project/${pid}` }, draft.name || pid), icon("caret-right"), h("span", {}, "Settings")),
    pageHead(`${draft.name || pid} Settings`, "Everything tasks know about this project, and what it's connected to."), bar, body, secretsEl);

  /* Secrets (vault.py) save on their own: values go to the keyring and never come back. */
  async function loadSecrets() {
    let rows;
    try { rows = await bridge.call("secrets", { project: pid }); } catch { return; }
    if (!alive()) return;
    const name = h("input", { class: "input mono", placeholder: "stripe", "aria-label": "Name" });
    const env = h("input", { class: "input mono", placeholder: "STRIPE_KEY", "aria-label": "Variable" });
    const value = h("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: "value", "aria-label": "Value" });
    fill(secretsEl, h("details", { class: "fold" },
      h("summary", {}, icon("key"), `Secrets${rows.length ? ` (${rows.length})` : ""}`, h("span", { class: "faint" }, "kept in your keyring, given to this project's agents as variables")),
      h("div", { class: "fold-body" },
        rows.length ? h("div", { class: "rows" }, rows.map((r) => h("div", { class: "row" },
          h("div", { class: "row-main" }, h("span", { class: "row-title mono" }, `$${r.env}`), h("span", { class: "row-sub" }, `${r.name} · saved ${ago(r.updated_at)}`)),
          h("div", { class: "row-actions" }, h("button", { class: "btn small ghost danger", onclick: async (e) => {
            if (await confirmDialog({ title: `Remove ${r.name}?`, body: "Deletes it from your keyring. Agents started after this won't get it.", confirm: "Remove", danger: true }))
              runAction(e.currentTarget, () => bridge.call("remove_secret", { name: r.name, project: pid }), "Removed.").then(loadSecrets);
          } }, icon("trash")))))) : h("p", { class: "faint" }, "None. Values go to your desktop keyring, never into Lago's files, and nothing shows them again once saved."),
        h("form", { class: "controls-line", style: "margin-top:10px", onsubmit: async (e) => {
            e.preventDefault();
            const ok = await runAction(e.submitter, () => bridge.call("save_secret", { name: name.value.trim(), env: env.value.trim(), value: value.value, project: pid }), "Saved to your keyring.");
            value.value = "";
            if (ok) loadSecrets();
          } }, name, env, value, h("button", { class: "btn small primary", type: "submit" }, icon("lock-key"), "Save Secret")))));
  }
  loadSecrets();

  const update = (fn) => { fn(); render(); };
  const edit = (fn) => { fn(); renderBar(); };

  function renderBar() {
    fill(bar,
      h("a", { class: "btn ghost", href: `#project/${pid}` }, icon("arrow-left"), "Back"),
      h("span", { style: "flex:1" }),
      dirty() ? h("span", { class: "muted" }, "Unsaved changes") : null,
      dirty() ? h("button", { class: "btn ghost", onclick: () => { draft = structuredClone(saved()); render(); } }, "Discard") : null,
      h("button", { class: "btn primary", disabled: !dirty(), onclick: save }, icon("floppy-disk"), "Save Changes"));
  }

  async function save(e) {
    const projects = { ...structuredClone(state.config.projects), [pid]: structuredClone(draft) };
    if (await runAction(e.currentTarget, () => bridge.call("save_projects", { projects }))) {
      state.config.projects = projects;
      render();
    }
  }

  const field = (label, control, help) => h("div", { class: "field" }, h("label", { for: control.id }, label), control, help ? h("p", { class: "help" }, help) : null);
  const input = (obj, key, id, { mono = false, placeholder = "", type = "text" } = {}) => h("input", { id, type, class: `input${mono ? " mono" : ""}`, value: obj[key] ?? "", placeholder, spellcheck: "false",
    oninput: (e) => edit(() => {
      const v = e.target.value.trim();
      if (type === "number") { if (v) obj[key] = Number(v); else delete obj[key]; } else obj[key] = v;
    }) });
  const fold = (key, title, count, ...content) => h("details", { class: "fold settings-fold", open: openFolds.has(key) || undefined,
      ontoggle: (e) => { if (e.currentTarget.open) openFolds.add(key); else openFolds.delete(key); } },
    h("summary", {}, title, count !== null ? h("span", { class: "faint" }, count) : null), h("div", { class: "fold-body" }, content));
  const unknownAgents = (ids) => {
    const known = new Set(state.catalog.agents.map((a) => a.id));
    const missing = ids.filter((a) => !known.has(a));
    return missing.length ? h("p", { class: "error" }, `Not in the catalog: ${missing.join(", ")}`) : null;
  };
  const removeButton = (label, fn) => h("button", { type: "button", class: "btn icon small ghost danger", "aria-label": label, title: label, onclick: () => update(fn) }, icon("trash"));

  function render() {
    renderBar();
    const p = draft;
    p.areas ||= {}; p.services ||= []; p.links ||= []; p.commands ||= {}; p.github ||= { repos: [], boards: [] };
    p.github.repos ||= []; p.github.boards ||= [];
    const areaOptions = (current) => [h("option", { value: "" }, "—"), ...Object.entries(p.areas).map(([id, a]) => h("option", { value: id, selected: id === current }, a.name || id))];
    fill(body,
      fold("basics", "Basics", null, h("div", { class: "team-form" },
        field("Name", input(p, "name", "s-name")),
        field("Folder", input(p, "cwd", "s-cwd", { mono: true, placeholder: "~/projects/…" })),
        field("Repository", input(p, "repo", "s-repo", { mono: true, placeholder: "github.com/owner/repo" })),
        field("Main branch", input(p, "branch", "s-branch", { mono: true, placeholder: "main" })),
        field("Stack", input(p, "stack", "s-stack", { placeholder: "e.g. FastAPI, React + Vite" })),
        field("Icon", h("select", { id: "s-icon", class: "select", onchange: (e) => update(() => { p.icon = e.target.value; }) },
          TEAM_ICONS.map((i) => h("option", { value: i, selected: i === p.icon }, humanize(i)))))),
        h("div", { class: "team-form wide" },
          field("Description", h("textarea", { id: "s-desc", class: "input", rows: 3, placeholder: "What it is, who it's for, what matters right now…",
            oninput: (e) => edit(() => { p.description = e.target.value.trim(); }) }, p.description || "")),
          field("Read first", h("textarea", { id: "s-context", class: "input mono", rows: 3, placeholder: "CLAUDE.md\ndocs/architecture.md", spellcheck: "false",
            oninput: (e) => edit(() => { p.context = lines(e.target.value); }) }, (p.context || []).join("\n")), "One path per line. Every agent reads these first."))),

      fold("areas", "Sections", Object.keys(p.areas).length,
        h("p", { class: "muted" }, "The parts of the project you pick from when you start work. Agents are told which paths they work in."),
        Object.keys(p.areas).length ? h("div", { class: "table-wrap" }, h("table", { class: "member-table" },
          h("colgroup", {}, h("col", { style: "width:18%" }), h("col", { style: "width:20%" }), h("col", { style: "width:22%" }), h("col", { style: "width:20%" }), h("col", { style: "width:20%" }), h("col", { style: "width:44px" })),
          h("thead", {}, h("tr", {}, ["Name", "Paths", "Description", "Repository", "Preferred agents", ""].map((c) => h("th", { style: "padding:10px 8px" }, c)))),
          h("tbody", {}, Object.entries(p.areas).map(([aid, a]) => h("tr", {},
            h("td", {}, h("input", { class: "input", value: a.name || "", "aria-label": "Section name", oninput: (e) => edit(() => { a.name = e.target.value.trim(); }) })),
            h("td", {}, h("input", { class: "input mono", value: (a.paths || []).join(", "), "aria-label": "Paths", placeholder: "src/api/", spellcheck: "false", oninput: (e) => edit(() => { a.paths = csv(e.target.value); }) })),
            h("td", {}, h("input", { class: "input", value: a.description || "", "aria-label": "Description", oninput: (e) => edit(() => { a.description = e.target.value.trim(); }) })),
            h("td", {}, h("input", { class: "input mono", value: a.repo || "", "aria-label": "Repository", placeholder: "owner/repo", spellcheck: "false", oninput: (e) => edit(() => { a.repo = e.target.value.trim(); }) })),
            h("td", {}, h("input", { class: "input mono", value: (a.agents || []).join(", "), "aria-label": "Preferred agents", spellcheck: "false", onchange: (e) => update(() => { a.agents = csv(e.target.value); }) }), unknownAgents(a.agents || [])),
            h("td", {}, removeButton(`Remove ${a.name || aid}`, () => { delete p.areas[aid]; }))))))) : null,
        h("button", { class: "btn small", onclick: async () => {
          const name = await promptDialog({ title: "New Section", label: "Name", placeholder: "e.g. Frontend", confirm: "Add" });
          if (name) update(() => { p.areas[uniqueId(name, p.areas)] = { name, paths: [], description: "", agents: [] }; });
        } }, icon("plus"), "Add Section")),

      fold("services", "Services", p.services.length,
        h("p", { class: "muted" }, "Local servers, containers and live sites. Lago checks them with a port, a health URL or a Docker container, and runs the start/stop commands in the project folder."),
        p.services.map((s) => h("details", { class: "service-edit" },
          h("summary", {}, h("span", { class: "row-title" }, s.name || s.id), h("span", { class: "faint mono" }, s.url || (s.port ? `:${s.port}` : s.container || ""))),
          h("div", { class: "team-form" },
            field("Name", input(s, "name", `sv-${s.id}-name`)),
            field("Section", h("select", { id: `sv-${s.id}-area`, class: "select", onchange: (e) => update(() => { if (e.target.value) s.area = e.target.value; else delete s.area; }) }, areaOptions(s.area))),
            field("Port", input(s, "port", `sv-${s.id}-port`, { mono: true, type: "number", placeholder: "8005" })),
            field("Address", input(s, "url", `sv-${s.id}-url`, { mono: true, placeholder: "http://localhost:8005" })),
            field("Health check URL", input(s, "health", `sv-${s.id}-health`, { mono: true, placeholder: "http://localhost:8005/health" })),
            field("Docker container", input(s, "container", `sv-${s.id}-container`, { mono: true, placeholder: "redis-dev" })),
            field("Start command", input(s, "start", `sv-${s.id}-start`, { mono: true, placeholder: "docker start redis-dev" })),
            field("Stop command", input(s, "stop", `sv-${s.id}-stop`, { mono: true, placeholder: "docker stop redis-dev" })),
            field("Log file", input(s, "logs", `sv-${s.id}-logs`, { mono: true, placeholder: "/tmp/service.log" }))),
          h("button", { class: "btn small ghost danger", onclick: () => update(() => { p.services = p.services.filter((x) => x !== s); }) }, icon("trash"), "Remove Service"))),
        h("div", { class: "team-form" },
          field("Start everything", input(p.commands, "start_all", "s-start-all", { mono: true, placeholder: "bash start_local.sh" })),
          field("Stop everything", input(p.commands, "stop_all", "s-stop-all", { mono: true, placeholder: "bash stop_local.sh" }))),
        h("button", { class: "btn small", onclick: async () => {
          const name = await promptDialog({ title: "New Service", label: "Name", placeholder: "e.g. Registry API", confirm: "Add" });
          if (name) update(() => { p.services.push({ id: uniqueId(name, Object.fromEntries(p.services.map((s) => [s.id, 1]))), name }); });
        } }, icon("plus"), "Add Service")),

      fold("links", "Links", p.links.length,
        p.links.map((l, i) => h("div", { class: "inline-row" },
          h("input", { class: "input", value: l.name || "", placeholder: "Name", "aria-label": "Link name", oninput: (e) => edit(() => { l.name = e.target.value.trim(); }) }),
          h("input", { class: "input mono", value: l.url || "", placeholder: "https://…", "aria-label": "Link URL", spellcheck: "false", oninput: (e) => edit(() => { l.url = e.target.value.trim(); }) }),
          removeButton("Remove link", () => { p.links.splice(i, 1); }))),
        h("button", { class: "btn small", onclick: () => update(() => { p.links.push({ name: "", url: "https://" }); }) }, icon("plus"), "Add Link")),

      fold("github", "GitHub", null,
        h("p", { class: "muted" }, "Uses your gh login. The project's repository and its sections' repositories are included already."),
        field("More repositories", h("textarea", { id: "s-repos", class: "input mono", rows: 2, placeholder: "owner/repo", spellcheck: "false",
          oninput: (e) => edit(() => { p.github.repos = lines(e.target.value); }) }, p.github.repos.join("\n")), "One per line."),
        h("h3", { class: "label", style: "margin-top:6px" }, "Project boards"),
        p.github.boards.map((b, i) => h("div", { class: "inline-row" },
          h("input", { class: "input", value: b.name || "", placeholder: "Name", "aria-label": "Board name", oninput: (e) => edit(() => { b.name = e.target.value.trim(); }) }),
          h("input", { class: "input mono", value: b.owner || "", placeholder: "owner (user or org)", "aria-label": "Board owner", spellcheck: "false", oninput: (e) => edit(() => { b.owner = e.target.value.trim(); }) }),
          h("input", { class: "input mono narrow", value: b.number || "", placeholder: "number", "aria-label": "Board number", oninput: (e) => edit(() => { b.number = Number(e.target.value) || ""; }) }),
          removeButton("Remove board", () => { p.github.boards.splice(i, 1); }))),
        h("button", { class: "btn small", onclick: () => update(() => { p.github.boards.push({ name: "", owner: "", number: "" }); }) }, icon("plus"), "Add Board")),

      fold("agents", "Agents & Variables", null, h("div", { class: "team-form wide" },
        field("Preferred agents", h("input", { id: "s-agents", class: "input mono", value: (p.agents || []).join(", "), placeholder: "spec-writer, architect", spellcheck: "false",
          onchange: (e) => update(() => { p.agents = csv(e.target.value); }) }), "Comma-separated agent ids to prefer in this project."),
        field("Variables", h("textarea", { id: "s-vars", class: "input mono", rows: 3, placeholder: "brief_file = inputs/product-brief.md", spellcheck: "false",
          oninput: (e) => edit(() => { p.vars = Object.fromEntries(lines(e.target.value).filter((l) => l.includes("=")).map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]).filter(([k]) => k)); }) },
          Object.entries(p.vars || {}).map(([k, v]) => `${k} = ${v}`).join("\n")), "name = value per line. They fill agent inputs with the same name.")),
        unknownAgents(p.agents || [])),

      fold("server", "Servers", (p.servers || (p.server?.ssh ? [1] : [])).length, serverSettings(p)),

      fold("mcp", "MCP Servers", Object.keys(p.mcp_servers || {}).length, mcpEditor(p)),

      h("div", { class: "danger-zone" },
        h("button", { class: "btn ghost danger", onclick: async () => {
          const ok = await confirmDialog({ title: `Delete ${p.name || pid}?`, body: "Removes the project from Lago Agents. Its folder isn't touched.", confirm: "Delete Project", danger: true });
          if (!ok) return;
          const projects = structuredClone(state.config.projects);
          delete projects[pid];
          if (await runAction(null, () => bridge.call("save_projects", { projects }))) { state.config.projects = projects; location.hash = "#projects"; }
        } }, icon("trash"), "Delete Project")));
  }

  function serverSettings(p) {
    if (!p.servers) {
      p.servers = p.server?.ssh ? [{ id: "production", role: "production", ...p.server }] : [];
      delete p.server;
    }
    return [
      h("p", { class: "muted" }, "Where this project runs. Lago connects with your SSH setup (keys and ~/.ssh/config), reads each server's state, and runs its deploy script in a terminal you watch. Deploys to a production server stop if it has uncommitted changes; a local server runs your working copy."),
      p.servers.map((sv, i) => {
        sv.watch ||= [];
        const f = (key, label, opts) => field(label, input(sv, key, `sv-${i}-${key}`, opts));
        return h("details", { class: "service-edit", open: p.servers.length === 1 || undefined },
          h("summary", {}, h("span", { class: "row-title" }, sv.name || sv.id || "New server"), h("span", { class: "badge" }, sv.role || "production"), h("span", { class: "faint mono" }, sv.ssh || "")),
          h("div", { class: "team-form" },
            f("name", "Name", { placeholder: "e.g. Hetzner production" }),
            field("Role", h("select", { id: `sv-${i}-role`, class: "select", onchange: (e) => update(() => { sv.role = e.target.value; }) },
              [["production", "Production (guarded deploys)"], ["local", "Local replica"]].map(([v, l]) => h("option", { value: v, selected: (sv.role || "production") === v }, l)))),
            f("ssh", "SSH host", { mono: true, placeholder: "alias, user@host or ssh://user@host:port" }),
            f("path", "Code folder on the server", { mono: true, placeholder: "/srv/webstore" }),
            f("site", "Site address", { mono: true, placeholder: "https://example.com or http://localhost:8080" }),
            f("start", "Start command (optional)", { mono: true, placeholder: "docker compose -f local-server/compose.yml up -d" })),
          field("Deploy script", h("textarea", { id: `sv-${i}-deploy`, class: "input mono", rows: 5, spellcheck: "false", placeholder: "git pull --ff-only origin main\ndocker compose up -d --build",
            oninput: (e) => edit(() => { sv.deploy = e.target.value; }) }, sv.deploy || ""),
            (sv.role || "production") === "production" ? "Runs in the code folder, after Lago checks the server has no uncommitted changes to tracked files." : "Runs in the code folder."),
          h("h3", { class: "label", style: "margin-top:6px" }, "Folders to compare"),
          sv.watch.map((w, j) => h("div", { class: "inline-row" },
            h("input", { class: "input", value: w.name || "", placeholder: "Name", "aria-label": "Name", oninput: (e) => edit(() => { w.name = e.target.value.trim(); }) }),
            h("input", { class: "input mono", value: w.remote || "", placeholder: "/opt/app/agents", "aria-label": "Server folder", spellcheck: "false", oninput: (e) => edit(() => { w.remote = e.target.value.trim(); }) }),
            h("input", { class: "input mono narrow", value: (w.local || []).join(", "), placeholder: "local folders", "aria-label": "Local folders", spellcheck: "false", oninput: (e) => edit(() => { w.local = csv(e.target.value); }) }),
            removeButton("Remove folder", () => { sv.watch.splice(j, 1); }))),
          h("div", { class: "toolbar" },
            h("button", { class: "btn small", onclick: () => update(() => { sv.watch.push({ name: "", remote: "", local: [] }); }) }, icon("plus"), "Add Folder"),
            h("button", { class: "btn small ghost danger", onclick: () => update(() => { p.servers.splice(i, 1); }) }, icon("trash"), "Remove Server")));
      }),
      h("button", { class: "btn small", onclick: async () => {
        const name = await promptDialog({ title: "New Server", label: "Name", placeholder: "e.g. Staging", confirm: "Add" });
        if (name) update(() => { p.servers.push({ id: uniqueId(name, Object.fromEntries(p.servers.map((x) => [x.id, 1]))), name, role: "production", ssh: "", path: "", deploy: "", watch: [] }); });
      } }, icon("plus"), "Add Server"),
    ];
  }

  function mcpEditor(p) {
    const error = h("p", { class: "error", hidden: true });
    const text = h("textarea", { id: "s-mcp", class: "input mono", rows: 6, spellcheck: "false",
      placeholder: '{\n  "postgres": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://…"] }\n}',
      onchange: (e) => {
        try {
          const value = e.target.value.trim() ? JSON.parse(e.target.value) : {};
          if (typeof value !== "object" || Array.isArray(value)) throw new Error("It must be an object of name → server.");
          error.hidden = true;
          edit(() => { p.mcp_servers = value; });
        } catch (err) { error.textContent = `Not valid: ${err.message}`; error.hidden = false; }
      } }, Object.keys(p.mcp_servers || {}).length ? JSON.stringify(p.mcp_servers, null, 2) : "");
    const wrap = field("Extra MCP servers for this project's agents", text, "Same format as mcpServers in .mcp.json. Your user-level MCP servers are available anyway.");
    wrap.append(error);
    return wrap;
  }

  viewCleanup = () => {};
  render();
  refreshCatalog().then(() => { if (alive() && !dirty()) render(); }).catch(() => {});
}

/* ================================================================ Lago (the Orchestrator) */

const ORCHESTRATOR = "orchestrator";
const OPEN_RUN = new Set(["queued", "sent", "working"]);
const REPORT_KINDS = {
  solved: ["seal-check", "Fully done"], done: ["check-circle", "Stage done"], failed: ["x-circle", "Failed"], question: ["question", "Question"],
  gate: ["eye", "Result to check"], approval: ["flow-arrow", "Plan to approve"], blocked: ["prohibit", "Blocked"],
  permission: ["lock-key", "Needs permission"], waiting: ["hourglass", "Waiting for you"], update: ["info", "Update"],
  news: ["newspaper", "News"], decided: ["gavel", "Lago decided for you"],
};

/* Lago's face, by persona (personas.py): an otter, a retro computer or a diving robot who show
   what he's doing (off, idle, listening, thinking, speaking, attention; styles.css animates
   each). The page has a big one; the sidebar link a small one. */
const OTTER_SVG = `<svg viewBox="0 0 48 48"><circle class="part ring" cx="24" cy="25" r="22.5"/><g class="head">
  <g class="part ears"><circle cx="8.6" cy="20" r="3.6" fill="currentColor"/><circle cx="39.4" cy="20" r="3.6" fill="currentColor"/>
    <circle class="ink" cx="8.6" cy="20" r="1.5" fill-opacity=".35"/><circle class="ink" cx="39.4" cy="20" r="1.5" fill-opacity=".35"/></g>
  <path fill="currentColor" d="M24 13c10.5 0 18 6 18 14.5S34.5 41 24 41 6 36 6 27.5 13.5 13 24 13z"/>
  <path class="muzzle" d="M10 31.5c2.5-4 8-5.5 14-5.5s11.5 1.5 14 5.5c-2 5.5-7.5 8-14 8s-12-2.5-14-8z"/>
  <circle class="muzzle" cx="19.6" cy="31.6" r="5.4"/><circle class="muzzle" cx="28.4" cy="31.6" r="5.4"/>
  <g class="ink" fill-opacity=".45"><circle cx="17" cy="30.6" r=".7"/><circle cx="18.6" cy="33" r=".7"/><circle cx="31" cy="30.6" r=".7"/><circle cx="29.4" cy="33" r=".7"/></g>
  <path class="ink-line" stroke-opacity=".35" stroke-width="1" d="M14.5 31.4 7 30M14.8 33.6 7.5 35M33.5 31.4 41 30M33.2 33.6l7.3 1.4"/>
  <g class="part eyes"><circle class="ink" cx="16.4" cy="22.6" r="2.5"/><circle class="ink" cx="31.6" cy="22.6" r="2.5"/>
    <circle fill="#fff" fill-opacity=".85" cx="17.2" cy="21.8" r=".7"/><circle fill="#fff" fill-opacity=".85" cx="32.4" cy="21.8" r=".7"/></g>
  <path class="shut ink-line" stroke-width="1.6" d="M13.9 22.8q2.5 1.8 5 0M29.1 22.8q2.5 1.8 5 0"/>
  <path class="ink" d="M20.6 26.4c0-1 1.5-1.6 3.4-1.6s3.4.6 3.4 1.6c0 1.4-2 2.8-3.4 2.8s-3.4-1.4-3.4-2.8z"/>
  <path class="ink-line" stroke-width="1.3" d="M24 29.2v2"/><path class="smile ink-line" stroke-width="1.3" d="M21 33q1.5 1.2 3-.4 1.5 1.6 3 .4"/>
  <g class="part open mouth"><path class="ink" d="M20.8 32.8q1.6.9 3.2-.4 1.6 1.3 3.2.4c0 2.9-1.4 4.8-3.2 4.8s-3.2-1.9-3.2-4.8z"/><path fill="#e8838a" d="M21.9 36.3c.7-1 3.5-1 4.2 0-.6.9-1.4 1.3-2.1 1.3s-1.5-.4-2.1-1.3z"/></g></g></svg>`;

const CHIP_SVG = `<svg viewBox="0 0 48 48"><circle class="part ring" cx="24" cy="25" r="22.5"/><g class="head">
  <g class="part ears"><path d="M34.5 9l3.6-5" stroke="#a49880" stroke-width="1.6" stroke-linecap="round"/>
    <circle class="part bulb" cx="38.4" cy="3.6" r="2" fill="#ff9d3c"/></g>
  <path fill="#a49880" d="M18 37.5h12l2.6 5H15.4z"/><rect x="12.5" y="41.8" width="23" height="2.8" rx="1.4" fill="#8f846d"/>
  <rect x="6.5" y="8.5" width="35" height="29.5" rx="6" fill="#ddd3bb"/>
  <rect x="6.5" y="31" width="35" height="7" rx="3" fill="#cfc4a9"/>
  <rect x="10" y="12" width="28" height="18.5" rx="4" fill="#b9ad92"/>
  <rect x="11.5" y="13.5" width="25" height="15.5" rx="3" fill="#10241d"/>
  <path fill="#fff" fill-opacity=".07" d="M11.5 16.5a3 3 0 0 1 3-3H26l-14.5 9z"/>
  <rect x="25" y="33.4" width="11" height="1.6" rx=".8" fill="#8f846d"/><circle cx="12" cy="34.2" r="1.1" fill="#7dffb0"/>
  <g class="part eyes" fill="#7dffb0"><rect x="16.4" y="17" width="3.8" height="4.8" rx=".6"/><rect x="27.8" y="17" width="3.8" height="4.8" rx=".6"/></g>
  <path class="shut" fill="none" stroke="#7dffb0" stroke-width="1.5" d="M16.2 20.2h4.2M27.6 20.2h4.2"/>
  <path class="smile" fill="none" stroke="#7dffb0" stroke-width="1.5" d="M18.6 23.6v1.4h10.8v-1.4"/>
  <rect class="part open" x="20.4" y="23.2" width="7.2" height="3.8" rx=".6" fill="#7dffb0"/></g></svg>`;

// Atlas has no face: his window glows when he speaks.
const ATLAS_SVG = `<svg viewBox="0 0 48 48"><circle class="part ring" cx="24" cy="25" r="22.5"/><g class="head">
  <rect x="22.8" y="6" width="2.4" height="3.6" fill="#9a722e"/><rect class="part bulb" x="20.6" y="3.4" width="6.8" height="3.2" rx="1.2" fill="#ecc97a"/>
  <g class="part ears"><circle cx="7.8" cy="25" r="3.6" fill="#17324a" stroke="#ecc97a" stroke-width="1.6"/>
    <circle cx="40.2" cy="25" r="3.6" fill="#17324a" stroke="#ecc97a" stroke-width="1.6"/></g>
  <path fill="#9a722e" d="M11 37h26l1.6 7.2H9.4z"/>
  <g fill="#ecc97a"><circle cx="14" cy="40.6" r="1.1"/><circle cx="24" cy="40.6" r="1.1"/><circle cx="34" cy="40.6" r="1.1"/></g>
  <circle cx="24" cy="25" r="16.5" fill="#c99a45"/>
  <path fill="none" stroke="#f2d48f" stroke-opacity=".75" stroke-width="2" stroke-linecap="round" d="M12.6 18.2A13.5 13.5 0 0 1 22 11.6"/>
  <g fill="#ecc97a"><circle cx="24" cy="11.6" r="1.1"/><circle cx="37.4" cy="25" r="1.1"/><circle cx="24" cy="38.4" r="1.1"/><circle cx="10.6" cy="25" r="1.1"/></g>
  <circle cx="24" cy="25" r="10.4" fill="#17324a" stroke="#ecc97a" stroke-width="2.6"/>
  <circle class="part glow" cx="24" cy="25" r="8.4" fill="#8fe3ff" fill-opacity=".4"/>
  <path fill="none" stroke="#fff" stroke-opacity=".22" stroke-width="1.8" stroke-linecap="round" d="M17.4 20.2a8 8 0 0 1 5-4.2"/>
  <path fill="none" stroke="#fff" stroke-opacity=".12" stroke-width="1.4" stroke-linecap="round" d="M31.2 29.4a8 8 0 0 1-3.6 3.6"/></g></svg>`;

const PERSONA_FACES = { lago: OTTER_SVG, chip: CHIP_SVG, atlas: ATLAS_SVG };
const personaFace = (id) => PERSONA_FACES[id] || OTTER_SVG;

function lagoOrb(size = "", persona = state.persona) {
  const orb = h("span", { class: `orb ${size}`, "aria-hidden": "true", dataset: { state: "off", persona: persona || "lago" } });
  orb.innerHTML = personaFace(persona);
  return orb;
}
document.querySelectorAll(".orb:empty").forEach((orb) => { orb.innerHTML = OTTER_SVG; });  // the sidebar's, until init says who he is

/* The three personas as cards: his face, what he's like, and a button to hear him. */
function personaCards(selected, onPick) {
  return h("div", { class: "persona-cards", role: "radiogroup", "aria-label": "Who you talk to" },
    (state.personas || []).map((p) => {
      const face = lagoOrb("big", p.id);
      face.dataset.state = "idle";
      face.dataset.fixed = "";
      const hear = async (e) => {
        e.stopPropagation();
        face.dataset.state = "thinking";  // his voice is being made: the mouth waits for the sound
        try { await bridge.call("preview_persona", { persona: p.id }); } catch (error) {
          toast(error.message, "error");
          face.dataset.state = "idle";
          return;
        }
        let spoke = false;
        const follow = () => {
          spoke ||= mouth.playing;
          face.dataset.state = mouth.playing ? "speaking" : spoke ? "idle" : "thinking";
          if (spoke && !mouth.playing) mouth.listeners.delete(follow);
        };
        mouth.listeners.add(follow);
        watchVoice();
        setTimeout(() => { if (mouth.listeners.delete(follow)) face.dataset.state = "idle"; }, 20000);
      };
      return h("div", { class: `persona-card${p.id === selected ? " on" : ""}`, role: "radio", tabindex: 0, "aria-checked": String(p.id === selected),
        onclick: () => onPick(p.id), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(p.id); } } },
        face, h("b", {}, p.name), h("small", { class: "faint" }, p.figure), h("p", {}, p.about),
        h("button", { type: "button", class: "btn small ghost", onclick: hear }, icon("speaker-high"), `Hear ${p.name}`));
    }));
}

function personaDialog() {
  return openDialog((close) => {
    let picked = state.persona;
    const slot = h("div");
    const draw = () => fill(slot, personaCards(picked, (id) => { picked = id; draw(); }));
    draw();
    return h("form", { onsubmit: async (e) => {
        e.preventDefault();
        const ok = await runAction(e.submitter, () => bridge.call("set_persona", { persona: picked }));
        if (ok) { applyPersona(picked); close(true); }
      } },
      h("h2", {}, "Who do you want to talk to?"),
      h("p", { class: "desc" }, "Same assistant, same memory and work: only his name, face and voice change. His voice and effect switch with him; you can still change them in his settings."),
      slot,
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, "Switch")));
  }, { wide: true });
}

/* The persona's name, and every face and label that shows it, after init or a switch. */
const lagoName = () => (state.personas || []).find((p) => p.id === state.persona)?.name || "Lago";
function applyPersona(persona) {
  state.persona = persona;
  document.querySelectorAll(".orb:not([data-fixed])").forEach((orb) => {
    if (orb.dataset.persona === persona && orb.firstChild) return;
    orb.dataset.persona = persona;
    orb.innerHTML = personaFace(persona);
  });
  const link = byId("lago-link")?.querySelector("strong");
  if (link) link.textContent = `Talk to ${lagoName()}`;
  document.querySelectorAll("[data-lago-name]").forEach((el) => { el.textContent = lagoName(); });
}

/* His mouth follows his voice coming out of the speakers, not the seconds it takes to make
   it: voice_state is asked every 150 ms while anything is queued or playing, and listeners
   hear each change. */
const mouth = { playing: false, timer: null, listeners: new Set() };
function watchVoice() {
  if (mouth.timer) return;
  const tick = async () => {
    let v = { playing: false, queued: false };
    try { v = await bridge.call("voice_state"); } catch { /* treated as quiet */ }
    if (v.playing !== mouth.playing) {
      mouth.playing = v.playing;
      mouth.listeners.forEach((fn) => fn());
    }
    mouth.timer = v.playing || v.queued ? setTimeout(tick, 150) : null;
  };
  mouth.timer = setTimeout(tick, 0);
}
mouth.listeners.add(() => { if (state.live) syncLagoLink(state.live); });

function lagoMood({ status, speaking, recording, transcribing, rotating }) {
  if (recording) return ["listening", "Listening…"];
  if (transcribing) return ["thinking", "Catching what you said…"];
  if (rotating) return ["thinking", "Writing down our last conversation…"];
  if (status === "exited") return ["attention", "Stopped unexpectedly"];
  if (status === "stopped") return ["off", "Asleep. Say something to wake him"];
  if (status === "needs_approval" || status === "needs_input") return ["attention", "Needs you"];
  if (speaking) return ["speaking", "Speaking"];
  if (status === "starting") return ["thinking", "Waking up…"];
  if (status === "connecting") return ["thinking", "Connecting his tools…"];
  if (status === "working") return ["thinking", "Thinking…"];
  return ["idle", "Here. What's next?"];
}

/* The sidebar's Talk to Lago link follows his mood. */
function syncLagoLink(live) {
  const link = byId("lago-link");
  if (!link) return;
  const status = live.agents[ORCHESTRATOR]?.status || "stopped";
  if (live.lago_speaking || live.lago_voice_queued) watchVoice();
  const [mood, label] = lagoMood({ status, speaking: mouth.playing, recording: state.lagoRecording });
  link.querySelector(".orb").dataset.state = mood;
  byId("lago-mood").textContent = mood === "off" ? "Asleep" : mood === "idle" ? "Ready" : label.replace(/…$/, "");
}

/* A small, safe Markdown subset for Lago's replies: paragraphs, lists, headings, bold,
   inline code and links (opened in the browser). */
/* "session #13", "work session #13", "pipeline #12" and "plan #12" in text become links to
   that session or plan. */
const REF = /\b((?:work )?sessions? #|pipelines? #|plans? #)(\d+)/gi;
function refs(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(REF)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const href = /^(?:work )?session/i.test(m[1]) ? `#work/${m[2]}` : `#pipeline/${m[2]}`;
    out.push(h("a", { class: "ref", href }, m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out.length ? out : [text];
}

/* File paths in a message ("docs/design.md", "~/notes/plan.txt", "/home/…/report.pdf")
   become links that open the file: in its usual app on the computer, in a reader on the
   phone. A relative path is looked for in the folders of the sessions the message names. */
const FILE_EXT = "md|markdown|txt|pdf|html|json|ya?ml|toml|csv|log|py|js|mjs|ts|tsx|jsx|css|sh|sql|kt|kts|cs|go|rs|java|rb|php|png|jpe?g|svg|gif|webp|ipynb";
const FILE_PATH = new RegExp(`(?<![\\w:/.@-])(?:~?/|\\.{1,2}/)?(?:[\\w@.-]+/)*[\\w@-][\\w@.-]*\\.(?:${FILE_EXT})(?::\\d+)?(?![\\w/])`, "gi");
const isFilePath = (text) => new RegExp(`^${FILE_PATH.source}$`, "i").test(text.trim());
const sessionNumbers = (text) => [...new Set([...String(text).matchAll(/#(\d+)/g)].map((m) => Number(m[1])))];

async function openFileRef(path, sessions) {
  try {
    if (!state.remote) { await bridge.call("open_file", { path, sessions }); return; }
    const file = await bridge.call("read_file", { path, sessions });
    openDialog((close) => h("div", {},
      h("h2", {}, file.name), h("p", { class: "help faint mono" }, file.path),
      file.markdown ? h("div", { class: "md file-view" }, markdown(file.text)) : h("pre", { class: "file-view" }, file.text),
      h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn primary", onclick: () => close() }, "Close"))), { wide: true });
  } catch (error) {
    toast(error.message, "error");
  }
}

function fileLink(path, sessions, inner) {
  return h("a", { class: "file-ref", href: "#", title: `Open ${path}`,
    onclick: (e) => { e.preventDefault(); openFileRef(path, sessions); } }, inner);
}

/* Plain text with session and plan refs and file paths turned into links. */
function linked(text, sessions) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(FILE_PATH)) {
    if (m.index > last) out.push(refs(text.slice(last, m.index)));
    out.push(fileLink(m[0], sessions, m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(refs(text.slice(last)));
  return out.length ? out : [text];
}

function markdown(text, { sessions = sessionNumbers(text) } = {}) {
  const inline = (line) => {
    const out = [];
    const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\((https?:\/\/[^)\s]+)\))/g;
    let last = 0;
    for (const m of line.matchAll(pattern)) {
      if (m.index > last) out.push(linked(line.slice(last, m.index), sessions));
      const token = m[0];
      if (token.startsWith("**")) out.push(h("strong", {}, linked(token.slice(2, -2), sessions)));
      else if (token.startsWith("`")) {
        const code = token.slice(1, -1);
        out.push(isFilePath(code) ? fileLink(code, sessions, h("code", {}, code)) : h("code", {}, code));
      }
      else out.push(h("a", { href: m[2], onclick: (e) => { e.preventDefault(); openUrl(m[2]); } }, token.slice(1, token.indexOf("]"))));
      last = m.index + token.length;
    }
    if (last < line.length) out.push(linked(line.slice(last), sessions));
    return out;
  };
  const item = /^\s*(?:[-*•]|\d+[.)])\s+/;
  return text.split(/\n{2,}/).map((block) => {
    const lines = block.split("\n").filter((l) => l.trim());
    if (!lines.length) return null;
    if (lines.every((l) => item.test(l))) {
      return h(/^\s*\d/.test(lines[0]) ? "ol" : "ul", {}, lines.map((l) => h("li", {}, inline(l.replace(item, "")))));
    }
    if (lines.length === 1 && /^#{1,6}\s/.test(lines[0])) return h("p", { class: "md-h" }, inline(lines[0].replace(/^#+\s*/, "")));
    return h("p", {}, lines.map((l, i) => [i ? h("br") : null, inline(l)]));
  });
}

const SPOKEN_LINE = /^[^\w🔊]*🔊.*$/gmu;
const hhmm = (iso) => (iso ? new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "");

views.orchestrator = (root, alive) => {
  let data = null;
  let terminalFor = null;
  let lastTools = "";
  let lastChat = "";
  let lastSide = "";
  const openRuns = new Set();  // runs whose steps you unfolded, kept open across refreshes
  root.classList.add("lago-view");
  const orb = lagoOrb("big");
  const mood = h("p", { class: "lago-mood", "aria-live": "polite" });
  const tools = h("div", { class: "lago-tools" });
  const needsSlot = h("div");
  const chat = h("div", { class: "chat", role: "log", "aria-label": `Conversation with ${lagoName()}` }, skeleton(3));
  const talk = talkBox({ onSent: () => load(), onPhase: () => renderMood() });
  const focusBar = h("div", { class: "focus-bar", hidden: true, role: "status" });
  let lastFocus = "";
  const side = h("aside", { class: "lago-side", "aria-label": "Tasks and reports" });
  root.append(h("div", { class: "lago-layout" },
    h("section", { class: "lago-main" },
      h("header", { class: "lago-hero" }, orb, h("div", { class: "lago-title" },
        h("div", { class: "lago-name" }, h("h1", { "data-lago-name": "" }, lagoName()),
          featureOn("personas") ? h("button", { type: "button", class: "btn small ghost", title: "Choose who you talk to: Lago, Chip or Atlas", onclick: () => personaDialog() }, icon("user-switch"), "Switch") : null),
        mood), tools),
      needsSlot, chat, focusBar, talk.el),
    side));

  function renderMood() {
    if (!data) return;
    if (data.speaking || data.voice_queued) watchVoice();
    const [state_, label] = lagoMood({ status: data.status, speaking: mouth.playing, rotating: data.rotating,
      recording: talk.phase() === "recording", transcribing: talk.phase() === "transcribing" });
    orb.dataset.state = state_;
    const step = state_ === "thinking" && data.status === "working" && data.activity?.findLast((a) => !a.done);
    mood.textContent = step ? `${step.text.replace(/\s*(for\s*)?“.*”/, "").trim()}…` : label;
  }

  function renderTools() {
    const running = RUNNING(data.status);
    const key = JSON.stringify([data.status, running, data.unrestricted, data.skip_permissions, data.voice.talk, state.lagoTerminal, data.focus?.id, data.mode]);
    if (key === lastTools) return;
    lastTools = key;
    const act = (fn) => (e) => runAction(e.currentTarget, fn).then(() => { load(); pollLive(); });
    const modeButton = (value) => h("button", { type: "button", class: "btn small", "aria-pressed": String(data.mode === value), title: LAGO_MODES[value][1],
      onclick: data.mode === value ? null : act(() => bridge.call("set_lago_mode", { mode: value })) }, LAGO_MODES[value][0]);
    fill(tools,
      h("div", { class: "mode-switch", role: "group", "aria-label": "How much Lago decides" }, modeButton("check"), modeButton("decide")),
      !running ? h("button", { class: "btn primary small", onclick: (e) => startLago(e.currentTarget, data.skip_permissions) }, icon("power"),
        data.skip_permissions ? "Wake Unrestricted" : "Wake Lago") : null,
      running && data.unrestricted ? unrestrictedPill() : null,
      running ? h("button", { class: "btn small ghost", title: "Stop what he's doing now (Esc)", onclick: act(() => bridge.call("press", { agent_id: ORCHESTRATOR, key: "interrupt" })) }, icon("hand"), "Interrupt") : null,
      running ? h("button", { class: "btn small ghost", title: "When he's stuck and Interrupt doesn't help: end his session and start him again", onclick: async (e) => {
        const button = e.currentTarget;
        if (!(await confirmDialog({ title: "Force restart Lago?", body: "His session ends at once and starts again. He keeps this conversation and his notes; whatever he was in the middle of stops.", confirm: "Force Restart", danger: true }))) return;
        await runAction(button, () => bridge.call("restart_lago"));
        load(); pollLive();
      } }, icon("arrow-counter-clockwise"), "Force Restart") : null,
      featureOn("voice") ? h("button", { class: "btn small ghost", "aria-pressed": String(data.voice.talk), title: data.voice.talk ? "He speaks his replies: click to make him quiet" : "He's quiet: click to let him speak",
        onclick: act(() => bridge.call("set_talk", { on: !data.voice.talk })) }, icon(data.voice.talk ? "speaker-high" : "speaker-slash"), data.voice.talk ? "Speaks" : "Quiet") : null,
      h("button", { class: "btn small ghost", "aria-haspopup": "menu", onclick: (e) => {
        const r = e.currentTarget.getBoundingClientRect();
        showMenu(r.left - 160, r.bottom + 4, [
          running ? { icon: "terminal-window", label: state.lagoTerminal ? "Hide His Terminal" : "Show His Terminal", action: () => { state.lagoTerminal = !state.lagoTerminal; syncTerminal(true); lastTools = ""; renderTools(); } } : null,
          { icon: "crosshair-simple", label: data.focus ? `Working with ${data.focus.name}…` : "Work with One Agent…", action: pickAgent },
          { icon: "brain", label: "What Lago Remembers", action: memoryDialog },
          { icon: "broom", label: "New Conversation", action: async () => {
            if (!(await confirmDialog({ title: "Start a new conversation?", body: "This chat is written down in Lago's notes and cleared. He remembers it the way he remembers earlier days.", confirm: "New Conversation" }))) return;
            await runAction(null, () => bridge.call("new_conversation"));
            load(); pollLive();
          } },
          { icon: "sliders-horizontal", label: featureOn("voice") ? "Settings: Model, Voice, Quiet Hours" : "Settings", action: settingsDialog },
          { icon: "puzzle-piece", label: "Features…", action: featuresDialog },
          running && !data.unrestricted && data.skip_permissions ? { icon: "lock-key-open", label: "Restart Without Permission Checks", action: () => startLago(null, true) } : null,
          running ? { icon: "moon", label: "Put Him to Sleep", action: () => runAction(null, () => bridge.call("stop_agent", { agent_id: ORCHESTRATOR })).then(() => { load(); pollLive(); }) } : null,
        ].filter(Boolean));
      } }, icon("dots-three"), "More"));
  }

  async function startLago(button, unrestricted) {
    let answer = { password: "", mode: data.mode };
    if (unrestricted) {
      answer = await lagoWakeDialog(data.mode);
      if (answer === null) return;
    }
    await runAction(button, () => bridge.call("start_orchestrator", { ...answer, unrestricted }));
    load(); pollLive();
  }

  function renderNeeds() {
    if (!["needs_approval", "needs_input", "exited"].includes(data.status)) { needsSlot.replaceChildren(); return; }
    const act = (key) => (e) => runAction(e.currentTarget, () => bridge.call("press", { agent_id: ORCHESTRATOR, key })).then(load);
    needsSlot.replaceChildren(h("div", { class: "needs", role: "alert" },
      h("div", { class: "needs-top" }, icon("warning-circle", "ph-fill"),
        h("p", {}, { needs_approval: data.detail || "Lago is asking permission to use a tool.",
          needs_input: "Claude Code is asking something before Lago can continue (the first time, whether to trust his folder). Open his terminal and answer it there.",
          exited: data.detail || "His session crashed. Restart him to carry on." }[data.status]),
        data.status === "needs_approval" ? [h("button", { class: "btn small primary", onclick: act("approve") }, icon("check"), "Approve"),
          h("button", { class: "btn small", onclick: act("deny") }, icon("x"), "Deny"),
          alwaysAllowButton(ORCHESTRATOR, load)] : null,
        data.status === "needs_input" ? h("button", { class: "btn small primary", onclick: () => { state.lagoTerminal = true; syncTerminal(true); renderTools(); } }, icon("terminal-window"), "Show His Terminal") : null,
        data.status === "exited" ? h("button", { class: "btn small primary", onclick: (e) => startLago(e.currentTarget, data.skip_permissions) }, icon("arrow-clockwise"), "Restart Lago") : null),
      data.screen ? h("pre", { class: "screen" }, data.screen.replace(/\n{3,}/g, "\n\n").trim()) : null));
  }

  function renderChat() {
    const thinking = ["working", "starting"].includes(data.status) || data.rotating;
    const key = JSON.stringify([data.conversation, data.days, thinking, data.activity, data.rotating, data.start_card]);
    if (key === lastChat) return;
    const first = !lastChat;
    lastChat = key;
    const nearBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
    const earlier = data.days.length ? h("details", { class: "chat-earlier" },
      h("summary", {}, icon("brain"), "What Lago remembers from recent days"),
      data.days.map((d) => h("div", { class: "chat-day" }, h("strong", {}, new Date(`${d.day}T12:00`).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })),
        h("ul", {}, d.headlines.map((x) => h("li", {}, x)))))) : null;
    const messages = data.conversation.map(message);
    fill(chat, earlier,
      !messages.length && !thinking ? h("div", { class: "chat-empty" },
        h("strong", {}, `Hi${data.user_name ? `, ${data.user_name}` : ""}. Tell me what to work on.`),
        h("span", {}, data.mic ? ["Press ", h("kbd", {}, "Ctrl"), " ", h("kbd", {}, "Space"), " and talk, or type below. For example: "] : "Type below. For example: ",
          `“${exampleRequest()}”`)) : null,
      messages,
      thinking ? working() : null,
      data.start_card && !thinking ? startCard() : null);
    if (first || nearBottom) chat.scrollTop = chat.scrollHeight;
  }

  /* After the first start: two ways to begin, like a teammate's first question. */
  function startCard() {
    const go = async (e, plan) => {
      if (plan) {
        await runAction(e.currentTarget, () => bridge.call("say_to_orchestrator", { text: "Interview me: ask me three or four questions in one message about what I'm working on, what I want to achieve first, my constraints and what done looks like. Then propose a plan and which agents should carry it out, for me to approve." }), "Lago will ask you a few questions.");
      } else {
        bridge.call("dismiss_start_card").catch(() => {});
        const box = document.querySelector(".lago-input textarea");
        if (box) { box.placeholder = "Describe the task: what you want done, where, and what done looks like…"; box.focus(); }
      }
      load();
    };
    return h("div", { class: "start-card", role: "group", "aria-label": "How would you like to start?" },
      h("strong", {}, "How would you like to start?"),
      h("button", { type: "button", class: "start-choice", onclick: (e) => go(e, true) },
        icon("chats-circle"), h("span", {}, h("b", {}, "Plan something with me"), h("small", {}, "A few questions about what you're working on, then a short plan and the agents to carry it out, for you to approve."))),
      h("button", { type: "button", class: "start-choice", onclick: (e) => go(e, false) },
        icon("pencil-simple-line"), h("span", {}, h("b", {}, "I have a task in mind"), h("small", {}, "Describe it and Lago gets the right agents on it."))));
  }

  // A file you sent: a thumbnail (bigger on click), a named file (opens in its app), or a
  // note once the sweep has deleted it.
  function sentFile(f) {
    if (!f.kept) return h("span", { class: "msg-file gone" }, icon(f.image ? "image-broken" : "file-text"), `${f.image ? "Picture" : f.name} no longer kept`);
    if (!f.image) {
      return h("button", { class: "msg-file", type: "button", title: `Open ${f.name}`,
        onclick: () => bridge.call("open_attachment", { path: f.path }).catch((e) => toast(e.message, "error")) },
        icon(/\.pdf$/i.test(f.name) ? "file-pdf" : "file-text"), f.name);
    }
    return h("button", { class: "msg-image", type: "button", title: "Show it bigger", "aria-label": `${f.name}: show it bigger`,
      onclick: () => openDialog((close) => h("div", { class: "image-view" },
        h("img", { src: f.url, alt: `${f.name}, sent to Lago` }),
        h("div", { class: "dialog-actions" }, h("button", { class: "btn ghost", onclick: () => close() }, "Close"))), { wide: true }) },
      h("img", { src: f.url, alt: `${f.name}, sent to Lago`, loading: "lazy" }));
  }

  // While Lago works: each step of this turn as it happens, as Claude Code lists its tool
  // calls, with the dots under them.
  function working() {
    const dots = h("div", { class: "msg lago typing", "aria-label": "Lago is thinking" }, h("span"), h("span"), h("span"));
    if (!data.activity?.length) return dots;
    return h("div", { class: "msg lago" }, h("ul", { class: "activity", "aria-live": "polite", "aria-label": "What Lago is doing" },
      data.activity.map((a) => h("li", { class: a.done ? "step-done" : "step-now" }, icon(a.done ? "check" : "circle-notch"), h("span", {}, a.text)))), dots);
  }

  function message(m) {
    if (m.who === "you") {
      const editable = m.id && !data.rotating;
      return h("div", { class: "msg you" }, m.files.length ? h("div", { class: "msg-images" }, m.files.map(sentFile)) : null,
        m.text ? h("div", { class: "bubble" }, m.text) : null,
        h("span", { class: "msg-meta" }, m.voice ? [icon("microphone"), " said"] : "typed", ` · ${hhmm(m.at)}`,
          editable ? h("button", { class: "say-again", title: "Edit: Lago goes back to just before this message", "aria-label": "Edit this message",
            onclick: () => talk.edit(m) }, icon("pencil-simple"), "Edit") : null));
    }
    if (m.who === "action") {
      return h("div", { class: "msg-action" }, icon("lightning", "ph-fill"), m.text);
    }
    if (m.who === "note") {
      const [firstLine, ...rest] = m.text.replace(/^\[Lago[^\]]*\]\s*/, "").split("\n");
      const label = /just opened Lago Agents/.test(firstLine) ? "You opened Lago" : firstLine.slice(0, 140);
      const more = rest.join("\n").trim();
      return h("div", { class: "msg-note" }, more
        ? h("details", {}, h("summary", {}, refs(label), ` · ${hhmm(m.at)}`), h("p", { class: "prewrap" }, refs(more)))
        : h("span", {}, refs(label), ` · ${hhmm(m.at)}`));
    }
    const text = m.text.replace(SPOKEN_LINE, "").trim();
    return h("div", { class: "msg lago" },
      h("div", { class: "bubble md" }, text ? markdown(text) : m.spoken),
      h("span", { class: "msg-meta" }, hhmm(m.at),
        m.spoken ? h("button", { class: "say-again", title: `Say it again: “${m.spoken}”`, "aria-label": "Say it again",
          onclick: () => bridge.call("lago_say", { text: m.spoken, category: m.category }).catch((e) => toast(e.message, "error")) }, icon("speaker-high"), "Say again") : null));
  }

  // Working with one agent: everything you ask goes to it, through Lago, and nothing else runs.
  function renderFocus() {
    const f = data.focus;
    const key = JSON.stringify(f);
    if (key === lastFocus) return;
    lastFocus = key;
    focusBar.hidden = !f;
    talk.setPlaceholder(f ? `Tell Lago what ${f.name} should do…` : null);
    if (!f) { focusBar.replaceChildren(); return; }
    fill(focusBar, icon("crosshair-simple", "ph-fill"),
      h("div", { class: "focus-text" }, h("strong", {}, `Working with ${f.name} only`),
        h("span", { class: "faint" }, "Everything you ask goes to this agent. Lago adds no other agents, sessions or searches.")),
      h("button", { class: "btn small ghost", onclick: pickAgent }, "Change"),
      h("button", { class: "btn icon ghost small", title: "Stop: back to normal", "aria-label": `Stop working with ${f.name}`,
        onclick: (e) => runAction(e.currentTarget, () => bridge.call("set_lago_focus", { agent_id: null }), "Back to normal").then(load) }, icon("x")));
  }

  async function pickAgent() {
    await refreshCatalog().catch(() => {});
    const agents = state.catalog.agents.filter((a) => a.enabled)
      .sort((a, b) => (a.eval?.rank || 999) - (b.eval?.rank || 999));
    const picked = await openDialog((close) => {
      const list = h("div", { class: "rows agent-pick" });
      const search = h("input", { class: "input", type: "search", placeholder: "Search agents…", "aria-label": "Search agents", autofocus: true,
        oninput: () => renderList() });
      function renderList() {
        const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
        const shown = agents.filter((a) => words.every((w) => [a.id, a.name, a.summary, a.use_when, a.tags.join(" ")].join(" ").toLowerCase().includes(w)));
        fill(list, shown.length ? shown.map((a) => h("button", { type: "button", class: `row agent-pick-row${data.focus?.id === a.id ? " current" : ""}`, onclick: () => close(a.id) },
          h("div", { class: "row-main" }, h("span", { class: "row-title" }, a.name, h("span", { class: "faint mono", style: "font-weight:400;font-size:12px;margin-left:8px" }, a.id)),
            h("span", { class: "muted" }, a.summary)),
          a.eval && a.eval.ups + a.eval.downs + a.eval.tests_run ? h("span", { class: "badge", title: `Rank #${a.eval.rank}` }, `${a.eval.score}`) : null))
          : h("p", { class: "muted" }, "No agents match."));
      }
      renderList();
      return h("div", {},
        h("h2", {}, "Work with One Agent"),
        h("p", { class: "desc" }, "Lago asks what it should do, then hands every task to this agent and nothing else, until you stop."),
        search, list,
        h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close(null) }, "Cancel")));
    }, { wide: true });
    if (!picked) return;
    await runAction(null, () => bridge.call("set_lago_focus", { agent_id: picked }), "Lago will ask what it should do");
    load();
  }

  function renderSide() {
    const key = JSON.stringify([data.sessions, data.events, data.schedules, data.runs]);
    if (key === lastSide) return;
    lastSide = key;
    const working = data.runs.filter((r) => OPEN_RUN.has(r.status));
    const finished = data.runs.filter((r) => !OPEN_RUN.has(r.status));
    fill(side,
      data.runs.length ? [h("h2", { class: "side-title" }, "Agent Runs", h("span", { class: "side-actions" },
          h("span", { class: "faint" }, working.length ? `${working.length} working` : ""),
          finished.length ? h("button", { class: "btn small ghost", title: "Close every finished run (their results stay in History)",
            onclick: () => closeRuns(finished) }, icon("x"), "Clear finished") : null)),
        h("div", { class: "task-list" }, data.runs.map(runRow))] : null,
      h("h2", { class: "side-title" }, "Tasks", h("span", { class: "faint" }, data.sessions.length || "")),
      data.sessions.length ? h("div", { class: "task-list" }, data.sessions.map(sessionRow))
        : h("p", { class: "faint side-empty" }, "None open. Ask Lago to start one."),
      data.schedules.length ? [h("h2", { class: "side-title" }, "Reminders", h("span", { class: "faint" }, data.schedules.length)),
        h("div", { class: "task-list" }, data.schedules.map(scheduleRow))] : null,
      h("h2", { class: "side-title" }, "Reports"),
      data.events.length ? h("div", { class: "timeline reports" }, data.events.map(reportItem))
        : h("p", { class: "faint side-empty" }, "Tasks report here when they finish, need an answer or have a plan for you."));
  }

  async function closeRuns(runs) {
    data.runs = data.runs.filter((r) => !runs.includes(r));
    renderSide();
    await runAction(null, () => bridge.call("close_runs", { task_ids: runs.map((r) => r.task_id) }), runs.length > 1 ? `Closed ${runs.length} runs` : "Closed");
  }

  // One agent Lago ran directly: what it's doing now and its steps so far, live.
  function runRow(r) {
    const open = OPEN_RUN.has(r.status);
    const elapsed = Math.max(0, Math.round(((open ? Date.now() / 1000 : r.updated_at) - r.started_at) / 60));
    const statusText = open ? (r.live === "needs_approval" ? "Needs permission" : r.live === "working" ? "Working"
      : r.live === "idle" ? "Wrapping up" : STATUS_LABELS[r.live] || "Waiting")
      : { done: "Done", failed: "Failed", cancelled: "Cancelled" }[r.status] || r.status;
    return h("details", { class: `run-row${open ? " open-run" : ""}`, open: openRuns.has(r.agent_id) || undefined,
        ontoggle: (e) => { if (e.currentTarget.open) openRuns.add(r.agent_id); else openRuns.delete(r.agent_id); } },
      h("summary", {},
        h("span", { class: "pipeline-main" },
          h("span", { class: "task-title" }, r.name, r.test ? h("span", { class: "badge", style: "margin-left:6px" }, "test") : null),
          h("span", { class: "task-meta" }, `#${r.task_id} · ${elapsed < 1 ? "just started" : `${elapsed} min`}`,
            open && r.detail ? [" · ", h("span", { class: "run-now" }, r.detail)] : "")),
        h("span", { class: `pill ${open ? (r.live === "needs_approval" ? "needs_you" : "working") : r.status === "done" ? "stopped" : "exited"}` }, statusText),
        open ? null : h("button", { type: "button", class: "btn icon ghost small run-close", title: "Close this run (its result stays in History)",
          "aria-label": `Close ${r.name} run #${r.task_id}`, onclick: (e) => { e.preventDefault(); e.stopPropagation(); closeRuns([r]); } }, icon("x"))),
      h("div", { class: "run-body" },
        h("p", { class: "muted", style: "margin:0 0 6px;font-size:12.5px" }, r.title),
        r.steps.length ? h("ul", { class: "activity" }, r.steps.map((a) => h("li", { class: a.done ? "step-done" : "step-now" },
          icon(a.done ? "check" : "circle-notch"), h("span", {}, a.text)))) : null,
        r.result ? h("p", { class: "prewrap run-result" }, r.result) : null,
        h("a", { href: `#agent/${r.agent_id}`, class: "btn small ghost" }, icon("terminal-window"), "Open Its Terminal")));
  }

  function scheduleRow(r) {
    const next = new Date(r.next_at * 1000);
    const today = next.toDateString() === new Date().toDateString();
    const when = today ? next.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
      : next.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    return h("div", { class: "pipeline-row compact schedule-row" },
      icon(r.repeat === "once" ? "alarm" : "repeat"),
      h("span", { class: "pipeline-main" }, h("span", { class: "task-title" }, r.text),
        h("span", { class: "task-meta" }, `Next ${when}`, r.repeat === "once" ? "" : ` · ${r.repeat}`, ` · #${r.id}`)),
      h("button", { class: "btn icon ghost small", title: "Cancel this reminder", "aria-label": `Cancel reminder ${r.id}`,
        onclick: async (e) => {
          const button = e.currentTarget;
          if (!(await confirmDialog({ title: "Cancel this reminder?", body: r.text, confirm: "Cancel Reminder" }))) return;
          await runAction(button, () => bridge.call("cancel_schedule", { schedule_id: r.id }), "Reminder cancelled");
          load();
        } }, icon("x")));
  }

  function sessionRow(s) {
    const waiting = s.pipeline?.status === "draft";
    return h("a", { class: `pipeline-row compact${waiting ? " waiting" : ""}`, href: `#work/${s.id}` },
      h("span", { class: "pipeline-main" }, h("span", { class: "task-title" }, s.title),
        h("span", { class: "task-meta" }, projectName(s.project), s.area ? ` / ${areaName(s.project, s.area)}` : "", ` · #${s.id}`,
          s.event ? [" · ", `${REPORT_KINDS[s.event.kind]?.[1] || s.event.kind} ${ago(s.event.created_at)}`] : "")),
      waiting ? h("span", { class: "pill needs_you" }, "Plan waiting") : pill(s.live));
  }

  function reportItem(e) {
    const [iconName, label] = REPORT_KINDS[e.kind] || ["info", e.kind];
    return h("div", { class: `event report k-${e.kind}${e.seen ? "" : " unseen"}` }, icon(iconName, "ph-fill"),
      h("div", { class: "text" },
        h("div", {}, h("strong", {}, label), e.workspace ? [" · ", h("a", { href: `#work/${e.workspace}` }, `${projectName(e.project)} #${e.workspace}`)] : null),
        e.text ? h("details", { class: "report-text" }, h("summary", { class: "muted" }, e.text.split("\n")[0].slice(0, 120)),
          h("p", { class: "prewrap" }, e.text)) : null,
        e.kind === "decided" ? decisionVerdict(e) : null,
        e.kind === "question" && e.form && !e.answered_at ? questionForm(e, JSON.parse(e.form), () => load()) : null,
        e.kind === "question" && e.answered_at ? h("span", { class: "faint" }, "Answered.") : null),
      h("span", { class: "when" }, ago(e.created_at)));
  }

  function decisionVerdict(e) {
    const judge = async (verdict, button) => {
      let note = "";
      if (verdict < 0) {
        note = await promptDialog({ title: "What would you have done?", label: "Your note (Lago reads it before deciding next time)", placeholder: "e.g. ask me before changing prices", confirm: "Save" });
        if (note === undefined || note === null) return;
      }
      runAction(button, () => bridge.call("judge_decision", { event_id: e.id, verdict, note }), "Lago will remember that.").then(() => load());
    };
    return h("div", { class: "verdict-line" },
      e.verdict ? h("span", { class: "faint" }, e.verdict > 0 ? "You marked this a good call." : `You'd have done otherwise${e.verdict_note ? `: ${e.verdict_note}` : "."}`) : null,
      h("button", { class: `btn small ghost${e.verdict > 0 ? " on" : ""}`, "aria-pressed": String(e.verdict > 0), onclick: (ev) => judge(1, ev.currentTarget) }, icon("thumbs-up"), "Good Call"),
      h("button", { class: `btn small ghost${e.verdict < 0 ? " on" : ""}`, "aria-pressed": String(e.verdict < 0), onclick: (ev) => judge(-1, ev.currentTarget) }, icon("thumbs-down"), "Not What I'd Do"));
  }

  async function memoryDialog() {
    let mem;
    try { mem = await bridge.call("lago_memory"); } catch (error) { toast(error.message, "error"); return; }
    return openDialog((close) => {
      const about = h("textarea", { id: "lago-about", class: "input mono", rows: 12 }, mem.about);
      const threads = h("textarea", { id: "lago-threads", class: "input mono", rows: 8, placeholder: "- **Project**: what, where it stands, the next step" }, mem.threads);
      return h("form", { onsubmit: async (e) => {
          e.preventDefault();
          if (await runAction(e.submitter, () => bridge.call("save_lago_memory", { about: about.value, threads: threads.value }), "Saved")) close(true);
        } },
        h("h2", {}, "What Lago Remembers"),
        h("p", { class: "desc" }, "Lago starts a fresh conversation each day, or when one gets very long. Before he does, Sonnet writes down the day, updates where your work stands and what he knows about you. Each new conversation starts with these notes; older days stay here for him to look up."),
        h("div", { class: "field" }, h("label", { for: "lago-threads" }, "Where things stand"), threads,
          h("p", { class: "help" }, "The work you have going, its state and next step: how Lago picks up where you left off and knows what to suggest. Rewritten after each conversation and when a task closes; correct or delete anything.")),
        h("div", { class: "field" }, h("label", { for: "lago-about" }, "About you"), about,
          h("p", { class: "help" }, "Lasting facts: preferences, people, how you like to work. Lago adds to it when you tell him something worth keeping; edit or delete anything.")),
        h("div", { class: "field" }, h("span", { class: "label" }, "Days"),
          mem.days.length ? h("div", { class: "memory-days" }, mem.days.map((d, i) => h("details", { class: "fold", open: i === 0 },
            h("summary", {}, new Date(`${d.day}T12:00`).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" })),
            h("div", { class: "fold-body md" }, markdown(d.text.replace(/^# .*\n/, ""))))))
            : h("p", { class: "faint" }, "Nothing written down yet. The first summary appears after Lago's current conversation ends.")),
        h("p", { class: "help faint" }, `Kept in ${mem.folder}`),
        h("div", { class: "dialog-actions" },
          h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Close"),
          h("button", { type: "submit", class: "btn primary" }, "Save")));
    }, { wide: true });
  }

  function settingsDialog() {
    const v = structuredClone(data.voice);
    let skip = data.skip_permissions;
    let model = data.model;
    const MODEL_NOTES = { haiku: "Cheapest and fastest. Fine for relaying, poor at keeping the thread.",
      sonnet: "Quicker and about half of Opus's usage, but weaker at picking up where you left off.",
      opus: "Recommended: keeps your work's thread across conversations and suggests what's next.",
      fable: "Most capable model, and the most expensive." };
    return openDialog((close) => {
      const field = (label, control, help) => h("div", { class: "field" }, h("label", { for: control.id }, label), control, help ? h("p", { class: "help" }, help) : null);
      const switchRow = (labelText, on, set) => h("label", { class: "switch-row" },
        h("button", { class: "switch", role: "switch", type: "button", "aria-checked": String(on),
          onclick: (e) => { const next = e.currentTarget.getAttribute("aria-checked") !== "true"; e.currentTarget.setAttribute("aria-checked", String(next)); set(next); } }), labelText);
      const modelHelp = h("p", { class: "help" }, MODEL_NOTES[model] || "");
      // A sound picker that plays each choice (the wake phrase's two sounds).
      const soundField = (id, label, key) => h("div", { class: "field" }, h("label", { for: id }, label),
        h("div", { style: "display:flex;gap:8px" },
          h("select", { id, class: "select", onchange: (e) => { v[key] = e.target.value; bridge.call("play_wake_sound", { choice: v[key] }); } },
            data.wake_sounds.map((o) => h("option", { value: o.id, selected: o.id === v[key] }, o.label))),
          h("button", { type: "button", class: "btn icon ghost", title: "Play it", "aria-label": `Play: ${label}`,
            onclick: () => bridge.call("play_wake_sound", { choice: v[key] }) }, icon("play"))));
      // One chime per kind of news, so you know what's coming before the words.
      v.chimes = { ...(v.chimes || {}) };
      const CHIME_HINTS = { needs_you: "Questions, plans to approve, results to check, blocked, permission", done: "A session's whole task is done",
        progress: "A stage finished, and Lago's ordinary replies", problem: "Something failed or crashed" };
      const chimeFields = h("div", { class: "inline-row", style: "grid-template-columns:1fr 1fr" }, (data.chime_categories || []).map((c) =>
        h("div", { class: "field" }, h("label", { for: `chime-${c.id}` }, c.label),
          h("div", { style: "display:flex;gap:8px" },
            h("select", { id: `chime-${c.id}`, class: "select", onchange: (e) => { v.chimes[c.id] = e.target.value; bridge.call("play_wake_sound", { choice: v.chimes[c.id] }); } },
              data.wake_sounds.map((o) => h("option", { value: o.id, selected: o.id === v.chimes[c.id] }, o.label))),
            h("button", { type: "button", class: "btn icon ghost", title: "Play it", "aria-label": `Play: ${c.label}`,
              onclick: () => bridge.call("play_wake_sound", { choice: v.chimes[c.id] }) }, icon("play"))),
          h("p", { class: "help" }, CHIME_HINTS[c.id] || ""))));
      const modelSelect = h("select", { id: "l-model", class: "select", onchange: (e) => { model = e.target.value; modelHelp.textContent = MODEL_NOTES[model] || ""; } },
        data.models.map((m) => h("option", { value: m, selected: m === model }, m[0].toUpperCase() + m.slice(1))));
      const voiceSelect = h("select", { id: "v-voice", class: "select", onchange: (e) => { v.voice = e.target.value; } },
        data.voices.map((o) => h("option", { value: o.id, selected: o.id === v.voice }, o.label)));
      const fallbackSelect = h("select", { id: "v-fallback", class: "select", onchange: (e) => { v.fallback = e.target.value; } },
        h("option", { value: "", selected: !v.fallback }, "None"),
        data.voices.filter((o) => o.offline).map((o) => h("option", { value: o.id, selected: o.id === v.fallback }, o.label)));
      const effectSelect = h("select", { id: "v-effect", class: "select", onchange: (e) => { v.effect = e.target.value; } },
        data.effects.map((o) => h("option", { value: o.id, selected: o.id === v.effect }, o.label)));
      const rate = h("input", { id: "v-rate", class: "input", type: "number", min: -100, max: 100, step: 10, value: v.rate, onchange: (e) => { v.rate = Number(e.target.value); } });
      const volume = h("input", { id: "v-volume", class: "input", type: "number", min: 25, max: 200, step: 25, value: v.volume ?? 100, onchange: (e) => { v.volume = Number(e.target.value); } });
      const from = h("input", { id: "v-from", class: "input", type: "time", value: v.quiet_from, onchange: (e) => { v.quiet_from = e.target.value; } });
      const to = h("input", { id: "v-to", class: "input", type: "time", value: v.quiet_to, onchange: (e) => { v.quiet_to = e.target.value; } });
      const kinds = h("div", { class: "app-list", style: "max-height:none" }, data.kinds.map((k) =>
        h("label", { class: "app-option" }, h("input", { type: "checkbox", checked: v.kinds.includes(k),
          onchange: (e) => { v.kinds = e.target.checked ? [...v.kinds, k] : v.kinds.filter((x) => x !== k); } }),
          icon(REPORT_KINDS[k][0]), REPORT_KINDS[k][1])));
      const voiceOn = featureOn("voice");
      return h("form", { onsubmit: async (e) => {
          e.preventDefault();
          if (await runAction(e.submitter, async () => {
            if (model !== data.model) await bridge.call("set_lago_model", { model });
            await bridge.call("set_skip_permissions", { on: skip });
            if (voiceOn) await bridge.call("save_voice", { settings: v });
            return { ok: true, message: "Saved." };
          })) { close(true); lastTools = ""; load(); }
        } },
        h("h2", {}, "Lago's Settings"),
        field("Model", modelSelect, modelHelp),
        h("p", { class: "help faint", style: "margin-top:-8px;font-size:12.5px" }, "Takes effect when Lago next wakes. Tasks keep their own model."),
        h("label", { class: "switch-row" }, h("button", { class: "switch danger", role: "switch", type: "button", "aria-checked": String(skip),
          onclick: (e) => { skip = !skip; e.currentTarget.setAttribute("aria-checked", String(skip)); } }),
          "Skip permissions: ask for my password when Lago wakes"),
        voiceOn ? [h("h3", { class: "section-title" }, "Voice"),
        switchRow("Say a short version of each reply aloud", v.talk, (on) => { v.talk = on; }),
        switchRow("Greet me when Lago Agents opens", v.greet, (on) => { v.greet = on; }),
        switchRow("Speak reports from tasks", v.enabled, (on) => { v.enabled = on; }),
        switchRow("Chime first: a different sound for each kind of news", v.chime, (on) => { v.chime = on; }),
        chimeFields,
        switchRow("Listen for the wake phrase, like a smart speaker", v.wake, (on) => { v.wake = on; }),
        h("p", { class: "help faint", style: "margin-top:-8px;font-size:12.5px" }, "Off until you switch it on. On, a small background service listens to your microphone; what it hears is transcribed on this computer, never sent or saved. Off stops and removes it."),
        field("Wake phrase", h("input", { id: "v-wake", class: "input", value: v.wake_phrase, maxlength: 40, onchange: (e) => { v.wake_phrase = e.target.value; } }),
          "Say it, then what you want; the wake sound means he's listening. Nothing is recorded until the phrase."),
        featureOn("personas") ? switchRow(`Answer to every persona's name (${(state.personas || []).map((p) => `"Hey ${p.name}"`).join(", ")}): the one you call answers`,
          v.wake_personas !== false, (on) => { v.wake_personas = on; }) : null,
        h("div", { class: "inline-row", style: "grid-template-columns:1fr 1fr" },
          soundField("v-wake-sound", "When he starts listening", "wake_sound"),
          soundField("v-sent-sound", "When he's heard you", "sent_sound")),
        h("div", { class: "inline-row", style: "grid-template-columns:2fr 2fr 1fr 1fr" }, field("Voice", voiceSelect),
          field("When it's offline", fallbackSelect), field("Speed", rate), field("Volume %", volume)),
        field("Voice effect", effectSelect, "Played on top of the voice. Not available for eSpeak."),
        h("div", { class: "inline-row", style: "grid-template-columns:1fr 1fr" }, field("Quiet from", from), field("Quiet until", to)),
        h("p", { class: "help faint", style: "margin-top:-8px;font-size:12.5px" }, "During quiet hours nothing is spoken. Leave both empty for none."),
        h("div", { class: "field" }, h("span", { class: "label" }, "Speak when a session"), kinds)] : null,
        h("div", { class: "dialog-actions" },
          voiceOn ? h("button", { type: "button", class: "btn ghost", style: "margin-right:auto", onclick: (e) => runAction(e.currentTarget, () => bridge.call("test_voice", { settings: v })) }, icon("play"), "Test") : null,
          h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Cancel"),
          h("button", { type: "submit", class: "btn primary" }, "Save")));
    }, { wide: true });
  }

  function syncTerminal(force = false) {
    const want = RUNNING(data.status) && state.lagoTerminal;
    if (want && (terminalFor !== ORCHESTRATOR || force)) {
      terminalFor = ORCHESTRATOR;
      bridge.call("show_terminal", { agent_id: ORCHESTRATOR }).catch(() => {});
    } else if (!want && (terminalFor || force)) {
      terminalFor = null;
      bridge.call("hide_terminal").catch(() => {});
    }
  }

  async function load() {
    try {
      const next = await bridge.call("orchestrator");
      if (!alive()) return;
      data = next;
      talk.setAvailable(data.mic);
      renderMood();
      renderTools();
      syncTerminal();
      renderNeeds();
      renderChat();
      renderSide();
      renderFocus();
      if (data.events.some((e) => !e.seen)) bridge.call("mark_events_seen").then(pollLive).catch(() => {});
    } catch (error) {
      if (alive()) chat.replaceChildren(errorBox(error, load));
    }
  }
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 1500);
  mouth.listeners.add(renderMood);
  viewCleanup = () => { clearInterval(timer); mouth.listeners.delete(renderMood); talk.close(); bridge.call("hide_terminal").catch(() => {}); };
  bridge.call("hide_terminal").catch(() => {});  // another page's; his own shows on request
  load();
  if (state.talkOnOpen) { state.talkOnOpen = false; talk.toggle(); }
};

/* What you say to Lago: typed (Enter sends) or spoken (the big microphone button or
   Ctrl+Space, again to stop). Speech is transcribed on this machine. Pictures, PDFs and
   text files come along from the paperclip, Ctrl+V or dragging them in; Lago opens each
   one. edit(message) puts one of your messages back in the box: sending it rewinds Lago
   to just before it, as Esc Esc does in Claude Code. */
function talkBox({ onSent, onPhase }) {
  let phase = "idle";   // idle, recording, transcribing
  let timer = null;
  let started = 0;
  let autoSend = true;
  try { autoSend = localStorage.getItem("lago.talk.autosend") !== "0"; } catch { /* default */ }
  const text = h("textarea", { class: "input", rows: 1, "aria-label": "Message to Lago",
    placeholder: "Message Lago… (Enter sends, Shift+Enter for a new line)",
    oninput: () => grow(),
    onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } },
    onpaste: (e) => paste(e) });
  // Waiting to be sent: {data: data URL, name, image} new, or {path, url, name, image} from
  // the message being edited.
  let pending = [];
  let editing = null;  // {id, at} of the message being edited
  const banner = h("div", { class: "edit-banner", hidden: true });
  const strip = h("div", { class: "attach-strip", hidden: true });
  const picker = h("input", { type: "file", multiple: true, hidden: true,
    accept: "image/png,image/jpeg,image/gif,image/webp,application/pdf,text/*,.md,.csv,.json,.log,.yaml,.yml,.toml,.py,.js,.ts,.sh,.sql,.html,.css,.xml,.ipynb",
    onchange: () => { attach([...picker.files]); picker.value = ""; } });
  const attachButton = h("button", { class: "btn icon ghost attach", type: "button", title: "Attach pictures, PDFs or text files (or paste or drop them here)",
    "aria-label": "Attach files", onclick: () => picker.click() }, icon("paperclip"));
  const micButton = h("button", { class: "mic-big", type: "button", onclick: () => toggle() });
  const sendButton = h("button", { class: "btn icon primary send", type: "button", title: "Send", "aria-label": "Send", onclick: () => send() }, icon("paper-plane-right", "ph-fill"));
  const autoSwitch = h("label", { class: "switch-row small", title: "Off: what you said lands in the box for you to check first" },
    h("button", { class: "switch", role: "switch", type: "button", "aria-checked": String(autoSend), onclick: (e) => {
      autoSend = !autoSend;
      e.currentTarget.setAttribute("aria-checked", String(autoSend));
      try { localStorage.setItem("lago.talk.autosend", autoSend ? "1" : "0"); } catch { /* per-session only */ }
    } }), "Send when I stop talking");
  const hint = h("span", { class: "faint talk-keys" }, h("kbd", {}, "Ctrl"), " ", h("kbd", {}, "Space"), " to talk");
  const el = h("div", { class: "lago-composer",
      ondragover: (e) => { if (e.dataTransfer?.types.includes("Files")) { e.preventDefault(); el.classList.add("dropping"); } },
      ondragleave: (e) => { if (!el.contains(e.relatedTarget)) el.classList.remove("dropping"); },
      ondrop: (e) => {
        el.classList.remove("dropping");
        const files = [...(e.dataTransfer?.files || [])];
        if (!files.length) return;
        e.preventDefault();
        attach(files);
      } },
    micButton, banner, strip,
    h("div", { class: "lago-input" }, attachButton, picker, text, sendButton),
    h("div", { class: "lago-composer-foot" }, hint, autoSwitch));

  // Lago Agents checks what each file really is; these only catch the obvious early.
  function attach(files) {
    for (const file of files) {
      const image = /^image\/(png|jpeg|gif|webp)$/.test(file.type);
      const limit = (file.type === "application/pdf" ? 20 : image ? 10 : 5) * 1024 * 1024;
      if (file.type.startsWith("image/") && !image) { toast(`${file.name}: Lago can see PNG, JPEG, GIF and WebP pictures`, "error"); continue; }
      if (file.size > limit) { toast(`${file.name} is over ${limit / 1024 / 1024} MB`, "error"); continue; }
      const reader = new FileReader();
      reader.onload = () => { pending.push({ data: reader.result, name: file.name, image }); renderStrip(); };
      reader.readAsDataURL(file);
    }
    text.focus();
  }

  /* Ctrl+V: files WebKit hands over are attached; ordinary text pastes as text; anything
     else (a screenshot, an image copied from a page, files copied in Files, a path to a
     picture) is read from the system clipboard by the app, as Claude Code does. */
  function paste(e) {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); attach(files); return; }
    const pasted = e.clipboardData?.getData("text/plain") || "";
    if (pasted && !/^(\s*(file:\/\/|\/)\S[^\n]*\n?)+$/.test(pasted)) return;
    e.preventDefault();
    bridge.call("clipboard_files").then((res) => {
      if (res.files.length) {
        for (const f of res.files) pending.push({ data: f.data, name: f.name, image: /^data:image\//.test(f.data) });
        renderStrip();
      } else if (pasted) {
        text.setRangeText(pasted, text.selectionStart, text.selectionEnd, "end");
        grow();
      }
    }).catch((error) => toast(error.message, "error"));
  }

  function renderStrip() {
    strip.hidden = !pending.length;
    fill(strip, pending.map((f) => h("div", { class: `attach-thumb${f.image ? "" : " file"}`, title: f.name },
      f.image ? h("img", { src: f.data || f.url, alt: f.name || "Picture" })
        : [icon(/\.pdf$/i.test(f.name) ? "file-pdf" : "file-text"), h("span", { class: "attach-name" }, f.name)],
      h("button", { type: "button", class: "attach-remove", title: "Remove", "aria-label": `Remove ${f.name || "picture"}`,
        onclick: () => { pending = pending.filter((x) => x !== f); renderStrip(); } }, icon("x")))));
  }

  function renderBanner() {
    banner.hidden = !editing;
    if (!editing) return;
    fill(banner, icon("pencil-simple"),
      h("span", {}, `Editing your message from ${hhmm(editing.at)}. Sending it takes Lago back to just before it: he forgets it and everything after.`),
      h("button", { type: "button", class: "btn small ghost", onclick: () => cancelEdit() }, "Cancel"));
  }

  function edit(m) {
    editing = { id: m.id, at: m.at };
    text.value = m.text;
    pending = m.files.filter((f) => f.kept);
    if (m.files.some((f) => !f.kept)) toast("Some of that message's files are no longer kept; attach them again if he needs them.");
    renderBanner();
    renderStrip();
    grow();
    text.focus();
  }

  function cancelEdit() {
    editing = null;
    text.value = "";
    pending = [];
    renderBanner();
    renderStrip();
    grow();
  }

  function grow() {
    text.style.height = "auto";
    text.style.height = `${Math.min(text.scrollHeight, 160)}px`;
  }

  function setPhase(next) {
    phase = next;
    state.lagoRecording = phase === "recording";
    renderMic();
    onPhase?.(phase);
  }

  function renderMic() {
    clearInterval(timer);
    micButton.classList.toggle("recording", phase === "recording");
    micButton.disabled = phase === "transcribing";
    if (phase === "recording") {
      const tick = () => {
        fill(micButton, icon("stop", "ph-fill"));
        micButton.setAttribute("aria-label", `Stop and send (${Math.floor((Date.now() - started) / 1000)}s)`);
        micButton.title = `Recording ${Math.floor((Date.now() - started) / 1000)}s: click or Ctrl+Space to stop`;
      };
      tick();
      timer = setInterval(tick, 500);
    } else if (phase === "transcribing") {
      fill(micButton, icon("spinner-gap"));
      micButton.setAttribute("aria-label", "Transcribing");
    } else {
      fill(micButton, icon("microphone", "ph-fill"));
      micButton.setAttribute("aria-label", `Talk to ${lagoName()}`);
      micButton.title = "Talk (Ctrl+Space)";
    }
  }

  async function toggle() {
    if (phase === "transcribing") return;
    if (phase === "idle") {
      try {
        await bridge.call("mic_start");
        started = Date.now();
        setPhase("recording");
      } catch (error) { toast(error.message, "error"); }
      return;
    }
    setPhase("transcribing");
    try {
      const res = await bridge.call("mic_stop", { send: autoSend });
      if (!res.text) toast("I didn't catch anything. Try again a little closer to the mic.", "error");
      else if (res.sent) onSent();
      else { text.value = (text.value ? `${text.value} ` : "") + res.text; grow(); text.focus(); }
    } catch (error) { toast(error.message, "error"); }
    setPhase("idle");
  }

  async function send() {
    const value = text.value.trim();
    if (!value && !pending.length) return;
    sendButton.disabled = true;
    try {
      const params = { text: value, files: pending.filter((f) => f.data).map((f) => ({ data: f.data, name: f.name })),
        keep: pending.filter((f) => f.path).map((f) => f.path) };
      const res = editing ? await bridge.call("rewind_lago", { message_id: editing.id, ...params })
        : await bridge.call("say_to_orchestrator", params);
      editing = null;
      text.value = "";
      pending = [];
      renderBanner();
      renderStrip();
      grow();
      if (res.message !== "Sent.") toast(res.message);
      onSent();
    } catch (error) { toast(error.message, "error"); }
    sendButton.disabled = false;
  }

  renderMic();
  window.lagoTalk = toggle;
  return {
    el, toggle, edit,
    phase: () => phase,
    setAvailable(on) { micButton.hidden = !on; autoSwitch.hidden = !on; hint.hidden = !on; },
    setPlaceholder(value) { text.placeholder = value || "Message Lago… (Enter sends, Shift+Enter for a new line)"; },
    close() {
      clearInterval(timer);
      state.lagoRecording = false;
      if (window.lagoTalk === toggle) window.lagoTalk = null;
      if (phase === "recording") bridge.call("mic_cancel").catch(() => {});
    },
  };
}

/* The two modes (config_store.LAGO_MODES): how much Lago decides for you. */
const LAGO_MODES = {
  check: ["Check with me", "He brings you every decision, ready to answer: what to decide, his recommendation, the options."],
  decide: ["Decide for me", "He decides the routine and important things himself on Opus, judged by what matters to the business, and logs each one for you. Only critical ones come to you: production, money, anything irreversible or in your name."],
};

/* Waking Lago without permission checks: your password (checked by sudo, never stored) and
   the mode he works in. {password, mode}; password "" when you already gave it since the
   app opened; null if you cancel. */
async function lagoWakeDialog(mode) {
  const unlocked = await bridge.call("bypass_unlocked").catch(() => false);
  const result = await openDialog((close) => {
    let chosen = mode || "check";
    const input = unlocked ? null : h("input", { id: "pw-input", class: "input", type: "password", autocomplete: "current-password", autofocus: true });
    const card = (value) => h("label", { class: "mode-card" },
      h("input", { type: "radio", name: "lago-mode", value, checked: value === chosen, onchange: () => { chosen = value; } }),
      h("span", {}, h("strong", {}, LAGO_MODES[value][0]), h("span", { class: "muted" }, LAGO_MODES[value][1])));
    return h("form", { onsubmit: (e) => { e.preventDefault(); if (!input || input.value) close({ password: input ? input.value : "", mode: chosen }); } },
      h("h2", {}, "Wake Lago without permission checks?"),
      h("p", { class: "desc" }, "He and every task he opens run all commands without asking you, including over SSH on your projects' servers. They still ask before anything irreversible."),
      h("fieldset", { class: "mode-cards" }, h("legend", {}, "How much should he decide?"), card("check"), card("decide")),
      input ? h("div", { class: "field" }, h("label", { for: "pw-input" }, "Your password (the one you use for sudo)"), input,
        h("p", { class: "help" }, "Checked by sudo on this machine and never stored. Asked once until you close the app.")) : null,
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(undefined) }, "Cancel"),
        h("button", { type: "submit", class: "btn danger" }, icon("lock-key-open"), "Start Unrestricted")));
  });
  return result || null;
}

/* On opening the app: greet, waking Lago (with your password and mode if he should skip
   permissions; cancel starts it with normal checks). */
async function welcome(params = {}) {
  try {
    const res = await bridge.call("welcome", params);
    if (!res.needs_password) { pollLive(); return; }
    const mode = await bridge.call("orchestrator").then((d) => d.mode).catch(() => "check");
    const answer = await lagoWakeDialog(mode);
    welcome(answer !== null ? answer : { restricted: true });
  } catch (error) {
    toast(`Couldn't wake Lago: ${error.message}`, "error");
    if (params.password) welcome();  // wrong password: ask again
  }
}

/* Ctrl+Space anywhere (the app window forwards it from the terminal too): talk to Lago,
   opening his page first if needed. */
function talkShortcut() {
  if (window.lagoTalk) { window.lagoTalk(); return; }
  state.talkOnOpen = true;
  location.hash = "#orchestrator";
}
window.lagoTalkShortcut = talkShortcut;

/* ================================================================ work session */

views.work = (root, alive, wid) => {
  let data = null;
  let terminalFor = null;
  let lastStatus = null;
  let lastKey = "";
  const crumbs = h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, h("a", { href: "#projects" }, "Projects"));
  const head = h("div");
  const toolbar = h("div", { class: "toolbar" });
  const needsSlot = h("div");
  const questionsSlot = h("div");
  let questionsKey = "";
  const docsSlot = h("div");
  const changesSlot = h("div");
  const body = h("div", {}, skeleton(3));
  root.append(crumbs, head, toolbar, needsSlot, questionsSlot, docsSlot, changesSlot, body);
  documentsFold(docsSlot, Number(wid), alive);
  changesFold(changesSlot, Number(wid), alive);
  const conductor = () => `w${wid}`;

  /* Kept while you pick: re-rendered only when the open questions change. */
  function renderQuestions() {
    const key = data.questions.map((q) => q.id).join(",");
    if (key === questionsKey) return;
    questionsKey = key;
    fill(questionsSlot, data.questions.map((q) => h("div", { class: "needs question-card", role: "region", "aria-label": "Question from the lead" },
      h("div", { class: "needs-top" }, icon("question", "ph-fill"), h("p", { class: "prewrap" }, q.text)),
      questionForm(q, q.form, () => { questionsKey = ""; load(); }))));
  }

  function renderHead() {
    fill(crumbs, h("a", { href: "#projects" }, "Projects"), icon("caret-right"), projectCrumb(data.project),
      icon("caret-right"), h("span", {}, data.title));
    fill(head, h("header", { class: "agent-head" },
      h("span", { class: "avatar" }, icon("hexagon")),
      h("div", {}, h("h1", {}, data.title, data.status === "closed" ? h("span", { class: "pill stopped" }, "Closed") : data.held_at ? h("span", { class: "pill over_budget" }, "On hold") : pill(data.activity || data.status_live),
          data.unrestricted ? unrestrictedPill() : null),
        h("p", {}, data.area ? areaName(data.project, data.area) : data.project ? "Whole project" : h("span", { class: "mono" }, data.folder), ` · started ${ago(data.created_at)}`,
          data.budget?.limit ? ` · ${dollars(data.budget.spent)} of its ${dollars(data.budget.limit)} budget` : "",
          data.goal ? [" · ", icon("target"), ` ${data.goal}`] : "",
          data.detail ? [" · ", h("span", { class: "muted" }, data.detail)] : ""))));
  }

  function renderToolbar() {
    const running = RUNNING(data.status_live);
    const act = (fn) => (e) => runAction(e.currentTarget, fn).then(() => { load(); pollLive(); });
    fill(toolbar,
      data.status === "closed" ? h("button", { class: "btn primary", onclick: async (e) => {
          const button = e.currentTarget;
          let password = "";
          if (data.unrestricted) {
            password = await askPassword({ title: "Reopen without permission checks?", body: "This task runs its lead and agents without permission checks.", confirm: "Reopen Unrestricted" });
            if (password === null) return;
          }
          await runAction(button, () => bridge.call("reopen_workspace", { workspace_id: data.id, password }));
          load(); pollLive();
        } }, icon("arrow-counter-clockwise"), "Reopen")
        : !running ? h("button", { class: "btn primary", onclick: act(() => bridge.call("start_agent", { agent_id: conductor() })) }, icon("play"), "Resume Conversation") : null,
      running ? h("button", { class: "btn", "aria-pressed": String(state.terminalOpen), onclick: () => { toggleTerminal(); syncTerminal(true); renderToolbar(); } },
        icon("terminal-window"), state.terminalOpen ? "Hide Terminal" : "Watch Live") : null,
      running ? h("button", { class: "btn", title: "Stop what it's doing now (Esc)", onclick: act(() => bridge.call("press", { agent_id: conductor(), key: "interrupt" })) }, icon("hand"), "Interrupt") : null,
      running ? h("button", { class: "btn ghost", onclick: act(() => bridge.call("open_external", { agent_id: conductor() })) }, icon("arrow-square-out"), "Open in Window") : null,
      data.status !== "closed" ? h("button", { class: "btn ghost", "aria-pressed": String(Boolean(data.held_at)),
        title: data.held_at ? "Let the lead and its agents carry on" : "Interrupt the lead and every agent in this task, and hold new work until you resume",
        onclick: act(() => bridge.call("hold_session", { workspace_id: data.id, on: !data.held_at })) },
        icon(data.held_at ? "play-circle" : "pause-circle"), data.held_at ? "Resume Session" : "Hold Session") : null,
      data.status !== "closed" ? h("button", { class: "btn ghost", "aria-pressed": String(Boolean(data.quiet)),
        title: "A quiet session speaks and rings the bell only when it needs you",
        onclick: act(() => bridge.call("set_session_quiet", { workspace_id: data.id, on: !data.quiet })) },
        icon(data.quiet ? "bell-slash" : "bell"), data.quiet ? "Quiet" : "Make Quiet") : null,
      h("span", { style: "flex:1" }),
      data.status !== "closed" ? h("button", { class: "btn ghost danger", onclick: async (e) => {
        const ok = await confirmDialog({ title: "Close this task?", body: "Stops its lead and agents, and cancels any plan still open. Its conversation stays: you can reopen it later.", confirm: "Close Task", danger: true });
        if (ok) { await runAction(e.currentTarget, () => bridge.call("close_workspace", { workspace_id: data.id })); load(); pollLive(); }
      } }, icon("x-circle"), "Close Task") : null);
  }

  function renderNeeds() {
    const s = data.status_live;
    // A step agent its budget stopped: the Conductor may still be waiting on it.
    const stopped = data.agents.find((a) => liveOf(a).status === "over_budget");
    if (s !== "over_budget" && stopped && data.status !== "closed") {
      needsSlot.replaceChildren(h("div", { class: "needs", role: "alert" }, h("div", { class: "needs-top" }, icon("warning-circle", "ph-fill"),
        h("p", {}, `${state.config.agents[stopped]?.name || stopped}: ${liveOf(stopped).detail || "It used its budget and stopped."}`),
        budgetButtons(stopped, () => { load(); pollLive(); }))));
      return;
    }
    if (!["needs_approval", "needs_input", "over_budget", "exited"].includes(s)) {
      needsSlot.replaceChildren(data.solved && data.status !== "closed" ? h("div", { class: "needs solved", role: "status" },
        h("div", { class: "needs-top" }, icon("seal-check", "ph-fill"),
          h("p", {}, data.outcome ? `You ${data.outcome} the result: the agents' scores count it. Close the session once you're done; you can reopen it later.`
            : "The whole task is done. Is the result right? Your verdict is what scores the agents that worked on it."),
          data.outcome ? null : judgeButtons(data.id, load),
          h("button", { class: "btn small primary", onclick: (e) => runAction(e.currentTarget, () => bridge.call("close_workspace", { workspace_id: data.id })).then(() => { load(); pollLive(); }) },
            icon("x-circle"), "Close Task"))) : "");
      return;
    }
    const act = (key) => (e) => runAction(e.currentTarget, () => bridge.call("press", { agent_id: conductor(), key })).then(load);
    needsSlot.replaceChildren(h("div", { class: "needs", role: "alert" },
      h("div", { class: "needs-top" }, icon("warning-circle", "ph-fill"),
        h("p", {}, { needs_approval: data.detail || "The lead is asking permission to use a tool.",
          needs_input: "Claude Code is asking something before it can continue (for example, whether to trust this folder). Open the terminal and answer it there.",
          over_budget: data.detail || "The session used its budget and stopped.",
          exited: data.detail || "The lead crashed. Restart it to carry on." }[s]),
        s === "over_budget" ? budgetButtons(conductor(), () => { load(); pollLive(); }) : null,
        s === "needs_approval" ? [h("button", { class: "btn small primary", onclick: act("approve") }, icon("check"), "Approve"),
          h("button", { class: "btn small", onclick: act("deny") }, icon("x"), "Deny"),
          alwaysAllowButton(conductor(), load)] : null,
        s === "needs_input" ? h("button", { class: "btn small primary", onclick: () => { state.terminalOpen = true; syncTerminal(true); renderToolbar(); } }, icon("terminal-window"), "Show Terminal") : null,
        s === "exited" ? h("button", { class: "btn small primary", onclick: (e) => runAction(e.currentTarget, () => bridge.call("start_agent", { agent_id: conductor() })).then(() => { load(); pollLive(); }) }, icon("arrow-clockwise"), "Restart") : null),
      data.screen ? h("pre", { class: "screen" }, data.screen.replace(/\n{3,}/g, "\n\n").trim()) : null));
  }

  function renderBody() {
    const key = JSON.stringify([data.pipelines, data.agents.map((a) => [a, liveOf(a).status])]);
    if (key === lastKey) return;
    lastKey = key;
    const [latest, ...older] = data.pipelines;
    const awake = data.agents.filter((a) => isAwake(a));
    fill(body,
      latest ? pipelineCard(latest, true) : h("div", { class: "empty talk-hint" }, icon("chat-circle-dots"),
        data.status === "closed" ? h("strong", {}, "This session is closed. Reopen it to carry on.")
          : RUNNING(data.status_live) ? h("strong", {}, "Talk to the lead in the terminal.")
            : h("strong", {}, "The lead is paused. Resume the conversation to carry on."),
        "When it has a plan (the agents, skills and steps), it appears here for your approval."),
      awake.length ? h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Agents Awake")),
        h("div", { class: "org-children" }, awake.map((a) => agentCard(a, false)))) : null,
      older.length ? h("details", { class: "fold" }, h("summary", {}, icon("flow-arrow"), `Earlier plans (${older.length})`), older.map((p) => pipelineCard(p, false))) : null);
  }

  function pipelineCard(p, expanded) {
    return h("section", { class: "pl-card" },
      h("div", { class: "pl-head" }, h("h2", {}, p.title), pipelinePill(p.status), stepDots(p), h("span", { class: "spacer" }),
        h("a", { class: "btn small ghost", href: `#pipeline/${p.id}` }, "Full View")),
      expanded ? [pipelineNotes(p), h("div", { class: "toolbar" }, pipelineActions(p, load)),
        h("ol", { class: "steps", "aria-label": "Steps" }, p.steps.map(stepItem))]
        : h("p", { class: "muted" }, p.summary || p.goal));
  }

  function syncTerminal(force = false) {
    const want = data.status !== "closed" && RUNNING(data.status_live) && state.terminalOpen;
    if (want && (terminalFor !== conductor() || force)) {
      terminalFor = conductor();
      bridge.call("show_terminal", { agent_id: conductor() }).catch(() => {});
    } else if (!want && (terminalFor || force)) {
      terminalFor = null;
      bridge.call("hide_terminal").catch(() => {});
    }
  }

  async function load() {
    try {
      const next = await bridge.call("workspace", { workspace_id: Number(wid) });
      if (document.visibilityState === "visible") bridge.call("mark_read", { items: [`session:${wid}`] }).catch(() => {});
      if (!alive()) return;
      data = next;
      renderHead();
      const statusKey = [data.status, data.status_live, data.quiet, data.held_at].join("|");
      if (statusKey !== lastStatus) { renderToolbar(); syncTerminal(); lastStatus = statusKey; }
      renderNeeds();
      renderQuestions();
      renderBody();
    } catch (error) {
      if (alive()) body.replaceChildren(errorBox(error, load));
    }
  }
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !byId("dialog").open) load(); }, 2500);
  viewCleanup = () => { clearInterval(timer); bridge.call("hide_terminal").catch(() => {}); };
  load();
};

/* ================================================================ live map, people, roles */

const ROLE_RANK = { watch: 1, operate: 2, admin: 3 };
/* Whether you may do something needing `level` on `project` ("" for work outside projects).
   At the desktop you're the owner; in a browser your role comes from the company. */
function can(level, project = "") {
  const me = state.me;
  if (!me || me.owner || me.admin) return true;
  const role = me.projects?.[project ?? ""];
  return Boolean(role) && ROLE_RANK[role] >= ROLE_RANK[level];
}
const canAnywhere = (level) => !state.me || state.me.owner || state.me.admin || Object.values(state.me.projects || {}).some((r) => ROLE_RANK[r] >= ROLE_RANK[level]);

/* In a browser, show only what your role lets you open. */
function applyRoleChrome() {
  if (!state.web) return;
  const me = state.me || {};
  document.documentElement.classList.toggle("can-operate", canAnywhere("operate"));
  document.documentElement.classList.toggle("is-admin", Boolean(me.owner || me.admin));
  const chip = byId("account-chip");
  if (chip) chip.hidden = true;
  const who = document.querySelector(".sidebar-hint");
  if (who) who.textContent = `Signed in as ${me.name || me.login} · ${me.owner ? "owner" : me.admin ? "company admin" : "member"}`;
}

const stripAnsi = (text) => (text || "").replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "").replace(/\x1b\][^\x07]*\x07/g, "");
const MAP_KIND_ICONS = { project: "folders", task: "list-checks", lead: "compass", agent: "robot", run: "lightning" };

/* ================================================================ live: what needs you, what's going on, each task over time */

/* Runs `fn` too when the page is left, after whatever the page already cleans up. */
function onLeave(fn) {
  const before = viewCleanup;
  viewCleanup = () => { before?.(); fn(); };
}

/* The top of the Live Map: what needs you, with its answer when it's a yes or no, then what
   the agents are doing in plain words. `project` narrows both ("" is work outside projects;
   undefined is everything). */
function liveStrip(project, alive) {
  const spot = h("section", { class: "spotlight", "aria-label": "Needs you" });
  const ticker = h("ol", { class: "ticker", "aria-label": "What the agents are doing" });
  let after = 0, lines = [], lastSpot = "";
  const act = (label, kind, work) => h("button", { type: "button", class: `btn small ${kind}`,
    onclick: (e) => runAction(e.currentTarget, work).then(pollLive) }, label);
  const answers = (n) => {
    const [kind, id] = n.key.split(":");
    if (kind === "pipeline") return act("Approve", "primary", () => bridge.call("decide_pipeline", { pipeline_id: Number(id), decision: "approve" }));
    if (kind === "agent" && n.kind === "permission") return [
      act("Allow", "primary", () => bridge.call("press", { agent_id: id, key: "approve" })),
      act("Deny", "ghost", () => bridge.call("press", { agent_id: id, key: "deny" }))];
    return null;
  };
  const renderSpot = () => {
    const notes = (state.live?.notifications || []).filter((n) => n.urgent && (project === undefined || (n.project || "") === project));
    const key = JSON.stringify(notes.map((n) => [n.key, n.text]));
    if (key === lastSpot) return;
    lastSpot = key;
    const open = (n) => (e) => { e.preventDefault(); followNote(n); };
    fill(spot, notes.length
      ? [h("h2", { class: "spot-head" }, icon("hand", "ph-fill"), "Needs you", h("span", { class: "faint" }, notes.length)),
         h("ul", { class: "spot-list", role: "list" }, notes.slice(0, 6).map((n) => h("li", { class: "spot-card" },
           h("a", { class: "spot-main", href: n.href, onclick: open(n) }, icon(NOTE_ICONS[n.kind] || "info", "ph-fill"),
             h("span", { class: "spot-body" }, h("strong", {}, n.title),
               n.context ? h("small", { class: "faint" }, n.context) : null,
               n.text ? h("span", { class: "spot-text" }, n.text) : null)),
           h("div", { class: "spot-actions" }, answers(n),
             h("a", { class: "btn small ghost", href: n.href, onclick: open(n) }, "Open")))))]
      : h("p", { class: "spot-clear" }, icon("check-circle"), "Nothing needs you right now."));
  };
  const tick = async () => {
    let rows;
    try { rows = await bridge.call("live_activity", { project: project ?? null, after, limit: 6 }); } catch { return; }
    if (!alive()) return;
    if (rows.length) after = rows[0].id;
    lines = [...rows.map((r) => ({ ...r, fresh: true })), ...lines.map((r) => ({ ...r, fresh: false }))].slice(0, 6);
    fill(ticker, lines.length ? lines.map((r) => h("li", { class: r.fresh ? "fresh" : "" },
        h("b", {}, r.agent), ` ${r.sentence}`, h("span", { class: "faint" }, ` · ${ago(r.at)}`)))
      : h("li", { class: "faint" }, "Quiet: no agent is at work right now."));
  };
  renderSpot();
  liveListeners.add(renderSpot);
  tick();
  const timer = setInterval(() => { if (document.visibilityState === "visible") tick(); }, 2000);
  onLeave(() => clearInterval(timer));
  return h("div", { class: "live-strip" }, spot, h("section", { class: "ticker-wrap" }, h("h2", { class: "ticker-head" }, icon("pulse"), "Happening now"), ticker));
}

function lasted(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}

/* What a task has spent so far at API prices, as a small gauge with its budget as a line: amber
   past 80% of it, red once it's used up or stopped. With no budget, just the amount. */
function costMeter({ spent, limit, paused }, workspace) {
  const share = limit ? spent / limit : 0;
  const level = limit && (paused || share >= 1) ? "over" : limit && share >= 0.8 ? "near" : "";
  const scale = Math.max(limit, spent) || 1;
  // Spend is counted per work session: plans in the same session show the same total.
  const title = `Work session #${workspace} has spent ${dollars(spent)} at API prices` +
    (limit ? ` of its ${dollars(limit)} budget${paused ? " (stopped)" : ""}` : " (no budget)");
  return h("span", { class: `tl-cost ${level}`, title, "aria-label": title },
    limit ? h("span", { class: "tl-gauge" }, h("span", { class: "tl-gauge-fill", style: `width:${Math.min(100, (spent / scale) * 100).toFixed(1)}%` }),
      h("span", { class: "tl-gauge-line", style: `left:${((limit / scale) * 100).toFixed(1)}%` })) : null,
    h("span", { class: "tl-cost-text" }, dollars(spent), limit ? h("span", { class: "faint" }, ` / ${dollars(limit)}`) : null));
}

const PLAN_FINISHED = ["done", "failed", "cancelled"];

/* One task's plan as a bar: one piece per step, as long as it took (or has taken by `now`), in
   the colour of where it is. Steps still to come are short dashed pieces at the end, and a
   diamond marks where you approve before the next step. The title opens the task; a finished
   plan offers its replay. `still` (the replay itself) leaves out the links. */
function timelineRow(p, now, { project, waiting = new Set(), still = false } = {}) {
  const href = p.workspace ? `#work/${p.workspace}` : `#pipeline/${p.id}`;
  const started = p.steps.filter((st) => st.start);
  const took = (st) => (st.end || now) - st.start;
  const total = started.reduce((sum, st) => sum + took(st), 0);
  const first = Math.min(...started.map((st) => st.start), Infinity);
  const last = p.steps.some((st) => st.start && !st.end) ? now : Math.max(...started.map((st) => st.end || st.start), 0);
  const needsYou = !still && (p.status === "draft" || waiting.has(href) || waiting.has(`#pipeline/${p.id}`));
  const pieces = p.steps.flatMap((st, i) => [
    h("span", { class: `tl-seg st-${st.status}`, style: `flex-grow:${st.start ? Math.max(took(st), total * 0.04 || 1) : (total * 0.06 || 1)}`,
        title: `${st.title}\n${st.agent_name} · ${STEP_LABELS[st.status] || st.status}${st.start ? ` · ${lasted(took(st))}` : ""}` },
      h("span", { class: "tl-seg-label" }, st.agent_name)),
    st.gate && i < p.steps.length - 1 ? h("span", { class: "tl-gate", title: "You approve before the next step" }) : null]);
  const replayable = !still && PLAN_FINISHED.includes(p.status) && started.length;
  return h("div", { class: `tl-row${needsYou ? " needs-you" : ""}${p.cost ? " with-cost" : ""}${still ? " still" : ""}` },
    h("span", { class: "tl-label" }, still ? h("strong", {}, `${p.steps.filter((st) => st.status === "done").length} of ${p.steps.length} steps done`)
      : h("a", { class: "tl-link", href }, h("strong", {}, p.title)),
      h("small", { class: "faint" }, [project === undefined ? projectName(p.project) : null, PIPELINE_LABELS[p.status] || p.status,
        started.length ? (p.status === "running" ? `running for ${lasted(now - first)}` : `took ${lasted(last - first)}`) : null].filter(Boolean).join(" · "),
        replayable ? h("a", { class: "tl-replay", href: `#replay/${p.id}` }, icon("play-circle"), "Replay") : null)),
    h("span", { class: "tl-track" }, pieces), p.cost ? costMeter(p.cost, p.workspace) : null);
}

/* Every task's plan over the last two days, newest first. */
function timelineView(project, alive) {
  const body = h("div", { class: "timeline" }, skeleton(3));
  const load = async () => {
    let plans;
    try { plans = await bridge.call("timeline", { project: project ?? null, hours: 48 }); } catch (error) { if (alive()) body.replaceChildren(errorBox(error, load)); return; }
    if (!alive()) return;
    const now = Date.now() / 1000;
    const waiting = new Set((state.live?.notifications || []).filter((n) => n.urgent).map((n) => n.href));
    body.replaceChildren(plans.length
      ? h("div", { class: "tl-rows" }, plans.map((p) => timelineRow(p, now, { project, waiting })))
      : h("p", { class: "muted" }, "No task had a plan in the last two days. Each task's steps show here as they run."));
  };
  load();
  liveListeners.add(load);
  return body;
}

const REPLAY_SPEEDS = [1, 10, 60];
const REPLAY_QUIET = 5; // seconds of playback with nothing happening before it skips ahead

/* A finished task played back: its plan's bar and what each agent was doing, as of any moment
   you pick on the scrubber, or playing at 1x, 10x or 60x. Long quiet stretches are skipped. */
views.replay = (root, alive, pid) => {
  const crumbs = h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, h("a", { href: "#map" }, "Live Map"));
  const head = h("div", {}, pageHead("Replay", "Loading the task's history…"));
  const body = h("div", { class: "replay" }, skeleton(3));
  root.append(crumbs, head, body);
  let raf = null;
  onLeave(() => cancelAnimationFrame(raf));
  const load = async () => {
    let data;
    try { data = await bridge.call("replay", { pipeline_id: Number(pid) }); } catch (error) { if (alive()) body.replaceChildren(errorBox(error, load)); return; }
    if (!alive()) return;
    const { plan, start, end, events } = data;
    const span = Math.max(1, end - start);
    const mapHref = `#map/${encodeURIComponent(plan.project || "_none")}`;
    fill(crumbs, h("a", { href: "#map" }, "Live Map"), icon("caret-right"), h("a", { href: mapHref }, plan.project ? projectName(plan.project) : "No project"),
      icon("caret-right"), h("a", { href: `${mapHref}/timeline` }, "Timeline"), icon("caret-right"), h("span", {}, "Replay"));
    fill(head, pageHead(plan.title, `${PIPELINE_LABELS[plan.status] || plan.status} · ${new Date(start * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })} · took ${lasted(span)}`));
    // The moments worth stopping on: each call, each step starting and ending.
    const moments = [...events.map((e) => e.at), ...plan.steps.flatMap((st) => [st.start, st.end]).filter(Boolean)].sort((a, b) => a - b);
    let t = start, speed = 10, playing = false, shownUpTo = -1, lastDraw = 0, last = 0;

    const asOf = (at) => ({ ...plan, status: at >= end ? plan.status : "running",
      steps: plan.steps.map((st) => !st.start || at < st.start ? { ...st, status: "pending", start: null, end: null }
        : st.end && at >= st.end ? st : { ...st, status: "running", end: null }) });
    const row = h("div", { class: "tl-rows" });
    const lines = h("ol", { class: "ticker", "aria-label": "What the agents were doing" });
    const toggle = h("button", { type: "button", class: "btn primary rp-play", onclick: () => (playing ? pause() : play()) });
    const scrub = h("input", { type: "range", class: "rp-scrub", min: 0, max: span, step: "any", value: 0, "aria-label": "Moment in the task",
      oninput: () => { pause(); t = start + Number(scrub.value); draw(true); } });
    const when = h("span", { class: "rp-when" });
    const speeds = h("div", { class: "segmented", role: "radiogroup", "aria-label": "Speed" }, REPLAY_SPEEDS.map((x) => h("button", {
      type: "button", role: "radio", "aria-checked": String(x === speed),
      onclick: () => { speed = x; speeds.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-checked", String(REPLAY_SPEEDS[i] === speed))); } }, `${x}x`)));
    // How busy the agents were along the way, behind the scrubber.
    const BINS = 120;
    const counts = Array(BINS).fill(0);
    for (const e of events) counts[Math.min(BINS - 1, Math.floor(((e.at - start) / span) * BINS))] += 1;
    const peak = Math.max(1, ...counts);
    const density = h("div", { class: "rp-density", "aria-hidden": "true" }, counts.map((c) => h("span", { style: `height:${c ? Math.max(8, (c / peak) * 100) : 0}%` })));
    const marks = h("div", { class: "rp-marks", "aria-hidden": "true" }, plan.steps.filter((st) => st.start).map((st) =>
      h("span", { class: "rp-mark", style: `left:${(((st.start - start) / span) * 100).toFixed(2)}%`, title: `${st.agent_name} starts “${st.title}”` })));

    const setToggle = () => fill(toggle, icon(playing ? "pause" : "play", "ph-fill"), playing ? "Pause" : t >= end ? "Play Again" : "Play");
    function draw(force = false) {
      const now = performance.now();
      if (!force && now - lastDraw < 100) return;
      lastDraw = now;
      fill(row, timelineRow(asOf(t), t, { project: plan.project, still: true }));
      scrub.value = String(t - start);
      when.textContent = `${clock(t * 1000)} · ${lasted(t - start)} in`;
      let upTo = -1;
      for (let i = 0; i < events.length && events[i].at <= t; i++) upTo = i;
      if (upTo === shownUpTo && !force) return;
      const fresh = upTo > shownUpTo && playing ? upTo - shownUpTo : 0;
      shownUpTo = upTo;
      const recent = events.slice(Math.max(0, upTo - 7), upTo + 1).reverse();
      fill(lines, recent.length ? recent.map((e, i) => h("li", { class: i < Math.min(fresh, 3) ? "fresh" : "" },
          h("b", {}, e.agent), ` ${e.sentence}`, h("span", { class: "faint" }, ` · ${clock(e.at * 1000)}`)))
        : h("li", { class: "faint" }, events.length ? "Nothing yet at this moment." : "No record of what the agents did."));
    }
    function frame(stamp) {
      if (!playing || !alive()) return;
      t = Math.min(end, t + ((stamp - last) / 1000) * speed);
      last = stamp;
      const next = moments.find((m) => m > t);
      if (next !== undefined && next - t > REPLAY_QUIET * speed) t = next - speed;  // nothing for a while: skip to just before it
      draw();
      if (t >= end) { pause(); draw(true); return; }
      raf = requestAnimationFrame(frame);
    }
    function play() {
      if (t >= end) { t = start; shownUpTo = -1; }
      playing = true;
      setToggle();
      last = performance.now();
      raf = requestAnimationFrame(frame);
    }
    function pause() { playing = false; cancelAnimationFrame(raf); setToggle(); }

    const note = data.source === "transcripts" ? "Read back from the agents' conversations."
      : !events.length ? "Lago keeps what agents do for 3 days and couldn't find their conversations, so only the steps play back."
        : data.more ? `Showing the first ${events.length} things the agents did.` : null;
    body.replaceChildren(row,
      h("section", { class: "rp-player", "aria-label": "Playback" },
        h("div", { class: "rp-controls" }, toggle, speeds, when),
        h("div", { class: "rp-track" }, density, marks, scrub),
        h("div", { class: "rp-ends faint" }, h("span", {}, clock(start * 1000)), h("span", {}, clock(end * 1000)))),
      h("section", { class: "ticker-wrap" }, h("h2", { class: "ticker-head" }, icon("clock-counter-clockwise"), "Happening then"), lines),
      ...(note ? [h("p", { class: "faint files-note" }, note)] : []));
    setToggle();
    draw(true);
  };
  load();
};

/* The Live Map's pages: everything over time, or the projects; inside a project, its map or
   its timeline. */
function mapTabs(tabs, current) {
  return h("nav", { class: "segmented page-tabs", "aria-label": "Live Map views" },
    tabs.map(([href, label, ic]) => h("a", { href, role: "tab", "aria-selected": String(href === current), class: href === current ? "on" : "" }, icon(ic), label)));
}

/* The tabs inside a project's Live Map. Work outside projects has no folder, so no Files. */
function mapProjectTabs(projectId, current) {
  const here = `#map/${encodeURIComponent(projectId)}`;
  return mapTabs([[here, "Map", "graph"], [`${here}/timeline`, "Timeline", "chart-bar-horizontal"],
    projectId !== "_none" ? [`${here}/files`, "Files", "files"] : null, [`${here}/terminals`, "Terminals", "terminal-window"]].filter(Boolean),
  current ? `${here}/${current}` : here);
}

/* Terminal output with its colours, as spans: the 16 basic colours follow the theme (.ansi-f*,
   .ansi-b*), 256-colour and true-colour codes keep their exact colour. Other escapes drop out. */
function ansiSpans(text) {
  const out = [];
  let st = {}, last = 0;
  const xterm = (n) => {
    if (n < 16) return null;
    if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
    const c = n - 16, step = (v) => (v ? 55 + v * 40 : 0);
    return `rgb(${step(Math.floor(c / 36))},${step(Math.floor(c / 6) % 6)},${step(c % 6)})`;
  };
  const push = (chunk) => {
    if (!chunk) return;
    let [fg, bg] = [st.fg, st.bg];
    if (st.inverse) [fg, bg] = [bg ?? "fg", fg ?? "bg"];
    const cls = [st.bold && "ansi-bold", st.dim && "ansi-dim", st.italic && "ansi-italic", st.underline && "ansi-underline",
      typeof fg === "number" && `ansi-f${fg}`, typeof bg === "number" && `ansi-b${bg}`, fg === "bg" && "ansi-fbg", bg === "fg" && "ansi-bfg"].filter(Boolean).join(" ");
    // An exact background (a diff's red or green) keeps its text readable in either theme.
    if (typeof bg === "string" && bg.startsWith("rgb") && typeof fg !== "string") {
      const [r, g, b] = bg.match(/\d+/g).map(Number);
      fg = 0.299 * r + 0.587 * g + 0.114 * b > 140 ? "rgb(20,20,20)" : "rgb(235,235,235)";
    }
    const style = [typeof fg === "string" && fg.startsWith("rgb") && `color:${fg}`, typeof bg === "string" && bg.startsWith("rgb") && `background:${bg}`].filter(Boolean).join(";");
    out.push(cls || style ? h("span", { class: cls || null, style: style || null }, chunk) : chunk);
  };
  const sgr = (params) => {
    const codes = params.split(/[;:]/).map((x) => Number(x || 0));
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) st = {};
      else if (c === 1) st.bold = true; else if (c === 2) st.dim = true; else if (c === 3) st.italic = true;
      else if (c === 4) st.underline = true; else if (c === 7) st.inverse = true;
      else if (c === 22) st.bold = st.dim = false; else if (c === 23) st.italic = false;
      else if (c === 24) st.underline = false; else if (c === 27) st.inverse = false;
      else if (c >= 30 && c <= 37) st.fg = c - 30; else if (c >= 90 && c <= 97) st.fg = c - 82;
      else if (c >= 40 && c <= 47) st.bg = c - 40; else if (c >= 100 && c <= 107) st.bg = c - 92;
      else if (c === 39) delete st.fg; else if (c === 49) delete st.bg;
      else if (c === 38 || c === 48) {
        const key = c === 38 ? "fg" : "bg";
        if (codes[i + 1] === 5) { const n = codes[i + 2]; st[key] = n < 16 ? n : xterm(n); i += 2; }
        else if (codes[i + 1] === 2) { st[key] = `rgb(${codes[i + 2] | 0},${codes[i + 3] | 0},${codes[i + 4] | 0})`; i += 4; }
      }
    }
  };
  const escapes = /\x1b\[([0-9;:?]*)([ -/]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]/g;
  for (const m of (text || "").matchAll(escapes)) {
    push(text.slice(last, m.index));
    if (m[3] === "m" && !m[2]) sgr(m[1]);
    last = m.index + m[0].length;
  }
  push((text || "").slice(last));
  return out;
}

/* Every running agent's terminal in the project, small and live: refreshed each second while
   you're looking. Click one to open that agent with its terminal showing. */
function terminalsView(project, alive) {
  const grid = h("div", { class: "term-wall" }, skeleton(2));
  const tiles = new Map(); // agent id → { el, screen, head, text }
  let busy = false;
  const open = (id) => {
    state.terminalOpen = true;
    try { localStorage.setItem("lago.terminalOpen", "1"); } catch { /* not kept: fine */ }
    location.hash = `#agent/${id}`;
  };
  const groups = new Map(); // task title → { el, grid }: one row of terminals per task
  const head = (t) => [h("span", { class: `map-dot s-${t.status}` }), icon(MAP_KIND_ICONS[t.kind] || "terminal-window"),
    h("strong", {}, t.label), h("span", { class: "faint term-task" }, STATUS_LABELS[t.status] || t.status)];
  const groupFor = (task) => {
    if (!groups.has(task)) {
      const tilesOf = h("div", { class: "term-grid" });
      const el = h("section", { class: "term-group" }, h("h3", { class: "term-group-head" }, icon(task ? "list-checks" : "lightning"), h("span", {}, task || "Running on their own")), tilesOf);
      groups.set(task, { el, grid: tilesOf });
      grid.append(el);
    }
    return groups.get(task).grid;
  };
  const pull = async () => {
    if (busy || document.visibilityState !== "visible") return;
    busy = true;
    let wall;
    try { wall = await bridge.call("terminal_wall", { project, lines: 40 }); } catch (error) { busy = false; if (alive() && !tiles.size) grid.replaceChildren(errorBox(error, pull)); return; }
    busy = false;
    if (!alive()) return;
    if (!wall.length) {
      tiles.clear();
      groups.clear();
      grid.replaceChildren(h("p", { class: "muted" }, "No agent is running here right now. Their terminals show up here as soon as one starts."));
      return;
    }
    const ids = new Set(wall.map((t) => t.id));
    for (const [id, tile] of tiles) if (!ids.has(id)) { tile.el.remove(); tiles.delete(id); }
    if (!grid.querySelector(".term-tile")) { grid.replaceChildren(); groups.clear(); }
    for (const t of wall) {
      let tile = tiles.get(t.id);
      if (!tile) {
        const screen = h("pre", { class: "term-screen", "aria-hidden": "true" });
        const top = h("span", { class: "term-head" });
        const el = h("button", { type: "button", class: "term-tile", onclick: () => open(t.id) }, top, screen,
          h("span", { class: "term-detail faint" }));
        tile = { el, screen, top, text: null, headKey: "" };
        tiles.set(t.id, tile);
      }
      const home = groupFor(t.task);
      if (tile.el.parentNode !== home) home.append(tile.el);
      const key = JSON.stringify([t.status, t.label, t.task, t.detail]);
      if (key !== tile.headKey) {
        tile.headKey = key;
        fill(tile.top, head(t));
        tile.el.setAttribute("aria-label", `${t.label}${t.task ? `, ${t.task}` : ""}: ${STATUS_LABELS[t.status] || t.status}. Open its terminal.`);
        tile.el.querySelector(".term-detail").textContent = t.detail || "";
      }
      if (t.text !== tile.text) {
        tile.text = t.text;
        tile.screen.replaceChildren(...ansiSpans(t.text));
        tile.screen.scrollTop = tile.screen.scrollHeight;
      }
    }
    for (const [task, group] of groups) if (!group.grid.children.length) { group.el.remove(); groups.delete(task); }
  };
  pull();
  const timer = setInterval(pull, 1000);
  onLeave(() => clearInterval(timer));
  return h("div", {}, grid, h("p", { class: "faint files-note" }, "Each screen refreshes every second while this page is open. Click one to open its terminal."));
}

const FILE_FADE = 600; // seconds a read or an edit stays lit
const CONFLICT_SHOWN = 3600; // seconds a clash between two agents' edits stays red

/* A project's files as the agents touch them: a file glows while it's read and turns amber
   when it's edited, fading over ten minutes. Red marks a file two agents edited close
   together. Click a file to see who touched it, and an edit's changes. */
function filesView(project, alive) {
  const tree = h("div", { class: "files-tree" }, skeleton(4));
  const panel = h("aside", { class: "map-panel", hidden: true, "aria-live": "polite" });
  const note = h("p", { class: "faint files-note" });
  const modes = h("div", { class: "segmented", role: "radiogroup", "aria-label": "Which files" });
  let data = null, files = null, onlyTouched = false, selected = null, treeKey = "", panelKey = "";
  const open = new Set(), closed = new Set(), seen = {};
  const rows = new Map(); // path → its row ("dir/" paths for folders)

  const parents = (path) => path.split("/").slice(0, -1).map((_, i, parts) => parts.slice(0, i + 1).join("/") + "/");
  const heat = (at, now) => at ? Math.max(0, 1 - (now - at) / FILE_FADE) : 0;
  const lit = (t, now) => {
    if (!t) return null;
    const edit = heat(t.edit_at, now), read = heat(t.read_at, now);
    return { conflict: t.conflict && now - t.edit_at < CONFLICT_SHOWN, edit, read };
  };

  const renderModes = () => fill(modes, [["All files", false], ["Touched lately", true]].map(([label, value]) =>
    h("button", { type: "button", role: "radio", "aria-checked": String(onlyTouched === value),
      onclick: () => { onlyTouched = value; renderModes(); renderTree(); } }, label)));

  const toggle = (dir) => {
    if (open.has(dir)) { open.delete(dir); closed.add(dir); } else { open.add(dir); closed.delete(dir); }
    renderTree();
  };

  const fileMeta = () => h("span", { class: "file-meta" }, h("span", { class: "file-who" }), h("span"));
  const nest = (paths) => {
    const top = { dirs: new Map(), files: [] };
    for (const path of paths) {
      let node = top;
      const parts = path.split("/");
      parts.slice(0, -1).forEach((part, i) => {
        if (!node.dirs.has(part)) node.dirs.set(part, { path: parts.slice(0, i + 1).join("/") + "/", dirs: new Map(), files: [] });
        node = node.dirs.get(part);
      });
      node.files.push(path);
    }
    return top;
  };

  function renderTree() {
    const touched = data.touched;
    const paths = onlyTouched ? Object.keys(touched).sort() : [...new Set([...files, ...Object.keys(touched)])].sort();
    const key = JSON.stringify([paths.length, paths.slice(0, 3), Object.keys(touched).sort(), onlyTouched, [...open]]);
    if (key === treeKey) { paint(); return; }
    treeKey = key;
    rows.clear();
    const depth = (n) => `--depth:${n}`;
    const walk = (node, d) => [
      ...[...node.dirs.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, dir]) => {
        const isOpen = onlyTouched || open.has(dir.path);
        const row = h("button", { type: "button", class: "file-row dir", style: depth(d), "aria-expanded": String(isOpen),
          onclick: () => toggle(dir.path) }, icon(isOpen ? "caret-down" : "caret-right"), icon(isOpen ? "folder-open" : "folder", "ph-fill"),
          h("span", { class: "file-name" }, name), fileMeta());
        rows.set(dir.path, row);
        return h("li", {}, row, isOpen ? h("ul", { role: "list" }, walk(dir, d + 1)) : null);
      }),
      ...node.files.map((path) => {
        const row = h("button", { type: "button", class: "file-row", style: depth(d), onclick: () => { selected = path; panelKey = ""; paint(); renderPanel(); } },
          h("span", { class: "file-gap" }), icon("file"), h("span", { class: "file-name" }, path.split("/").pop()), fileMeta());
        rows.set(path, row);
        return h("li", {}, row);
      })];
    const listed = nest(paths);
    tree.replaceChildren(paths.length ? h("ul", { role: "list", class: "files-list" }, walk(listed, 0))
      : h("p", { class: "muted", style: "padding:8px" }, onlyTouched ? "No agent has read or edited a file here in the last day." : "This folder has no files."));
    paint();
  }

  // Lights the rows from the latest data without rebuilding the tree: each row's glow fades with time.
  function paint() {
    const now = Date.now() / 1000;
    const folders = {};
    for (const [path, t] of Object.entries(data.touched)) {
      const l = lit(t, now);
      for (const dir of parents(path)) {
        const f = folders[dir] ||= { edit: 0, read: 0, conflict: false, at: 0 };
        f.edit = Math.max(f.edit, l.edit); f.read = Math.max(f.read, l.read); f.conflict ||= l.conflict;
        f.at = Math.max(f.at, t.touches[0]?.at || 0);
      }
    }
    for (const [path, row] of rows) {
      const t = data.touched[path];
      const l = path.endsWith("/") ? folders[path] : lit(t, now);
      const level = l ? Math.max(l.edit, l.read) : 0;
      row.classList.toggle("lit-edit", !!l && l.edit > 0 && l.edit >= l.read);
      row.classList.toggle("lit-read", !!l && l.read > l.edit);
      row.classList.toggle("conflict", !!l?.conflict);
      row.classList.toggle("touched", !!l);
      row.classList.toggle("on", path === selected);
      row.style.setProperty("--heat", level.toFixed(3));
      const [who, when] = row.querySelector(".file-meta").children;
      const label = [t ? `${t.touches[0].agent} · ` : "", t ? ago(t.touches[0].at) : l?.at ? ago(l.at) : ""];
      if (who.textContent !== label[0]) who.textContent = label[0];
      if (when.textContent !== label[1]) when.textContent = label[1];
      if (t && seen[path] && seen[path] < t.touches[0].at) { row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash"); }
    }
    for (const [path, t] of Object.entries(data.touched)) seen[path] = t.touches[0].at;
  }

  function renderPanel() {
    const t = selected && data.touched[selected];
    const key = JSON.stringify([selected, t?.touches?.[0]?.at, Math.floor(Date.now() / 30000)]);
    if (!selected || key === panelKey) return;
    panelKey = key;
    panel.hidden = false;
    // Repeats in a row (the same agent reading one file five times) read as one line.
    const runs = [];
    for (const touch of t?.touches || []) {
      const last = runs[runs.length - 1];
      if (last && last.agent_id === touch.agent_id && last.kind === touch.kind) last.count += 1;
      else runs.push({ ...touch, count: 1 });
    }
    const diffPath = data.repo_prefix ? `${data.repo_prefix}/${selected}` : selected;
    // Two leads are both "Lead · <project>": their task tells them apart.
    const ids = {};
    for (const x of t?.touches || []) (ids[x.agent] ||= new Set()).add(x.agent_id);
    const who = (x) => ids[x.agent].size > 1 && x.workspace ? `${x.agent} (task #${x.workspace})` : x.agent;
    const editors = [...new Map((t?.touches || []).filter((x) => x.kind === "edit").map((x) => [x.agent_id, who(x)])).values()];
    fill(panel,
      h("div", { class: "map-panel-head" },
        h("div", {}, h("div", { class: "task-top" }, icon("file"), h("strong", { class: "mono", style: "word-break:break-all" }, selected.split("/").pop())),
          h("p", { class: "faint mono", style: "margin:4px 0 0;font-size:12px;word-break:break-all" }, selected)),
        h("button", { class: "btn icon ghost", "aria-label": "Close", onclick: () => { panel.hidden = true; selected = null; paint(); } }, icon("x"))),
      t?.conflict ? h("p", { class: "files-conflict" }, icon("warning", "ph-fill"),
        `${editors.join(" and ")} both edited this file within 15 minutes of each other. Check their changes don't undo each other.`) : null,
      h("h3", { class: "label", style: "margin-top:14px" }, "Who touched it"),
      runs.length ? h("ul", { class: "files-touches", role: "list" }, runs.map((r) => h("li", {},
        icon(r.kind === "edit" ? "pencil-simple" : "eye"),
        h("span", {}, h("b", {}, who(r)), ` ${r.kind === "edit" ? "edited it" : "read it"}${r.count > 1 ? ` ${r.count} times` : ""}`,
          h("span", { class: "faint" }, ` · ${ago(r.at)}`), r.branch ? h("span", { class: "faint" }, ` · on ${r.branch}`) : null),
        r.kind === "edit" && r.workspace ? h("button", { type: "button", class: "btn small ghost", onclick: () => diffDialog(r.workspace, diffPath, r.branch) }, icon("git-diff"), "Changes") : null)))
        : h("p", { class: "muted" }, "No agent read or edited this file in the last day."));
  }

  async function load(withTree) {
    let got;
    try { got = await bridge.call("file_activity", { project, tree: withTree }); } catch (error) { if (alive()) tree.replaceChildren(errorBox(error, () => load(true))); return; }
    if (!alive()) return;
    if (got.missing) { tree.replaceChildren(h("p", { class: "muted", style: "padding:8px" }, `This project's folder (${got.folder}) isn't there.`)); return; }
    if (got.files) files = got.files;
    const now = Date.now() / 1000;
    for (const [path, t] of Object.entries(got.touched)) {
      const fresh = !data || (data.touched[path]?.touches[0]?.at || 0) < t.touches[0].at;
      if (fresh && now - t.touches[0].at < FILE_FADE) for (const dir of parents(path)) if (!closed.has(dir)) open.add(dir);
    }
    data = got;
    note.textContent = `Lit for ten minutes after an agent reads or edits a file; the last day's are listed.${got.more ? ` Showing the first ${files.length.toLocaleString()} files.` : ""}`;
    renderTree();
    if (selected) renderPanel();
  }

  renderModes();
  load(true);
  let polls = 0;
  const timer = setInterval(() => { if (document.visibilityState === "visible") load(++polls % 10 === 0); }, 3000);
  onLeave(() => clearInterval(timer));
  return h("div", {},
    h("div", { class: "files-bar" }, modes,
      h("div", { class: "map-legend" }, h("span", {}, h("span", { class: "file-swatch lit-read" }), "Read"),
        h("span", {}, h("span", { class: "file-swatch lit-edit" }), "Edited"), h("span", {}, h("span", { class: "file-swatch conflict" }), "Two agents edited it"))),
    h("div", { class: "map-wrap" }, h("div", { class: "map-scroll files-scroll" }, tree), panel), note);
}

views.map = (root, alive, projectId, sub) => {
  if (projectId === undefined || projectId === "_projects") { mapIndex(root, alive, projectId === "_projects"); return; }
  const project = decodeURIComponent(projectId === "_none" ? "" : projectId);
  const here = `#map/${encodeURIComponent(projectId)}`;
  const crumbsFor = () => h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, h("a", { href: "#map" }, "Live Map"), icon("caret-right"), h("span", {}, project ? projectName(project) : "No project"));
  if (sub === "timeline") {
    root.append(crumbsFor(), pageHead(project ? projectName(project) : "Work outside projects", "Each task's steps as they ran, and what needs you."),
      liveStrip(project, alive), mapProjectTabs(projectId, "timeline"), timelineView(project, alive));
    return;
  }
  if (sub === "terminals") {
    root.append(crumbsFor(), pageHead(project ? projectName(project) : "Work outside projects", "Every running agent's terminal, live."),
      liveStrip(project, alive), mapProjectTabs(projectId, "terminals"), terminalsView(project, alive));
    return;
  }
  if (sub === "files" && project) {
    root.append(crumbsFor(), pageHead(projectName(project), "The project's files as the agents read and edit them."),
      liveStrip(project, alive), mapProjectTabs(projectId, "files"), filesView(project, alive));
    return;
  }
  let data = null;
  let lastKey = "";
  let selected = null;
  let outputTimer = null;
  const canvas = h("div", { class: "map-canvas" });
  const svgNS = "http://www.w3.org/2000/svg";
  const dots = document.createElementNS(svgNS, "svg"); // handoffs travel here, above the map, so its redraws don't cut them short
  dots.setAttribute("class", "map-dots");
  dots.setAttribute("aria-hidden", "true");
  let geometry = null;
  let seenHandoffs = null;
  const panel = h("aside", { class: "map-panel", hidden: true, "aria-live": "polite" });
  const crumbs = crumbsFor();
  root.append(crumbs, pageHead(project ? projectName(project) : "Work outside projects", "Every task, its lead and the agents working on it, live. Click any of them to watch and act."),
    liveStrip(project, alive), mapProjectTabs(projectId),
    h("div", { class: "map-legend" }, ["working", "needs_you", "idle", "stopped", "exited"].map((st) => h("span", {}, h("span", { class: `map-dot s-${st}` }), STATUS_LABELS[st] || st))),
    h("div", { class: "map-wrap" }, h("div", { class: "map-scroll" }, h("div", { class: "map-stage" }, canvas, dots)), panel));

  const layout = (nodes) => {
    // Columns: the project, its tasks (and agents running alone), each task's lead, the lead's agents.
    const W = 204, GAP = 46, ROW = 92, TOP = 16;
    const byParent = {};
    for (const n of nodes) if (n.kind !== "project") (byParent[n.parent || (n.kind === "lead" ? null : "project")] ||= []).push(n);
    const tasks = nodes.filter((n) => n.kind === "task" || n.kind === "run");
    const pos = {};
    let row = 0;
    for (const t of tasks) {
      if (t.kind === "run") { pos[t.id] = { col: 1, row }; row += 1; continue; }
      const lead = nodes.find((n) => n.kind === "lead" && n.parent === t.id);
      const agents = lead ? nodes.filter((n) => n.kind === "agent" && n.parent === lead.id) : [];
      const span = Math.max(1, agents.length);
      agents.forEach((a, i) => { pos[a.id] = { col: 3, row: row + i }; });
      pos[t.id] = { col: 1, row: row + (span - 1) / 2 };
      if (lead) pos[lead.id] = { col: 2, row: row + (span - 1) / 2 };
      row += span;
    }
    pos.project = { col: 0, row: Math.max(0, (row - 1) / 2) };
    const xy = (p) => ({ x: p.col * (W + GAP) + 8, y: TOP + p.row * ROW });
    return { pos, xy, W, height: TOP * 2 + Math.max(1, row) * ROW, width: 4 * (W + GAP) };
  };

  const nodeBox = (n, at, W) => {
    const status = n.kind === "project" ? "" : n.status;
    return h("button", { type: "button", class: `map-node k-${n.kind} s-${status}${selected === n.id ? " on" : ""}${n.held ? " held" : ""}`, dataset: { id: n.id },
      style: `left:${at.x}px;top:${at.y}px;width:${W}px`, "aria-label": `${n.label}: ${STATUS_LABELS[status] || status || "project"}`,
      onclick: () => { selected = n.id; lastKey = ""; render(); openPanel(n); } },
      h("span", { class: "map-node-top" }, icon(MAP_KIND_ICONS[n.kind] || "circle"), h("strong", {}, n.label),
        n.codex ? h("span", { class: "badge", title: "Runs on Codex" }, "Codex") : null),
      status ? h("span", { class: "map-node-status" }, h("span", { class: `map-dot s-${status}` }), n.held ? "On hold" : STATUS_LABELS[status] || status) : h("span", { class: "map-node-status faint" }, `${data.nodes.filter((x) => x.kind === "task").length} tasks open`),
      n.detail ? h("span", { class: "map-node-detail" }, n.detail) : n.task ? h("span", { class: "map-node-detail" }, n.task.title) : null);
  };

  function render() {
    if (!data) return;
    const key = JSON.stringify([data.nodes, selected]);
    if (key === lastKey) return;
    lastKey = key;
    const { pos, xy, W, height, width } = layout(data.nodes);
    geometry = { pos, xy, W };
    dots.setAttribute("width", width);
    dots.setAttribute("height", height);
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("class", "map-edges");
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    for (const e of data.edges) {
      if (!pos[e.from] || !pos[e.to]) continue;
      const a = xy(pos[e.from]), b = xy(pos[e.to]);
      const x1 = a.x + W, y1 = a.y + 34, x2 = b.x, y2 = b.y + 34, mid = (x1 + x2) / 2;
      const path = document.createElementNS(svgNS, "path");
      path.setAttribute("d", `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`);
      const target = data.nodes.find((n) => n.id === e.to);
      path.setAttribute("class", `s-${target?.status || ""}`);
      svg.append(path);
    }
    canvas.style.height = `${height}px`;
    canvas.style.width = `${width}px`;
    fill(canvas, svg, data.nodes.filter((n) => pos[n.id]).map((n) => nodeBox(n, xy(pos[n.id]), W)),
      data.nodes.length === 1 ? h("div", { class: "map-empty" }, h("strong", {}, "Nothing running here right now."),
        canAnywhere("operate") ? h("button", { type: "button", class: "linklike", onclick: () => newTaskDialog({ project }) }, "Start a task") : "Tasks appear here as soon as someone starts one.") : null);
    if (selected) {
      const n = data.nodes.find((x) => x.id === selected);
      if (n) refreshPanelHead(n);
    }
  }

  let panelHead = null;
  function refreshPanelHead(n) {
    if (!panelHead) return;
    fill(panelHead, h("div", { class: "task-top" }, icon(MAP_KIND_ICONS[n.kind] || "circle"), h("strong", {}, n.label),
      n.kind !== "project" ? h("span", { class: `pill ${n.status}` }, n.held ? "On hold" : STATUS_LABELS[n.status] || n.status) : null),
      n.detail ? h("p", { class: "muted", style: "margin:6px 0 0" }, n.detail) : null,
      n.task ? h("p", { class: "faint", style: "margin:4px 0 0;font-size:12.5px" }, `Task #${n.task.id}: ${n.task.title}`) : null);
  }

  function openPanel(n) {
    clearInterval(outputTimer);
    panel.hidden = false;
    panelHead = h("div");
    refreshPanelHead(n);
    const isAgent = ["lead", "agent", "run"].includes(n.kind);
    const output = h("pre", { class: "map-output" }, isAgent ? "Loading its output…" : "");
    const operate = can("operate", project);
    const message = h("textarea", { class: "input", rows: 2, placeholder: `Message ${n.label}…` });
    const actions = [];
    if (n.kind === "task") {
      actions.push(h("a", { class: "btn small", href: n.href }, icon("arrow-square-out"), "Open Task"));
      if (operate) actions.push(h("button", { class: "btn small ghost", onclick: (e) => runAction(e.currentTarget, () => bridge.call("hold_session", { workspace_id: n.workspace, on: !n.held })).then(load) }, icon(n.held ? "play-circle" : "pause-circle"), n.held ? "Resume" : "Hold"));
    }
    if (isAgent) actions.push(h("a", { class: "btn small ghost", href: `#agent/${n.id}` }, icon("arrow-square-out"), "Full Page"));
    if (isAgent && operate && n.running) actions.push(h("button", { class: "btn small ghost", title: "Stop what it's doing now", onclick: (e) => runAction(e.currentTarget, () => bridge.call("press", { agent_id: n.id, key: "interrupt" }), "Interrupted.").then(load) }, icon("hand"), "Interrupt"));
    fill(panel,
      h("div", { class: "map-panel-head" }, panelHead, h("button", { class: "btn icon ghost", "aria-label": "Close", onclick: () => { panel.hidden = true; selected = null; lastKey = ""; clearInterval(outputTimer); render(); } }, icon("x"))),
      actions.length ? h("div", { class: "controls-line", style: "margin:10px 0" }, actions) : null,
      isAgent ? [h("h3", { class: "label" }, "Live output"), output] : null,
      isAgent && operate ? h("form", { class: "map-message", onsubmit: async (e) => {
          e.preventDefault();
          if (!message.value.trim()) return;
          const ok = await runAction(e.submitter, () => bridge.call("send_message", { agent_id: n.id, text: message.value }), "Sent.");
          if (ok) message.value = "";
        } }, message, h("button", { class: "btn small primary", type: "submit" }, icon("paper-plane-tilt"), "Send")) : null,
      isAgent && !operate ? h("p", { class: "faint", style: "font-size:12.5px" }, "You can watch this project. Ask an admin for operate access to message or interrupt agents.") : null,
      n.kind === "project" ? h("p", { class: "muted" }, "Pick a task, a lead or an agent to see what it's doing.") : null);
    if (!isAgent) return;
    const pull = async () => {
      const snap = await bridge.call("terminal_snapshot", { agent_id: n.id, lines: 120 }).catch(() => null);
      if (!alive() || panel.hidden) { clearInterval(outputTimer); return; }
      if (!snap || !snap.running) { output.textContent = "Not running right now."; return; }
      const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
      output.textContent = stripAnsi(snap.text).replace(/\n{3,}/g, "\n\n").trimEnd();
      if (nearBottom) output.scrollTop = output.scrollHeight;
    };
    pull();
    outputTimer = setInterval(pull, 1500);
  }

  /* Work changing hands: a dot runs along the edge from whoever handed it out (or reported
     back) to whoever gets it, and that node lights up as it lands. With reduced motion, only
     the light. */
  function handoff(x) {
    const from = geometry?.pos[x.from], to = geometry?.pos[x.to];
    if (!from || !to) return;
    const land = () => {
      const node = canvas.querySelector(`.map-node[data-id="${CSS.escape(x.to)}"]`);
      if (!node) return;
      node.classList.remove("got-handoff");
      void node.offsetWidth;
      node.classList.add("got-handoff");
      setTimeout(() => node.classList.remove("got-handoff"), 1400);
    };
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) { land(); return; }
    // The edge between them, drawn left to right; a report runs it backwards.
    const forward = from.col < to.col;
    const a = geometry.xy(forward ? from : to), b = geometry.xy(forward ? to : from);
    const x1 = a.x + geometry.W, y1 = a.y + 34, x2 = b.x, y2 = b.y + 34, mid = (x1 + x2) / 2;
    const at = (t) => {
      const u = 1 - t;
      return [u * u * u * x1 + 3 * u * u * t * mid + 3 * u * t * t * mid + t * t * t * x2,
        u * u * u * y1 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y2];
    };
    const halo = document.createElementNS(svgNS, "circle");
    const dot = document.createElementNS(svgNS, "circle");
    halo.setAttribute("r", 11);
    halo.setAttribute("class", "map-dot-halo");
    dot.setAttribute("r", 5);
    dot.setAttribute("class", `map-handoff ${x.kind}`);
    dots.append(halo, dot);
    const start = performance.now(), length = 1100;
    const step = (time) => {
      const k = Math.min(1, (time - start) / length);
      const eased = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      const [px, py] = at(forward ? eased : 1 - eased);
      for (const c of [halo, dot]) { c.setAttribute("cx", px.toFixed(1)); c.setAttribute("cy", py.toFixed(1)); }
      if (k < 1 && alive()) requestAnimationFrame(step);
      else { halo.remove(); dot.remove(); land(); }
    };
    requestAnimationFrame(step);
  }

  function playHandoffs() {
    const now = Date.now() / 1000;
    const fresh = (data.handoffs || []).filter((x) => !seenHandoffs?.has(x.id) && (seenHandoffs || now - x.at < 8));
    seenHandoffs ||= new Set();
    for (const x of data.handoffs || []) seenHandoffs.add(x.id);
    fresh.forEach((x, i) => setTimeout(() => alive() && handoff(x), i * 250));
  }

  async function load() {
    try {
      data = await bridge.call("project_map", { project_id: project });
      if (alive()) { render(); playHandoffs(); }
    } catch (error) {
      if (alive()) fill(canvas, errorBox(error, load));
    }
  }
  const timer = setInterval(() => { if (document.visibilityState === "visible") load(); }, 2000);
  onLeave(() => { clearInterval(timer); clearInterval(outputTimer); });
  load();
};

/* The Live Map's front page: your projects as ecosystems, with what's running in each. */
async function mapIndex(root, alive, projectsTab) {
  const body = h("div", {}, skeleton(3));
  const mine = h("div");
  root.append(pageHead("Live Map", "What needs you, what the agents are doing, and every task's steps over time. Open a project to see its tasks and agents as a map."),
    mine, liveStrip(undefined, alive), mapTabs([["#map", "Timeline", "chart-bar-horizontal"], ["#map/_projects", "Projects", "folders"]], projectsTab ? "#map/_projects" : "#map"));
  if (state.web && !state.me?.owner) subscriptionCard(mine, alive);
  if (!projectsTab) { root.append(timelineView(undefined, alive)); return; }
  root.append(body);
  const load = async () => {
    let cards, tasks;
    try { [cards, tasks] = await Promise.all([bridge.call("projects"), bridge.call("my_tasks")]); } catch (error) { if (alive()) body.replaceChildren(errorBox(error, load)); return; }
    if (!alive()) return;
    const live = state.live?.agents || {};
    const working = (pid) => Object.entries(state.config.agents).filter(([id, a]) => a.swarm && !a.orchestrator && (a.project || "") === pid && ["working", "waiting_on_team", "agents_working"].includes(live[id]?.status)).length;
    const openTasks = (pid) => tasks.filter((t) => t.open && t.kind === "session" && t.project === pid).length;
    const tile = (pid, name, sub) => h("a", { class: "map-tile", href: `#map/${pid === "" ? "_none" : encodeURIComponent(pid)}` },
      h("span", { class: "map-tile-top" }, icon(pid ? "folders" : "folder-dashed"), h("strong", {}, name)),
      h("span", { class: "faint" }, sub),
      h("span", { class: "map-tile-stats" }, h("span", {}, h("b", {}, openTasks(pid)), " open tasks"), h("span", {}, h("b", {}, working(pid)), " agents working")),
      state.me ? h("span", { class: "badge" }, state.me.owner ? "Owner" : (state.me.admin ? "Admin" : (state.me.projects?.[pid] || "").replace(/^./, (c) => c.toUpperCase()))) : null);
    body.replaceChildren(h("div", { class: "map-tiles" },
      cards.map((c) => tile(c.id, c.name, c.description || c.cwd)),
      (!state.me || state.me.projects?.[""] || state.me.admin || state.me.owner) ? tile("", "No project", "Work outside any project") : null));
  };
  load();
  const timer = setInterval(() => { if (document.visibilityState === "visible") load(); }, 4000);
  onLeave(() => clearInterval(timer));
}

/* After joining a company hosted elsewhere: where it lives, and a way back to it. */
views.joined = (root) => {
  const url = state.joined || state.config?.company?.joined || "";
  root.append(pageHead("Your company", "Its agents run on the company's computer. You use them in your browser."),
    h("section", { class: "provider-row on" }, icon("buildings"),
      h("div", { class: "row-main" }, h("span", { class: "row-title" }, "Opened in your browser"),
        h("span", { class: "row-sub mono" }, url || "No address saved")),
      url ? h("div", { class: "row-actions" }, h("button", { class: "btn small primary", onclick: () => bridge.call("open_url", { url }).catch(() => window.open(url, "_blank", "noopener")) }, icon("arrow-square-out"), "Open Again")) : null),
    h("p", { class: "muted", style: "margin-top:12px" }, "There, ask to join. Once an admin lets you in, you'll see the projects you have access to, live, and connect your own Claude or ChatGPT subscription to start tasks."));
};

/* A member's own subscription: their tasks run on it (member_accounts.py). */
async function subscriptionCard(slot, alive) {
  let sub;
  try { sub = await bridge.call("my_subscription"); } catch { return; }
  if (!alive()) return;
  const name = sub.provider === "codex" ? "ChatGPT" : "Claude";
  const ok = sub[sub.provider];
  fill(slot, h("section", { class: `provider-row${ok ? " on" : ""}`, style: "margin-bottom:16px" },
    icon(sub.provider === "codex" ? "open-ai-logo" : "asterisk"),
    h("div", { class: "row-main" }, h("span", { class: "row-title" }, `Your ${name} subscription`),
      h("span", { class: "row-sub" }, ok ? "Connected: the tasks you start run on it." : `Not connected yet. Connect it to start tasks: they run on your own ${name} plan, not anyone else's.`)),
    h("div", { class: "row-actions" }, h("button", { class: `btn small${ok ? " ghost" : " primary"}`, onclick: () => loginDialog(sub.provider, () => subscriptionCard(slot, alive)) },
      icon("sign-in"), ok ? "Connect Again" : "Connect"))));
}

/* The login runs on the company's computer; you see its terminal here and paste the code it asks for. */
async function loginDialog(provider, done) {
  try { await bridge.call("connect_my_subscription", { provider }); } catch (error) { toast(error.message, "error"); return; }
  const screen = h("pre", { class: "map-output", style: "min-height:180px" }, "Starting the login…");
  const code = h("input", { class: "input mono", placeholder: provider === "codex" ? "(nothing to paste: enter the code on the page the link opens)" : "Paste the code the login page gives you" });
  let timer = null;
  const pull = async () => {
    const snap = await bridge.call("my_login_screen").catch(() => null);
    if (!snap) return;
    if (!snap.running) { screen.textContent = "The login has finished. Close this, then check that it's connected."; return; }
    // Links in the login's output open in a new tab.
    const text = stripAnsi(snap.text).trimEnd();
    fill(screen, text.split(/(https:\/\/[^\s"'<>]+)/g).map((part, i) => i % 2
      ? h("a", { href: part, target: "_blank", rel: "noopener noreferrer" }, part) : part));
  };
  const closed = openDialog((close) => h("div", {},
    h("h2", {}, `Connect your ${provider === "codex" ? "ChatGPT" : "Claude"} subscription`),
    h("p", { class: "desc" }, provider === "codex" ? "Open the link below on any device, sign in to ChatGPT and enter the code shown." : "Open the link below, sign in to Claude, and paste the code it gives you here."),
    screen,
    provider === "claude" ? h("form", { class: "controls-line", onsubmit: (e) => { e.preventDefault(); runAction(e.submitter, () => bridge.call("my_login_send", { text: code.value }), "Sent.").then(() => { code.value = ""; }); } },
      code, h("button", { class: "btn small primary", type: "submit" }, "Send Code")) : null,
    h("div", { class: "dialog-actions" }, h("button", { class: "btn", onclick: () => close() }, "Done"))), { wide: true });
  pull();
  timer = setInterval(pull, 1500);
  await closed;
  clearInterval(timer);
  done?.();
}

/* People: who's in the company, what they may do in each project, and browser access. */
views.people = (root, alive) => {
  const body = h("div", {}, skeleton(4));
  root.append(pageHead("People", "Who can see and steer your agents, and what each may do in each project."), body);
  const roleSelect = (value, onchange, withDefault = false) => h("select", { class: "select", onchange: (e) => onchange(e.target.value) },
    [...(withDefault ? [["", "Company role"]] : []), ["none", "No access"], ["watch", "Watch"], ["operate", "Operate"], ["admin", "Admin"]]
      .filter(([v]) => withDefault || v !== "none").map(([v, l]) => h("option", { value: v, selected: v === (value ?? "") }, l)));
  const load = async () => {
    let p, net;
    try { [p, net] = await Promise.all([bridge.call("people"), bridge.call("company_network")]); } catch (error) { if (alive()) body.replaceChildren(errorBox(error, load)); return; }
    if (!alive()) return;
    const pending = p.members.filter((m) => m.status === "pending");
    const active = p.members.filter((m) => m.status === "active");
    const act = (fn, msg) => (e) => runAction(e.currentTarget, fn, msg).then(load);
    const running = net.state === "Running";
    const serving = running && net.via === "company" && p.web.on;
    const onTailnet = p.web.via !== "company";
    const link = (url, label) => h("a", { href: url, target: "_blank", rel: "noopener noreferrer", onclick: (e) => { e.preventDefault(); openUrl(url); } }, label);
    const blockInput = h("input", { class: "input", placeholder: "name@gmail.com", "aria-label": "Tailscale login to keep out" });
    // Browser access on this computer's own tailnet: the default.
    const thisTailnet = h("section", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, "Browser access")),
      h("p", { class: "desc" }, "People reach Lago Agents in a browser over your Tailscale network; Tailscale tells it who they are, so there are no passwords. Newcomers can only ask to join. People you keep out see nothing at all."),
      onTailnet && p.web.on ? h("div", { class: "provider-row on" }, icon("globe"),
          h("div", { class: "row-main" }, h("span", { class: "row-title" }, "Browser access on"), h("span", { class: "row-sub mono" }, p.web.url || "")),
          h("div", { class: "row-actions" }, h("button", { class: "btn small ghost", onclick: act(() => bridge.call("set_web_access", { on: false })) }, "Turn Off")))
        : h("button", { class: "btn primary", onclick: act(() => bridge.call("use_this_tailnet")) }, icon("globe"), "Turn On Browser Access"),
      onTailnet && p.web.on ? h("ol", { class: "criteria" },
        h("li", {}, "Share this computer with them: ", link("https://login.tailscale.com/admin/machines", "Tailscale admin → Machines"), " → this computer → Share, with their email."),
        h("li", {}, "They install Tailscale, accept, and open ", h("span", { class: "mono" }, p.web.url || ""), "."),
        h("li", {}, "They press Ask to Join; you let them in below and pick their roles.")) : null);
    const keptOut = h("section", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, `Kept out (${p.blocked.length})`)),
      h("p", { class: "desc" }, "People on your tailnet for something else. They can't see the company or ask to join."),
      h("div", { class: "rows" }, p.blocked.map((login) => h("div", { class: "row" },
        h("div", { class: "row-main" }, h("span", { class: "row-title" }, login)),
        h("div", { class: "row-actions" }, h("button", { class: "btn small ghost", onclick: act(() => bridge.call("unblock_person", { login })) }, "Allow to Ask"))))),
      h("form", { class: "controls-line", style: "margin-top:10px", onsubmit: (e) => { e.preventDefault(); act(() => bridge.call("block_person", { login: blockInput.value }))({ currentTarget: e.submitter }); } },
        blockInput, h("button", { type: "submit", class: "btn small" }, icon("prohibit"), "Keep Out")));
    body.replaceChildren(
      thisTailnet,
      h("details", { class: "section" },
        h("summary", { class: "faint" }, "Or use a separate company network"),
        h("p", { class: "desc" }, "The company gets its own Tailscale network, separate from your personal one: this computer joins it with a second connection used only by Lago Agents. People you invite there open the company's address in a browser; Tailscale tells Lago Agents who they are, so there are no passwords."),
        !net.running || (!running && !net.auth_url) ? h("button", { class: "btn primary", onclick: act(() => bridge.call("company_network_login"), "Open the link that appears to sign in.") }, icon("network"), "Set Up the Company Network")
          : !running ? h("div", { class: "needs" }, h("div", { class: "needs-top" }, icon("sign-in", "ph-fill"),
              h("p", {}, "Open this link in a browser where you sign in with the company's account (", h("b", {}, "not your personal one"), "), or create the account there: ",
                h("a", { href: net.auth_url, target: "_blank", rel: "noopener noreferrer", onclick: (e) => { e.preventDefault(); openUrl(net.auth_url); } }, net.auth_url)),
              h("button", { class: "btn small", onclick: () => load() }, icon("arrow-clockwise"), "I've Signed In")))
            : h("div", { class: "provider-row on" }, icon("network"),
              h("div", { class: "row-main" }, h("span", { class: "row-title" }, `Connected to ${net.tailnet || "the company tailnet"}`),
                h("span", { class: "row-sub" }, `as ${net.login} · this computer is ${net.name}`)),
              h("div", { class: "row-actions" }, serving ? h("span", { class: "pill p-running" }, "Browser access on")
                : h("button", { class: "btn small primary", onclick: act(() => bridge.call("company_network_use")) }, "Turn On Browser Access Here"))),
        serving ? h("div", { class: "fold-body", style: "padding:10px 0 0" },
          h("p", {}, "Address: ", h("span", { class: "mono" }, net.url)),
          h("ol", { class: "criteria" },
            h("li", {}, "Invite someone to the company's tailnet: ", h("a", { href: "https://login.tailscale.com/admin/users", target: "_blank", rel: "noopener noreferrer", onclick: (e) => { e.preventDefault(); openUrl("https://login.tailscale.com/admin/users"); } }, "Tailscale admin → Users → Invite users"), " (signed in with the company's account)."),
            h("li", {}, "They install Tailscale, accept the invite, and open ", h("span", { class: "mono" }, net.url), "."),
            h("li", {}, "They press Ask to Join; you let them in below and pick their roles."))) : null),
      pending.length ? h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, `Asking to join (${pending.length})`)),
        h("div", { class: "rows" }, pending.map((m) => {
          let role = "watch";
          return h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-title" }, m.name), h("span", { class: "row-sub" }, `${m.login} · asked ${ago(m.requested_at)}`)),
            h("div", { class: "row-actions" }, roleSelect(role, (v) => { role = v; }),
              h("button", { class: "btn small primary", onclick: (e) => runAction(e.currentTarget, () => bridge.call("admit_person", { login: m.login, role }), `${m.name} is in.`).then(load) }, "Let In"),
              h("button", { class: "btn small ghost", onclick: act(() => bridge.call("remove_person", { login: m.login })) }, "Refuse"),
              h("button", { class: "btn small ghost danger", title: "They can't see the company or ask again", onclick: act(() => bridge.call("block_person", { login: m.login })) }, "Keep Out")));
        }))) : null,
      active.length && Object.keys(p.projects).length ? h("section", { class: "section" },
        h("div", { class: "section-head" }, h("h2", {}, "Who answers each project")),
        h("p", { class: "desc" }, "A project's questions, permission requests and plans to approve ring that person's bell instead of yours. You still see them, quieter, and Lago stops saying them aloud here."),
        h("div", { class: "rows" }, Object.entries(p.projects).map(([pid, name]) => h("div", { class: "row" },
          h("div", { class: "row-main" }, h("span", { class: "row-title" }, name)),
          h("div", { class: "row-actions" }, h("select", { class: "select", "aria-label": `Who answers ${name}`,
            onchange: (e) => runAction(null, () => bridge.call("set_project_handler", { project: pid, login: e.target.value })).then(load) },
            [["", "You"], ...active.filter((m) => ["operate", "admin"].includes(m.projects[pid] || m.role)).map((m) => [m.login, m.name])]
              .map(([v, l]) => h("option", { value: v, selected: v === (p.handlers[pid] || "") }, l)))))))) : null,
      h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, `Members (${active.length + 1})`)),
        h("div", { class: "rows" },
          h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", { class: "row-title" }, `${state.config.user_name || "You"} (owner)`), h("span", { class: "row-sub" }, `${p.company.owner || "this computer"} · admin everywhere`))),
          active.map((m) => h("div", { class: "row person-row" },
            h("div", { class: "row-main" }, h("span", { class: "row-title" }, m.name, m.admin ? h("span", { class: "badge", style: "margin-left:8px" }, "Company admin") : null),
              h("span", { class: "row-sub" }, `${m.login}${m.last_seen ? ` · seen ${ago(m.last_seen)}` : ""}`),
              h("details", {}, h("summary", { class: "faint" }, "Roles per project"),
                h("div", { class: "person-projects" }, [...Object.entries(p.projects), ["", "No project"]].map(([pid, name]) => h("label", {}, h("span", {}, name),
                  roleSelect(m.projects[pid], (v) => runAction(null, () => bridge.call("set_person_role", { login: m.login, project: pid, role: v || null }), "Saved."), true)))))),
            h("div", { class: "row-actions" },
              roleSelect(m.role, (v) => runAction(null, () => bridge.call("admit_person", { login: m.login, role: v, admin: Boolean(m.admin) }), "Saved.").then(load)),
              h("button", { class: "btn small ghost", title: "Company admins manage people and settings", onclick: act(() => bridge.call("admit_person", { login: m.login, role: m.role, admin: !m.admin })) }, m.admin ? "Make Member" : "Make Admin"),
              h("button", { class: "btn small ghost danger", onclick: async (e) => {
                if (await confirmDialog({ title: `Remove ${m.name}?`, body: "They lose access at once. Their history stays.", confirm: "Remove", danger: true })) act(() => bridge.call("remove_person", { login: m.login }))(e);
              } }, "Remove")))))),
      keptOut);
  };
  load();
};

/* ================================================================ first start and New Task */

/* An example request in the user's own terms: their project names when they have some. */
function exampleRequest() {
  const names = Object.values(state.config?.projects || {}).map((p) => p.name).filter(Boolean);
  if (names.length >= 2) return `Fix the login bug in ${names[0]}, and draft the launch post for ${names[1]}.`;
  if (names.length === 1) return `Look around ${names[0]} and tell me what's worth fixing first.`;
  return "Research the three best note-taking apps and compare their prices.";
}

/* The welcome wizard (onboarding.py): one question per screen, ending with Lago ready. */
function onboardingWizard() {
  return new Promise((resolve) => {
    const answers = { name: state.config?.user_name || "", folder: "", projectName: "", skipProject: false, provider: "", company: "", hub: "", persona: state.persona || "lago" };
    const checks = {};
    let step = 0;
    let detected = null;
    const box = h("div", { class: "onboard-box" });
    const overlay = h("div", { class: "onboard", role: "dialog", "aria-modal": "true", "aria-label": "Welcome to Lago Agents" }, box);
    document.body.append(overlay);
    const dots = () => h("div", { class: "onboard-dots", "aria-label": `Step ${step + 1} of 5` }, [0, 1, 2, 3, 4].map((i) => h("span", { class: i <= step ? "on" : "" })));
    const nav = (next, { label = "Continue", disabled = false, back = true } = {}) => h("div", { class: "onboard-nav" },
      back && step > 0 ? h("button", { type: "button", class: "btn ghost", onclick: () => { step -= 1; render(); } }, icon("arrow-left"), "Back") : h("span"),
      h("button", { type: "submit", class: "btn primary", disabled }, label, icon("arrow-right")));
    // Joining a company hosted on another computer: check its address, open it in the browser.
    const joinScreen = () => {
      const address = h("input", { class: "input mono onboard-input", value: answers.hub, placeholder: "http://company-computer:7480", autofocus: true, oninput: (e) => { answers.hub = e.target.value; } });
      const hint = h("p", { class: "onboard-hint" }, "Ask an admin of your company for its address. Your VPN (Tailscale) must be on.");
      return h("form", { onsubmit: async (e) => {
          e.preventDefault();
          const ok = await runAction(e.submitter, () => bridge.call("join_company", { url: answers.hub }));
          if (ok) { overlay.remove(); resolve({ joined: answers.hub }); }
        } },
        h("h1", {}, "Join your company"),
        h("p", { class: "onboard-sub" }, "Your company's agents run on its own computer. You open them in your browser, where you ask to join; an admin lets you in."),
        h("label", { class: "field" }, h("span", {}, "The company's address"), address), hint,
        h("div", { class: "onboard-nav" }, h("button", { type: "button", class: "btn ghost", onclick: () => { answers.mode = ""; render(); } }, icon("arrow-left"), "Back"),
          h("button", { type: "submit", class: "btn primary" }, "Check and Open", icon("arrow-right"))));
    };
    const screens = [
      () => h("form", { onsubmit: (e) => e.preventDefault() },
        h("img", { src: "lago-agents-icon.svg", alt: "", width: 56, height: 56, class: "onboard-logo" }),
        h("h1", {}, "Welcome to Lago Agents"),
        h("p", { class: "onboard-sub" }, "A place where your company's AI agents work, and where everyone with access can see them live and steer them."),
        h("div", { class: "provider-cards" },
          h("button", { type: "button", class: "provider-card", onclick: () => { answers.mode = "new"; step = 1; render(); } },
            icon("buildings"), h("b", {}, "Start a new company"), h("small", {}, "Agents run on this computer; people join over your VPN")),
          h("button", { type: "button", class: "provider-card", onclick: () => { answers.mode = "join"; render(); } },
            icon("sign-in"), h("b", {}, "Join an existing one"), h("small", {}, "Your company already runs Lago Agents")))),
      () => {
        const input = h("input", { class: "input onboard-input", value: answers.name, placeholder: "e.g. Alex", autofocus: true, oninput: (e) => { answers.name = e.target.value; } });
        const companyInput = h("input", { class: "input onboard-input", value: answers.company, placeholder: "e.g. Acme", oninput: (e) => { answers.company = e.target.value; } });
        return h("form", { onsubmit: (e) => { e.preventDefault(); if (answers.name.trim()) { step = 2; render(); } } },
          h("img", { src: "lago-agents-icon.svg", alt: "", width: 56, height: 56, class: "onboard-logo" }),
          h("h1", {}, "Your company"),
          h("p", { class: "onboard-sub" }, "Lago is your assistant. You tell him what you want done; he gets a team of AI agents on it and keeps you posted. They run on your Claude or ChatGPT subscription."),
          h("label", { class: "field" }, h("span", {}, "What should Lago call you?"), input),
          h("label", { class: "field" }, h("span", {}, "Your company's name"), companyInput), nav());
      },
      () => {
        // Connect a model: a Claude or a ChatGPT subscription. Picking one checks it for real.
        const check = async (provider) => {
          checks[provider] = { busy: true };
          render();
          const res = await bridge.call("test_provider", { provider }).catch((e) => ({ ok: false, message: e.message }));
          checks[provider] = res;
          if (step === 2) render();
        };
        const pick = (provider) => { answers.provider = provider; if (!checks[provider]) check(provider); else render(); };
        const card = (provider, title, sub, logo) => h("button", { type: "button", class: `provider-card${answers.provider === provider ? " on" : ""}`,
          "aria-pressed": String(answers.provider === provider), onclick: () => pick(provider) },
          icon(logo), h("b", {}, title), h("small", {}, sub));
        const c = checks[answers.provider];
        const status = !answers.provider ? null
          : !c || c.busy ? h("p", { class: "onboard-status" }, icon("circle-notch"), "Testing the connection…")
            : c.ok ? h("p", { class: "onboard-status ok" }, icon("check-circle", "ph-fill"), c.message)
              : h("div", { class: "onboard-status warn" }, icon("warning-circle"), h("span", {}, c.message));
        return h("form", { onsubmit: (e) => { e.preventDefault(); if (c?.ok) { step = 3; render(); } } },
          h("h1", {}, "Connect a model"),
          h("p", { class: "onboard-sub" }, "Lago and his agents run on your subscription, through its app on this computer."),
          h("div", { class: "provider-cards" },
            card("claude", "Claude", "Subscription · Claude Code", "asterisk"),
            card("codex", "ChatGPT", "Subscription · Codex", "open-ai-logo")),
          status,
          answers.provider && c && !c.busy && !c.ok ? h("div", { class: "controls-line", style: "justify-content:center" },
            h("button", { type: "button", class: "btn primary", onclick: (e) => runAction(e.currentTarget, () => bridge.call("login_provider", { provider: answers.provider })) }, icon("sign-in"), "Log In"),
            h("button", { type: "button", class: "btn", onclick: () => check(answers.provider) }, icon("arrow-clockwise"), "Test Again")) : null,
          nav(null, { disabled: !c?.ok }));
      },
      () => {
        const folder = h("input", { class: "input mono onboard-input", value: answers.folder, placeholder: "~/projects/my-app", autofocus: true,
          oninput: (e) => { answers.folder = e.target.value; detected = null; hint.replaceChildren(); },
          onchange: async () => { await look(); } });
        const name = h("input", { class: "input onboard-input", value: answers.projectName, placeholder: "Its name", oninput: (e) => { answers.projectName = e.target.value; } });
        const hint = h("p", { class: "onboard-hint" }, detected?.message || "");
        const look = async () => {
          if (!answers.folder.trim()) return;
          detected = await bridge.call("detect_folder", { folder: answers.folder }).catch((e) => ({ ok: false, message: e.message }));
          hint.textContent = detected.message || "";
          hint.className = `onboard-hint${detected.ok ? "" : " error"}`;
          if (detected.ok && !answers.projectName) { answers.projectName = detected.name; name.value = detected.name; }
        };
        return h("form", { onsubmit: async (e) => {
            e.preventDefault();
            if (!answers.folder.trim()) { answers.skipProject = true; step = 4; render(); return; }
            if (!detected) await look();
            if (detected?.ok) { answers.skipProject = false; step = 4; render(); }
          } },
          h("h1", {}, "Your first project"),
          h("p", { class: "onboard-sub" }, "The folder where your code or work lives. Agents work there, and Lago learns its stack and repository from it."),
          h("label", { class: "field" }, h("span", {}, "Folder"), folder), hint,
          h("label", { class: "field" }, h("span", {}, "Name"), name),
          h("p", { class: "onboard-hint" }, h("button", { type: "button", class: "linklike", onclick: () => { answers.skipProject = true; answers.folder = ""; step = 4; render(); } }, "Skip: I'll add projects later, or work without one")),
          nav());
      },
      () => h("form", { onsubmit: async (e) => {
          e.preventDefault();
          const ok = await runAction(e.submitter, async () => {
            const res = await bridge.call("finish_onboarding", { name: answers.name, provider: answers.provider, company_name: answers.company,
              project: answers.skipProject ? null : { folder: answers.folder, name: answers.projectName } });
            if (featureOn("personas")) {
              await bridge.call("set_persona", { persona: answers.persona, tell: false });  // his voice comes with him
              applyPersona(answers.persona);
            }
            state.config = res.config;
            return { ok: true, message: `Welcome, ${answers.name.trim()}.` };
          });
          if (ok) { overlay.remove(); resolve(); }
        } },
        h("h1", {}, featureOn("personas") ? "Who do you want to talk to?" : "You're all set"),
        h("p", { class: "onboard-sub" }, (answers.skipProject ? `Lago will greet you, ${answers.name.trim()}, and ask what you'd like to do first.`
          : `Lago knows ${answers.projectName || "your project"} and will ask what you'd like to do first.`)
          + (featureOn("personas") ? " Pick his face and voice; you can switch later on his page." : "")),
        featureOn("personas") ? personaCards(answers.persona, (id) => { answers.persona = id; render(); }) : null,
        h("ul", { class: "onboard-tips" },
          h("li", {}, icon("plus-circle"), h("span", {}, h("b", {}, "New Task"), " in the sidebar (or Ctrl+N) is the one way to ask for anything.")),
          featureOn("voice") ? h("li", {}, icon("microphone"), h("span", {}, "Press Ctrl+Space and talk to him, or type.")) : null,
          h("li", {}, icon("bell"), h("span", {}, featureOn("voice") ? "The bell (top right) lists what needs you; he also says it aloud." : "The bell (top right) lists what needs you.")),
          h("li", {}, icon("sliders"), h("span", {}, "Voice, personas, routines, phone and more are extras: switch them on in Features, at the bottom of the sidebar."))),
        nav(null, { label: "Get Started" })),
    ];
    const render = () => (answers.mode === "join" ? fill(box, joinScreen()) : fill(box, dots(), screens[step]()));
    render();
  });
}

/* New Task: one box for anything. Lago decides who does it unless you pick. */
function newTaskDialog(preset = {}) {
  const text = h("textarea", { class: "input", rows: 4, required: true, autofocus: true, placeholder: `Describe a task… For example: ${exampleRequest()}` }, preset.text || "");
  // In a browser, members pick one of their projects: Lago is the owner's assistant.
  const member = state.web && !(state.me?.owner || state.me?.admin);
  const projectChoices = Object.entries(state.config.projects).filter(([id]) => !member || can("operate", id)).map(([id, p]) => [id, p.name || id]);
  const project = h("select", { class: "select", "aria-label": "Project" },
    [...(member ? [] : [["", "Lago decides"]]), ...projectChoices].map(([v, l]) => h("option", { value: v, selected: v === (preset.project || "") }, l)));
  const agent = h("select", { class: "select", "aria-label": "Who does it" },
    [["", "Lago picks the agents"], ...state.catalog.agents.filter((a) => a.enabled !== false).sort((a, b) => a.name.localeCompare(b.name)).map((a) => [a.id, a.name])].map(([v, l]) => h("option", { value: v, selected: v === (preset.agent || "") }, l)));
  const note = h("p", { class: "faint", style: "font-size:12.5px;margin:0" });
  const explain = () => { note.textContent = agent.value ? "That agent does it right away; Lago gets the result and tells you."
    : project.value ? "A task opens on that project: its lead plans the work with the agents and asks you to approve the plan. Lago follows it."
      : "Lago reads it, asks only what he must, and gets the right agents on it."; };
  project.onchange = explain; agent.onchange = explain; explain();
  return openDialog((close) => h("form", { class: "new-task", onsubmit: async (e) => {
      e.preventDefault();
      let href = null;
      const ok = await runAction(e.submitter, async () => {
        const res = await bridge.call("new_task", { text: text.value, project: project.value, agent: agent.value });
        href = res.href;
        return res;
      });
      if (ok) { close(); preset.onStarted?.(); if (href) location.hash = href.slice(1); pollLive(); }
    } },
    h("h2", {}, "New Task"), text,
    h("div", { class: "new-task-row" }, h("label", {}, icon("folder"), project), h("label", {}, icon("users-three"), agent)),
    note,
    h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn ghost", onclick: () => close() }, "Cancel"),
      h("button", { type: "submit", class: "btn primary" }, icon("paper-plane-tilt"), "Start"))), { wide: true });
}

/* ================================================================ keyboard & boot */

document.addEventListener("keydown", (event) => {
  if (byId("dialog").open) return;
  if (event.key === "Escape" && openMenu) { closeMenu(); return; }
  if (event.key === "Escape" && !byId("notifications").hidden) { closeNotifications(); byId("bell").focus(); return; }
  if (event.ctrlKey && !event.shiftKey && event.key.toLowerCase() === "n") {
    event.preventDefault();
    newTaskDialog();
    return;
  }
  if (event.ctrlKey && /^[1-8]$/.test(event.key)) {
    event.preventDefault();
    document.querySelectorAll(".nav a")[Number(event.key) - 1]?.click();
  }
});

window.addEventListener("hashchange", () => { navigate(); renderTopbar(); });

/* Light or dark. No theme (Auto with a desktop that doesn't say) leaves it to the CSS media query. */
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  document.querySelector('meta[name="theme-color"]').setAttribute("content", bg);
}

/* The Auto / Light / Dark switch under the sidebar. */
function appearanceSwitch(current) {
  const group = byId("appearance");
  const mark = (value) => group.querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.appearance === value)));
  mark(current);
  group.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button || button.getAttribute("aria-pressed") === "true") return;
    const previous = current;
    current = button.dataset.appearance;
    mark(current);
    try {
      applyTheme((await bridge.call("set_appearance", { appearance: current })).theme);
    } catch (error) {
      current = previous;
      mark(current);
      toast(error.message, "error");
    }
  });
}

(async function boot() {
  try {
    const init = await bridge.call("init");
    state.config = init.config;
    state.remote = Boolean(init.remote);  // the phone: files open in a reader, not a desktop app
    state.web = Boolean(init.web);        // a browser on the company's tailnet
    state.me = init.me || null;            // who you are there, and your roles per project
    applyRoleChrome();
    state.definitions = init.definitions;
    state.catalog = init.catalog;
    state.swarmTeam = init.config.teams.swarm || null;
    applyTheme(init.theme);
    state.personas = init.personas || [];
    applyFeatures(init.features, init.feature_list);
    applyPersona(init.persona || "lago");
    const featuresLink = byId("features-link");
    if (featuresLink) {
      featuresLink.hidden = state.web;  // the owner's, at this computer
      featuresLink.addEventListener("click", () => featuresDialog());
    }
    if (!state.remote) appearanceSwitch(init.appearance || "auto");
    await pollLive();
    setInterval(() => { if (document.visibilityState === "visible") pollLive(); }, 2000);
    refreshAccountChip();
    setInterval(() => { if (document.visibilityState === "visible") refreshAccountChip(); }, 60000);
    byId("new-task")?.addEventListener("click", () => newTaskDialog());
    if (init.onboarding) {
      const result = await onboardingWizard();
      if (result?.joined) {
        state.joined = result.joined;
        location.hash = "joined";
        navigate();
        return;
      }
      location.hash = "orchestrator";
      navigate();
      welcome({ first: true });
      return;
    }
    navigate();
    if (!state.web || state.me?.owner || state.me?.admin) welcome();
  } catch (error) {
    byId("main").replaceChildren(h("div", { class: "view" }, errorBox(error, () => location.reload())));
  }
})();
