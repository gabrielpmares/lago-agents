"""The app without GTK, end to end in a real browser engine: app_server.py serving the page on
throwaway data, an agent's terminal shown in the page (xterm.js) and typed into, its tmux
session on a tmux server of the script's own. Run on macOS by CI with WebKit, Safari's engine.

    python3 tests/e2e_app_window.py [chromium|webkit] [screenshot folder]

Needs Playwright (pip install playwright; python3 -m playwright install webkit) and tmux.
"""

import os
import sys
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402  throwaway state, data, keyring; no real services

import app_server  # noqa: E402
import config_store  # noqa: E402
import runner  # noqa: E402

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "chromium"
SHOTS = Path(sys.argv[2]) if len(sys.argv) > 2 else support.TMP / "shots"


def main():
    support.reset()
    config = config_store.load()
    config["onboarded"] = True
    config["user_name"] = "Tester"
    config_store.save(config)
    runner.SOCKET = f"lago-e2e-{os.getpid()}"
    runner.tmux = support.REAL_TMUX
    runner.is_running = lambda agent: support.REAL_TMUX("has-session", "-t", f"={runner.session_name(agent)}")[0]
    runner.running_agents = lambda: {l[3:] for l in support.REAL_TMUX("list-sessions", "-F", "#{session_name}")[1].splitlines() if l.startswith("la-")}
    ok, out = runner.tmux("new-session", "-d", "-s", runner.session_name("dev-qa"), "-x", "100", "-y", "30",
                          "sh", "-c", "PS1='$ ' exec sh -i")
    assert ok, out
    support.note_tmux()
    import api
    api.Api.welcome = lambda self, *a, **k: {"ok": True}  # never wakes Lago (a real Claude session)
    api.Api.start_orchestrator = lambda self, *a, **k: {"ok": True}
    try:
        with mock.patch("background.start_loops"):
            server = app_server.Server()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        run(server)
    finally:
        support.end_tmux()


def run(server):
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    failures = []
    check = lambda cond, what: (print(("ok   " if cond else "FAIL ") + what), cond or failures.append(what))  # noqa: E731
    with sync_playwright() as p:
        browser = getattr(p, ENGINE).launch()
        page = browser.new_page(viewport={"width": 1440, "height": 880})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server.url + "&view=tasks")
        page.wait_for_selector(".sidebar", timeout=15000)
        page.wait_for_timeout(1500)
        check("page-desk" in page.evaluate("document.documentElement.className"), "the page knows it's without GTK")
        check(page.locator("h1", has_text="Tasks").count() == 1, "the app opens on the page asked for")
        page.evaluate("bridge.call('show_terminal', { agent_id: 'dev-qa' })")
        page.wait_for_selector(".page-terminal .xterm-screen", timeout=15000)
        page.wait_for_timeout(1200)
        page.locator(".page-terminal .xterm").click()
        page.keyboard.type("echo from-$((6*7))-page")
        page.keyboard.press("Enter")
        deadline, screen = time.time() + 10, ""
        while time.time() < deadline:
            screen = runner.tmux("capture-pane", "-p", "-t", f"={runner.session_name('dev-qa')}:")[1]
            if "from-42-page" in screen:
                break
            time.sleep(0.3)
        check("from-42-page" in screen, "keys typed in the page run in the agent's session")
        page.wait_for_timeout(800)
        shown = page.evaluate("document.querySelector('.page-terminal .xterm-rows')?.innerText || ''")
        check("from-42-page" in shown, "its output shows in the page")
        page.screenshot(path=str(SHOTS / f"app-window-{ENGINE}.png"))
        width_before = runner.tmux("display-message", "-p", "-t", f"={runner.session_name('dev-qa')}:", "#{window_width}")[1]
        page.set_viewport_size({"width": 1100, "height": 800})
        page.wait_for_timeout(1500)
        width_after = runner.tmux("display-message", "-p", "-t", f"={runner.session_name('dev-qa')}:", "#{window_width}")[1]
        check(width_before != width_after, f"the session follows the pane's size ({width_before} -> {width_after} columns)")
        runner.tmux("kill-session", "-t", f"={runner.session_name('dev-qa')}")
        page.wait_for_timeout(2500)
        check(page.evaluate("document.querySelector('.page-terminal')?.hidden") is True, "the pane closes when the session ends")
        check(not errors, f"no script errors ({errors[:2]})")
        browser.close()
    if failures:
        sys.exit(f"{len(failures)} failed: {failures}")
    print("all good")


if __name__ == "__main__":
    main()
