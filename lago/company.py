"""A company on one Lago Agents: people, their roles per project, and what each role may do.

People are recognized by their Tailscale identity: when a browser connects over the tailnet,
`identify()` asks Tailscale whose device it is (`tailscale whois`), so there are no passwords.
Someone new lands on a "request access" page; an admin lets them in (People page) with a
company-wide role and, if they like, a different role per project.

Roles, from least to most: watch (see everything in their projects, live), operate (also
message agents, answer them, interrupt them, start and hold tasks), admin (also approve plans
and change settings, budgets, secrets and routines; company admins also manage people). The
owner, the person at this computer's desktop app, is an admin everywhere.

`authorize()` is the policy: for an Api method and its arguments, which role it needs and which
project it touches (found from the session, agent, plan, task... it names). Methods not listed
are for company admins only. `filter_result()` trims what list methods return to the projects
a person may see. Work outside any project counts as the project "" ("No project").
"""

import json
import re
import subprocess
import time

import config_store
import desktop
import hub

ROLES = ("watch", "operate", "admin")
RANK = {role: n for n, role in enumerate(ROLES, 1)}
WHOIS_FRESH = 60

_whois_cache = {}


class AccessError(Exception):
    pass


# ---------------------------------------------------------------- who is it

def whois(ip):
    """{"login", "name", "picture", "device"} for a tailnet address, or None."""
    now = time.time()
    cached = _whois_cache.get(ip)
    if cached and now - cached[0] < WHOIS_FRESH:
        return cached[1]
    try:
        out = subprocess.run([desktop.tailscale_cli(), "whois", "--json", ip], capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        data = None
    who = None
    if data and (data.get("UserProfile") or {}).get("LoginName"):
        profile, node = data["UserProfile"], data.get("Node") or {}
        who = {"login": profile["LoginName"].lower(), "name": profile.get("DisplayName") or profile["LoginName"],
               "picture": profile.get("ProfilePicURL") or "", "device": node.get("ComputedName") or ""}
    _whois_cache[ip] = (now, who)
    return who


def owner():
    """The owner's login: the Tailscale user of this computer (company.owner in config.json)."""
    config = config_store.load()
    return ((config.get("company") or {}).get("owner") or "").lower()


def company():
    config = config_store.load()
    return {"name": (config.get("company") or {}).get("name") or "", "owner": owner()}


def this_machine_login():
    """The Tailscale login of this computer's user, or "" when Tailscale isn't up."""
    try:
        out = subprocess.run([desktop.tailscale_cli(), "ip", "-4"], capture_output=True, text=True, timeout=5)
        ip = out.stdout.split()[0] if out.returncode == 0 and out.stdout.split() else ""
    except (OSError, subprocess.TimeoutExpired):
        ip = ""
    who = whois(ip) if ip else None
    return who["login"] if who else ""


def join(url):
    """This computer's user joins a company hosted elsewhere: remember its address."""
    config = config_store.without_swarm(config_store.load())
    config["company"] = {**(config.get("company") or {}), "joined": url}
    config["onboarded"] = True
    config_store.save(config)


def check_hub(url):
    """Whether a company's address answers like Lago Agents: {"ok", "message"}."""
    import urllib.request
    if not re.fullmatch(r"https?://[\w.\-\[\]:]+(/.*)?", url or ""):
        return {"ok": False, "message": "Give the address as http://name:port, as your admin sent it."}
    try:
        with urllib.request.urlopen(url, timeout=6) as reply:
            page = reply.read(4000).decode("utf-8", "replace")
    except Exception as error:  # unreachable, refused, timed out: say so plainly
        return {"ok": False, "message": f"Couldn't reach it ({type(error).__name__}). Is your VPN on?"}
    if "Lago Agents" not in page:
        return {"ok": False, "message": "Something answered, but it isn't Lago Agents."}
    return {"ok": True, "message": "Found the company. It opens in your browser; ask to join there."}


def setup(name, owner_login):
    """Start a company on this computer (the welcome wizard's "new company")."""
    config = config_store.without_swarm(config_store.load())
    config["company"] = {**(config.get("company") or {}), "name": " ".join((name or "").split())[:80],
                         "owner": (owner_login or "").lower()}
    config_store.save(config)
    return company()


# ---------------------------------------------------------------- members

def member(login):
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM members WHERE login = ?", ((login or "").lower(),)).fetchone()
    return dict(row) if row else None


def members(status=None):
    with hub.connect() as conn:
        rows = conn.execute("SELECT * FROM members" + (" WHERE status = ?" if status else "") + " ORDER BY name",
                            (status,) if status else ())
        out = [dict(r) for r in rows]
        roles = {}
        for r in conn.execute("SELECT * FROM member_projects"):
            roles.setdefault(r["login"], {})[r["project"]] = r["role"]
    for m in out:
        m["projects"] = roles.get(m["login"], {})
    return out


def request_access(who):
    """Someone new connected: record them as waiting for an admin. Returns their member row."""
    if is_blocked(who["login"]):
        raise AccessError("Not available")
    found = member(who["login"])
    if found:
        with hub.connect() as conn:
            conn.execute("UPDATE members SET last_seen = ?, name = ?, picture = ? WHERE login = ?",
                         (time.time(), who["name"], who.get("picture", ""), found["login"]))
        return member(who["login"])
    now = time.time()
    with hub.connect() as conn:
        conn.execute("INSERT INTO members (login, name, picture, role, admin, status, requested_at, last_seen) "
                     "VALUES (?, ?, ?, 'watch', 0, 'pending', ?, ?)",
                     (who["login"], who["name"], who.get("picture", ""), now, now))
    import activity
    activity.log("system", "people.requested", f"{who['name']} ({who['login']}) asked to join from {who.get('device') or 'a device'}",
                 target=who["login"])
    return member(who["login"])


def admit(login, role="watch", admin=False, by="you"):
    if role not in ROLES:
        raise AccessError(f"A role is one of: {', '.join(ROLES)}")
    if not member(login):
        raise AccessError(f"{login} hasn't asked to join")
    with hub.connect() as conn:
        conn.execute("UPDATE members SET status = 'active', role = ?, admin = ?, joined_at = COALESCE(joined_at, ?) "
                     "WHERE login = ?", (role, 1 if admin else 0, time.time(), login.lower()))
    import activity
    activity.log(by, "people.admitted", f"{login} as {'company admin' if admin else role}", target=login)
    return member(login)


def set_project_role(login, project, role, by="you"):
    """A role for one project, or None to fall back to their company-wide role."""
    if role is not None and role not in ROLES + ("none",):
        raise AccessError(f"A role is one of: {', '.join(ROLES)}, none")
    with hub.connect() as conn:
        if role is None:
            conn.execute("DELETE FROM member_projects WHERE login = ? AND project = ?", (login.lower(), project))
        else:
            conn.execute("INSERT INTO member_projects (login, project, role) VALUES (?, ?, ?) "
                         "ON CONFLICT (login, project) DO UPDATE SET role = excluded.role", (login.lower(), project, role))
    import activity
    activity.log(by, "people.role", f"{login}: {role or 'company role'} on {project or 'work outside projects'}",
                 target=login, project=project or None)


def remove(login, by="you"):
    with hub.connect() as conn:
        conn.execute("UPDATE members SET status = 'removed' WHERE login = ?", (login.lower(),))
        conn.execute("DELETE FROM member_projects WHERE login = ?", (login.lower(),))
    import activity
    activity.log(by, "people.removed", login, target=login)


def blocked():
    """Logins kept out (company.blocked in config.json): people on the tailnet for something else,
    like friends you share this computer with. They get a bare "not available", no join page."""
    return sorted({l.lower() for l in (config_store.load().get("company") or {}).get("blocked") or []})


def is_blocked(login):
    return bool(login) and login.lower() in blocked() and not is_owner(login)


def block(login, by="you"):
    login = (login or "").strip().lower()
    if not login or "@" not in login:
        raise AccessError("Give the person's Tailscale login, like name@gmail.com")
    if is_owner(login):
        raise AccessError("You can't keep yourself out")
    config = config_store.without_swarm(config_store.load())
    info = config.setdefault("company", {})
    info["blocked"] = sorted(set(blocked()) | {login})
    config_store.save(config)
    if member(login):
        remove(login, by)
    import activity
    activity.log(by, "people.blocked", login, target=login)


def unblock(login, by="you"):
    config = config_store.without_swarm(config_store.load())
    info = config.setdefault("company", {})
    info["blocked"] = [l for l in blocked() if l != (login or "").lower()]
    config_store.save(config)
    import activity
    activity.log(by, "people.unblocked", login, target=login)


# ---------------------------------------------------------------- roles

def handlers(config=None):
    """{project: login} of the members who handle each project's questions and approvals."""
    config = config or config_store.load()
    return {pid: p["handler"].lower() for pid, p in (config.get("projects") or {}).items()
            if p.get("handler") and member_active(p["handler"])}


def member_active(login):
    found = member(login)
    return bool(found) and found["status"] == "active"


def set_handler(project, login, by="you"):
    """Send a project's questions and approvals to a member ("" or None: to you)."""
    config = config_store.without_swarm(config_store.load())
    if project not in config.get("projects", {}):
        raise AccessError(f"There's no project {project!r}")
    login = (login or "").strip().lower()
    if login:
        if not member_active(login):
            raise AccessError(f"{login} isn't an active member")
        if RANK.get(role_for(login, project) or "", 0) < RANK["operate"]:
            raise AccessError(f"{login} needs operate access to {project} to answer its questions")
        config["projects"][project]["handler"] = login
    else:
        config["projects"][project].pop("handler", None)
    config_store.save(config)
    import activity
    activity.log(by, "people.handler", f"{project}: {login or 'you'}", target=login or "owner", project=project)


def owner_logins():
    """Every login that is the owner: this computer's Tailscale user, and the owner's account on
    the company's own tailnet (company_tailnet.py), if it has one."""
    config = config_store.load()
    extra = (config.get("company") or {}).get("owner_logins") or []
    return {l.lower() for l in [owner(), *extra] if l}


def add_owner_login(login):
    config = config_store.without_swarm(config_store.load())
    info = config.setdefault("company", {})
    logins = {l.lower() for l in info.get("owner_logins") or []}
    if login and login.lower() not in logins and login.lower() != owner():
        info["owner_logins"] = sorted(logins | {login.lower()})
        config_store.save(config)


def is_owner(login):
    return bool(login) and login.lower() in owner_logins()


def role_for(login, project):
    """Their role on a project ("" for work outside projects), or None."""
    if is_owner(login):
        return "admin"
    m = member(login)
    if not m or m["status"] != "active":
        return None
    if m["admin"]:
        return "admin"
    with hub.connect() as conn:
        row = conn.execute("SELECT role FROM member_projects WHERE login = ? AND project = ?",
                           (m["login"], project or "")).fetchone()
    role = row["role"] if row else m["role"]
    return None if role == "none" else role


def company_admin(login):
    if is_owner(login):
        return True
    m = member(login)
    return bool(m and m["status"] == "active" and m["admin"])


def projects_for(login):
    """{project id: role} for every project they may see ("" is work outside projects)."""
    config = config_store.load()
    out = {}
    for pid in [*config["projects"], ""]:
        role = role_for(login, pid)
        if role:
            out[pid] = role
    return out


# ---------------------------------------------------------------- what an action touches

def _agent_project(agent_id):
    config = config_store.load()
    agent = config["agents"].get(agent_id)
    if agent is None:
        raise AccessError(f"No agent {agent_id}")
    if agent.get("orchestrator") or not agent.get("swarm"):
        return None  # Lago and fixed teams belong to the company, not a project
    return agent.get("project") or ""


def _workspace_project(workspace_id):
    ws = hub.get_workspace(int(workspace_id))
    if ws is None:
        raise AccessError(f"No task #{workspace_id}")
    return ws["project"] or ""


def _from_id(table, column="project"):
    def resolve(row_id):
        with hub.connect() as conn:
            row = conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (int(row_id),)).fetchone()
        if row is None:
            raise AccessError(f"Not found: {table} #{row_id}")
        return row[column] or ""
    return resolve


def _task_project(task_id):
    task = hub.get_task(int(task_id))
    if task is None:
        raise AccessError(f"No task #{task_id}")
    return _agent_project(task["to_agent"])


def _doc_project(doc_id):
    with hub.connect() as conn:
        row = conn.execute("SELECT workspace FROM documents WHERE id = ?", (int(doc_id),)).fetchone()
    if row is None:
        raise AccessError(f"No document #{doc_id}")
    return _workspace_project(row["workspace"])


def _repo_project(repo):
    import pull_requests
    return pull_requests._project_of(repo)[0]


SCOPES = {"project_id": lambda v: v or "", "project": lambda v: v or "", "workspace_id": _workspace_project,
          "agent_id": _agent_project, "pipeline_id": _from_id("pipelines"), "task_id": _task_project,
          "event_id": _from_id("events"), "doc_id": _doc_project, "routine_id": _from_id("routines"),
          "goal_id": _from_id("goals"), "card_id": _from_id("status_cards"), "repo": _repo_project}

# Which role each action needs; anything not here is for company admins only.
WATCH = {"browser_view", "session_changes", "session_change_diff", "init", "me", "catalog", "definitions", "projects", "project", "overview", "my_tasks", "tasks",
         "task_comments", "pipelines", "pipeline", "workspace", "agent_detail", "session_documents", "document",
         "goals", "project_summary", "status_cards", "activity", "search", "github_repos", "github_board",
         "services_status", "service_logs", "server_status", "routines", "routine_runs", "project_map",
         "terminal_snapshot", "terminal_fit", "terminal_unfit", "terminal_running", "show_terminal",
         "hide_terminal", "alerts", "live_activity", "timeline", "file_activity", "terminal_wall", "replay"}
OPERATE = {"new_task", "send_message", "press", "terminal_send", "terminal_key", "answer_question",
           "comment_task", "comment_document", "hold_session", "set_session_quiet", "give_task", "start_agent",
           "stop_agent", "open_workspace", "close_workspace", "reopen_workspace", "rate_task", "set_task",
           "set_task_labels", "run_routine", "service_action", "dismiss_notification", "notification_opened"}
PROJECT_ADMIN = {"decide_pipeline", "cancel_pipeline", "link_goal", "extend_budget", "stop_over_budget",
                 "secrets", "save_secret", "remove_secret", "routine_status", "pr_check", "merge_pr",
                 "server_deploy", "server_deploy_preview", "project_command", "add_status_card",
                 "refresh_status_card", "archive_status_card", "judge_decision"}
# A person's own things: any member may use them, for themselves only.
PERSONAL = {"mark_read", "my_subscription", "connect_my_subscription", "my_login_screen", "my_login_send"}
# Methods that list across projects: anyone may call them; filter_result trims the answer.
LISTS = {"init", "me", "catalog", "definitions", "projects", "overview", "my_tasks", "tasks", "pipelines",
         "activity", "search", "status_cards", "routines", "goals", "alerts", "live_activity", "timeline"}


def needed(method):
    if method in WATCH:
        return "watch"
    if method in OPERATE:
        return "operate"
    if method in PROJECT_ADMIN:
        return "admin"
    return None  # company admins only


def scope(method, params):
    """The project an action touches ("" for none), or None when it names no project."""
    for name, resolve in SCOPES.items():
        if name in params and params[name] not in (None, ""):
            return resolve(params[name])
    if method in ("new_task", "open_workspace", "save_secret", "remove_secret", "secrets"):
        return params.get("project") or ""
    if method == "project_map":
        return params.get("project_id") or ""
    if method == "terminal_wall":
        return params.get("project") or ""
    return None


def authorize(login, method, params):
    """Raise AccessError unless `login` may call `method` with `params`. Returns their role."""
    if is_owner(login):
        return "admin"
    if method in PERSONAL:
        return "watch"
    level = needed(method)
    if level is None:
        if company_admin(login):
            return "admin"
        raise AccessError("Only company admins can do that")
    if method == "press" and params.get("key") in ("approve", "deny"):
        level = "admin"  # letting an agent use a tool is an admin's call
    project = scope(method, params)
    if project is None:
        if method in LISTS:
            return "watch"
        if company_admin(login):
            return "admin"
        raise AccessError("That belongs to the company, not one of your projects")
    role = role_for(login, project)
    if not role or RANK[role] < RANK[level]:
        raise AccessError(f"You need {level} access to {project or 'work outside projects'} for that")
    return role


# ---------------------------------------------------------------- trimming answers

def _visible(login):
    return projects_for(login)


def _agent_visible(agent_id, agent, visible):
    if agent.get("orchestrator") or not agent.get("swarm"):
        return False
    return (agent.get("project") or "") in visible


def _href_visible(href, visible):
    try:
        kind, _, ident = (href or "").lstrip("#").partition("/")
        if kind == "work":
            return _workspace_project(int(ident)) in visible
        if kind == "pipeline":
            return _from_id("pipelines")(int(ident)) in visible
        if kind == "agent":
            project = _agent_project(ident)
            return project is not None and project in visible
        if kind == "project":
            return ident in visible
    except (AccessError, ValueError):
        return False
    return False


def filter_result(login, method, result):
    """What a list method returns, trimmed to the projects `login` may see."""
    if is_owner(login) or company_admin(login):
        return result
    visible = _visible(login)
    if method == "init":
        config = result["config"]
        config["projects"] = {k: v for k, v in config["projects"].items() if k in visible}
        config["agents"] = {k: v for k, v in config["agents"].items() if _agent_visible(k, v, visible)}
        config["teams"] = {k: v for k, v in config["teams"].items() if v.get("swarm")}
        for secret_key in ("accounts", "budgets", "budget_windows", "voice", "orchestrator", "connectors"):
            config.pop(secret_key, None)
        return result
    if method == "overview":
        result["agents"] = {k: v for k, v in result["agents"].items()
                            if _agent_visible(k, (config_store.load()["agents"].get(k) or {}), visible)}
        result["swarm_agents"] = {k: v for k, v in result["swarm_agents"].items() if _agent_visible(k, v, visible)}
        result["notifications"] = [n for n in result["notifications"] if _href_visible(n.get("href"), visible)]
        result["open_sessions"] = [s for s in result.get("open_sessions") or []
                                   if _href_visible(f"#work/{s.get('id')}", visible)]
        result["account_alert"] = None
        result["pipelines_waiting"] = sum(1 for n in result["notifications"] if n.get("kind") == "plan")
        return result
    if method == "projects":
        return [p for p in result if p.get("id") in visible] if isinstance(result, list) else \
            {k: v for k, v in result.items() if k in visible}
    if method == "my_tasks":
        return [t for t in result if _href_visible(t["href"], visible)]
    if method == "tasks":
        config = config_store.load()
        return [t for t in result if _agent_visible(t["to_agent"], config["agents"].get(t["to_agent"]) or {}, visible)]
    if method == "pipelines":
        return [p for p in result if (p.get("project") or "") in visible]
    if method in ("activity", "status_cards", "routines", "goals", "live_activity", "timeline"):
        return [r for r in result if (r.get("project") or "") in visible]
    if method == "search":
        return [r for r in result if r.get("workspace") and _href_visible(f"#work/{r['workspace']}", visible)]
    return result
