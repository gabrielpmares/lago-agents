"""The first start: a few questions, one per screen, that end with Lago ready to talk.

The app shows the welcome wizard when `needed()`: nothing says you've been through it and
there's no name or no project yet (an existing setup never sees it). It asks your name,
checks Claude Code is installed and logged in (no model call), and offers your first project:
pick a folder, and `detect()` reads what it can from it (its name, git, the GitHub repository,
the stack). `finish()` saves it all and leaves a start card on Lago's page with two choices,
"Plan something with me" or "I have a task in mind", until your first message.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import accounts
import config_store

# Files that give a project's stack away, in the order they're named.
STACK_MARKERS = (
    ("package.json", "Node.js"), ("tsconfig.json", "TypeScript"), ("pyproject.toml", "Python"),
    ("requirements.txt", "Python"), ("go.mod", "Go"), ("Cargo.toml", "Rust"), ("pom.xml", "Java"),
    ("build.gradle", "Java/Kotlin"), ("Gemfile", "Ruby"), ("composer.json", "PHP"),
    ("pubspec.yaml", "Flutter"), ("docker-compose.yml", "Docker Compose"), ("Dockerfile", "Docker"),
)
FRAMEWORKS = (("react", "React"), ("next", "Next.js"), ("vue", "Vue"), ("svelte", "Svelte"),
              ("express", "Express"), ("vite", "Vite"), ("astro", "Astro"))


def needed(config):
    return not config.get("onboarded") and (not config.get("user_name") or not config.get("projects"))


def claude_status():
    """Whether Claude Code is installed and logged in (read from its own status, no model call)."""
    installed = shutil.which("claude") is not None
    status = accounts.status(accounts.MAIN, refresh=True) if installed else {}
    return {"installed": installed, "logged_in": bool(status.get("logged_in")),
            "email": status.get("email", ""), "plan": status.get("plan", "")}


def _git(folder, *args):
    try:
        proc = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def detect(folder):
    """What a folder says about the project in it."""
    path = Path(os.path.expanduser((folder or "").strip()))
    if not path.is_absolute():
        return {"ok": False, "message": "Give the folder as an absolute path or one starting with ~/."}
    name = path.name.replace("-", " ").replace("_", " ").strip().title() or "My Project"
    if not path.is_dir():
        return {"ok": True, "exists": False, "name": name, "git": False, "repo": "", "stack": "",
                "message": "It doesn't exist yet: it'll be created, with git."}
    git = bool(_git(path, "rev-parse", "--show-toplevel"))
    remote = _git(path, "remote", "get-url", "origin") if git else ""
    import github
    if remote.startswith("git@github.com:"):  # SSH remotes name the repository after a colon
        remote = "github.com/" + remote.split(":", 1)[1]
    repo = github.repo_name(remote) if "github.com" in remote else ""
    repo = repo or ""
    stack = []
    for marker, label in STACK_MARKERS:
        if (path / marker).exists() and label not in stack:
            stack.append(label)
    try:
        deps = json.loads((path / "package.json").read_text())
        names = {**deps.get("dependencies", {}), **deps.get("devDependencies", {})}
        stack += [label for key, label in FRAMEWORKS if key in names and label not in stack]
    except (OSError, ValueError, AttributeError):
        pass
    if list(path.glob("*.csproj")) or list(path.glob("*.sln")):
        stack.append(".NET")
    return {"ok": True, "exists": True, "name": name, "git": git, "repo": repo,
            "stack": ", ".join(stack[:5]),
            "message": ("A git repository" if git else "A folder without git")
                       + (f" on GitHub ({repo})" if repo else "") + (f": {', '.join(stack[:5])}." if stack else ".")}


def finish(name, project=None):
    """Save your name and first project (optional: {"name", "folder", "description"}).
    Returns the project id or None."""
    import swarm
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("Tell Lago your name")
    project_id = None
    if project and (project.get("folder") or "").strip():
        found = detect(project["folder"])
        if not found.get("ok"):
            raise ValueError(found["message"])
        project_id, _ = swarm.create_project(project.get("name") or found["name"], project["folder"],
                                             project.get("description", ""), found.get("stack", ""),
                                             found.get("repo", ""), git_init=not found.get("git"))
    config = config_store.without_swarm(config_store.load())
    config["user_name"] = name
    config["onboarded"] = True
    config["start_card"] = True
    config_store.save(config)
    return project_id


def dismiss_start_card():
    config = config_store.without_swarm(config_store.load())
    if config.pop("start_card", None):
        config_store.save(config)
