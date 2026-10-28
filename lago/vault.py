"""Secrets for agents, kept in your desktop keyring (hosted: an encrypted file) and handed to
the agents that need them.

A secret has a name, the environment variable agents see it as, and a project (or none, for
every agent). Its value lives only in the keyring (libsecret on Linux, the Keychain on macOS:
encrypted at rest and unlocked when you log in); Lago Agents keeps the name, variable, project and an access log in hub.db,
never the value, and nothing shows a value again once it's saved. When an agent of that
project starts, its launch script asks `vault.py --env <agent>` for its variables, so they
reach the process without being written to disk; each hand-out is logged.

Lago and the agents can list secret names, never read values through Lago's tools.
"""

import json
import os
import re
import subprocess
import sys
import time

import activity
import hub

ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
SECRET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class VaultError(Exception):
    pass


class KeyringBackend:
    """Your desktop keyring (GNOME Keyring, KWallet, KeePassXC: any Secret Service), through
    libsecret's GObject bindings."""

    def __init__(self):
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        self.Secret = Secret
        self.schema = Secret.Schema.new("com.lago-agents.Secret", Secret.SchemaFlags.NONE,
                                        {"name": Secret.SchemaAttributeType.STRING,
                                         "project": Secret.SchemaAttributeType.STRING})

    def _attrs(self, name, project):
        return {"name": name, "project": project or "-"}

    def store(self, name, project, value):
        ok = self.Secret.password_store_sync(self.schema, self._attrs(name, project),
                                             self.Secret.COLLECTION_DEFAULT,
                                             f"Lago Agents: {name}", value, None)
        if not ok:
            raise VaultError("The keyring didn't save it (is it unlocked?)")

    def lookup(self, name, project):
        return self.Secret.password_lookup_sync(self.schema, self._attrs(name, project), None)

    def clear(self, name, project):
        self.Secret.password_clear_sync(self.schema, self._attrs(name, project), None)


class MacKeychainBackend:
    """The macOS Keychain, through its `security` command. A value travels on stdin (its
    interactive mode), never on a command line where other programs could see it."""
    SERVICE = "Lago Agents"

    def _account(self, name, project):
        return f"{project or '-'}/{name}"

    def store(self, name, project, value):
        quote = lambda text: '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
        command = (f"add-generic-password -U -s {quote(self.SERVICE)} -a {quote(self._account(name, project))} "
                   f"-l {quote('Lago Agents: ' + name)} -w {quote(value)}\n")
        proc = subprocess.run(["security", "-i"], input=command, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0 or "error" in (proc.stderr or "").lower():
            raise VaultError("The Keychain didn't save it: " + (proc.stderr or proc.stdout).strip()[:200])

    def lookup(self, name, project):
        proc = subprocess.run(["security", "find-generic-password", "-s", self.SERVICE, "-a", self._account(name, project), "-w"],
                              capture_output=True, text=True, timeout=30)
        return proc.stdout.rstrip("\n") if proc.returncode == 0 else None

    def clear(self, name, project):
        subprocess.run(["security", "delete-generic-password", "-s", self.SERVICE, "-a", self._account(name, project)],
                       capture_output=True, timeout=30)


class FileBackend:
    """An encrypted file in the state folder, for hosted Lago, where there's no keyring. Its key
    is $LAGO_VAULT_KEY, which the host hands the container when it starts and keeps apart from
    the customer's volume: a copy of the volume (a backup) doesn't give the values away."""

    def __init__(self, key):
        import base64
        import hashlib
        from cryptography.fernet import Fernet
        from config_store import STATE_DIR
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
        self.path = STATE_DIR / "vault.enc"

    def _key(self, name, project):
        return f"{project or '-'}/{name}"

    def _read(self):
        from cryptography.fernet import InvalidToken
        try:
            return json.loads(self.fernet.decrypt(self.path.read_bytes()))
        except FileNotFoundError:
            return {}
        except InvalidToken as error:
            raise VaultError("The vault's key doesn't open it") from error

    def _write(self, values):
        tmp = self.path.with_suffix(".tmp")
        tmp.touch(mode=0o600)
        tmp.write_bytes(self.fernet.encrypt(json.dumps(values).encode()))
        os.replace(tmp, self.path)

    def store(self, name, project, value):
        values = self._read()
        values[self._key(name, project)] = value
        self._write(values)

    def lookup(self, name, project):
        return self._read().get(self._key(name, project))

    def clear(self, name, project):
        values = self._read()
        if values.pop(self._key(name, project), None) is not None:
            self._write(values)


_backend = None


def backend():
    global _backend
    if _backend is None:
        if os.environ.get("LAGO_VAULT_KEY"):
            _backend = FileBackend(os.environ["LAGO_VAULT_KEY"])
        else:
            _backend = MacKeychainBackend() if sys.platform == "darwin" else KeyringBackend()
    return _backend


def secrets(project=None):
    """Names, variables and projects: never values."""
    with hub.connect() as conn:
        rows = conn.execute("SELECT * FROM secrets" + (" WHERE project = ?" if project is not None else "")
                            + " ORDER BY project, name", (project,) if project is not None else ())
        return [dict(r) for r in rows]


def save(name, env, value, project=""):
    """Add or replace a secret. The value goes to the keyring only."""
    name, env = (name or "").strip().lower(), (env or "").strip().upper()
    if not SECRET_NAME.match(name):
        raise VaultError("A secret's name uses lowercase letters, digits and dashes")
    if not ENV_NAME.match(env):
        raise VaultError("The variable name uses capital letters, digits and underscores, like STRIPE_KEY")
    if not value:
        raise VaultError("Give the value")
    with hub.connect() as conn:
        clash = conn.execute("SELECT name FROM secrets WHERE env = ? AND project = ? AND name != ?",
                             (env, project or "", name)).fetchone()
    if clash:
        raise VaultError(f"{env} is already the variable of the secret {clash['name']}")
    backend().store(name, project, value)
    now = time.time()
    with hub.connect() as conn:
        conn.execute("INSERT INTO secrets (name, project, env, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT (name, project) DO UPDATE SET env = excluded.env, updated_at = excluded.updated_at",
                     (name, project or "", env, now, now))
    activity.log(activity.actor(), "secret.saved", f"{name} ({env})" + (f" for {project}" if project else ""),
                 target=f"secret {name}", project=project or None)
    import redact
    redact.forget()  # hidden on screen from now on
    return {"ok": True}


def remove(name, project=""):
    with hub.connect() as conn:
        done = conn.execute("DELETE FROM secrets WHERE name = ? AND project = ?", (name, project or "")).rowcount
    if not done:
        raise VaultError(f"There's no secret {name}")
    backend().clear(name, project)
    activity.log(activity.actor(), "secret.removed", name, target=f"secret {name}", project=project or None)
    return {"ok": True}


def _agent_project(agent_id):
    import config_store
    agent = config_store.load()["agents"].get(agent_id) or {}
    return agent.get("project") or ""


def env_for(agent_id):
    """{VARIABLE: value} for an agent: its project's secrets and the ones for every agent.
    Each hand-out is logged."""
    project = _agent_project(agent_id)
    out = {}
    for row in secrets():
        if row["project"] not in ("", project):
            continue
        value = backend().lookup(row["name"], row["project"])
        if value is None:
            continue
        if row["env"] in out and row["project"] == "":
            continue  # a project's own secret wins over a general one with the same variable
        out[row["env"]] = value
        with hub.connect() as conn:
            conn.execute("INSERT INTO secret_access (name, project, agent_id, at) VALUES (?, ?, ?, ?)",
                         (row["name"], row["project"], agent_id, time.time()))
    return out


def access_log(limit=100):
    with hub.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM secret_access ORDER BY rowid DESC LIMIT ?", (limit,))]


def _shell_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    # `vault.py --env <agent>`: export lines for the agent's launch script, read by `eval`.
    if len(sys.argv) == 3 and sys.argv[1] == "--env":
        try:
            for key, value in env_for(sys.argv[2]).items():
                print(f"export {key}={_shell_quote(value)}")
        except Exception as error:  # an agent must start even when the keyring is unavailable
            print(f"echo 'Lago Agents: secrets unavailable ({type(error).__name__})' >&2")
