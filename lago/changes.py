"""What a work session changed: the files its agents edited, and how.

When a session opens, the commit its folder's git repository is on is kept (`base_commit`).
Its changes are everything between that commit and the folder now, committed or not, plus new
files git doesn't track yet; and, for steps that ran in a worktree of their own
(worktrees.py), what each step's branch changed. Other edits made in the same folder
meanwhile (yours, another session's) show up too: it's the folder's change, not a ledger of
who typed what. A session opened before this existed, or in a folder that isn't a git
repository, shows its uncommitted changes only, or nothing.

Diffs pass through redact.py like everything else shown.
"""

import re
import subprocess
from pathlib import Path

import config_store
import hub
import redact

MAX_FILES = 300
MAX_DIFF = 200_000  # characters of one file's diff


class ChangesError(Exception):
    pass


def _git(cwd, *args, check=True):
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ChangesError(f"git didn't answer: {error}") from error
    if check and proc.returncode:
        raise ChangesError((proc.stderr or proc.stdout).strip()[:300] or "git failed")
    return proc.stdout


def folder(workspace):
    import swarm
    return swarm.project_cwd(swarm.session_project(config_store.load(), workspace))


def repo_root(path):
    try:
        return Path(_git(path, "rev-parse", "--show-toplevel").strip())
    except ChangesError:
        return None


def record_base(workspace_id):
    """Remember the commit the session's folder is on now (nothing if it isn't a git repo)."""
    workspace = hub.get_workspace(workspace_id)
    path = folder(workspace)
    if not repo_root(path):
        return None
    try:
        head = _git(path, "rev-parse", "HEAD").strip()
    except ChangesError:  # a repository with no commits yet
        head = ""
    hub.update_workspace(workspace_id, base_commit=head or "EMPTY")
    return head


def _numstat(root, *args):
    files = {}
    for line in _git(root, "diff", "--numstat", "-M", *args).splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if " => " in path:  # a rename: {old => new}
            path = re.sub(r"\{[^{}]* => ([^{}]*)\}", r"\1", path).replace("//", "/")
            path = path.split(" => ")[-1]
        files[path] = {"path": path, "added": int(added) if added.isdigit() else 0,
                       "removed": int(removed) if removed.isdigit() else 0, "binary": added == "-"}
    return files


def _statuses(root, *args):
    out = {}
    for line in _git(root, "diff", "--name-status", "-M", *args).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out[parts[-1]] = {"A": "added", "D": "deleted", "R": "renamed"}.get(parts[0][0], "modified")
    return out


def _base_args(workspace):
    base = workspace.get("base_commit")
    if base and base != "EMPTY":
        return [base], True
    return (["HEAD"], False)


def _untracked(root):
    return [p for p in _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
            if p and not p.startswith(".worktrees/")]


def summary(workspace_id):
    """{"repo", "since", "full", "files": [{path, status, added, removed, binary}], "branches": [...]}."""
    workspace = hub.get_workspace(int(workspace_id))
    if workspace is None:
        raise ChangesError(f"There's no work session #{workspace_id}")
    path = folder(workspace)
    root = repo_root(path) if path.is_dir() else None
    if not root:
        return {"repo": "", "since": "", "full": False, "files": [], "branches": [],
                "note": "This session's folder isn't a git repository, so its changes can't be shown."}
    args, full = _base_args(workspace)
    try:
        stats, statuses = _numstat(root, *args), _statuses(root, *args)
    except ChangesError:  # e.g. a base commit that no longer exists (history rewritten)
        args, full = ["HEAD"], False
        stats, statuses = _numstat(root, *args), _statuses(root, *args)
    files = [{**f, "status": statuses.get(p, "modified")} for p, f in stats.items()]
    for p in _untracked(root):
        try:
            with open(root / p, "rb") as file:
                lines = sum(1 for _ in file)
        except OSError:
            lines = 0
        files.append({"path": p, "status": "added", "added": lines, "removed": 0, "binary": False, "untracked": True})
    files.sort(key=lambda f: f["path"])
    return {"repo": str(root), "since": args[0][:10] if full else "", "full": full,
            "files": files[:MAX_FILES], "more": max(0, len(files) - MAX_FILES),
            "branches": _branches(root, workspace_id),
            "note": "" if full else "Uncommitted changes only: this session started before changes were tracked."}


def _branches(root, workspace_id):
    """Worktree branches of this session's pipeline steps, with what each changed."""
    pipelines = [p["id"] for p in hub.pipelines("workspace = ?", (workspace_id,), limit=100)]
    out = []
    for line in _git(root, "branch", "--list", "lago/p*", "--format=%(refname:short)", check=False).splitlines():
        m = re.match(r"lago/p(\d+)-", line)
        if not m or int(m.group(1)) not in pipelines:
            continue
        merge_base = _git(root, "merge-base", "HEAD", line, check=False).strip()
        if not merge_base:
            continue
        stats = _numstat(root, f"{merge_base}..{line}")
        merged = not _git(root, "rev-list", f"HEAD..{line}", check=False).strip()
        out.append({"branch": line, "merged": merged, "files": sorted(stats.values(), key=lambda f: f["path"])})
    return out


def diff(workspace_id, path, branch=None):
    """One file's diff, as unified diff text (secrets hidden)."""
    workspace = hub.get_workspace(int(workspace_id))
    if workspace is None:
        raise ChangesError(f"There's no work session #{workspace_id}")
    root = repo_root(folder(workspace))
    if not root:
        raise ChangesError("This session's folder isn't a git repository")
    target = (root / path).resolve()
    if root.resolve() not in target.parents:
        raise ChangesError("That file is outside the session's folder")
    if branch:
        if not re.fullmatch(r"lago/p\d+-[a-z0-9-]+", branch):
            raise ChangesError("Not a step's branch")
        merge_base = _git(root, "merge-base", "HEAD", branch).strip()
        text = _git(root, "diff", "-M", f"{merge_base}..{branch}", "--", path)
    else:
        args, _ = _base_args(workspace)
        text = _git(root, "diff", "-M", *args, "--", path, check=False)
        if not text and path in _untracked(root):
            text = _git(root, "diff", "--no-index", "--", "/dev/null", path, check=False)
    if len(text) > MAX_DIFF:
        text = text[:MAX_DIFF] + "\n… (the rest of this diff is too long to show)"
    return redact.text(text)
