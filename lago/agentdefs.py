"""Reading agent definitions: Claude Code's (.claude/agents) and the swarm catalog's.

The swarm catalog (catalog/agents/*.md in your data folder, config_store.DATA_DIR) holds
one file per agent. Its frontmatter is
the agent's manifest: what it does, the inputs it needs and what it produces. See
docs/agent-catalog.md for the format.
"""

import json
import os
import re
from pathlib import Path

import config_store

APP_DIR = config_store.APP_DIR
USER_AGENTS = Path.home() / ".claude" / "agents"
CATALOG_DIR = APP_DIR / "prompts"  # the app's own prompts: Lago's and the Conductor's
USER_CATALOG = config_store.DATA_DIR / "catalog"  # your agents, tests, MCP servers, pipelines
CATALOG_AGENTS = USER_CATALOG / "agents"
CATALOG_PIPELINES = USER_CATALOG / "pipelines"
CONDUCTOR_FILE = CATALOG_DIR / "conductor.md"
ORCHESTRATOR_FILE = CATALOG_DIR / "orchestrator.md"

# Built-in tools that let an agent spawn its own subagents. Leads in Lago delegate
# through Lago instead, so their team members show up as real sessions.
SUBAGENT_TOOLS = {"Task", "Agent"}

_memo = {}


def _stamp(*folders_and_files):
    """Names and modification times of the given files and of the files in the given
    folders (one level down, where manifests and SKILL.md files live)."""
    parts = []
    for path in folders_and_files:
        path = Path(path)
        if path.is_file():
            parts.append((str(path), path.stat().st_mtime_ns))
        elif path.is_dir():
            for child in sorted(path.iterdir()):
                target = child / "SKILL.md" if child.is_dir() else child
                if target.exists():
                    parts.append((str(target), target.stat().st_mtime_ns))
    return tuple(parts)


def _memoized(name, stamp, build):
    """build() again only when the files behind it changed."""
    hit = _memo.get(name)
    if hit and hit[0] == stamp:
        return hit[1]
    value = build()
    _memo[name] = (stamp, value)
    return value


_MAPPING_ITEM = re.compile(r"^[A-Za-z_][\w-]*:(\s|$)")
_TRUE = {"true", "yes", "on", "1"}


def _scalar(value):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(v) for v in value[1:-1].split(",") if v.strip()]
    return value.strip("\"'")


def _pair(text):
    name, value = text.split(":", 1)
    return name.strip(), _scalar(value)


def _parse_frontmatter(lines):
    """A small YAML subset: `key: value`, `[a, b]`, folded/literal blocks, `- item` lists
    and lists of one-level mappings (`- name: x` followed by indented `key: value`)."""
    data, key, block = {}, None, None
    for line in lines:
        if key and (line.startswith((" ", "\t")) or not line.strip()):
            stripped = line.strip()
            if block == "list" and stripped.startswith("- "):
                item = stripped[2:].strip()
                data[key].append(dict([_pair(item)]) if _MAPPING_ITEM.match(item) else _scalar(item))
            elif block == "list" and stripped and data[key] and isinstance(data[key][-1], dict) \
                    and _MAPPING_ITEM.match(stripped):
                name, value = _pair(stripped)
                data[key][-1][name] = value
            elif block == "text":
                data[key] += ("\n" if stripped == "" else " ") + stripped
            continue
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if value in (">", "|", ">-", "|-"):
            data[key], block = "", "text"
        elif value == "":
            data[key], block = [], "list"
        else:
            data[key], block = _scalar(value), None
    for name, value in data.items():
        if isinstance(value, str):
            data[name] = value.strip()
    return data


def _split(path):
    """(frontmatter dict, body text) of a Markdown file."""
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end:
            return _parse_frontmatter(lines[1:end]), "\n".join(lines[end + 1:]).strip()
    return {}, text


def _tool_list(tools):
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    return tools or None


def read(path):
    meta, body = _split(path)
    return {
        "name": meta.get("name") or Path(path).stem,
        "description": meta.get("description", ""),
        "tools": _tool_list(meta.get("tools")),
        "model": meta.get("model", ""),
        "plugins": _tool_list(meta.get("plugins")) or [],
        "skills": _tool_list(meta.get("skills")) or [],
        "body": body,
        "path": str(path),
    }


def discover(project_dirs=()):
    """All definitions, keyed by name. A project's own agents win over user agents."""
    found = {}
    folders = [USER_AGENTS] + [Path(os.path.expanduser(d)) / ".claude" / "agents"
                               for d in project_dirs]
    for folder in folders:
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            try:
                definition = read(path)
            except (OSError, UnicodeDecodeError):
                continue
            definition["scope"] = "user" if folder == USER_AGENTS else "project"
            found[definition["name"]] = definition
    return found


def find(name, project_dir):
    return discover([project_dir]).get(name)


# ---------------------------------------------------------------- swarm catalog

def _text(value):
    if isinstance(value, list):
        value = " ".join(str(v) for v in value)
    return " ".join(str(value or "").split())


def _items(value):
    """Manifest inputs/outputs as [{name, description, required}]; plain strings allowed."""
    items = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            item = {"name": item}
        if not item.get("name"):
            continue
        items.append({"name": str(item["name"]),
                      "description": _text(item.get("description")),
                      "required": str(item.get("required", "false")).lower() in _TRUE})
    return items


def manifest(path):
    meta, body = _split(path)
    path = Path(path)
    tags = meta.get("tags") or []
    return {
        "id": path.stem,
        "name": meta.get("name") or path.stem.replace("-", " ").title(),
        "summary": _text(meta.get("summary")),
        "domain": meta.get("domain") or "general",
        "extends": meta.get("extends", ""),
        "inputs": _items(meta.get("inputs")),
        "outputs": _items(meta.get("outputs")),
        "requires": _text(meta.get("requires")),
        "use_when": _text(meta.get("use_when")),
        "avoid_when": _text(meta.get("avoid_when")),
        "tags": tags if isinstance(tags, list) else [tags],
        "tools": _tool_list(meta.get("tools")),
        "mcp": _tool_list(meta.get("mcp")) or [],
        "skills": _tool_list(meta.get("skills")) or [],  # shown to it in full; the rest by name
        "model": meta.get("model", ""),
        "runtime": meta.get("runtime", ""),  # codex runs it on the Codex CLI (codex_runtime.py)
        "codex_model": meta.get("codex_model", ""),
        "source": meta.get("source", ""),
        "body": body,
        "path": str(path),
    }


def catalog():
    """Every swarm agent's manifest, keyed by id (its file name). Re-read only when a
    manifest changes."""
    return _memoized("catalog", _stamp(CATALOG_AGENTS), _read_catalog)


def _read_catalog():
    entries = {}
    for path in sorted(CATALOG_AGENTS.glob("*.md")) if CATALOG_AGENTS.is_dir() else []:
        try:
            entries[path.stem] = manifest(path)
        except (OSError, UnicodeDecodeError):
            continue
    return entries


AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def write_agent(agent_id, name, summary, prompt="", domain="general", extends="", inputs=None,
                outputs=None, requires="", use_when="", avoid_when="", tags=None, tools="",
                model="", replace=False, folder=None, mcp="", source="", skills="", runtime="", codex_model=""):
    """Add a swarm agent to the catalog (catalog/agents/<id>.md). It belongs to no project:
    any Conductor can wake it. With `folder`, write it there as agent.md instead (a draft
    for you to approve). Returns its path."""
    if not AGENT_ID.match(agent_id or ""):
        raise ValueError("The id can only use lowercase letters, digits and dashes.")
    path = Path(folder) / "agent.md" if folder else CATALOG_AGENTS / f"{agent_id}.md"
    if path.exists() and not replace:
        raise ValueError(f"There's already a swarm agent `{agent_id}`.")
    if not (name or "").strip() or not _text(summary):
        raise ValueError("An agent needs a name and a one-sentence summary.")
    if not extends and not (prompt or "").strip():
        raise ValueError("Give it a prompt (its instructions), or `extends` naming a Claude "
                         "Code agent definition to build on.")
    if extends and not find(extends, Path.home()):
        raise ValueError(f"There's no Claude Code agent definition {extends!r} to extend.")
    one_line = lambda text: " ".join(str(text or "").split())
    lines = ["---", f"name: {one_line(name)}", f"domain: {one_line(domain) or 'general'}",
             "summary: >", f"  {_text(summary)}"]
    if extends:
        lines.append(f"extends: {one_line(extends)}")
    for key, items in (("inputs", inputs), ("outputs", outputs)):
        clean = [i if isinstance(i, dict) else {"name": str(i)} for i in items or []]
        if clean:
            lines.append(f"{key}:")
            for item in clean:
                lines.append(f"  - name: {one_line(item.get('name'))}")
                if item.get("required"):
                    lines.append("    required: true")
                if item.get("description"):
                    lines.append(f"    description: {one_line(item['description'])}")
    for key, value in (("requires", requires), ("use_when", use_when), ("avoid_when", avoid_when)):
        if _text(value):
            lines += [f"{key}: >", f"  {_text(value)}"]
    tags = [one_line(t).replace(",", " ") for t in (tags or []) if one_line(t)]
    if tags:
        lines.append(f"tags: [{', '.join(tags)}]")
    if tools:
        lines.append(f"tools: {one_line(', '.join(tools) if isinstance(tools, list) else tools)}")
    if model:
        lines.append(f"model: {one_line(model)}")
    if mcp:
        lines.append(f"mcp: {one_line(mcp)}")
    if skills:
        lines.append(f"skills: {one_line(', '.join(skills) if isinstance(skills, list) else skills)}")
    if runtime:
        lines.append(f"runtime: {one_line(runtime)}")
    if codex_model:
        lines.append(f"codex_model: {one_line(codex_model)}")
    if source:
        lines.append(f"source: {one_line(source)}")
    lines += ["---", "", (prompt or "").strip(), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def resolve(entry, project_dir=None):
    """The definition a swarm agent runs with: its manifest's body, on top of the Claude
    Code definition it extends (if any). None if that definition is missing."""
    base = None
    if entry["extends"]:
        base = find(entry["extends"], project_dir or Path.home())
        if base is None:
            return None
    body = "\n\n".join(part for part in ((base or {}).get("body", ""), entry["body"]) if part)
    return {
        "name": entry["id"],
        "description": entry["summary"],
        "tools": entry["tools"] or (base or {}).get("tools"),
        "model": entry["model"] or (base or {}).get("model", ""),
        "runtime": entry.get("runtime", ""),
        "codex_model": entry.get("codex_model", ""),
        "mcp": entry["mcp"],
        "skills": entry.get("skills") or (base or {}).get("skills", []),
        "plugins": (base or {}).get("plugins", []),
        "body": body,
        "path": entry["path"],
        "base_path": (base or {}).get("path"),
    }


def pipeline_templates():
    """Reusable pipeline shapes the Conductor can start from (catalog/pipelines/*.md)."""
    return _memoized("templates", _stamp(CATALOG_PIPELINES), _read_templates)


def _read_templates():
    templates = {}
    for path in sorted(CATALOG_PIPELINES.glob("*.md")) if CATALOG_PIPELINES.is_dir() else []:
        try:
            meta, body = _split(path)
        except (OSError, UnicodeDecodeError):
            continue
        agents = meta.get("agents") or []
        templates[path.stem] = {"id": path.stem, "name": meta.get("name") or path.stem,
                                "summary": _text(meta.get("summary")),
                                "domain": meta.get("domain") or "general",
                                "agents": agents if isinstance(agents, list) else [agents],
                                "body": body, "path": str(path)}
    return templates


def conductor():
    return read(CONDUCTOR_FILE) if CONDUCTOR_FILE.exists() else None


def orchestrator():
    return read(ORCHESTRATOR_FILE) if ORCHESTRATOR_FILE.exists() else None


# ---------------------------------------------------------------- skills and plugins

USER_SKILLS = Path.home() / ".claude" / "skills"
INSTALLED_PLUGINS = Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def _skill_dirs(project_dir=None):
    """(folder of skill folders, name prefix, source label) to scan."""
    dirs = [(USER_SKILLS, "", "user")]
    if project_dir:
        dirs.append((Path(os.path.expanduser(str(project_dir))) / ".claude" / "skills", "", "project"))
    try:
        plugins = json.loads(INSTALLED_PLUGINS.read_text()).get("plugins", {})
    except (OSError, ValueError):
        plugins = {}
    for key, installs in plugins.items():
        plugin = key.split("@")[0]
        for install in installs[-1:]:
            dirs.append((Path(install.get("installPath", "")) / "skills", f"{plugin}:",
                         f"plugin {plugin}"))
    return dirs


def skills(project_dir=None):
    """Claude Code skills an agent can load, keyed by the name it loads them with.
    Re-read only when a skill or the installed plugins change."""
    dirs = _skill_dirs(project_dir)
    stamp = _stamp(INSTALLED_PLUGINS, *(folder for folder, _, _ in dirs))
    return _memoized(f"skills:{project_dir}", stamp, lambda: _read_skills(dirs))


def _read_skills(dirs):
    found = {}
    for folder, prefix, source in dirs:
        for skill_file in sorted(folder.glob("*/SKILL.md")) if folder.is_dir() else []:
            try:
                meta, _ = _split(skill_file)
            except (OSError, UnicodeDecodeError):
                continue
            name = prefix + (meta.get("name") or skill_file.parent.name)
            found[name] = {"name": name, "description": _text(meta.get("description")),
                           "source": source, "path": str(skill_file)}
    return found


def plugins():
    """Installed Claude Code plugins: they are on in every session, agents included."""
    return _memoized("plugins", _stamp(INSTALLED_PLUGINS), _read_plugins)


def _read_plugins():
    try:
        data = json.loads(INSTALLED_PLUGINS.read_text()).get("plugins", {})
    except (OSError, ValueError):
        return []
    found = []
    for key, installs in data.items():
        install = installs[-1] if installs else {}
        path = Path(install.get("installPath", ""))
        description = ""
        manifest_file = path / ".claude-plugin" / "plugin.json"
        try:
            description = json.loads(manifest_file.read_text()).get("description", "")
        except (OSError, ValueError):
            pass
        found.append({"name": key.split("@")[0], "id": key, "version": install.get("version", ""),
                      "description": description,
                      "skills": sorted(p.parent.name for p in (path / "skills").glob("*/SKILL.md")),
                      "commands": sorted(p.stem for p in (path / "commands").glob("*.md"))})
    return found
