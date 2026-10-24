"""Each person's own subscription, on the computer that hosts the company.

A member connects their Claude or ChatGPT login from their browser: `connect()` makes them a
place of their own here (a Claude Code account folder, or a Codex home) and starts the login
in a terminal they see and type into from the browser (`screen()`, `send()`): Claude's login
gives a link and asks for the code it shows; Codex's device login gives a link and a code to
enter there. From then on, the tasks they start run on their own subscription (team.start
reads the account a work session or run was started with). Someone who hasn't connected one
can't start work, so nobody spends another person's subscription.
"""

import re
import subprocess

import accounts
import config_store
import hub
import runner

PROVIDERS = ("claude", "codex")


class MemberAccountError(Exception):
    pass


def account_id(login):
    return "m-" + re.sub(r"[^a-z0-9]+", "-", login.lower()).strip("-")[:24]


def codex_home(login):
    return config_store.STATE_DIR / "codex-homes" / account_id(login)


def _login_session(login):
    return f"login-{account_id(login)}"


def connect(login, name, provider):
    """Start connecting their subscription. Returns the login session to watch."""
    import server
    if provider not in PROVIDERS:
        raise MemberAccountError("Connect a Claude or a ChatGPT subscription")
    aid = account_id(login)
    if provider == "claude":
        config = config_store.without_swarm(config_store.load())
        config.setdefault("accounts", {})[aid] = {"label": f"{name} (member)", "email": login, "member": login}
        config_store.save(config)
        accounts.prepare(aid)
        command = accounts.login_command(aid, login)
        with hub.connect() as conn:
            conn.execute("UPDATE members SET account = ? WHERE login = ?", (aid, login.lower()))
    else:
        home = codex_home(login)
        home.mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)
        command = f"CODEX_HOME={home} codex login --device-auth"
        with hub.connect() as conn:
            conn.execute("UPDATE members SET codex = ? WHERE login = ?", (str(home), login.lower()))
    return server.launch(_login_session(login), "", command, f"{login} connects their {provider} subscription")


def screen(login):
    session = _login_session(login)
    if not runner.is_running(session):
        return {"running": False, "text": ""}
    ok, text = runner.tmux("capture-pane", "-p", "-t", f"={runner.session_name(session)}:", "-S", "-60")
    return {"running": ok, "text": text if ok else ""}


def send(login, text):
    """Type into their own login terminal (a pasted code), then Enter."""
    session = _login_session(login)
    if not runner.is_running(session):
        raise MemberAccountError("The login has finished; connect again if it didn't work")
    runner.paste(session, str(text)[:2000])
    return {"ok": True}


def status(login):
    """What they have connected: {"claude": bool, "codex": bool}."""
    from company import member
    m = member(login) or {}
    out = {"claude": False, "codex": False}
    if m.get("account"):
        out["claude"] = bool(accounts.status(m["account"], refresh=True).get("logged_in"))
    if m.get("codex"):
        try:
            proc = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=20,
                                  env={"CODEX_HOME": m["codex"], "PATH": "/usr/bin:/bin:/usr/local/bin:" + str(config_store.Path.home() / ".npm-global/bin")})
            out["codex"] = "Logged in" in (proc.stdout + proc.stderr)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return out


def run_on(login):
    """{"account", "codex_home"} their work should run on; raises if they connected nothing."""
    import codex_runtime
    from company import member
    m = member(login) or {}
    provider = codex_runtime.provider(config_store.load())
    if provider == "codex" and not m.get("codex"):
        raise MemberAccountError("Connect your ChatGPT subscription first (Live Map → Your subscription)")
    if provider == "claude" and not m.get("account"):
        raise MemberAccountError("Connect your Claude subscription first (Live Map → Your subscription)")
    return {"account": m.get("account"), "codex_home": m.get("codex")}
