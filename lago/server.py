"""A project's servers, over SSH: production, and local replicas of it.

Each server has an SSH host (an alias from ~/.ssh/config, user@host, or an
ssh://user@host:port URI), the folder of its git checkout there, other folders worth
watching, and a deploy script. Lago reads a server's state with one read-only SSH call,
opens interactive SSH sessions, and runs deploys in a terminal you can watch. Deploys to
a production server are guarded: they refuse to run when the server has uncommitted
changes to tracked files, so a fix made directly in production is never overwritten.
A local server (role "local") runs your working copy, so it has no such guard.
"""

import hashlib
import json
import shlex
import subprocess
import time

import cache
import config_store
import runner
from config_store import STATE_DIR

CACHE_SECONDS = 30

# Runs on the server with python3, reads only, prints one JSON object.
PROBE = r'''
import json, os, shutil, subprocess, sys
paths = json.loads(sys.argv[1])
def run(*cmd, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20, cwd=cwd)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None
out = {"host": os.uname().nodename}
with open("/proc/uptime") as f:
    out["uptime"] = float(f.read().split()[0])
out["load"] = os.getloadavg()
mem = {}
with open("/proc/meminfo") as f:
    for line in f:
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
out["memory"] = {"total": mem.get("MemTotal", 0), "available": mem.get("MemAvailable", 0)}
d = shutil.disk_usage("/")
out["disk"] = {"total": d.total, "used": d.used, "free": d.free}
ps = run("docker", "ps", "-a", "--format", "{{json .}}")
out["containers"] = [json.loads(l) for l in ps.splitlines()] if ps else []
df = run("docker", "system", "df", "--format", "{{json .}}")
out["docker_df"] = [json.loads(l) for l in df.splitlines()] if df else []
failed = run("systemctl", "--failed", "--no-legend", "--plain")
out["failed_units"] = [l.split()[0] for l in (failed or "").splitlines() if l.strip()]
units = run("systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain")
out["running_units"] = len((units or "").splitlines())
repo = paths["repo"]
if repo and os.path.isdir(os.path.join(repo, ".git")):
    status = run("git", "status", "--porcelain", cwd=repo) or ""
    out["git"] = {
        "branch": run("git", "branch", "--show-current", cwd=repo),
        "head": run("git", "rev-parse", "HEAD", cwd=repo),
        "subject": run("git", "log", "-1", "--format=%s", cwd=repo),
        "date": run("git", "log", "-1", "--format=%cI", cwd=repo),
        "changed": [l[3:] for l in status.splitlines() if not l.startswith("??")],
        "untracked": [l[3:] for l in status.splitlines() if l.startswith("??")],
    }
out["folders"] = {}
for folder in paths["watch"]:
    if os.path.isdir(folder):
        out["folders"][folder] = sorted(os.listdir(folder))
print(json.dumps(out))
'''

GUARD = """set -euo pipefail
cd {path}
echo "== Checking the server's code before deploying"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo
  echo "Stopped: the server has uncommitted changes to tracked files."
  echo "Commit them (or move them into the repository) first, so the deploy doesn't overwrite them:"
  git status --short | grep -v '^??' || true
  exit 1
fi
git fetch --quiet origin
echo "Server is on $(git rev-parse --short HEAD); origin/{branch} is $(git rev-parse --short origin/{branch})."
git log --oneline HEAD..origin/{branch} || true
echo
echo "== Deploying"
set -x
"""


class ServerError(Exception):
    """A problem worth showing as-is."""


SSH_OPTIONS = ["-o", "StrictHostKeyChecking=accept-new"]


def servers(project):
    """The project's servers. An older single `server` setting counts as production."""
    listed = project.get("servers")
    if listed is None and (project.get("server") or {}).get("ssh"):
        listed = [{"id": "production", "role": "production", **project["server"]}]
    return [s for s in listed or [] if s.get("ssh")]


def settings(project, server_id=None):
    found = servers(project)
    if not found:
        raise ServerError("This project has no server. Add one in its Settings.")
    if not server_id:
        return found[0]
    for server in found:
        if server.get("id") == server_id:
            return server
    raise ServerError(f"There's no server {server_id!r}")


def guarded(server):
    return server.get("role", "production") == "production"


def _ssh(host, *args, input_text=None, timeout=40):
    try:
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *SSH_OPTIONS, host, *args],
                              input=input_text, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise ServerError(f"{host} didn't answer in time") from error
    except OSError as error:
        raise ServerError(f"Couldn't run ssh: {error}") from error
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip().splitlines()
        raise ServerError(message[-1] if message else f"ssh to {host} failed")
    return proc.stdout


def _local_git(cwd, *args):
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _compare(project, server_git):
    """How the server's checkout relates to your local repository and GitHub."""
    cwd = config_store.team_cwd({"teams": {}}, {"cwd": project.get("cwd")})
    branch = project.get("branch") or "main"
    head = server_git.get("head")
    local_head = _local_git(cwd, "rev-parse", "HEAD")
    remote_head = _local_git(cwd, "rev-parse", f"origin/{branch}")
    known = bool(head and _local_git(cwd, "cat-file", "-e", f"{head}^{{commit}}") is not None)

    def commits(since, until):
        if not (known and until):
            return []
        log = _local_git(cwd, "log", "--format=%h%x09%s", f"{since}..{until}") or ""
        return [dict(zip(("sha", "subject"), l.split("\t", 1))) for l in log.splitlines()]
    return {"branch": branch, "local_head": local_head, "remote_head": remote_head,
            "known_locally": known,
            "to_deploy": commits(head, remote_head),
            "local_unpushed": commits(remote_head, local_head) if remote_head else [],
            "server_only": (not known) and bool(head)}


def _drift(project, watch, folders):
    """Folders on the server (like deployed agents) next to their local counterparts."""
    cwd = config_store.team_cwd({"teams": {}}, {"cwd": project.get("cwd")})
    result = []
    for w in watch:
        remote = set(folders.get(w.get("remote"), []))
        local = set()
        for folder in w.get("local") or []:
            path = cwd / folder
            if path.is_dir():
                local |= {p.name for p in path.iterdir() if p.is_dir() and not p.name.startswith((".", "__"))}
        result.append({"name": w.get("name") or w["remote"], "remote": w["remote"], "local": w.get("local") or [],
                       "server_count": len(remote), "both": len(remote & local),
                       "server_only": sorted(remote - local), "local_only": sorted(local - remote)})
    return result


def _probe(server):
    """The server's state, read over SSH (the slow part, so it's cached)."""
    watch = server.get("watch") or []
    paths = json.dumps({"repo": server.get("path", ""), "watch": [w["remote"] for w in watch if w.get("remote")]})
    raw = _ssh(server["ssh"], "python3", "-", shlex.quote(paths), input_text=PROBE)
    try:
        data = json.loads(raw.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise ServerError("The server answered with something unexpected") from error
    data["checked_at"] = time.time()
    return data


def _cache_key(project_id, server):
    digest = hashlib.sha1(json.dumps(server, sort_keys=True).encode()).hexdigest()[:10]
    return f"server:{project_id}:{server.get('id')}:{digest}"


def status(project_id, project, server_id=None, refresh=False):
    """The server's state from the cache (refreshed after 30 s, in the background), with
    fresh comparisons against your local repository."""
    server = settings(project, server_id)
    result = cache.get(_cache_key(project_id, server), lambda: _probe(server),
                       fresh=CACHE_SECONDS, keep=86400, refresh=refresh)
    data = dict(result["value"])
    watch = server.get("watch") or []
    if data.get("git"):
        data["compare"] = _compare(project, data["git"])
    data["drift"] = _drift(project, watch, data.get("folders", {}))
    data["cache"] = cache.meta(result)
    data["server"] = {"id": server.get("id"), "role": server.get("role", "production"),
                      "guarded": guarded(server)}
    return data


def terminal_id(kind, project_id, server_id=None):
    return f"{kind}-{project_id}" + (f"-{server_id}" if server_id and server_id != "production" else "")


def launch(session_id, project_id, command, note):
    """Run a command in its own tmux session, shown in the app's terminal pane."""
    runner.kill(session_id)
    run_dir = STATE_DIR / "runs" / session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    script = run_dir / "launch.sh"
    script.write_text(f"""#!/usr/bin/env bash
# Written by Lago Agents: {note}
{command}
code=$?
echo
echo "[Finished with exit code $code. Press Enter to close.]"
read -r _
""")
    ok, output = runner.new_session(session_id, script, config_store.STATE_DIR)
    if not ok:
        raise ServerError(output or "tmux couldn't start the session")
    return session_id


def open_shell(project_id, project, server_id=None):
    server = settings(project, server_id)
    path = server.get("path") or "~"
    remote = f"cd {shlex.quote(path)} 2>/dev/null; exec bash -l"
    command = f"ssh -t {shlex.join(SSH_OPTIONS)} {shlex.quote(server['ssh'])} {shlex.quote(remote)}"
    return launch(terminal_id("ssh", project_id, server.get("id")), project_id, command,
                   f"SSH session to {server['ssh']}")


def deploy_script(project, server_id=None):
    server = settings(project, server_id)
    if not (server.get("deploy") or "").strip():
        raise ServerError("No deploy script is set. Add one in the project's Settings, under Servers.")
    if not server.get("path"):
        raise ServerError("Set the folder of the code on the server first (Settings, Servers).")
    if guarded(server):
        head = GUARD.format(path=shlex.quote(server["path"]), branch=project.get("branch") or "main")
    else:
        head = (f"set -euo pipefail\ncd {shlex.quote(server['path'])}\n"
                "echo \"== Deploying your working copy to the local server\"\nset -x\n")
    return head + server["deploy"].strip() + "\n"


def deploy(project_id, project, server_id=None):
    server = settings(project, server_id)
    session = terminal_id("deploy", project_id, server.get("id"))
    script_file = STATE_DIR / "runs" / session / "deploy-remote.sh"
    script_file.parent.mkdir(parents=True, exist_ok=True)
    script_file.write_text(deploy_script(project, server.get("id")))
    command = (f"echo 'Deploying {project.get('name', project_id)} on {server['ssh']}'; echo; "
               f"ssh {shlex.join(SSH_OPTIONS)} {shlex.quote(server['ssh'])} 'bash -s' "
               f"< {shlex.quote(str(script_file))}")
    cache.forget(f"server:{project_id}:{server.get('id')}:")
    return launch(session, project_id, command, f"deploy to {server['ssh']}")


SITE_WAIT_SECONDS = 180


def _site_up(url):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        return error.code < 500
    except (OSError, ValueError):
        return False


def open_site(project_id, project, server_id=None):
    """Open a server's site in the browser, starting the server first if it has a start
    command and the site isn't answering. Returns what happened, for the message."""
    import services
    server = settings(project, server_id)
    url = server.get("site")
    if not url:
        raise ServerError("This server has no site address. Add one in Settings, under Servers.")
    started = False
    if not _site_up(url):
        if not server.get("start"):
            raise ServerError(f"{url} isn't answering.")
        log = services.run(project_id, project, f"server-{server.get('id')}", server["start"])
        deadline = time.time() + SITE_WAIT_SECONDS
        while not _site_up(url):
            if time.time() > deadline:
                raise ServerError(f"Started the server, but {url} still isn't answering after "
                                  f"{SITE_WAIT_SECONDS // 60} minutes. Output: {log}")
            time.sleep(3)
        started = True
    subprocess.Popen(["xdg-open", url], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"url": url, "started": started}
