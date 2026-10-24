#!/usr/bin/env python3
"""Lago Agents: a control hub for teams of AI agents.

The interface is plain HTML/CSS/JS in ui/, shown in a WebKitGTK view. Next to it,
a native terminal (VTE) shows the live session of the agent you're looking at.
The page talks to Python through a `lago` script message handler (see ui/app.js).

    lago_agents.py                  open on the Overview
    lago_agents.py agent/<id>       open on a page (any #hash of the UI)
    lago_agents.py --browser        without GTK, in a browser window (always, on macOS)

Set LAGO_DEBUG=1 to enable the web inspector and print the page's console to stdout.
With LAGO_DEBUG=1, LAGO_SNAPSHOT=<file.png> saves a picture of the page after 5 seconds
(LAGO_SNAPSHOT_DELAY=<seconds> to wait longer).
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path


def _gtk_available():
    try:
        import gi
        for name, version in (("Gtk", "3.0"), ("WebKit2", "4.1"), ("Vte", "2.91")):
            gi.require_version(name, version)
        from gi.repository import Gtk, Vte, WebKit2  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


# Without GTK (macOS, or `lago-agents --browser`): the same app, served to a window (app_server.py).
if __name__ == "__main__" and (sys.platform == "darwin" or "--browser" in sys.argv or not _gtk_available()):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import app_server
    views = [a for a in sys.argv[1:] if not a.startswith("--")]
    app_server.serve(views[0].lstrip("#") if views else "")
    sys.exit(0)

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte, WebKit2  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_store  # noqa: E402
import runner  # noqa: E402
from api import UI_THREAD, Api, ApiError  # noqa: E402

UI_DIR = Path(__file__).resolve().parent / "ui"
ICON_FILE = UI_DIR / "lago-agents-icon.svg"
DEBUG = os.environ.get("LAGO_DEBUG") == "1"

# What a click opens in the terminal: web links, and file paths (anything with a slash, or a
# name with a usual document/code extension), resolved from the agent's folder.
LINK_PATTERNS = (
    r"https?://[^\s<>\"'`)\]]+[^\s<>\"'`)\].,:;]",
    r"(?:~|\.{1,2})?/?(?:[\w.@+-]+/)+(?:[\w.@+-]*\w)?",
    r"[\w@+-][\w.@+-]*\.(?:md|pdf|html?|txt|csv|tsv|json|ya?ml|toml|docx?|pptx?|xlsx?|odt|png"
    r"|jpe?g|gif|svg|webp|mp4|py|js|ts|tsx|cs|sh|log)\b",
)
PCRE2_MULTILINE = 0x00000400

TERMINAL_COLORS = {
    "dark": ("#e8e9eb", "#0b0c0e"),
    "light": ("#16181b", "#fcfcfc"),
}


class LagoAgentsWindow(Gtk.Window):
    def __init__(self, start_view=""):
        super().__init__(title="Lago Agents")
        self.set_icon_from_file(str(ICON_FILE))
        self.set_default_size(1440, 880)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("destroy", self.on_destroy)
        self.connect("key-press-event", self.on_key)
        self.api = Api(self)
        self.terminal_agent = None
        self.terminal_pid = None

        manager = WebKit2.UserContentManager()
        manager.connect("script-message-received::lago", self.on_message)
        manager.register_script_message_handler("lago")
        self.webview = WebKit2.WebView.new_with_user_content_manager(manager)
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(DEBUG)
        settings.set_enable_write_console_messages_to_stdout(DEBUG)
        settings.set_enable_back_forward_navigation_gestures(False)
        self.webview.connect("decide-policy", self.on_decide_policy)
        if not DEBUG:
            self.webview.connect("context-menu", lambda *_: True)

        self.terminal = Vte.Terminal()
        self.terminal.set_font(Pango.FontDescription.from_string("Monospace 10.5"))
        self.terminal.set_scrollback_lines(0)  # tmux keeps the history
        self.terminal.connect("child-exited", self.on_terminal_exited)
        self.terminal.set_allow_hyperlink(True)
        for pattern in LINK_PATTERNS:
            tag = self.terminal.match_add_regex(Vte.Regex.new_for_match(pattern, -1, PCRE2_MULTILINE), 0)
            self.terminal.match_set_cursor_name(tag, "pointer")
        self.terminal.connect("button-press-event", self.on_terminal_press)
        self.terminal.connect("button-release-event", self.on_terminal_release)
        self.link_press = None
        self.set_terminal_theme(Api._theme() or "dark")
        self.terminal_box = Gtk.Box()
        self.terminal_box.set_border_width(0)
        self.terminal_box.pack_start(self.terminal, True, True, 0)
        self.terminal_box.set_no_show_all(True)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.pack1(self.webview, resize=True, shrink=False)
        self.paned.pack2(self.terminal_box, resize=True, shrink=False)
        self.add(self.paned)
        self.webview.load_uri((UI_DIR / "index.html").as_uri() + (f"#{start_view}" if start_view else ""))
        if DEBUG and os.environ.get("LAGO_SNAPSHOT"):
            delay = int(os.environ.get("LAGO_SNAPSHOT_DELAY", "5"))
            GLib.timeout_add_seconds(delay, self.save_snapshot, os.environ["LAGO_SNAPSHOT"])

    def save_snapshot(self, path):
        def done(view, result):
            surface = view.get_snapshot_finish(result)
            surface.write_to_png(path)
            print(f"lago-agents: snapshot saved to {path}")
        self.webview.get_snapshot(WebKit2.SnapshotRegion.FULL_DOCUMENT,
                                  WebKit2.SnapshotOptions.NONE, None, done)
        return False

    def set_terminal_theme(self, theme):
        fg, bg = TERMINAL_COLORS.get(theme, TERMINAL_COLORS["dark"])
        fg_rgba, bg_rgba = Gdk.RGBA(), Gdk.RGBA()
        fg_rgba.parse(fg)
        bg_rgba.parse(bg)
        self.terminal.set_colors(fg_rgba, bg_rgba, None)

    # --- terminal

    def show_terminal(self, agent_id):
        if self.terminal_agent == agent_id and self.terminal_pid:
            self.terminal_box.show()
            self.terminal.show()
            return
        self.detach_terminal()
        self.terminal.reset(True, True)
        self.terminal_agent = agent_id
        env = [f"{k}={v}" for k, v in os.environ.items() if k != "TMUX"]
        self.terminal.spawn_async(Vte.PtyFlags.DEFAULT, str(Path.home()),
                                  runner.attach_argv(agent_id), env, GLib.SpawnFlags.SEARCH_PATH,
                                  None, None, -1, None, self.on_spawned, agent_id)
        if not self.terminal_box.get_visible():
            self.terminal_box.show()
            self.terminal.show()
            width = self.get_allocated_width()
            self.paned.set_position(int(width * 0.5))

    def on_spawned(self, _terminal, pid, error, agent_id):
        if error is not None or agent_id != self.terminal_agent:
            if pid and pid > 0:
                os.kill(pid, signal.SIGHUP)
            return
        self.terminal_pid = pid

    def detach_terminal(self):
        """Close our view of the session; the agent keeps running in tmux."""
        if self.terminal_pid:
            try:
                os.kill(self.terminal_pid, signal.SIGHUP)
            except OSError:
                pass
        self.terminal_pid = None
        self.terminal_agent = None

    def hide_terminal(self):
        self.detach_terminal()
        self.terminal_box.hide()

    def on_terminal_exited(self, _terminal, _status):
        agent_id = self.terminal_agent
        self.terminal_pid = None
        self.terminal_agent = None
        if agent_id and not runner.is_running(agent_id):
            # The session itself ended (a login or deploy you closed, an agent that stopped):
            # close the pane and let the page drop its "in the terminal" bar.
            self.terminal_box.hide()
            self.webview.evaluate_javascript(
                f"window.lagoTerminalEnded && window.lagoTerminalEnded({json.dumps(agent_id)})",
                -1, None, None, None, None, None)
            return
        self.terminal.feed(b"\r\n\x1b[2m[This session has ended.]\x1b[0m\r\n")

    def link_at(self, event):
        """The OSC 8 link or the matched path/URL under the pointer, or None."""
        return self.terminal.hyperlink_check_event(event) or self.terminal.match_check_event(event)[0]

    def on_terminal_press(self, _terminal, event):
        """Remember a plain left press on a link; the release opens it (a drag still selects)."""
        plain = not event.state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK)
        self.link_press = (event.x, event.y) if event.button == 1 and plain and self.link_at(event) else None
        return False

    def on_terminal_release(self, _terminal, event):
        press, self.link_press = self.link_press, None
        if event.button != 1 or press is None or abs(event.x - press[0]) > 4 or abs(event.y - press[1]) > 4:
            return False
        link = self.link_at(event)
        return bool(link) and self.open_link(link)

    def open_link(self, link):
        """Open a URL in the browser, or a file or folder from the terminal's agent folder in
        its usual app. Text that names nothing on disk isn't a link: True only if opened."""
        if re.match(r"https?://", link):
            target = link
        else:
            link = link[len("file://"):] if link.startswith("file://") else link
            config = config_store.load()
            agent = config["agents"].get(self.terminal_agent) or {}
            base = config_store.team_cwd(config, agent) if agent else Path.home()
            path = Path(os.path.expanduser(link.rstrip(".,:;")))
            path = path if path.is_absolute() else base / path
            if not path.exists():
                return False
            target = str(path)
        try:
            subprocess.Popen(["xdg-open", target], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        return True

    def on_key(self, _window, event):
        """Ctrl+Space talks to the Orchestrator, even while the terminal has focus."""
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and event.keyval == Gdk.KEY_space:
            self.webview.evaluate_javascript("window.lagoTalkShortcut && window.lagoTalkShortcut()",
                                             -1, None, None, None, None, None)
            return True
        return False

    def on_destroy(self, *_):
        config_store.lock_bypass()  # your password is asked again next time the app opens
        self.detach_terminal()
        Gtk.main_quit()

    # --- web bridge

    # Only our own local files may load; nothing from the network.
    def on_decide_policy(self, _view, decision, kind):
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith(UI_DIR.as_uri()):
                decision.ignore()
                return True
        return False

    def on_message(self, _manager, js_result):
        try:
            message = json.loads(js_result.get_js_value().to_string())
            call_id, method = message["id"], message["method"]
            params = message.get("params") or {}
        except (ValueError, KeyError, TypeError):
            return
        handler = getattr(self.api, method, None)
        if method.startswith("_") or not callable(handler):
            self.reply(call_id, False, f"Unknown method {method}")
            return
        if method in UI_THREAD:
            self.reply(call_id, *self.invoke(handler, params))
        else:
            threading.Thread(target=lambda: GLib.idle_add(
                self.reply, call_id, *self.invoke(handler, params)), daemon=True).start()

    @staticmethod
    def invoke(handler, params):
        try:
            return True, handler(**params)
        except ApiError as error:
            return False, str(error)
        except Exception as error:  # report backend bugs instead of hanging the promise
            print(f"lago-agents: {handler.__name__} failed: {error!r}", file=sys.stderr)
            return False, f"Something went wrong: {error}"

    def reply(self, call_id, ok, data):
        script = (f"window.lagoBridge.resolve({json.dumps(call_id)}, {json.dumps(ok)}, "
                  f"{json.dumps(data)})")
        self.webview.evaluate_javascript(script, -1, None, None, None, None, None)
        return False


def main():
    GLib.set_prgname("lago-agents")
    GLib.set_application_name("Lago Agents")
    views = [a for a in sys.argv[1:] if not a.startswith("--")]
    start_view = views[0].lstrip("#") if views else ""
    LagoAgentsWindow(start_view).show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
