"""Publishing an agent to GitHub so other people can find it, after a scrub you check.

prepare() copies an agent into its own folder, ready to become a public repository: its
manifest and prompt (and the Claude Code definition it extends, if any), its tests, a README
written from its manifest, an MIT LICENSE and lago.json, which marks it as a Lago agent. On the
way every file is scrubbed:

- **removed**: secrets (redact.py's shapes and known values: the vault, projects' .env files),
  email addresses, IP addresses, your home folder (paths become ~/...), and your name;
- **to check**: your projects' names and ids, addresses on your tailnet, and paths to folders
  on your computer (they won't exist on anyone else's). They may be fine in context, or may
  say what you work on: you read them before publishing.

Nothing leaves the computer until publish(), which only the app calls, from your click after
you've seen the exact files: Lago and the agents can prepare, never publish. The repository is
created under your GitHub account (`gh`) with the topic `lago-agent`, so every Lago's GitHub
list finds it. Publishing again pushes the changes to the same repository.
"""

import json
import re
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path

import activity
import agentdefs
import config_store
import evals
import github
import redact
from team import TeamError

PUBLISH_DIR = config_store.STATE_DIR / "publish"
TOPICS = ("lago-agent", "claude-code-agents", "ai-agents")
REMOVED = "[removed]"

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
LOCAL_PATH = re.compile(r"~/[\w./-]*[\w/]")  # not the full stop after it
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOME_PATH = re.compile(r"/home/[\w.-]+|/Users/[\w.-]+")
TAILNET = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.ts\.net\b")

MIT = """MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


# ---------------------------------------------------------------- scrubbing

def _names():
    """Words that identify you: your name, as Lago knows it and as git does."""
    names = {config_store.load().get("user_name") or ""}
    try:
        names.add(subprocess.run(["git", "config", "--global", "user.name"], capture_output=True,
                                 text=True, timeout=5).stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return sorted((n for n in names if len(n) >= 3), key=len, reverse=True)


def _projects():
    """Your projects' ids and names, the words that say what you work on."""
    words = {}  # one per spelling, whatever its case: "webstore" and "Webstore" are one word
    for project_id, project in config_store.load().get("projects", {}).items():
        for word in (project_id, project.get("name") or ""):
            words.setdefault(word.lower(), word)
    return sorted((w for w in words.values() if len(w) >= 4), key=len, reverse=True)


def scrub(name, text):
    """(clean text, findings): what was removed, and what you should check."""
    findings = []

    def note(kind, found, action):
        findings.append({"file": name, "kind": kind, "text": found[:80], "action": action})

    cleaned = redact.text(text, on=True)
    if cleaned != text:
        note("secret", "a key, token or password", "removed")
    text = cleaned.replace(redact.HIDDEN, REMOVED)
    home = str(Path.home())
    if home in text:
        note("path", home, "removed")
        text = text.replace(home, "~")
    for pattern, kind in ((HOME_PATH, "path"), (EMAIL, "email"), (IPV4, "address")):
        for found in sorted(set(pattern.findall(text))):
            note(kind, found, "removed")
        text = pattern.sub("~" if kind == "path" else REMOVED, text)
    for person in _names():
        if re.search(rf"\b{re.escape(person)}\b", text):
            note("name", person, "removed")
            text = re.sub(rf"\b{re.escape(person)}\b", "the user", text)
    for found in sorted(set(TAILNET.findall(text))):
        note("address", found, "check")
    for found in sorted(set(LOCAL_PATH.findall(text))):
        note("path", found, "check")  # a folder on your computer: it won't exist elsewhere
    for word in _projects():
        if re.search(rf"(?i)\b{re.escape(word)}\b", text):
            note("project", word, "check")
    return text, findings


# ---------------------------------------------------------------- preparing

def _folder(agent_id):
    return PUBLISH_DIR / agent_id


def _meta(agent_id):
    path = _folder(agent_id) / "publish.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_meta(agent_id, meta):
    (_folder(agent_id) / "publish.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def owner():
    """Your GitHub login, from `gh`."""
    try:
        return github._gh("api", "user", "--jq", "{login: .login}")["login"]
    except github.GitHubError as error:
        raise TeamError(f"Sign in to GitHub first (gh auth login): {error}") from error


def _readme(entry):
    rows = lambda items: "\n".join(  # noqa: E731 - one table row per input or output
        f"| `{i['name']}` | {'yes' if i.get('required') else 'no'} | {i.get('description', '')} |"
        for i in items) or "| (none) | | |"
    return f"""# {entry['name']}

{entry['summary']}

A [Lago Agents](https://github.com/topics/lago-agent) swarm agent: one job, its prompt and its
tests. Lago finds it under **Agents → Find on GitHub**, checks it and asks before installing it.

## When to use it
{entry.get('use_when') or 'See the summary.'}

## When not to
{entry.get('avoid_when') or 'Outside the job above.'}

## Inputs
| Name | Required | What it is |
|---|---|---|
{rows(entry.get('inputs') or [])}

## Outputs
| Name | Required | What it is |
|---|---|---|
{rows(entry.get('outputs') or [])}

## Files
- `agent.md`: the manifest (frontmatter) and the prompt.
- `tests.json`: the cases it is graded on (task and criteria).
- `lago.json`: marks this repository as a Lago agent.
"""


def prepare(agent_id):
    """Write the scrubbed files for an agent's repository. Returns what the app shows: the
    files, the findings, the repository name, and whether it was published before."""
    entry = agentdefs.catalog().get(agent_id)
    if entry is None:
        raise TeamError(f"There's no swarm agent `{agent_id}`.")
    meta = _meta(agent_id)
    files = {"agent.md": (agentdefs.CATALOG_AGENTS / f"{agent_id}.md").read_text(encoding="utf-8")}
    if entry.get("extends"):
        base = config_store.DATA_DIR / "claude-agents" / f"{entry['extends']}.md"
        if base.exists():
            files[f"extends/{entry['extends']}.md"] = base.read_text(encoding="utf-8")
    files["tests.json"] = json.dumps({"tests": [{"id": t.get("id"), "task": t["task"],
                                                 "inputs": t.get("inputs", {}),
                                                 "criteria": t.get("criteria", [])}
                                                for t in evals.tests(agent_id)]}, indent=2)
    files["README.md"] = _readme(entry)
    files["lago.json"] = json.dumps({"lago_agent": 1, "id": agent_id, "name": entry["name"],
                                     "version": evals.version_of(entry["body"]),
                                     "domain": entry.get("domain", ""),
                                     # the repo it runs on: the next build of it reuses this one
                                     "source": entry.get("source", "")}, indent=2)
    cleaned, findings = {}, []
    for name, text in files.items():
        cleaned[name], found = scrub(name, text)
        findings += found
    holder = owner()
    cleaned["LICENSE"] = MIT.format(year=date.today().year, holder=holder)
    folder = _folder(agent_id)
    for old in folder.glob("*") if folder.exists() else []:  # keep .git and publish.json
        if old.name not in (".git", "publish.json"):
            shutil.rmtree(old) if old.is_dir() else old.unlink()
    for name, text in cleaned.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    repo = meta.get("repo") or f"{holder}/{agent_id}"
    meta.update({"repo": repo, "version": evals.version_of(entry["body"]),
                 "prepared_at": time.time(), "findings": findings})
    _save_meta(agent_id, meta)
    return {"repo": repo, "files": cleaned, "findings": findings,
            "published_at": meta.get("published_at"), "summary": entry["summary"]}


# ---------------------------------------------------------------- publishing

def _git(folder, *args):
    proc = subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise TeamError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout


def _gh(*args):
    """`gh` for commands that answer in plain text (github._gh expects JSON)."""
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TeamError(f"gh {args[0]} {args[1]} didn't run: {error}") from error
    if proc.returncode != 0:
        raise TeamError(f"GitHub said: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout


def publish(agent_id, checked=False):
    """Push the prepared files to GitHub: a new public repository the first time, a new
    commit after. Only from the app, after you've read the files."""
    meta = _meta(agent_id)
    entry = agentdefs.catalog().get(agent_id)
    if not meta.get("prepared_at") or entry is None:
        raise TeamError("Prepare it first: the files you check are the files that go out.")
    if meta["version"] != evals.version_of(entry["body"]):
        raise TeamError("The agent changed since you checked its files. Prepare it again.")
    to_check = [f for f in meta["findings"] if f["action"] == "check"]
    if to_check and not checked:
        raise TeamError("Some words need your check first: " + ", ".join(
            sorted({f["text"] for f in to_check})) + ".")
    folder = _folder(agent_id)
    if not (folder / ".git").exists():
        _git(folder, "init", "-q", "-b", "main")
    (folder / ".gitignore").write_text("publish.json\n", encoding="utf-8")
    _git(folder, "add", "-A")
    if _git(folder, "status", "--porcelain").strip():
        _git(folder, "commit", "-q", "-m", f"{entry['name']}: version {meta['version']}")
    repo = meta["repo"]
    if not meta.get("published_at"):
        _gh("repo", "create", repo, "--public", "--source", str(folder), "--push",
            "--description", entry["summary"][:300])
        try:
            _gh("repo", "edit", repo, *[a for t in TOPICS for a in ("--add-topic", t)])
        except TeamError:
            pass  # topics help others find it; the repository is there either way
    else:
        _git(folder, "push", "-q", "origin", "main")
    meta["published_at"] = time.time()
    _save_meta(agent_id, meta)
    activity.log(activity.actor(), "agent.published", f"{agent_id} → {repo}", target=repo)
    return f"Published {entry['name']} at https://github.com/{repo}."


def status(agent_id):
    meta = _meta(agent_id)
    return {"repo": meta.get("repo"), "published_at": meta.get("published_at")}
