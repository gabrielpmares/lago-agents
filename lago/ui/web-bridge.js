/*
  Lago Agents in a browser on the company's tailnet: the bridge app.js talks to.

  On the desktop, app.js posts each call to the GTK window through
  window.webkit.messageHandlers.lago and gets the answer back through
  window.lagoBridge.resolve. Here the same object posts the call to the computer that hosts
  the company (POST /v1/call, same origin). That computer knows who you are from your
  Tailscale identity and answers only what your role allows.
*/
(() => {
  "use strict";
  window.webkit = {
    messageHandlers: {
      lago: {
        postMessage(json) {
          const call = JSON.parse(json);
          fetch("/v1/call", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Lago-Web": "1" },
            body: json,
            credentials: "same-origin",
          })
            .then((r) => r.json())
            .then((reply) => window.lagoBridge?.resolve(call.id, Boolean(reply.ok), reply.ok ? reply.data : reply.error))
            .catch(() => window.lagoBridge?.resolve(call.id, false, "Can't reach the company's computer. Is your VPN on?"));
        },
      },
    },
  };
})();
