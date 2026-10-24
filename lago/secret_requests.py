"""Agents asking for a secret: an API key, a token, a password they need to carry on.

Instead of stopping to ask in chat (where you'd paste the value into its conversation), an
agent calls `request_secret` with the variable it needs and why. You see it in the bell and on
the Permissions page, and either give the value or decline. A value you give goes to the
vault (vault.py, the keyring), never into the conversation: the agent gets it as an
environment variable in its next sessions, and right away through a file only it is told
about (`runs/<agent>/secrets.env`, readable by you only, deleted when the session stops),
which it loads in the command that needs it.
"""

import time

import activity
import config_store
import hub

SCHEMA = """
CREATE TABLE IF NOT EXISTS secret_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    project     TEXT NOT NULL DEFAULT '',
    env         TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending, given, declined
    note        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT
);
"""


class SecretRequestError(Exception):
    pass


def _ensure():
    with hub.connect() as conn:
        conn.executescript(SCHEMA)


def env_file(agent_id):
    return config_store.STATE_DIR / "runs" / agent_id / "secrets.env"


def _agent(agent_id):
    return config_store.load()["agents"].get(agent_id) or {}


def requests(status="pending"):
    _ensure()
    config = config_store.load()
    with hub.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM secret_requests" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC LIMIT 100",
            (status,) if status else ())]
    for row in rows:
        row["agent_name"] = (config["agents"].get(row["agent_id"]) or {}).get("name") or row["agent_id"]
    return rows


def get(request_id):
    _ensure()
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM secret_requests WHERE id = ?", (int(request_id),)).fetchone()
    if not row:
        raise SecretRequestError("That request no longer exists")
    return dict(row)


def request(agent_id, env, reason):
    """Record an agent's request. Returns what to tell it."""
    import vault
    env = (env or "").strip().upper()
    if not vault.ENV_NAME.match(env):
        raise SecretRequestError("Name the environment variable you need, like STRIPE_KEY")
    if not (reason or "").strip():
        raise SecretRequestError("Say what you need it for, so the human can decide")
    project = _agent(agent_id).get("project") or ""
    if any(s["env"] == env and s["project"] in ("", project) for s in vault.secrets()):
        return (f"{env} is already in the vault for you. It's in your environment in new sessions; "
                f"if it isn't in this one, load it in the command that needs it: "
                f"`set -a; . {env_file(agent_id)}; set +a; <command>`." if _write_env_file(agent_id)
                else f"{env} is in the vault but couldn't be read; tell the human.")
    _ensure()
    with hub.connect() as conn:
        same = conn.execute("SELECT id FROM secret_requests WHERE agent_id = ? AND env = ? AND status = 'pending'",
                            (agent_id, env)).fetchone()
        if same:
            return f"You already asked for {env} (request #{same['id']}); it's still waiting for the human."
        request_id = conn.execute("INSERT INTO secret_requests (agent_id, project, env, reason, created_at) "
                                  "VALUES (?, ?, ?, ?, ?)", (agent_id, project, env, reason.strip()[:1000],
                                                             time.time())).lastrowid
    activity.log(agent_id, "secret.requested", f"{env}: {reason.strip()[:200]}", target=f"secret request {request_id}",
                 project=project or None)
    return (f"Asked the human for {env} (request #{request_id}). Don't ask for the value in chat and "
            "don't wait idle: carry on with what you can, and you'll get a message when it's given or declined.")


def _write_env_file(agent_id):
    import vault
    try:
        values = vault.env_for(agent_id)
    except Exception:  # a locked keyring
        return False
    path = env_file(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("".join(f"export {k}={vault._shell_quote(v)}\n" for k, v in values.items()))
    return True


def give(request_id, value, for_all=False, by=None):
    """Save the value in the vault and tell the agent where to load it from."""
    import redact
    import team
    import vault
    row = get(request_id)
    if row["status"] != "pending":
        raise SecretRequestError("That request was already answered")
    project = "" if for_all else row["project"]
    name = row["env"].lower().replace("_", "-")[:48]
    try:
        vault.save(name, row["env"], value, project)
    except vault.VaultError as error:
        raise SecretRequestError(str(error)) from error
    redact.forget()
    _resolve(row["id"], "given", "", by)
    if _write_env_file(row["agent_id"]):
        how = (f"load it in the command that needs it, without printing it: "
               f"`set -a; . {env_file(row['agent_id'])}; set +a; <command>`")
    else:
        how = "it's in your environment from your next session (the keyring couldn't be read now)"
    try:
        team.message(row["agent_id"], f"The human gave you {row['env']} (request #{row['id']}). To use it now, {how}. "
                     "New sessions have it in their environment.")
    except team.TeamError:
        pass  # the agent is gone; the secret stays in the vault
    return {"ok": True}


def decline(request_id, note="", by=None):
    import team
    row = get(request_id)
    if row["status"] != "pending":
        raise SecretRequestError("That request was already answered")
    _resolve(row["id"], "declined", note, by)
    try:
        team.message(row["agent_id"], f"The human declined your request for {row['env']} (#{row['id']})"
                     + (f": {note}" if note else ".") + " Do without it, or explain in your result what it blocks.")
    except team.TeamError:
        pass
    return {"ok": True}


def _resolve(request_id, status, note, by):
    with hub.connect() as conn:
        conn.execute("UPDATE secret_requests SET status = ?, note = ?, resolved_at = ?, resolved_by = ? WHERE id = ?",
                     (status, note, time.time(), by or activity.actor(), request_id))
    activity.log(by or activity.actor(), f"secret.{status}", f"request #{request_id}", target=f"secret request {request_id}")


def forget_file(agent_id):
    """The session stopped: its secrets file goes (new sessions get the vault's values at launch)."""
    env_file(agent_id).unlink(missing_ok=True)
