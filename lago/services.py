"""A project's running pieces: local servers, containers and live websites.

Each service in a project's config can say how to check it (a port, a health URL or a
Docker container) and how to start and stop it (shell commands run in the project
folder). Commands run detached, with their output in the state folder.
"""

import socket
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config_store
from config_store import STATE_DIR

LOG_DIR = STATE_DIR / "services"
CHECK_TIMEOUT = 2.5
LOG_LINES = 200


class ServiceError(Exception):
    """A problem worth showing as-is."""


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except (OSError, ValueError):
        return False


def _http_ok(url):
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "lago-agents"})
    try:
        with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        return error.code < 500  # it answered: it's up, even if this path needs auth
    except (OSError, ValueError):
        return False


def _container_state(name):
    try:
        proc = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", name],
                              capture_output=True, text=True, timeout=CHECK_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else "missing"


def check(service):
    """{"status": up|down|unknown, "detail": str} for one service."""
    if service.get("container"):
        state = _container_state(service["container"])
        if state is None:
            return {"status": "unknown", "detail": "Docker isn't available"}
        return {"status": "up" if state == "running" else "down", "detail": f"container {state}"}
    if service.get("health"):
        up = _http_ok(service["health"])
        return {"status": "up" if up else "down", "detail": "responds" if up else "no response"}
    if service.get("port"):
        up = _port_open(service["port"])
        return {"status": "up" if up else "down",
                "detail": f"port {service['port']} {'open' if up else 'closed'}"}
    return {"status": "unknown", "detail": "nothing to check"}


def _check_all(services):
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(check, services))
    return {s["id"]: r for s, r in zip(services, results)}


def statuses(project, refresh=False):
    """Every service's status, from the cache (checked again after 15 s, in the background)."""
    import hashlib
    import json
    import cache
    services = project.get("services") or []
    if not services:
        return {}
    key = "services:" + hashlib.sha1(json.dumps(services, sort_keys=True).encode()).hexdigest()[:12]
    result = cache.get(key, lambda: _check_all(services), fresh=15, keep=86400, refresh=refresh)
    return {**result["value"], "_cache": cache.meta(result)}


def _find(project, service_id):
    for service in project.get("services") or []:
        if service["id"] == service_id:
            return service
    raise ServiceError(f"There's no service {service_id!r}")


def _log_path(project_id, name):
    LOG_DIR.mkdir(exist_ok=True)
    return LOG_DIR / f"{project_id}-{name}.log"


def run(project_id, project, name, command):
    """Run a service or project command detached in the project folder."""
    if not (command or "").strip():
        raise ServiceError("No command is set for this. Add one in the project's Settings.")
    cwd = config_store.team_cwd({"teams": {}}, {"cwd": project.get("cwd")})
    if not cwd.is_dir():
        raise ServiceError(f"The folder {cwd} doesn't exist")
    log = _log_path(project_id, name)
    with open(log, "ab") as out:
        out.write(f"\n$ {command}\n".encode())
        out.flush()
        subprocess.Popen(["bash", "-lc", command], cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    return str(log)


def service_action(project_id, project, service_id, action):
    service = _find(project, service_id)
    if action not in ("start", "stop"):
        raise ServiceError(f"Unknown action {action!r}")
    return run(project_id, project, service_id, service.get(action))


def logs(project_id, project, service_id):
    """The end of the service's own log file, or of the output of Lago's commands."""
    service = _find(project, service_id)
    candidates = [Path(service["logs"]).expanduser()] if service.get("logs") else []
    candidates.append(_log_path(project_id, service_id))
    for path in candidates:
        if path.is_file():
            try:
                lines = path.read_text(errors="replace").splitlines()[-LOG_LINES:]
            except OSError as error:
                raise ServiceError(str(error)) from error
            return {"path": str(path), "text": "\n".join(lines)}
    return {"path": "", "text": ""}


def describe(project, service_statuses=None):
    """Services as text lines for agents: what runs where and how to reach it."""
    lines = []
    for service in project.get("services") or []:
        parts = [f"- {service.get('name', service['id'])}"]
        if service.get("url"):
            parts.append(service["url"])
        elif service.get("port"):
            parts.append(f"http://localhost:{service['port']}")
        if service.get("container"):
            parts.append(f"container {service['container']}")
        if service.get("logs"):
            parts.append(f"logs {service['logs']}")
        if service_statuses and service["id"] in service_statuses:
            parts.append(f"[{service_statuses[service['id']]['status']}]")
        lines.append(" · ".join(parts))
    return lines
