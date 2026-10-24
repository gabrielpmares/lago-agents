"""Swarm agents built from GitHub repos, waiting for your approval.

The github-agent-builder reads a repo, vets and sandbox-tests it, and submits a draft
(submit_agent_draft): the agent's manifest, its tests, and how it runs the repo: an MCP
server launch (mcp.json, pinned) and/or an install script that sets the repo up under
~/lago-work/tools/<id>. Nothing is installed and nothing joins the catalog until you
approve the draft in the app. Then the install script runs (sandboxed: desktop.sandbox_argv), the agent, its tests and its
MCP server are added to the catalog, and its tests run once for a baseline.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import agentdefs
import config_store
import desktop
import hub
from team import TeamError

log = logging.getLogger("lago.agent_drafts")

DRAFTS_DIR = config_store.STATE_DIR / "agent-drafts"
TOOLS_DIR = config_store.SESSIONS_DIR / "tools"
INSTALL_TIMEOUT = 20 * 60
FILES = ("agent.md", "tests.json", "mcp.json", "install.sh")


def _digests(folder):
    """Each draft file's SHA-256, so what's installed is what you were shown."""
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
            for name in FILES if (folder / name).exists()}
# What an install script must never do; you read it before approving anyway.
FORBIDDEN = [(re.compile(p), why) for p, why in (
    (r"\bsudo\b", "uses sudo"),
    (r"\|\s*(ba|z)?sh\b", "pipes a download into a shell"),
    (r"(~|\$HOME)/\.(ssh|claude|config|gnupg)", "touches your private config folders"),
    (r"\brm\s+-[a-z]*r[a-z]*\s+(/|~|\$HOME)\s*($|;|&)", "deletes outside its folder"),
    (r"\bnpm\s+(i|install)\s+(-g|--global)\b", "installs globally"),
    (r"(^|[;&|(]\s*)(python3?\s+-m\s+)?pip3?\s+install\b", "installs Python packages "
     "outside a virtualenv (use .venv/bin/pip)"),
)]
STATUSES = ("ready", "installing", "installed", "failed", "rejected")
_lock = threading.Lock()


def _meta_path(draft_id):
    return DRAFTS_DIR / str(int(draft_id)) / "draft.json"


def _save(meta):
    path = _meta_path(meta["id"])
    meta["updated_at"] = time.time()
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def get(draft_id):
    try:
        meta = json.loads(_meta_path(draft_id).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TeamError(f"There's no agent draft #{draft_id}.") from error
    folder = _meta_path(draft_id).parent
    meta["files"] = {name: (folder / name).read_text(encoding="utf-8")
                     for name in FILES if (folder / name).exists()}
    log_file = folder / "install.log"
    meta["install_log"] = log_file.read_text(encoding="utf-8")[-4000:] if log_file.exists() else ""
    return meta


def drafts(status=None):
    found = []
    for path in sorted(DRAFTS_DIR.glob("*/draft.json"), key=lambda p: -int(p.parent.name)) \
            if DRAFTS_DIR.is_dir() else []:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if status is None or meta["status"] == status:
            found.append(meta)
    return found


# ---------------------------------------------------------------- submitting (the builder's tool)

def _check(folder, agent_id):
    """Problems with a draft folder, as a list of sentences."""
    problems = []
    if not agentdefs.AGENT_ID.match(agent_id or ""):
        problems.append("The id can only use lowercase letters, digits and dashes.")
    elif agent_id in agentdefs.catalog():
        problems.append(f"There's already a swarm agent `{agent_id}`: pick another id.")
    if not (folder / "agent.md").exists():
        return problems + ["agent.md (the manifest and prompt) is missing."]
    entry = agentdefs.manifest(folder / "agent.md")
    if not entry["summary"] or not entry["body"].strip():
        problems.append("agent.md needs a summary and a prompt.")
    if set(entry["tools"] or []) & agentdefs.SUBAGENT_TOOLS:
        problems.append("Swarm agents can't have Agent or Task.")
    if (folder / "mcp.json").exists():
        import team
        try:
            spec = json.loads((folder / "mcp.json").read_text(encoding="utf-8"))
        except ValueError:
            spec = None
        if not isinstance(spec, dict) or not spec.get("command"):
            problems.append("mcp.json needs a command and args.")
        elif not team.mcp_command_ok(spec["command"]) and not str(spec["command"]).startswith(
                str(TOOLS_DIR / agent_id)):
            problems.append(f"mcp.json's command must be one of {sorted(team.MCP_COMMANDS)} or "
                            f"a program under {TOOLS_DIR / agent_id}.")
        elif not re.search(r"@[\w.+-]+|[0-9a-f]{7,40}", " ".join(map(str, spec.get("args", [])))):
            problems.append("mcp.json must pin a version or commit (pkg@1.2.3, git+...@<sha>).")
        if agent_id not in entry["mcp"]:
            problems.append(f"agent.md's `mcp:` must name `{agent_id}` to use its server.")
    if (folder / "install.sh").exists():
        script = (folder / "install.sh").read_text(encoding="utf-8")
        problems += [f"install.sh {why}." for pattern, why in FORBIDDEN if pattern.search(script)]
    if (folder / "tests.json").exists():
        try:
            items = json.loads((folder / "tests.json").read_text(encoding="utf-8")).get("tests", [])
        except (ValueError, AttributeError):
            items = None
        if not isinstance(items, list) or len(items) < 3 or not all(
                isinstance(t, dict) and t.get("task") and t.get("criteria") for t in items):
            problems.append("tests.json needs {\"tests\": [...]} with at least 3 tests, each "
                            "with a task and criteria.")
    else:
        problems.append("tests.json is missing: every agent needs a test suite.")
    return problems


def submit(folder, agent_id, repo, summary, sandbox_report="", from_agent=None):
    folder = Path(os.path.expanduser(folder or ""))
    if not folder.is_dir():
        raise TeamError(f"There's no folder {folder}.")
    problems = _check(folder, agent_id)
    if problems:
        raise TeamError("The draft wasn't submitted. Fix these and try again:\n- "
                        + "\n- ".join(problems))
    with _lock:
        DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        draft_id = max([int(p.name) for p in DRAFTS_DIR.iterdir() if p.name.isdigit()],
                       default=0) + 1
        target = DRAFTS_DIR / str(draft_id)
        target.mkdir()
    for name in FILES:
        if (folder / name).exists():
            shutil.copyfile(folder / name, target / name)
    entry = agentdefs.manifest(target / "agent.md")
    meta = {"id": draft_id, "agent_id": agent_id, "name": entry["name"], "repo": repo,
            "summary": " ".join((summary or "").split())[:1500],
            "agent_summary": entry["summary"], "sandbox_report": sandbox_report or "",
            "status": "ready", "from_agent": from_agent, "created_at": time.time(), "notes": "",
            "digests": _digests(target)}
    _save(meta)
    return draft_id


def propose(agent_id, reason, tests, from_agent=None, **fields):
    """A Conductor proposes a new swarm agent when none in the catalog fits a step: the same
    draft you approve on the Swarm page as one built from GitHub, with no install script or
    MCP server. `fields` are agentdefs.write_agent's (name, summary, prompt, domain, ...)."""
    import tempfile
    if agent_id in agentdefs.catalog():
        raise TeamError(f"There's already a swarm agent `{agent_id}`: use it, or pick another id.")
    with tempfile.TemporaryDirectory(prefix="hire-", dir=config_store.STATE_DIR) as folder:
        try:
            agentdefs.write_agent(agent_id, folder=folder, **fields)
        except (ValueError, TypeError) as error:
            raise TeamError(str(error)) from error
        Path(folder, "tests.json").write_text(json.dumps({"tests": tests or []}, indent=2), encoding="utf-8")
        return submit(folder, agent_id, "", reason, from_agent=from_agent)


def dismiss(draft_id):
    """Hide a failed or installed draft from the Agents page."""
    meta = get(draft_id)
    if meta["status"] not in ("failed", "installed"):
        raise TeamError("Only failed or installed drafts can be dismissed.")
    meta = {k: v for k, v in meta.items() if k not in ("files", "install_log")}
    meta["status"] = "dismissed"
    _save(meta)


# ---------------------------------------------------------------- your decision

def decide(draft_id, approve):
    meta = get(draft_id)
    if meta["status"] != "ready":
        raise TeamError(f"Draft #{draft_id} is {meta['status']}.")
    if not approve:
        meta["status"] = "rejected"
        _save({k: v for k, v in meta.items() if k not in ("files", "install_log")})
        return "Rejected. Nothing was installed."
    folder = _meta_path(draft_id).parent
    if meta.get("digests") is not None and _digests(folder) != meta["digests"]:
        raise TeamError("Its files changed after it was submitted. Reject it and ask for a new draft.")
    problems = _check(folder, meta["agent_id"])  # the catalog may have changed since
    if problems:
        raise TeamError("It can't be installed now:\n- " + "\n- ".join(problems))
    meta = {k: v for k, v in meta.items() if k not in ("files", "install_log")}
    meta["status"] = "installing"
    _save(meta)
    threading.Thread(target=_install, args=(meta,), name=f"draft-{draft_id}", daemon=True).start()
    return (f"Installing {meta['name']}. It joins the swarm when its setup finishes, and its "
            "tests run once for a baseline.")


def _install(meta):
    folder = _meta_path(meta["id"]).parent
    agent_id = meta["agent_id"]
    try:
        script = folder / "install.sh"
        if script.exists():
            target = TOOLS_DIR / agent_id
            target.mkdir(parents=True, exist_ok=True)
            # The script gets a copy inside its folder: the sandbox sees nothing else.
            shutil.copyfile(script, target / ".lago-install.sh")
            argv = ["bash", "-euo", "pipefail", str(target / ".lago-install.sh")]
            sandboxed = desktop.sandbox_argv(argv, target, {"TOOL_DIR": target})
            with open(folder / "install.log", "w", encoding="utf-8") as out:
                if not sandboxed:
                    out.write("Not sandboxed: install bubblewrap (bwrap) so setup scripts can't reach your files.\n\n")
                    out.flush()
                # Without bubblewrap it still runs with none of your environment, at home in its folder.
                proc = subprocess.run(sandboxed or argv, cwd=target, stdout=out, stderr=subprocess.STDOUT,
                                      timeout=INSTALL_TIMEOUT,
                                      env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(target),
                                           "LANG": os.environ.get("LANG", "C.UTF-8"), "TOOL_DIR": str(target)})
            (target / ".lago-install.sh").unlink(missing_ok=True)
            if proc.returncode != 0:
                raise RuntimeError(f"install.sh exited with {proc.returncode} (see the log)")
        if agent_id in agentdefs.catalog():
            raise RuntimeError(f"`{agent_id}` appeared in the catalog meanwhile")
        if (folder / "mcp.json").exists():
            import team
            team.MCP_REGISTRY.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(folder / "mcp.json", team.MCP_REGISTRY / f"{agent_id}.json")
        agentdefs.CATALOG_AGENTS.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(folder / "agent.md", agentdefs.CATALOG_AGENTS / f"{agent_id}.md")
        import evals
        tests = json.loads((folder / "tests.json").read_text(encoding="utf-8"))["tests"]
        evals.save_tests(agent_id, [{**t, "origin": "drafted"} for t in tests])
        meta["status"] = "installed"
        try:
            evals.run_tests(agent_id)
            meta["notes"] = "Installed; its tests are running for a baseline."
        except TeamError as error:
            meta["notes"] = f"Installed; the baseline tests didn't start: {error}"
    except Exception as error:  # report it on the draft, never crash the app
        log.exception("installing draft %s failed", meta["id"])
        meta["status"] = "failed"
        meta["notes"] = str(error)[:600]
    _save(meta)
    hub.add_event(None, None, "news", f"{meta['name']}: "
                  + ("installed and in the swarm." if meta["status"] == "installed"
                     else f"installing failed. {meta['notes']}"))
