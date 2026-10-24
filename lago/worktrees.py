"""A git worktree per pipeline step, so steps that edit code side by side don't collide.

A step with `worktree: true` runs in `<repo>/.worktrees/p<pipeline>-<step>` on its own branch
`lago/p<pipeline>-<step>`, made from the repository's current HEAD the first time the step
runs (a re-run reuses it, keeping its work). Nothing is merged for you: the step's result
names its branch, and the Conductor merges it once the work is checked (or asks you).
remove() deletes a worktree and, if it was merged, its branch.
"""

import re
import subprocess
from pathlib import Path

DIR = ".worktrees"


class WorktreeError(Exception):
    pass


def _git(cwd, *args, check=True):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)
    if check and proc.returncode:
        raise WorktreeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout.strip()


def _slug(text):
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:40] or "step"


def names(pipeline_id, step_id):
    slug = f"p{pipeline_id}-{_slug(step_id)}"
    return slug, f"lago/{slug}"


def repo_root(folder):
    try:
        return Path(_git(folder, "rev-parse", "--show-toplevel"))
    except (WorktreeError, OSError):
        return None


def ensure(folder, pipeline_id, step_id):
    """The step's worktree (made if needed): {"path", "branch", "base", "new"}."""
    root = repo_root(folder)
    if root is None:
        raise WorktreeError(f"{folder} isn't a git repository, so the step can't get a worktree")
    slug, branch = names(pipeline_id, step_id)
    path = root / DIR / slug
    if path.exists():
        return {"path": str(path), "branch": branch, "base": "", "new": False}
    base = _git(root, "rev-parse", "--short", "HEAD")
    exists = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if exists:
        _git(root, "worktree", "add", str(path), branch)
    else:
        _git(root, "worktree", "add", "-b", branch, str(path), "HEAD")
    _ignore(root)
    return {"path": str(path), "branch": branch, "base": base, "new": True}


def _ignore(root):
    """Keep .worktrees/ out of `git status` without touching the tracked .gitignore."""
    exclude = Path(_git(root, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else root / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if f"/{DIR}/" not in text.split():
        exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + f"/{DIR}/\n")


def task_lines(info):
    return ["", "## Where to work",
            f"Work in the git worktree `{info['path']}` (branch `{info['branch']}`), not in the main "
            "checkout: other steps may be editing that one. Commit your work on this branch "
            "before you finish, and name the branch in your result. Don't merge it."]


def remove(folder, pipeline_id, step_id, force=False):
    """Delete the step's worktree; delete its branch too when it's merged into HEAD."""
    root = repo_root(folder)
    if root is None:
        raise WorktreeError(f"{folder} isn't a git repository")
    slug, branch = names(pipeline_id, step_id)
    path = root / DIR / slug
    if path.exists():
        _git(root, "worktree", "remove", *(["--force"] if force else []), str(path))
    merged = branch in _git(root, "branch", "--merged", "HEAD", "--format=%(refname:short)").split()
    if merged:
        _git(root, "branch", "-d", branch)
    return {"removed": str(path), "branch_deleted": merged}
