"""Export teams and swarm agents as one archive, and import one with a preview first.

An export (.tar.gz) holds manifest.json plus the files it names:
- teams from config.json with their agents (paths and project folders are left out),
- catalog agents (catalog/agents/<id>.md) with their tests (catalog/tests/<id>.json),
- the Claude Code definitions they build on (claude-agents/<name>.md, for `extends` and team
  agents' `definition`), skills (claude-skills/<name>/) and pipeline templates.

preview() reads an archive without changing anything: what's new, what you already have
(kept as is), and what differs from yours (kept as yours; never overwritten). apply() adds
only the new things. Archives are untrusted: members must be regular files under those
folders, with no links and no `..`, within MAX_BYTES.
"""

import io
import json
import os
import re
import tarfile
import time
from pathlib import Path

import activity
import agentdefs
import config_store

FORMAT = "lago-team/1"
APP_DIR = config_store.DATA_DIR  # paths in a bundle are relative to your data folder
MAX_BYTES = 20 * 1024 * 1024
ALLOWED = re.compile(r"^(catalog/agents/[a-z0-9-]+\.md|catalog/tests/[a-z0-9-]+\.json|"
                     r"catalog/pipelines/[a-z0-9-]+\.md|claude-agents/[A-Za-z0-9_-]+\.md|"
                     r"claude-skills/[a-z0-9-]+/[A-Za-z0-9._/-]+)$")
SKILLS_LINK_DIR = Path.home() / ".claude" / "skills"
AGENTS_LINK_DIR = Path.home() / ".claude" / "agents"
TEAM_KEYS = ("name", "icon", "description")
AGENT_KEYS = ("name", "team", "definition", "reports_to", "model", "permission_mode")


class PortabilityError(Exception):
    pass


def _files_for(teams, catalog, skills, templates, config):
    files = {}
    definitions = set()
    for tid in teams:
        for agent in config["agents"].values():
            if agent.get("team") == tid and agent.get("definition"):
                definitions.add(agent["definition"])
    entries = agentdefs.catalog()
    for cid in catalog:
        entry = entries.get(cid)
        if entry is None:
            raise PortabilityError(f"There's no swarm agent {cid!r}")
        files[f"catalog/agents/{cid}.md"] = APP_DIR / "catalog/agents" / f"{cid}.md"
        tests = APP_DIR / "catalog/tests" / f"{cid}.json"
        if tests.exists():
            files[f"catalog/tests/{cid}.json"] = tests
        if entry.get("extends"):
            definitions.add(entry["extends"])
    for name in sorted(definitions):
        path = APP_DIR / "claude-agents" / f"{name}.md"
        if path.exists():  # definitions from elsewhere (plugins, ~/.claude) aren't ours to ship
            files[f"claude-agents/{name}.md"] = path
    for name in skills:
        folder = APP_DIR / "claude-skills" / name
        if not (folder / "SKILL.md").exists():
            raise PortabilityError(f"There's no skill {name!r} in claude-skills/")
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files[f"claude-skills/{name}/{path.relative_to(folder).as_posix()}"] = path
    for name in templates:
        path = APP_DIR / "catalog/pipelines" / f"{name}.md"
        if not path.exists():
            raise PortabilityError(f"There's no pipeline template {name!r}")
        files[f"catalog/pipelines/{name}.md"] = path
    return files


def export(path, teams=(), catalog=(), skills=(), templates=()):
    """Write an archive. Returns its manifest."""
    config = config_store.load()
    for tid in teams:
        if tid not in config["teams"] or config["teams"][tid].get("swarm"):
            raise PortabilityError(f"There's no team {tid!r}")
    files = _files_for(teams, catalog, skills, templates, config)
    manifest = {
        "format": FORMAT, "exported_at": time.time(),
        "teams": {tid: {k: config["teams"][tid][k] for k in TEAM_KEYS if config["teams"][tid].get(k)}
                  for tid in teams},
        "agents": {aid: {k: a[k] for k in AGENT_KEYS if a.get(k) is not None}
                   for aid, a in config["agents"].items() if a.get("team") in teams and not a.get("swarm")},
        "files": sorted(files),
    }
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        data = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime = len(data), int(time.time())
        tar.addfile(info, io.BytesIO(data))
        for name, source in files.items():
            tar.add(source, arcname=name, recursive=False)
    activity.log(activity.actor(), "export.made", f"Exported {len(teams)} teams, {len(catalog)} agents, "
                 f"{len(skills)} skills, {len(templates)} templates to {path.name}")
    return manifest


def _read(path):
    """(manifest, {name: bytes}), refusing anything unsafe."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise PortabilityError(f"There's no file {path}")
    if path.stat().st_size > MAX_BYTES:
        raise PortabilityError("The archive is too large")
    contents, total, manifest = {}, 0, None
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise PortabilityError(f"{member.name} isn't a regular file")
                name = member.name
                if name != "manifest.json" and (".." in name.split("/") or not ALLOWED.match(name)):
                    raise PortabilityError(f"{name} isn't something a team archive may contain")
                total += member.size
                if total > MAX_BYTES:
                    raise PortabilityError("The archive unpacks too large")
                data = tar.extractfile(member).read()
                if name == "manifest.json":
                    manifest = json.loads(data)
                else:
                    contents[name] = data
    except (tarfile.TarError, OSError, ValueError) as error:
        raise PortabilityError(f"Couldn't read the archive: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise PortabilityError("This isn't a Lago Agents team archive")
    return manifest, contents


def preview(path):
    """What importing would do: {"new", "same", "different"} file names, and teams/agents."""
    manifest, contents = _read(path)
    config = config_store.load()
    new, same, different = [], [], []
    for name, data in sorted(contents.items()):
        target = APP_DIR / name
        if not target.exists():
            new.append(name)
        elif target.read_bytes() == data:
            same.append(name)
        else:
            different.append(name)
    teams_new = [t for t in manifest.get("teams", {}) if t not in config["teams"]]
    agents_new = [a for a in manifest.get("agents", {}) if a not in config["agents"]]
    return {"new": new, "same": same, "different": different,
            "teams": list(manifest.get("teams", {})), "teams_new": teams_new,
            "agents": list(manifest.get("agents", {})), "agents_new": agents_new,
            "exported_at": manifest.get("exported_at")}


def apply(path):
    """Add what's new. Returns the preview of what was done."""
    manifest, contents = _read(path)
    plan = preview(path)
    for name in plan["new"]:
        target = APP_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[name])
    _link_new(plan["new"])
    config = config_store.without_swarm(config_store.load())
    for tid in plan["teams_new"]:
        config["teams"][tid] = {**manifest["teams"][tid], "cwd": "~"}
    for aid in plan["agents_new"]:
        agent = dict(manifest["agents"][aid])
        if agent.get("team") in config["teams"]:
            config["agents"][aid] = agent
    if plan["teams_new"] or plan["agents_new"]:
        config_store.save(config, source="import")
    activity.log(activity.actor(), "import.applied", f"Imported {Path(path).name}: {len(plan['new'])} new files, "
                 f"{len(plan['teams_new'])} teams, {len(plan['agents_new'])} agents; kept "
                 f"{len(plan['different'])} of yours that differ")
    return plan


def _link_new(names):
    """New definitions and skills are linked into ~/.claude the way install.sh links them."""
    for name in names:
        parts = name.split("/")
        if parts[0] == "claude-skills" and len(parts) >= 3:
            link, target = SKILLS_LINK_DIR / parts[1], APP_DIR / "claude-skills" / parts[1]
        elif parts[0] == "claude-agents":
            link, target = AGENTS_LINK_DIR / parts[1], APP_DIR / name
        else:
            continue
        if not link.exists() and not link.is_symlink():
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, link)
