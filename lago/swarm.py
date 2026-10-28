"""The swarm: catalog agents that sleep until a Conductor wakes them for a pipeline.

You open a work session on a project (and one of its areas), or on no project at all, in a
folder of its own. Its Conductor, `w<N>`, is a Claude Code session in that folder that
talks with you, reads the project profile, agent manifests and skills, and proposes a
pipeline (steps, each run by one catalog agent). You approve it in the app; only then can
it wake agents: each step becomes a Lago task for `w<N>-<agent>`, a fresh session in the
same folder. Catalog agents are like tools: any number of sessions, in any projects, can
use one at once, and steps running side by side in one session get a copy each
(`w<N>-<agent>-2`, ...).
Used by the app (api.py) and by the Conductors' MCP servers.
"""

import os
import re
import subprocess
from pathlib import Path

import activity
import agentdefs
import config_store
import github
import hub
import memory
import notify
import runner
import services
import stepchecks
import team
import worktrees
from config_store import conductor_id
from team import TeamError

DONE_STEP = ("done", "skipped")
RESULT_PREVIEW = 1500
MAX_STEPS = 30


# ---------------------------------------------------------------- catalog and projects

def disabled(config):
    return set(config["swarm"].get("disabled", []))


def enabled_agents(config):
    off = disabled(config)
    return {cid: entry for cid, entry in agentdefs.catalog().items() if cid not in off}


def get_project(config, project_id):
    project = config["projects"].get(project_id)
    if project is None:
        known = ", ".join(config["projects"]) or "none yet (add them in the Projects page)"
        raise TeamError(f"There's no project {project_id!r}. Projects: {known}")
    return project


def instance_id(workspace_id, catalog_id, copy=1):
    suffix = f"-{copy}" if copy > 1 else ""
    return f"w{workspace_id}-{catalog_id}"[:48 - len(suffix)].rstrip("-") + suffix


def session_project(config, workspace):
    """The project a work session works on. One with no project gets a stand-in holding
    only its folder."""
    if workspace["project"]:
        return get_project(config, workspace["project"])
    return {"name": config_store.NO_PROJECT, "cwd": workspace.get("folder") or
            str(config_store.SESSIONS_DIR / f"session-{workspace['id']}")}


def session_text(config, workspace):
    """project_profile for a session: the project's profile, or its folder when it has none."""
    if workspace["project"]:
        return project_text(config, workspace["project"])
    cwd = project_cwd(session_project(config, workspace))
    lines = [f"# {config_store.NO_PROJECT} (work session #{workspace['id']})",
             f"Folder: {cwd}", "No repository, services, servers, boards or variables are "
             "set up: ask the human for what you need, and put the work in this folder."]
    if cwd.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in cwd.iterdir())
        lines.append("Contents: " + (", ".join(entries[:40]) or "empty")
                     + (f" (+{len(entries) - 40} more)" if len(entries) > 40 else ""))
    return "\n".join(lines + _git_summary(cwd) if cwd.is_dir() else lines)


def project_cwd(project):
    return config_store.team_cwd({"teams": {}}, {"cwd": project.get("cwd")})


def search(config, query="", domain=""):
    """Enabled agents matching the words in `query`, best match first."""
    words = [w for w in (query or "").lower().split() if len(w) > 2]
    scored = []
    for entry in enabled_agents(config).values():
        if domain and entry["domain"] != domain:
            continue
        text = " ".join([entry["id"], entry["name"], entry["summary"], entry["domain"],
                         entry["use_when"], " ".join(entry["tags"])]).lower()
        score = sum(text.count(w) for w in words) if words else 1
        if score:
            scored.append((-score, entry["id"], entry))
    return [entry for *_, entry in sorted(scored, key=lambda s: s[:2])]


def card(entry):
    """One agent's manifest in a few lines, for choosing between agents."""
    required = [i["name"] for i in entry["inputs"] if i["required"]]
    lines = [f"- `{entry['id']}` ({entry['domain']}) {entry['name']}: {entry['summary']}"]
    if entry["use_when"]:
        lines.append(f"  Use when: {entry['use_when']}")
    lines.append(f"  Needs: {', '.join(required) or 'nothing required'}"
                 f" · Produces: {', '.join(o['name'] for o in entry['outputs']) or 'see manifest'}")
    return "\n".join(lines)


def focus_brief(entry):
    """What Lago needs to work with one agent only: its job, inputs and limits."""
    required = [f"`{i['name']}` ({i['description']})" for i in entry["inputs"] if i["required"]]
    return (f"Working with `{entry['id']}` ({entry['name']}) only: {entry['summary']}\n"
            f"Required inputs: {', '.join(required) or 'none'}.\n"
            + (f"It can't do: {entry['avoid_when']}\n" if entry["avoid_when"] else "")
            + "Every task goes to it with run_agent; nothing else runs until they stop.")


def manifest_text(entry):
    def items(values, with_required):
        if not values:
            return ["- (none)"]
        return [f"- `{v['name']}`{' (required)' if with_required and v['required'] else ''}: "
                f"{v['description']}" for v in values]
    lines = [f"# {entry['name']} (`{entry['id']}`)", f"Domain: {entry['domain']}",
             f"Summary: {entry['summary']}"]
    for label, key in (("Requires", "requires"), ("Use when", "use_when"),
                       ("Avoid when", "avoid_when")):
        if entry[key]:
            lines.append(f"{label}: {entry[key]}")
    lines += ["", "## Inputs", *items(entry["inputs"], True),
              "", "## Outputs", *items(entry["outputs"], False)]
    if entry["tags"]:
        lines += ["", f"Tags: {', '.join(entry['tags'])}"]
    return "\n".join(lines)


def _git(cwd, *args):
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_summary(cwd):
    """Branch, uncommitted changes and recent commits, cached for 20 seconds."""
    import cache

    def read():
        branch = _git(cwd, "branch", "--show-current")
        if not branch:
            return []
        changed = len(_git(cwd, "status", "--porcelain").splitlines())
        return ["", f"## Git: on `{branch}`, {changed} uncommitted change(s)",
                _git(cwd, "log", "--oneline", "-8")]
    return cache.value(f"git:{cwd}", read, fresh=20, keep=3600)


def project_text(config, project_id, with_git=True):
    project = get_project(config, project_id)
    cwd = project_cwd(project)
    lines = [f"# {project.get('name', project_id)} (`{project_id}`)"]
    if project.get("description"):
        lines.append(project["description"])
    lines += [f"Folder: {cwd}"]
    for label, key in (("Repository", "repo"), ("Main branch", "branch"), ("Stack", "stack")):
        if project.get(key):
            lines.append(f"{label}: {project[key]}")
    if project.get("context"):
        lines.append("Read first: " + ", ".join(project["context"]))
    if project.get("links"):
        lines += ["", "## Links"] + [f"- {l.get('name', l['url'])}: {l['url']}" for l in project["links"]]
    if project.get("services"):
        lines += ["", "## Services (live status)"]
        lines += services.describe(project, services.statuses(project))
    import server as servers_module
    server_list = servers_module.servers(project)
    if server_list:
        lines += ["", "## Servers"]
        for srv in server_list:
            role = "production (deploys are guarded; never change it directly)" \
                if servers_module.guarded(srv) else "local replica (safe to change, test here first)"
            lines.append(f"- {srv.get('name') or srv['id']}: {role}. ssh {srv['ssh']}, code in {srv.get('path', '?')}")
    repos, boards = github.repos(project), github.boards(project)
    if repos or boards:
        lines += ["", "## GitHub (use github_board / gh for details)"]
        lines += [f"- repo {r}" for r in repos]
        lines += [f"- board {b.get('name') or b['number']}: owner {b['owner']}, number {b['number']}"
                  for b in boards]
    if project.get("vars"):
        lines += ["", "## Project variables (fill agent inputs with the same name)"]
        lines += [f"- {k}: {v}" for k, v in project["vars"].items()]
    if project.get("areas"):
        lines += ["", "## Areas"]
        for area_id, area in project["areas"].items():
            line = f"- `{area_id}` {area.get('name', area_id)}"
            if area.get("paths"):
                line += f" — paths: {', '.join(area['paths'])}"
            if area.get("repo"):
                line += f" — repo {area['repo']}"
            if area.get("description"):
                line += f". {area['description']}"
            if area.get("agents"):
                line += f" Preferred agents: {', '.join(area['agents'])}."
            lines.append(line)
    if project.get("agents"):
        lines += ["", f"Preferred agents for this project: {', '.join(project['agents'])}"]
    if with_git and cwd.is_dir():
        lines += _git_summary(cwd)
    return "\n".join(lines)


def templates_text():
    templates = agentdefs.pipeline_templates()
    if not templates:
        return "There are no pipeline templates."
    return "\n".join(f"- `{t['id']}` ({t['domain']}) {t['name']}: {t['summary']} "
                     f"Agents: {', '.join(t['agents'])}" for t in templates.values())


# ---------------------------------------------------------------- pipelines

def _as_list(value):
    if value in (None, ""):
        return []
    return [str(v).strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]


def _normalize_steps(config, project, raw_steps, previous=None):
    """Check a proposed plan against the catalog and manifests; return the steps to store.
    Steps kept from `previous` (same id and agent) keep their progress."""
    catalog = agentdefs.catalog()
    off = disabled(config)
    project_vars = project.get("vars") or {}
    known_skills = agentdefs.skills(project_cwd(project))
    kept = {s["id"]: s for s in previous or []}
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TeamError("A pipeline needs at least one step.")
    if len(raw_steps) > MAX_STEPS:
        raise TeamError(f"A pipeline can have at most {MAX_STEPS} steps.")
    steps, problems, seen = [], [], set()
    for number, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, dict):
            problems.append(f"Step {number} isn't an object.")
            continue
        step_id = str(raw.get("id") or f"step-{number}").strip()
        agent = str(raw.get("agent") or "").strip().strip("`")
        inputs = raw.get("inputs") or {}
        if not isinstance(inputs, dict):
            problems.append(f"Step `{step_id}`: inputs must be an object of name → value.")
            inputs = {}
        inputs = {str(k): str(v) for k, v in inputs.items() if str(v).strip()}
        after = _as_list(raw.get("after"))
        step_skills = _as_list(raw.get("skills"))
        unknown = [name for name in step_skills if name not in known_skills]
        if unknown:
            problems.append(f"Step `{step_id}`: unknown skills {', '.join(unknown)}. "
                            "Use find_skills to see the ones installed.")
        if step_id in seen:
            problems.append(f"Step id `{step_id}` is used twice.")
        entry = catalog.get(agent)
        if entry is None:
            problems.append(f"Step `{step_id}`: there's no swarm agent `{agent}`.")
        elif agent in off:
            problems.append(f"Step `{step_id}`: `{agent}` is switched off. Ask the human to switch "
                            "it on in the Swarm page, or pick another agent.")
        else:
            missing = [i["name"] for i in entry["inputs"]
                       if i["required"] and i["name"] not in inputs and i["name"] not in project_vars]
            if missing:
                problems.append(f"Step `{step_id}` ({agent}) is missing required inputs: "
                                f"{', '.join(missing)}.")
        for dep in after:
            if dep not in seen:
                problems.append(f"Step `{step_id}` runs after `{dep}`, which isn't an earlier step.")
        if not str(raw.get("task") or "").strip():
            problems.append(f"Step `{step_id}` has no task instructions.")
        criteria = [str(c).strip() for c in _as_list(raw.get("criteria")) if str(c).strip()]
        if len(criteria) > stepchecks.MAX_CRITERIA:
            problems.append(f"Step `{step_id}`: at most {stepchecks.MAX_CRITERIA} criteria.")
        reviewers = [str(r).strip().strip("`") for r in _as_list(raw.get("reviewers")) if str(r).strip()]
        if len(reviewers) > stepchecks.MAX_REVIEWERS:
            problems.append(f"Step `{step_id}`: at most {stepchecks.MAX_REVIEWERS} reviewers.")
        for reviewer in reviewers:
            if reviewer not in catalog:
                problems.append(f"Step `{step_id}`: there's no swarm agent `{reviewer}` to review it.")
            elif reviewer in off:
                problems.append(f"Step `{step_id}`: reviewer `{reviewer}` is switched off.")
        seen.add(step_id)
        step = {"id": step_id, "agent": agent,
                "title": str(raw.get("title") or (entry or {}).get("name") or agent)[:120],
                "task": str(raw.get("task") or "").strip(), "inputs": inputs, "after": after,
                "skills": step_skills,
                "gate": str(raw.get("gate", "")).lower() in ("true", "1", "yes"),
                "criteria": criteria, "reviewers": reviewers,
                "worktree": str(raw.get("worktree", "")).lower() in ("true", "1", "yes"),
                "status": "pending", "task_id": None, "attempts": 0, "result": ""}
        old = kept.get(step_id)
        if old and old["agent"] == agent and old["status"] in DONE_STEP:
            step.update({k: old[k] for k in ("status", "task_id", "attempts", "result")})
            if old.get("reviews"):
                step["reviews"] = old["reviews"]
            if old.get("instance"):
                step["instance"] = old["instance"]
        steps.append(step)
    if problems:
        raise TeamError("The pipeline wasn't saved. Fix these and try again:\n- " + "\n- ".join(problems))
    return steps


def propose(workspace_id, area, title, goal, steps):
    config = config_store.load()
    workspace = get_workspace(workspace_id)
    project_id = workspace["project"]
    project = session_project(config, workspace)
    area = (area or workspace["area"]) if project_id else None
    if area and area not in (project.get("areas") or {}):
        raise TeamError(f"{project.get('name', project_id)} has no area {area!r}. "
                        f"Areas: {', '.join(project.get('areas') or {}) or 'none'}")
    normalized = _normalize_steps(config, project, steps)
    pipeline_id = hub.create_pipeline(workspace_id, project_id, area or None,
                                      title.strip()[:120] or "Pipeline", goal.strip(), normalized)
    _waiting_for_approval(pipeline_id)
    return pipeline_id


def _waiting_for_approval(pipeline_id):
    pipeline = _get(pipeline_id)
    notify.report(pipeline["workspace"], "approval",
                  f"Pipeline #{pipeline_id} \"{pipeline['title']}\" is waiting for the human's "
                  f"approval in the app (work session #{pipeline['workspace']}).\n\n"
                  + status_text(pipeline))


# ---------------------------------------------------------------- work sessions

def get_workspace(workspace_id):
    workspace = hub.get_workspace(int(workspace_id))
    if workspace is None:
        raise TeamError(f"There's no work session #{workspace_id}")
    return workspace


def workspace_of(agent_id):
    """The work session a Conductor (`w<N>`) belongs to."""
    agent = config_store.load()["agents"].get(agent_id) or {}
    if not agent.get("conductor"):
        raise TeamError("Only a work session's Conductor can do this.")
    return get_workspace(agent["workspace"])


def _opening(project, area_id, text, project_id=""):
    area = (project.get("areas") or {}).get(area_id or "")
    where = f"the {area.get('name', area_id)} area of {project.get('name')}" if area \
        else f"{project.get('name')} (the whole project)" if project_id \
        else f"something outside my projects, in {project_cwd(project)}"
    if text:
        return f"I want to work on {where}.\n\n{text}"
    return (f"I want to work on {where}. Ask me what I want to do there; look around first "
            "so your question is specific.")


def _member_subscription():
    """When a company member (in a browser) starts work: the subscription it runs on, theirs.
    None for the owner and for agents (whose work runs on the active account)."""
    import company
    import member_accounts
    who = activity.person()
    if not who or company.is_owner(who["login"]):
        return None
    try:
        return member_accounts.run_on(who["login"])
    except member_accounts.MemberAccountError as error:
        raise TeamError(str(error)) from error


def open_workspace(project_id, area, text="", unrestricted=False, orchestrated=False, folder="",
                   goal=None):
    """Start a work session: a Conductor in the project folder, opening with your words.
    With no project, it works in `folder`, or in a new folder of its own under ~/lago-work."""
    config = config_store.load()
    text = (text or "").strip()
    folder = (folder or "").strip()
    whose = _member_subscription()
    if project_id:
        project = get_project(config, project_id)
        if area and area not in (project.get("areas") or {}):
            raise TeamError(f"{project.get('name', project_id)} has no area {area!r}")
        if not project_cwd(project).is_dir():
            raise TeamError(f"The folder {project_cwd(project)} doesn't exist. Fix it in Settings.")
        folder = None
    else:
        area = None
        if folder and not Path(os.path.expanduser(folder)).is_absolute():
            raise TeamError("Give the folder as an absolute path or one starting with ~/.")
    area_name = (project.get("areas") or {}).get(area or "", {}).get("name") if project_id else ""
    title = (text.splitlines()[0][:80] if text else "") or area_name \
        or ("Whole project" if project_id else config_store.NO_PROJECT)
    workspace_id = hub.create_workspace(project_id or "", area or None, title, unrestricted,
                                        orchestrated, folder or None)
    if whose:
        hub.update_workspace(workspace_id, account=whose["account"], codex_home=whose["codex_home"])
    if goal:
        import goals
        try:
            goals.link(workspace_id, int(goal))
        except (goals.GoalError, ValueError) as error:
            hub.update_workspace(workspace_id, status="closed")
            raise TeamError(str(error)) from error
    if not project_id:
        workspace = get_workspace(workspace_id)
        if not folder:
            hub.update_workspace(workspace_id, folder=session_project(config, workspace)["cwd"])
            workspace = get_workspace(workspace_id)
        project = session_project(config, workspace)
        project_cwd(project).mkdir(parents=True, exist_ok=True)
        _trust(project_cwd(project))
    import changes
    changes.record_base(workspace_id)
    import goals
    team.start(conductor_id(workspace_id),
               initial_text=_opening(project, area, text, project_id)
               + "\n".join(goals.session_lines(get_workspace(workspace_id))))
    activity.log(activity.actor(), "session.opened", title, target=f"session #{workspace_id}",
                 workspace=workspace_id, project=project_id or None)
    return workspace_id


def close_workspace(workspace_id):
    """End a work session: stop its Conductor and agents, cancel what's still open."""
    workspace = get_workspace(workspace_id)
    for pipeline in hub.pipelines("workspace = ? AND status IN "
                                  "('draft', 'changes_requested', 'approved', 'running')",
                                  (workspace["id"],)):
        _cancel(pipeline, notify=False)
    prefix = conductor_id(workspace["id"])
    for agent_id in [prefix] + [r["agent_id"] for r in hub.swarm_agents()
                                if r["workspace"] == workspace["id"]]:
        for task in hub.open_tasks_for(agent_id):
            hub.update_task(task["id"], status="cancelled")
        if runner.is_running(agent_id):
            team.stop(agent_id)
    hub.update_workspace(workspace["id"], status="closed")
    activity.log(activity.actor(), "session.closed", workspace["title"],
                 target=f"session #{workspace['id']}", workspace=workspace["id"],
                 project=workspace["project"] or None)
    memory.session_closed_later(workspace["id"])  # Lago keeps one line of it


def reopen_workspace(workspace_id):
    workspace = get_workspace(workspace_id)
    hub.update_workspace(workspace["id"], status="open")
    agent_id = conductor_id(workspace["id"])
    if not runner.is_running(agent_id):
        team.start(agent_id)


def revise(pipeline_id, steps=None, title=None, goal=None):
    """Change a pipeline. Anything but a draft goes back to you for approval."""
    pipeline = _get(pipeline_id)
    if pipeline["status"] not in hub.OPEN_PIPELINE:
        raise TeamError(f"Pipeline #{pipeline_id} is {pipeline['status']} and can't change. "
                        "Propose a new one instead.")
    fields = {"status": "draft", "feedback": ""}
    if steps is not None:
        running = [s["id"] for s in sync(pipeline)["steps"] if s["status"] == "running"]
        if running:
            raise TeamError(f"Steps {', '.join(running)} are still running. Wait for them first.")
        config = config_store.load()
        project = session_project(config, get_workspace(pipeline["workspace"]))
        fields["steps"] = _normalize_steps(config, project, steps, previous=pipeline["steps"])
    if title:
        fields["title"] = title.strip()[:120]
    if goal:
        fields["goal"] = goal.strip()
    hub.update_pipeline(pipeline_id, **fields)
    _waiting_for_approval(pipeline_id)
    return _get(pipeline_id)


def _get(pipeline_id):
    pipeline = hub.get_pipeline(int(pipeline_id))
    if pipeline is None:
        raise TeamError(f"There's no pipeline #{pipeline_id}")
    return pipeline


def sync(pipeline):
    """Bring each running step up to date with its task: check its criteria and pass it
    through its reviewers (stepchecks.py) before it counts as done."""
    changed, reruns = False, []
    for step in pipeline["steps"]:
        if step["status"] == "running" and step["task_id"]:
            task = hub.get_task(step["task_id"])
            if task is None or task["status"] in hub.OPEN_TASK:
                continue
            changed = True
            step["result"] = team.clip(task["result"], RESULT_PREVIEW)
            if task["status"] != "done":
                step["status"] = "failed"
                continue
            ok, missing = stepchecks.check_criteria(step.get("criteria"), task["result"])
            if not ok:
                step["status"] = "failed"
                step["result"] = team.clip("Its acceptance criteria aren't met:\n- " + "\n- ".join(missing)
                                           + "\n\n" + task["result"], RESULT_PREVIEW)
                continue
            if step.get("reviewers"):
                step["review"] = {"index": 0, "round": (step.get("review") or {}).get("round", 1)}
                _start_review(pipeline, step, task["result"])
            else:
                step["status"] = "done"
        elif step["status"] == "reviewing" and (step.get("review") or {}).get("task_id"):
            review = step["review"]
            task = hub.get_task(review["task_id"])
            if task is None or task["status"] in hub.OPEN_TASK:
                continue
            changed = True
            reviewer = step["reviewers"][review["index"]]
            if task["status"] != "done":
                step["status"] = "failed"
                step["result"] = team.clip(f"Reviewer `{reviewer}` didn't finish its review "
                                           f"(task #{task['id']} {task['status']}).", RESULT_PREVIEW)
                continue
            verdict, text = stepchecks.parse_review(task["result"])
            step.setdefault("reviews", []).append({"reviewer": reviewer, "verdict": verdict,
                                                   "round": review["round"], "task_id": task["id"],
                                                   "text": team.clip(text, 600)})
            activity.log(instance_id(pipeline["workspace"], reviewer), f"review.{verdict}",
                         f"#{pipeline['id']} step {step['id']}: {text.splitlines()[0][:200] if text else ''}",
                         target=f"plan #{pipeline['id']}", workspace=pipeline["workspace"],
                         project=pipeline["project"] or None)
            if verdict == "approved":
                if review["index"] + 1 < len(step["reviewers"]):
                    review["index"] += 1
                    _start_review(pipeline, step, hub.get_task(step["task_id"])["result"])
                else:
                    step["status"] = "done"
            elif review["round"] >= stepchecks.MAX_ROUNDS:
                step["status"] = "failed"
                step["result"] = team.clip(f"Reviewer `{reviewer}` still asks for changes after "
                                           f"{review['round']} rounds:\n{text}", RESULT_PREVIEW)
            else:
                step["status"] = "failed"  # run_step re-runs it with the findings below
                step["review"] = {"index": 0, "round": review["round"] + 1}
                reruns.append((step["id"], reviewer, text))
    if changed:
        hub.update_pipeline(pipeline["id"], steps=pipeline["steps"])
    for step_id, reviewer, text in reruns:
        try:
            run_step(pipeline["id"], step_id, f"Reviewer `{reviewer}` asked for changes:\n{text}")
            _tell_conductor(pipeline["workspace"], f"[Lago] Pipeline #{pipeline['id']}: reviewer "
                            f"`{reviewer}` asked for changes on step `{step_id}`, so it went back to "
                            "its agent with the findings. Wait for it as before.")
        except TeamError as error:
            _tell_conductor(pipeline["workspace"], f"[Lago] Pipeline #{pipeline['id']}: reviewer "
                            f"`{reviewer}` asked for changes on step `{step_id}`, but it couldn't "
                            f"re-run: {error}")
        pipeline = _get(pipeline["id"])
    return pipeline


def _start_review(pipeline, step, result):
    """Hand the step's result to its next reviewer, as a task from the Conductor."""
    config = config_store.load()
    review = step["review"]
    reviewer = step["reviewers"][review["index"]]
    name = (agentdefs.catalog().get(reviewer) or {}).get("name", reviewer)
    try:
        agent_id, copy = _free_instance(config, pipeline, {"agent": reviewer})
        project = session_project(config, get_workspace(pipeline["workspace"]))
        hub.add_swarm_agent(agent_id, pipeline["workspace"], pipeline["project"], reviewer,
                            f"{name}{f' ({copy})' if copy > 1 else ''} · "
                            f"#{pipeline['workspace']} {project.get('name', pipeline['project'])}")
        body = stepchecks.review_body(pipeline, step, name, result, review["index"] + 1,
                                      len(step["reviewers"]))
        review["task_id"] = team.assign(agent_id, f"#{pipeline['id']} Review: {step['title']}"[:120],
                                        body, from_agent=conductor_id(pipeline["workspace"]))
    except TeamError as error:
        step["status"] = "failed"
        step["result"] = team.clip(f"Its reviewer `{reviewer}` couldn't start: {error}", RESULT_PREVIEW)
        return
    step["status"] = "reviewing"


def get(pipeline_id):
    return sync(_get(pipeline_id))


# ---------------------------------------------------------------- your decisions (from the app)

def _tell_conductor(workspace_id, text):
    if not workspace_id:
        return
    agent_id = conductor_id(workspace_id)
    hub.queue_text(agent_id, text)
    try:
        team.pump(agent_id, auto_start=True)
    except TeamError:
        pass  # it's in the inbox and arrives when the Conductor next starts


def decide(pipeline_id, decision, note="", by_lago=False):
    """approve, changes (request changes) or reject a draft. `by_lago`: Lago decided it for
    the human in Decide mode, and the Conductor is told so."""
    pipeline = _get(pipeline_id)
    if pipeline["status"] not in ("draft", "changes_requested"):
        raise TeamError(f"Pipeline #{pipeline_id} is {pipeline['status']}, not waiting for a decision.")
    note = (note or "").strip()
    head = f"[Lago] Pipeline #{pipeline_id} \"{pipeline['title']}\""
    who = "Lago, deciding for the human (Decide mode)," if by_lago else "the human"
    feedback = f"Lago (Decide mode): {note}" if by_lago and note else note
    if decision == "approve":
        hub.update_pipeline(pipeline["id"], status="approved", feedback=feedback)
        text = (f"{head} was approved by {who}. Run it now: call run_step for each step whose "
                "dependencies are done, then wait_for_tasks.")
    elif decision == "changes":
        if not note:
            raise TeamError("Say what should change.")
        hub.update_pipeline(pipeline["id"], status="changes_requested", feedback=feedback)
        text = f"{head}: {who} asked for changes. Revise it with revise_pipeline."
    elif decision == "reject":
        hub.update_pipeline(pipeline["id"], status="rejected", feedback=feedback)
        text = f"{head} was rejected by {who}. Don't run it."
    else:
        raise TeamError(f"Unknown decision {decision!r}")
    activity.log("orchestrator" if by_lago else activity.actor(),
                 {"approve": "plan.approved", "changes": "plan.changes", "reject": "plan.rejected"}[decision],
                 f"#{pipeline['id']} {pipeline['title']}" + (f": {note}" if note else ""),
                 target=f"plan #{pipeline['id']}", workspace=pipeline["workspace"],
                 project=pipeline["project"] or None)
    _tell_conductor(pipeline["workspace"], text + (f"\n\n{'His' if by_lago else 'Their'} note:\n{note}" if note else ""))
    return _get(pipeline_id)


def cancel(pipeline_id):
    pipeline = get(pipeline_id)
    if pipeline["status"] not in hub.OPEN_PIPELINE:
        raise TeamError(f"Pipeline #{pipeline_id} is already {pipeline['status']}.")
    _cancel(pipeline)
    return _get(pipeline_id)


def _cancel(pipeline, notify=True):
    for step in pipeline["steps"]:
        if step["status"] == "reviewing" and (step.get("review") or {}).get("task_id"):
            hub.update_task(step["review"]["task_id"], status="cancelled")
            step["status"] = "failed"
            step["result"] = "Cancelled by the human during its review."
        if step["status"] == "running" and step["task_id"]:
            hub.update_task(step["task_id"], status="cancelled")
            step["status"] = "failed"
            step["result"] = "Cancelled by the human."
    hub.update_pipeline(pipeline["id"], status="cancelled", steps=pipeline["steps"])
    activity.log(activity.actor(), "plan.cancelled", f"#{pipeline['id']} {pipeline['title']}",
                 target=f"plan #{pipeline['id']}", workspace=pipeline["workspace"],
                 project=pipeline["project"] or None)
    _sleep_idle_agents(pipeline)
    if notify:
        _tell_conductor(pipeline["workspace"], f"[Lago] Pipeline #{pipeline['id']} "
                        f"\"{pipeline['title']}\" was cancelled by the human. Don't run any more "
                        "of its steps.")


# ---------------------------------------------------------------- running (the Conductor)

def _step(pipeline, step_id):
    for step in pipeline["steps"]:
        if step["id"] == step_id:
            return step
    raise TeamError(f"Pipeline #{pipeline['id']} has no step `{step_id}`. Steps: "
                    f"{', '.join(s['id'] for s in pipeline['steps'])}")


def _task_body(config, pipeline, step, entry, notes):
    project = session_project(config, get_workspace(pipeline["workspace"]))
    number = pipeline["steps"].index(step) + 1
    lines = [f"Pipeline #{pipeline['id']} \"{pipeline['title']}\", step `{step['id']}` "
             f"({number} of {len(pipeline['steps'])}), planned by the Conductor.",
             "", "## Project",
             f"{project.get('name', pipeline['project'])}. Folder: {project_cwd(project)}"
             + ("" if pipeline["project"] else " (this work belongs to no project)")]
    for label, key in (("Repository", "repo"), ("Main branch", "branch"), ("Stack", "stack")):
        if project.get(key):
            lines.append(f"{label}: {project[key]}")
    area = (project.get("areas") or {}).get(pipeline["area"] or "")
    if area:
        lines.append(f"Area: {area.get('name', pipeline['area'])}"
                     + (f" ({', '.join(area['paths'])})" if area.get("paths") else "")
                     + (f". {area['description']}" if area.get("description") else ""))
    if area and area.get("repo"):
        lines.append(f"This area's repository: {area['repo']}")
    if project.get("context"):
        lines.append("Read first: " + ", ".join(project["context"]))
    if project.get("links"):
        lines.append("Links: " + ", ".join(f"{l.get('name', l['url'])} {l['url']}" for l in project["links"]))
    if project.get("services"):
        lines += ["", "## Running services (check they're up before relying on them)"]
        lines += services.describe(project)
    lines += ["", "## Pipeline goal", pipeline["goal"], "", "## Your task", step["task"]]
    if step.get("skills"):
        lines += ["", "## Skills to use",
                  "Load these with the Skill tool before you start: "
                  + ", ".join(f"`{name}`" for name in step["skills"])]
    project_vars = project.get("vars") or {}
    inputs = dict(step["inputs"])
    for spec in entry["inputs"]:
        if spec["name"] not in inputs and spec["name"] in project_vars:
            inputs[spec["name"]] = project_vars[spec["name"]]
    if inputs:
        described = {spec["name"]: spec["description"] for spec in entry["inputs"]}
        lines += ["", "## Inputs"]
        lines += [f"- {name}: {value}" + (f"  ({described[name]})" if described.get(name) else "")
                  for name, value in inputs.items()]
    if entry["outputs"]:
        lines += ["", "## Expected output"]
        lines += [f"- {o['name']}: {o['description']}" for o in entry["outputs"]]
    if step.get("worktree_path"):
        lines += worktrees.task_lines({"path": step["worktree_path"], "branch": step["branch"]})
    lines += stepchecks.criteria_block(step.get("criteria"))
    if notes:
        lines += ["", f"## Notes from the Conductor (attempt {step['attempts'] + 1})", notes]
    lines += ["", "You go back to sleep when the pipeline ends and your conversation isn't kept "
              "for later steps, so put your work in files and say where they are in your result."]
    return "\n".join(lines)


def run_step(pipeline_id, step_id, notes=""):
    """Wake the step's agent and hand it the step as a task. Returns (task id, agent id)."""
    config = config_store.load()
    pipeline = get(pipeline_id)
    if pipeline["status"] in ("draft", "changes_requested"):
        raise TeamError(f"Pipeline #{pipeline_id} is waiting for the human's approval in the "
                        "Pipelines page. Wait for the approval message.")
    if pipeline["status"] not in ("approved", "running"):
        raise TeamError(f"Pipeline #{pipeline_id} is {pipeline['status']}.")
    import holds
    if (get_workspace(pipeline["workspace"]) or {}).get("held_at"):
        raise TeamError(f"Work session #{pipeline['workspace']} is on hold: the human paused it. "
                        "Wait until they resume it.")
    step = _step(pipeline, step_id)
    if step["status"] == "running":
        raise TeamError(f"Step `{step_id}` is already running as task #{step['task_id']}.")
    waiting = [dep for dep in step["after"] if _step(pipeline, dep)["status"] not in DONE_STEP]
    if waiting:
        raise TeamError(f"Step `{step_id}` has to wait: {', '.join(waiting)} "
                        f"{'isn' if len(waiting) == 1 else 'aren'}'t done yet.")
    entry = enabled_agents(config).get(step["agent"])
    if entry is None:
        raise TeamError(f"`{step['agent']}` is no longer available (switched off or removed).")
    project = session_project(config, get_workspace(pipeline["workspace"]))
    agent_id, copy = _free_instance(config, pipeline, step)
    existing = config["agents"].get(agent_id)
    if existing and not existing.get("swarm"):
        raise TeamError(f"The id `{agent_id}` belongs to {existing['name']} in the Teams page. "
                        "Ask the human to rename that agent so this swarm agent can wake.")
    hub.add_swarm_agent(agent_id, pipeline["workspace"], pipeline["project"], step["agent"],
                        f"{entry['name']}{f' ({copy})' if copy > 1 else ''} · "
                        f"#{pipeline['workspace']} {project.get('name', pipeline['project'])}")
    if step.get("worktree"):
        try:
            info = worktrees.ensure(project_cwd(project), pipeline["id"], step["id"])
        except worktrees.WorktreeError as error:
            raise TeamError(f"Step `{step_id}` wants its own worktree: {error}") from error
        step.update(worktree_path=info["path"], branch=info["branch"])
    body = _task_body(config, pipeline, step, entry, (notes or "").strip())
    task_id = team.assign(agent_id, f"#{pipeline_id} {step['title']}"[:120], body,
                          from_agent=conductor_id(pipeline["workspace"]))
    step.update(status="running", task_id=task_id, attempts=step["attempts"] + 1, result="",
                instance=agent_id)
    hub.update_pipeline(pipeline["id"], status="running", steps=pipeline["steps"])
    return task_id, agent_id


def _free_instance(config, pipeline, step):
    """The session that runs a step: the session's copy of the agent, or, while that one is
    busy with another step, a new copy, so steps side by side don't queue behind each other."""
    for copy in range(1, MAX_STEPS + 1):
        agent_id = instance_id(pipeline["workspace"], step["agent"], copy)
        if not hub.open_tasks_for(agent_id):
            return agent_id, copy
    raise TeamError(f"Every copy of `{step['agent']}` in this session is busy.")


def _trust(folder):
    """New folders would stop the session at Claude Code's trust question: trust
    ~/lago-work once (it covers every session folder in it), or the folder you named."""
    import accounts
    sessions = config_store.SESSIONS_DIR.resolve()
    folder = Path(folder).resolve()
    accounts.trust_folder(sessions if folder == sessions or sessions in folder.parents else folder)


# ---------------------------------------------------------------- one agent, one task (Lago)

# "Certain" runs move an agent one model tier up: accuracy over cost.
CERTAIN_MODELS = (("haiku", "claude-sonnet-5-5"), ("sonnet", "claude-opus-5-5"))


def certain_model(model):
    """The model a Certain run uses instead of the manifest's `model`."""
    for tier, stronger in CERTAIN_MODELS:
        if tier in (model or "").lower():
            return stronger
    return model or "claude-sonnet-5-5"


def run_agent(catalog_id, task, project_id="", folder="", inputs=None, unrestricted=False,
              from_agent=None, mode="cheap", variant=None, intro=None, allow_off=False, label=""):
    """Wake one catalog agent for one task, outside any work session: no Conductor, no
    pipeline. It works in the project's folder, in `folder`, or in ~/lago-work/runs, and its
    result goes back to `from_agent` (Lago). `mode` "certain" runs it on a stronger model
    with room to verify. `variant` runs a proposed manifest instead of the catalog's, and
    `intro` replaces the opening line (evals.py's test runs). `allow_off` runs it even while
    it's switched off (its tests: a specialist sits its exam before it's switched on). `label`
    is added to the run's name, so a run for something else says what it's for.
    Returns (task id, agent id, model)."""
    config = config_store.load()
    agents = agentdefs.catalog() if allow_off else enabled_agents(config)
    entry = agents.get((catalog_id or "").strip().strip("`"))
    if entry is None:
        raise TeamError(f"There's no switched-on swarm agent {catalog_id!r}. find_agents lists them.")
    if not (task or "").strip():
        raise TeamError("Say what the agent should do.")
    if project_id:
        project = get_project(config, project_id)
        cwd = project_cwd(project)
    else:
        project = {"name": config_store.NO_PROJECT}
        cwd = Path(os.path.expanduser(folder)) if folder else config_store.SESSIONS_DIR / "runs"
        if not cwd.is_absolute():
            raise TeamError("Give the folder as an absolute path or one starting with ~/.")
        cwd.mkdir(parents=True, exist_ok=True)
        _trust(cwd)
    if not cwd.is_dir():
        raise TeamError(f"The folder {cwd} doesn't exist.")
    whose = _member_subscription()
    taken = [int(r["agent_id"][1:].split("-")[0]) for r in hub.swarm_agents()
             if config_store.RUN_ID.match(r["agent_id"])]
    number = max(taken, default=0) + 1
    agent_id = f"r{number}-{entry['id']}"[:48].rstrip("-")
    certain = mode == "certain"
    model = certain_model(entry.get("model")) if certain else None
    hub.add_swarm_agent(agent_id, None, project_id or "", entry["id"],
                        f"{entry['name']} · run {number}"
                        + (" · certain" if certain else "")
                        + (f" · {project.get('name', project_id)}" if project_id else "")
                        + (f" · {label}" if label else ""),
                        folder=str(cwd), unrestricted=unrestricted, model=model,
                        variant=str(variant) if variant else None)
    if whose:
        with hub.connect() as conn:
            conn.execute("UPDATE swarm_agents SET account = ?, codex_home = ? WHERE agent_id = ?",
                         (whose["account"], whose["codex_home"], agent_id))
    inputs = {str(k): str(v) for k, v in (inputs or {}).items() if str(v).strip()}
    lines = [intro or "Lago, the human's assistant, asked you directly for this: one task, no pipeline.",
             "", "## Where",
             f"{project.get('name', project_id)}. Folder: {cwd}" if project_id
             else f"No project. Folder: {cwd}", "", "## Your task", task.strip()]
    if inputs:
        lines += ["", "## Inputs", *[f"- {k}: {v}" for k, v in inputs.items()]]
    if entry["outputs"]:
        lines += ["", "## Expected output", *[f"- {o['name']}: {o['description']}" for o in entry["outputs"]]]
    if certain:
        lines += ["", "## Mode: certain",
                  "The human chose accuracy over speed and cost for this run. Your limits on "
                  "searches, pages or steps are doubled. Confirm each figure the answer depends "
                  "on at its primary source; where sources disagree, say which you trust and why."]
    lines += ["", "Your result goes back to Lago, who tells the human: keep it short and to the "
              "point, and say where any files are. You go back to sleep afterwards."]
    task_id = team.assign(agent_id, task.strip().splitlines()[0][:100], "\n".join(lines),
                          from_agent=from_agent)
    return task_id, agent_id, model or entry.get("model") or "inherit"


def skip_step(pipeline_id, step_id, reason):
    pipeline = get(pipeline_id)
    if pipeline["status"] not in ("approved", "running"):
        raise TeamError(f"Pipeline #{pipeline_id} is {pipeline['status']}.")
    step = _step(pipeline, step_id)
    if step["status"] == "running":
        raise TeamError(f"Step `{step_id}` is running. Wait for it first.")
    step.update(status="skipped", result=f"Skipped: {reason}")
    hub.update_pipeline(pipeline["id"], steps=pipeline["steps"])


def finish(pipeline_id, status, summary, task_complete=False):
    """Close a pipeline. `task_complete`: it finished the session's whole task, so the report
    is "solved" (the human hears it's fully done and can close the session)."""
    pipeline = get(pipeline_id)
    if pipeline["status"] not in ("approved", "running"):
        raise TeamError(f"Pipeline #{pipeline_id} is {pipeline['status']}.")
    running = [s["id"] for s in pipeline["steps"] if s["status"] in ("running", "reviewing")]
    if running:
        raise TeamError(f"Steps {', '.join(running)} are still running. Wait for them, or ask the "
                        "human to cancel the pipeline.")
    status = status if status in ("done", "failed") else "done"
    hub.update_pipeline(pipeline["id"], status=status, summary=summary.strip())
    activity.log(conductor_id(pipeline["workspace"]), f"plan.{status}",
                 f"#{pipeline['id']} {pipeline['title']}: {summary.strip()[:300]}",
                 target=f"plan #{pipeline['id']}", workspace=pipeline["workspace"],
                 project=pipeline["project"] or None)
    kind = "solved" if task_complete and status == "done" else status
    notify.report(pipeline["workspace"], kind,
                  f"Pipeline #{pipeline['id']} \"{pipeline['title']}\" finished: {status}."
                  + (" The whole task is done: nothing is left for this session." if kind == "solved" else "")
                  + f"\n\n{summary.strip()}")
    return _sleep_idle_agents(pipeline)


def _sleep_idle_agents(pipeline):
    """Stop the sessions of this pipeline's agents that have no other open work."""
    slept = []
    for agent_id in {s.get("instance") or instance_id(pipeline["workspace"], s["agent"])
                     for s in pipeline["steps"]}:
        if runner.is_running(agent_id) and not hub.open_tasks_for(agent_id):
            team.stop(agent_id)
            slept.append(agent_id)
    return sorted(slept)


def status_text(pipeline, result_chars=None):
    """The pipeline as text; `result_chars` clips each step's result (None: in full)."""
    lines = [f"Pipeline #{pipeline['id']} \"{pipeline['title']}\" · "
             + (f"project {pipeline['project']}" if pipeline["project"] else "no project")
             + f"{' / ' + pipeline['area'] if pipeline['area'] else ''} · {pipeline['status']}"]
    if pipeline["feedback"]:
        lines.append(f"Human's note: {pipeline['feedback']}")
    for step in pipeline["steps"]:
        after = f" after {', '.join(step['after'])}" if step["after"] else ""
        gate = " [gate: ask the human after it]" if step["gate"] else ""
        gate += f" skills: {', '.join(step['skills'])}" if step.get("skills") else ""
        task = f" task #{step['task_id']}" if step["task_id"] else ""
        lines.append(f"- `{step['id']}` {step['agent']}: {step['status']}{task}{after}{gate}")
        if step["result"] and step["status"] != "running":
            result = team.clip(step["result"], result_chars) if result_chars else step["result"]
            lines.append("  Result: " + result.replace("\n", "\n  "))
    return "\n".join(lines)


def _project_id(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
    candidate, n = base, 2
    while candidate in taken or candidate in config_store.RESERVED_IDS:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def create_project(name, folder, description="", stack="", repo="", areas=None, git_init=False):
    """Add a project to config.json, making its folder if needed. Returns (id, notes)."""
    name, folder = (name or "").strip(), (folder or "").strip()
    if not name or not folder:
        raise TeamError("A project needs a name and a folder.")
    config = config_store.without_swarm(config_store.load())
    path = Path(os.path.expanduser(folder))
    if not path.is_absolute():
        raise TeamError("Give the folder as an absolute path or one starting with ~/.")
    for pid, project in config["projects"].items():
        if Path(os.path.expanduser(project.get("cwd", ""))) == path:
            raise TeamError(f"{project.get('name', pid)} (`{pid}`) already uses that folder.")
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    notes = ["created its folder" if not existed else "using the existing folder"]
    if git_init and not (path / ".git").exists():
        proc = subprocess.run(["git", "init", "-q"], cwd=path, capture_output=True, text=True)
        notes.append("ran git init" if proc.returncode == 0
                     else f"git init failed: {proc.stderr.strip()}")
    area_map = {}
    for area in areas or []:
        if str(area).strip():
            area_map[_project_id(str(area), area_map)] = {
                "name": str(area).strip(), "description": "", "paths": [], "agents": []}
    project_id = _project_id(name, config["projects"])
    config["projects"][project_id] = {
        "name": name, "icon": "cube", "cwd": folder, "repo": (repo or "").strip(), "branch": "main",
        "description": (description or "").strip(), "stack": (stack or "").strip(), "context": [],
        "vars": {}, "agents": [], "areas": area_map, "links": [], "services": [], "commands": {},
        "github": {"repos": [], "boards": []}, "mcp_servers": {}}
    config_store.save(config)
    activity.log(activity.actor(), "project.created", f"{name} in {folder}", target=f"project {project_id}",
                 project=project_id)
    return project_id, notes
