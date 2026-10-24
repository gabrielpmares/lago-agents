/*
  Lago Agents on the phone: the bridge app.js talks to.

  On the desktop, app.js posts each call to the GTK window through
  window.webkit.messageHandlers.lago and gets the answer back through
  window.lagoBridge.resolve. Here the same object posts to the Android app (LagoNative,
  which only exists on this app's own origin), and the app forwards the call to your
  computer over the pinned HTTPS connection, or answers itself for what happens on the
  phone (the terminal mirror, the microphone, opening files and links).

  Calls go as "id\nmethod\nparams-json"; answers come back as "R<id>\n<reply-json>",
  where the reply is {ok, data} or {ok: false, error}. Other messages from the app are
  JSON events.
*/
(() => {
  "use strict";
  const native = window.LagoNative;
  if (!native) return;

  window.webkit = {
    messageHandlers: {
      lago: {
        postMessage(json) {
          const call = JSON.parse(json);
          native.postMessage(`${call.id}\n${call.method}\n${JSON.stringify(call.params || {})}`);
        },
      },
    },
  };

  native.onmessage = (event) => {
    const text = typeof event.data === "string" ? event.data : "";
    if (text.startsWith("R")) {
      const cut = text.indexOf("\n");
      const id = Number(text.slice(1, cut));
      let reply;
      try { reply = JSON.parse(text.slice(cut + 1)); } catch { reply = { ok: false, error: "The computer sent something unreadable" }; }
      window.lagoBridge?.resolve(id, Boolean(reply.ok), reply.ok ? reply.data : reply.error);
      return;
    }
    let msg;
    try { msg = JSON.parse(text); } catch { return; }
    if (msg.type === "terminalEnded") window.lagoTerminalEnded?.(msg.id);
    else if (msg.type === "theme") applyTheme(msg.theme);
    else if (msg.type === "navigate" && typeof msg.hash === "string" && msg.hash.startsWith("#")) location.hash = msg.hash;
  };

  function applyTheme(theme) {
    if (theme !== "dark" && theme !== "light") return;
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    if (meta && bg) meta.setAttribute("content", bg);
  }

  /* The real height of the view, for mobile.css (100dvh isn't reliable in an app's WebView). */
  const fitHeight = () => document.documentElement.style.setProperty("--app-h", `${window.innerHeight}px`);
  fitHeight();
  window.addEventListener("resize", fitHeight);
  window.visualViewport?.addEventListener("resize", fitHeight);

  /* The app's own settings (lock, alerts, sign out), as the last tab. */
  function addSettingsTab() {
    const nav = document.querySelector(".nav");
    if (!nav || nav.querySelector(".nav-native")) return;
    const link = document.createElement("a");
    link.href = "#";
    link.setAttribute("role", "button");
    link.innerHTML = '<i class="ph ph-gear-six" aria-hidden="true"></i>App';
    link.addEventListener("click", (e) => { e.preventDefault(); native.postMessage("0\n__settings\n{}"); });
    const item = document.createElement("li");
    item.className = "nav-native";
    item.append(link);
    nav.append(item);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", addSettingsTab);
  else addSettingsTab();
})();
