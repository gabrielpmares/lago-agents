"""Bringing a Paperclip company over: its agents, skills, routines, projects and open tasks.

Paperclip exports a company as a package of Markdown files (the Agent Companies format:
COMPANY.md, agents/<slug>/AGENTS.md, skills/<slug>/SKILL.md, projects/<slug>/PROJECT.md,
tasks/<slug>/TASK.md) with Paperclip's own details in `.paperclip.yaml` (models, schedules,
task status). It comes as a zip from Paperclip's Export page, a folder from
`paperclipai company export`, or a GitHub repository.

stage() unpacks it into the state folder and returns a token; preview() says what's in it and
what Lago can't carry over; apply() imports what you ticked:

- **agents** join the catalog with their instructions, model and runtime (Claude or Codex);
- **skills** install the way GitHub skills do (checked against the Agent Skills spec, scripts
  not executable). Paperclip's own skills (its API) aren't needed here and are left out;
- **projects** get a folder on this computer, cloned from the project's repository when the
  folder doesn't exist yet;
- **routines** (recurring tasks) become Lago routines, switched off until you turn them on;
- **open tasks** wait on the Tasks page until you start one (imported_tasks()).

Everything in a package is someone else's text: instructions that try to steer an agent are
refused item by item (ghimport.check_text), and the zip is unpacked with no links, no paths
outside its folder and size limits.
"""

import io
import json
import re
import secrets
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import activity
import agentdefs
import config_store
import ghimport
import hub
import skill_sources
from team import TeamError

STAGING = config_store.STATE_DIR / "imports"
KEEP_HOURS = 24
MAX_ZIP_FILES = 5000
MAX_ZIP_TOTAL = 200 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
OPEN_STATUSES = ("backlog", "todo", "in_progress", "in_review", "blocked")
OWN_SKILLS = re.compile(r"^paperclip(-|$)")  # Paperclip's own API skills: no use outside it
ADAPTERS = {"claude_local": "", "codex_local": "codex"}  # Paperclip adapter -> Lago runtime
WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
MAX_COMMENTS_TEXT = 3000
SCHEMA = """
CREATE TABLE IF NOT EXISTS imported_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,              -- "paperclip"
    ref         TEXT NOT NULL DEFAULT '',   -- its id there, e.g. ACME-12
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    project     TEXT NOT NULL DEFAULT '',
    agent       TEXT NOT NULL DEFAULT '',
    priority    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'waiting',  -- waiting, started, dismissed
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class PaperclipError(TeamError):
    pass


# ---------------------------------------------------------------- reading the files

def _scalar(text):
    text = text.strip()
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text == "[]":
        return []
    if text == "{}":
        return {}
    if text.startswith('"'):
        try:
            return json.loads(text)
        except ValueError:
            return text.strip('"')
    if text.startswith("'") and text.endswith("'") and len(text) > 1:
        return text[1:-1].replace("''", "'")
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(part) for part in _split_flow(text[1:-1]) if part.strip()]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return re.sub(r"\s+#.*$", "", text)


def _split_flow(text):
    parts, depth, quote, current = [], 0, None, ""
    for char in text:
        if quote:
            quote = None if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    return parts + [current]


def yaml(text):
    """The YAML that packages use: nested maps and lists by indentation, quoted or plain
    scalars, `[a, b]` lists and `|`/`>` blocks. Enough for Paperclip's exports and for
    hand-written packages; anything stranger reads as plain text."""
    lines = [(len(raw) - len(raw.lstrip(" ")), raw.strip()) for raw in str(text).replace("\t", "  ").splitlines()]
    lines = [(indent, line) for indent, line in lines if line and not line.startswith("#") and line != "---"]
    value, _ = _block(lines, 0, lines[0][0] if lines else 0)
    return value


def _block(lines, i, indent):
    if i < len(lines) and lines[i][1].startswith("- ") or (i < len(lines) and lines[i][1] == "-"):
        out = []
        while i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
            rest = lines[i][1][1:].strip()
            if not rest:
                if i + 1 < len(lines) and lines[i + 1][0] > indent:
                    item, i = _block(lines, i + 1, lines[i + 1][0])
                else:
                    item, i = None, i + 1
            elif re.match(r"^(\"[^\"]*\"|[\w.-]+)\s*:(\s|$)", rest):
                # "- key: value" starts a map whose other keys sit under it
                inner = indent + 2
                sub = [(inner, rest)]
                j = i + 1
                while j < len(lines) and lines[j][0] > indent:
                    sub.append(lines[j])
                    j += 1
                item, _ = _block(sub, 0, inner)
                i = j
            else:
                item, i = _scalar(rest), i + 1
            out.append(item)
        return out, i
    out = {}
    while i < len(lines) and lines[i][0] == indent:
        match = re.match(r"^(\"[^\"]*\"|'[^']*'|[^:]+?)\s*:(?:\s+(.*))?$", lines[i][1])
        if not match:
            i += 1
            continue
        key = match.group(1).strip("\"'")
        rest = (match.group(2) or "").strip()
        if rest in ("|", "|-", ">", ">-", "|+", ">+"):
            j, body = i + 1, []
            while j < len(lines) and lines[j][0] > indent:
                body.append(lines[j][1])
                j += 1
            out[key] = ("\n" if rest.startswith("|") else " ").join(body)
            i = j
        elif rest:
            out[key] = _scalar(rest)
            i += 1
        elif i + 1 < len(lines) and (lines[i + 1][0] > indent or (lines[i + 1][0] == indent and lines[i + 1][1].startswith("-"))):
            out[key], i = _block(lines, i + 1, lines[i + 1][0])
        else:
            out[key] = None
            i += 1
    return out, i


def markdown(text):
    """(frontmatter, body) of a Markdown file with a YAML header."""
    text = str(text).replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            meta = yaml(text[4:end])
            return (meta if isinstance(meta, dict) else {}), text[end + 4:].lstrip("-").strip()
    return {}, text.strip()


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- unpacking

def _cleanup():
    if not STAGING.is_dir():
        return
    for folder in STAGING.iterdir():
        if folder.is_dir() and time.time() - folder.stat().st_mtime > KEEP_HOURS * 3600:
            shutil.rmtree(folder, ignore_errors=True)


def _new_stage():
    _cleanup()
    token = secrets.token_hex(8)
    folder = STAGING / f"paperclip-{token}"
    folder.mkdir(parents=True, mode=0o700)
    return token, folder / "src"


def _stage_dir(token):
    if not re.fullmatch(r"[0-9a-f]{16}", str(token or "")):
        raise PaperclipError("That import has expired. Choose the package again.")
    folder = STAGING / f"paperclip-{token}" / "src"
    if not folder.is_dir():
        raise PaperclipError("That import has expired. Choose the package again.")
    return folder


def _unzip(data, target):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise PaperclipError("That isn't a zip file. Export the company from Paperclip and choose the .zip.") from error
    entries = archive.infolist()
    if len(entries) > MAX_ZIP_FILES:
        raise PaperclipError(f"The package has more than {MAX_ZIP_FILES} files.")
    if sum(e.file_size for e in entries) > MAX_ZIP_TOTAL:
        raise PaperclipError(f"The package unpacks to more than {MAX_ZIP_TOTAL // 1024 // 1024} MB.")
    target.mkdir(parents=True)
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/") or re.match(r"^[A-Za-z]:", name):
            raise PaperclipError(f"The package has a file outside its folder: {name}")
        if (entry.external_attr >> 16) & 0o170000 == 0o120000:
            raise PaperclipError(f"The package has a link, which isn't allowed: {name}")
        if entry.is_dir():
            continue
        if entry.file_size > MAX_FILE:
            raise PaperclipError(f"{name} is larger than {MAX_FILE // 1024 // 1024} MB.")
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(entry) as source, open(path, "wb") as out:
            shutil.copyfileobj(source, out)


def _github(url, target):
    match = re.match(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/tree/([^/]+)(/.*)?)?/?$", url.strip())
    if not match:
        raise PaperclipError("Give the GitHub repository as owner/name or its github.com link.")
    owner, name, ref, sub = match.groups()
    args = ["git", "clone", "--depth", "1", "--quiet"] + (["--branch", ref] if ref else []) + \
        [f"https://github.com/{owner}/{name}.git", str(target)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=ghimport.CLONE_TIMEOUT)
    if proc.returncode != 0:
        raise PaperclipError(f"Couldn't fetch {owner}/{name}: {(proc.stderr or proc.stdout).strip()[-200:]}")
    shutil.rmtree(target / ".git", ignore_errors=True)
    return (sub or "").strip("/")


def stage(data_url=None, folder=None, github=None):
    """Unpack a package (a zip as a data URL, a folder on this computer, or a GitHub link) and
    return its token for preview() and apply()."""
    token, target = _new_stage()
    sub = ""
    try:
        if data_url:
            import base64
            header, _, encoded = str(data_url).partition(",")
            if not header.endswith(";base64"):
                raise PaperclipError("That file couldn't be read.")
            _unzip(base64.b64decode(encoded), target)
        elif folder:
            source = Path(folder).expanduser()
            if not source.is_dir():
                raise PaperclipError(f"There's no folder {folder}.")
            shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".git", "node_modules"))
            if any(p.is_symlink() for p in target.rglob("*")):
                raise PaperclipError("The folder has links in it; export the company again, or choose the zip.")
        elif github:
            sub = _github(github, target)
        else:
            raise PaperclipError("Choose the exported zip, a folder, or a GitHub repository.")
        root = _root(target / sub if sub else target)
    except Exception:
        shutil.rmtree(target.parent, ignore_errors=True)
        raise
    (target.parent / "root.txt").write_text(str(root.relative_to(target)))
    return token


def _root(folder):
    """The package's top: where COMPANY.md (or a team's TEAM.md) is (a zip may wrap it in a
    folder), else the folder that has agents/."""
    found = sorted([*folder.rglob("COMPANY.md"), *folder.rglob("TEAM.md")], key=lambda p: len(p.parts))
    if found:
        return found[0].parent
    for candidate in [folder, *sorted((p for p in folder.iterdir() if p.is_dir()), key=str)]:
        if (candidate / "agents").is_dir():
            return candidate
    raise PaperclipError("That isn't a Paperclip company package: there's no COMPANY.md or agents folder in it.")


# ---------------------------------------------------------------- what's in a package

def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:48].rstrip("-")


def _first_sentence(text, limit=200):
    plain = " ".join(re.sub(r"[#*_`>]+", " ", str(text or "")).split())
    sentence = re.split(r"(?<=[.!?])\s", plain, maxsplit=1)[0]
    return sentence[:limit].rstrip()


def _extension(root):
    for name in (".paperclip.yaml", ".paperclip.yml"):
        if (root / name).is_file():
            data = yaml(_read(root / name))
            return data if isinstance(data, dict) else {}
    return {}


def read_package(root):
    """Everything a package holds, as Lago will see it."""
    ext = _extension(root)
    top = next((root / n for n in ("COMPANY.md", "TEAM.md") if (root / n).is_file()), None)  # a team package has no company
    company_meta, company_body = markdown(_read(top)) if top else ({}, "")
    agents = {}
    for folder in sorted(p for p in (root / "agents").glob("*") if p.is_dir()) if (root / "agents").is_dir() else []:
        entry = next((folder / n for n in ("AGENTS.md", "AGENT.md") if (folder / n).is_file()), None)
        if not entry:
            continue
        meta, body = markdown(_read(entry))
        extras = [p for p in sorted(folder.glob("*.md")) if p != entry]
        agents[folder.name] = {"slug": folder.name, "meta": meta, "body": body, "extras": extras,
                               "ext": (ext.get("agents") or {}).get(folder.name) or {}}
    skills = {}
    for path in sorted((root / "skills").rglob("SKILL.md")) if (root / "skills").is_dir() else []:
        meta, body = markdown(_read(path))
        slug = path.parent.relative_to(root / "skills").as_posix().split("/")[-1]
        skills[slug] = {"slug": slug, "meta": meta, "body": body, "folder": path.parent}
    projects = {}
    for path in sorted((root / "projects").glob("*/PROJECT.md")) if (root / "projects").is_dir() else []:
        meta, body = markdown(_read(path))
        projects[path.parent.name] = {"slug": path.parent.name, "meta": meta, "body": body,
                                      "ext": (ext.get("projects") or {}).get(path.parent.name) or {}}
    tasks = {}
    for path in sorted(root.rglob("TASK.md")):
        meta, body = markdown(_read(path))
        slug = path.parent.name
        project = meta.get("project") or (path.parts[-4] if len(path.parts) >= 4 and path.parts[-3] == "tasks"
                                          and path.parts[-5:-4] == ("projects",) else None)
        recurring = bool(meta.get("recurring")) or slug in (ext.get("routines") or {}) or bool(meta.get("schedule"))
        tasks[slug] = {"slug": slug, "meta": meta, "body": body, "project": project, "recurring": recurring,
                       "ext": ((ext.get("routines") if recurring else ext.get("tasks")) or {}).get(slug) or {}}
    return {"company": {"name": company_meta.get("name") or root.name, "description": company_meta.get("description") or _first_sentence(company_body)},
            "agents": agents, "skills": skills, "projects": projects, "tasks": tasks, "ext": ext}


def _model(agent):
    adapter = agent["ext"].get("adapter") or {}
    config = adapter.get("config") or {}
    return str(config.get("model") or agent["meta"].get("model") or ""), str(adapter.get("type") or "")


def _prompt(agent):
    """The agent's instructions: its AGENTS.md, then its other notes (SOUL.md, TOOLS.md). The
    heartbeat checklist is about Paperclip's own loop, so it stays behind."""
    parts = [agent["body"].strip()]
    for path in agent["extras"]:
        if path.name.upper() == "HEARTBEAT.MD":
            continue
        text = _read(path).strip()
        if text:
            parts.append(f"## {path.stem.title()}\n\n{text}")
    return "\n\n".join(p for p in parts if p)


def _blocked(*texts):
    for name, text in texts:
        problem = ghimport.check_text(name, text or "")
        if problem:
            return problem
    return ""


def _cron(cron, timezone="", now=None):
    """A Paperclip schedule as Lago's: {"repeat", "every_minutes", "when", "text"}, or None
    when Lago's repeats (every N minutes, hourly, daily, weekdays, weekly) can't say it."""
    fields = str(cron or "").split()
    if len(fields) != 5:
        return None
    minute, hour, day, month, weekday = fields
    if day != "*" or month != "*":
        return None
    every = re.fullmatch(r"\*/(\d+)", minute)
    if every and hour == "*" and weekday == "*":
        n = int(every.group(1))
        return {"repeat": "minutes", "every_minutes": n, "when": None, "text": f"every {n} minutes"} if n >= 5 else None
    if not minute.isdigit():
        return None
    if hour == "*" and weekday == "*":
        return {"repeat": "hourly", "every_minutes": None, "when": None, "text": f"every hour at :{int(minute):02d}"}
    if not hour.isdigit():
        return None
    local = _local_time(int(hour), int(minute), timezone, now)
    clock = local.strftime("%H:%M")
    if weekday == "*":
        return {"repeat": "daily", "every_minutes": None, "when": clock, "text": f"every day at {clock}"}
    if weekday.lower() in ("1-5", "mon-fri"):
        return {"repeat": "weekdays", "every_minutes": None, "when": clock, "text": f"weekdays at {clock}"}
    name = weekday.lower()[:3]
    day_number = int(weekday) % 7 if weekday.isdigit() else WEEKDAYS.get(name)
    if day_number is None:
        return None
    days_ahead = (day_number - (local.isoweekday() % 7)) % 7
    first = local + timedelta(days=days_ahead)
    if first <= datetime.now(first.tzinfo):
        first += timedelta(days=7)
    label = first.strftime("%A")
    return {"repeat": "weekly", "every_minutes": None, "when": first.strftime("%Y-%m-%d %H:%M"),
            "text": f"every {label} at {clock}"}


def _local_time(hour, minute, timezone, now=None):
    """Today's hour:minute in the schedule's time zone, as this computer's local time."""
    now = now or datetime.now()
    if timezone:
        try:
            from zoneinfo import ZoneInfo
            there = datetime.now(ZoneInfo(timezone)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            return there.astimezone().replace(tzinfo=None)
        except Exception:  # an unknown zone: take the time as it's written
            pass
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _routine_schedule(task):
    triggers = task["ext"].get("triggers") or []
    schedule = next((t for t in triggers if isinstance(t, dict) and t.get("kind") == "schedule" and t.get("enabled", True) is not False), None)
    if not schedule:
        return None, ("it runs on a webhook in Paperclip: set when it runs here" if triggers else "the package gives no schedule: set when it runs here")
    mapped = _cron(schedule.get("cronExpression"), schedule.get("timezone"))
    if not mapped:
        return None, f"its schedule ({schedule.get('cronExpression')}) is one Lago's routines can't repeat on"
    return mapped, ""


def _suggest_folder(slug, name):
    for base in (Path.home() / "projects", Path.home() / "code", Path.home() / "src"):
        if (base / slug).is_dir():
            return f"~/{(base / slug).relative_to(Path.home())}"
    return f"~/lago-work/{slug or _slug(name)}"


def _repo(project):
    workspaces = project["ext"].get("workspaces") or {}
    items = list(workspaces.values()) if isinstance(workspaces, dict) else list(workspaces)
    items = [w for w in items if isinstance(w, dict) and w.get("repoUrl")]
    primary = next((w for w in items if w.get("isPrimary")), items[0] if items else None)
    return (primary or {}).get("repoUrl") or ""


def preview(token):
    """What the package holds and what Lago does with each item:
    {"company", "agents", "skills", "projects", "routines", "tasks", "left_out"}. Each item has
    "slug", "name", a "note", and "blocked" (why it can't be imported) or "ok"."""
    root = _root_of(token)
    package = read_package(root)
    catalog = agentdefs.catalog()
    config = config_store.load()
    agents = []
    for slug, agent in package["agents"].items():
        model, adapter = _model(agent)
        name = agent["meta"].get("name") or slug.replace("-", " ").title()
        runtime = ADAPTERS.get(adapter, "")
        note = [agent["meta"].get("title") or "", "on Codex" if runtime == "codex" else "", model]
        if adapter and adapter not in ADAPTERS:
            note.append(f"runs on {adapter} in Paperclip: here it runs on Claude")
        blocked = _blocked((f"agents/{slug}", _prompt(agent)))
        if not blocked and not _prompt(agent).strip():
            blocked = "it has no instructions"
        agents.append({"slug": slug, "name": name, "note": " · ".join(n for n in note if n),
                       "exists": _slug(name) in catalog, "skills": [str(s) for s in agent["meta"].get("skills") or []],
                       "blocked": blocked})
    skills = []
    for slug, skill in package["skills"].items():
        name = skill["meta"].get("name") or slug
        sources = ((skill["meta"].get("metadata") or {}).get("sources") or []) if isinstance(skill["meta"].get("metadata"), dict) else []
        referenced = not skill["body"].strip() and sources
        blocked = ""
        if OWN_SKILLS.match(slug) or OWN_SKILLS.match(str(name)):
            blocked = "Paperclip's own skill: Lago doesn't need it"
        elif referenced:
            url = next((s.get("url") or s.get("repo") for s in sources if isinstance(s, dict)), "")
            blocked = f"only a link to {url or 'its source'}; export with skills included, or import it from Find on GitHub"
        elif (skill_sources.SKILLS_DIR / str(name)).exists() or (skill_sources.LINK_DIR / str(name)).exists():
            blocked = "you already have a skill with this name"
        else:
            blocked = _blocked((f"skills/{slug}/SKILL.md", skill["body"]))
        skills.append({"slug": slug, "name": name, "note": _first_sentence(skill["meta"].get("description"), 140), "blocked": blocked})
    projects = []
    for slug, project in package["projects"].items():
        name = project["meta"].get("name") or slug.replace("-", " ").title()
        repo = _repo(project)
        projects.append({"slug": slug, "name": name, "note": _first_sentence(project["meta"].get("description") or project["body"], 140),
                         "repo": repo, "folder": _suggest_folder(slug, name),
                         "exists": slug in config["projects"], "blocked": ""})
    routines, tasks, finished = [], [], 0
    for slug, task in package["tasks"].items():
        title = task["meta"].get("name") or task["meta"].get("title") or slug.replace("-", " ").title()
        assignee = task["meta"].get("assignee") or ""
        if task["recurring"]:
            schedule, why = _routine_schedule(task)
            blocked = _blocked((f"tasks/{slug}", task["body"]))
            if not blocked and not assignee:
                blocked = "no agent does it"
            elif not blocked and assignee not in package["agents"] and _slug(assignee) not in catalog:
                blocked = f"its agent ({assignee}) isn't in the package"
            routines.append({"slug": slug, "name": title, "agent": assignee, "project": task["project"] or "",
                             "note": schedule["text"] if schedule else why, "schedule": bool(schedule), "blocked": blocked})
            continue
        status = str(task["ext"].get("status") or "todo")
        if status not in OPEN_STATUSES:
            finished += 1
            continue
        tasks.append({"slug": slug, "name": title, "agent": assignee, "project": task["project"] or "",
                      "note": " · ".join(x for x in (str(task["ext"].get("identifier") or ""), status.replace("_", " "),
                                                     str(task["ext"].get("priority") or "")) if x),
                      "blocked": _blocked((f"tasks/{slug}", task["body"]))})
    left_out = ["Who reports to whom: in Lago, each task gets a lead that hands the work out.",
                "Monthly budgets per agent: Lago's budgets are per task and per conversation (Accounts & Budgets).",
                "Agents waking on their own (heartbeats): make a routine for anything an agent should do regularly.",
                "Secrets: packages never carry them. Add the ones your agents need under Projects → Secrets."]
    if finished:
        left_out.insert(0, f"{finished} finished or cancelled task{'s' if finished != 1 else ''}: only open tasks come over.")
    return {"company": package["company"], "agents": agents, "skills": skills, "projects": projects,
            "routines": routines, "tasks": tasks, "left_out": left_out}


def _root_of(token):
    folder = _stage_dir(token)
    rel = (folder.parent / "root.txt").read_text().strip() if (folder.parent / "root.txt").exists() else ""
    return folder / rel if rel and rel != "." else folder


# ---------------------------------------------------------------- importing

def _clone_into(url, folder):
    proc = subprocess.run(["git", "clone", "--quiet", url, str(folder)], capture_output=True, text=True,
                          timeout=ghimport.CLONE_TIMEOUT)
    if proc.returncode != 0:
        raise PaperclipError(f"couldn't clone {url}: {(proc.stderr or proc.stdout).strip()[-160:]}")


def _add_project(slug, project, folder_text, catalog_ids):
    """The project in config.json, its folder made (or cloned) if it isn't there yet."""
    import api
    config = config_store.without_swarm(config_store.load())
    project_id = slug if slug not in config["projects"] else f"{slug[:44]}-2"
    n = 2
    while project_id in config["projects"]:
        n += 1
        project_id = f"{slug[:44]}-{n}"
    folder = Path(str(folder_text or "").strip() or _suggest_folder(slug, "")).expanduser()
    if not folder.is_absolute():
        raise PaperclipError(f"give {project['meta'].get('name') or slug} a full folder path")
    repo = _repo(project)
    if not folder.exists():
        if repo and re.match(r"^(https://|git@)", repo):
            folder.parent.mkdir(parents=True, exist_ok=True)
            _clone_into(repo, folder)
        else:
            folder.mkdir(parents=True)
    owner = project["meta"].get("owner")
    config["projects"][project_id] = {
        "name": project["meta"].get("name") or slug.replace("-", " ").title(), "icon": "cube",
        "cwd": str(folder).replace(str(Path.home()), "~", 1), "repo": repo, "branch": "main",
        "description": _first_sentence(project["meta"].get("description") or project["body"], 300), "stack": "",
        "context": [], "vars": {}, "agents": [catalog_ids[owner]] if owner in catalog_ids else [], "areas": {},
        "links": [], "services": [], "commands": {}, "github": {"repos": [], "boards": []}, "mcp_servers": {}}
    api._validate_projects(config["projects"])
    config_store.save(config)
    return project_id


def _comments(task):
    out, used = [], 0
    for comment in task["ext"].get("comments") or []:
        if not isinstance(comment, dict) or not str(comment.get("body") or "").strip():
            continue
        who = comment.get("authorAgentSlug") or ("you" if comment.get("authorType") == "user" else "someone")
        line = f"- {who}: {' '.join(str(comment['body']).split())}"
        if used + len(line) > MAX_COMMENTS_TEXT:
            out.append("- (older comments left out)")
            break
        out.append(line)
        used += len(line)
    return out


def apply(token, agents=(), skills=(), projects=None, routines=(), tasks=()):
    """Import what you ticked. `projects` maps a project's slug to its folder here. Returns
    {"done": [lines], "skipped": [lines]}."""
    root = _root_of(token)
    package = read_package(root)
    shown = preview(token)
    allowed = {kind: {i["slug"] for i in shown[kind] if not i["blocked"]} for kind in ("agents", "skills", "routines", "tasks")}
    done, skipped = [], []
    projects = projects or {}

    skill_names = {}
    for slug in skills:
        if slug not in allowed["skills"]:
            continue
        skill = package["skills"][slug]
        try:
            ghimport._check_tree(root, ghimport._files_under(skill["folder"]))
            name = skill_sources.install_from(skill["folder"], "paperclip", "", f"skills/{slug}")
            skill_names[slug] = name
            done.append(f"skill {name}")
        except (skill_sources.SkillSourceError, TeamError) as error:
            skipped.append(f"skill {slug}: {error}")

    catalog_ids = {}
    for slug in agents:
        if slug not in allowed["agents"]:
            continue
        agent = package["agents"][slug]
        name = agent["meta"].get("name") or slug.replace("-", " ").title()
        model, adapter = _model(agent)
        wanted = [skill_names.get(str(s), str(s)) for s in agent["meta"].get("skills") or [] if not OWN_SKILLS.match(str(s))]
        try:
            agent_id = ghimport._free_id(_slug(name) or slug)
            agentdefs.write_agent(agent_id, name, agent["meta"].get("title") or _first_sentence(agent["body"]) or name,
                                  prompt=_prompt(agent), domain="imported", model=model if ADAPTERS.get(adapter) != "codex" else "",
                                  source="paperclip", skills=", ".join(wanted), runtime=ADAPTERS.get(adapter, ""),
                                  codex_model=model if ADAPTERS.get(adapter) == "codex" else "")
            catalog_ids[slug] = agent_id
            done.append(f"agent {name}")
        except ValueError as error:
            skipped.append(f"agent {name}: {error}")

    project_ids = {}
    for slug, folder in projects.items():
        if slug not in package["projects"]:
            continue
        try:
            project_ids[slug] = _add_project(slug, package["projects"][slug], folder, catalog_ids)
            done.append(f"project {package['projects'][slug]['meta'].get('name') or slug}")
        except (PaperclipError, OSError, subprocess.SubprocessError) as error:
            skipped.append(f"project {slug}: {error}")

    import routines as routines_mod
    for slug in routines:
        if slug not in allowed["routines"]:
            continue
        task = package["tasks"][slug]
        title = task["meta"].get("name") or slug.replace("-", " ").title()
        assignee = str(task["meta"].get("assignee") or "")
        agent_id = catalog_ids.get(assignee) or (_slug(assignee) if _slug(assignee) in agentdefs.catalog() else "")
        if not agent_id:
            skipped.append(f"routine {title}: import its agent ({assignee}) too")
            continue
        schedule, _ = _routine_schedule(task)
        try:
            row = routines_mod.create(title, agent_id, task["body"].strip() or title,
                                      project=project_ids.get(task["project"] or "", ""),
                                      folder="" if project_ids.get(task["project"] or "") else "~/lago-work/routines",
                                      repeat=schedule["repeat"] if schedule else "daily",
                                      every_minutes=schedule["every_minutes"] if schedule else None,
                                      when=schedule["when"] if schedule else None)
            routines_mod.set_status(row["id"], "paused")
            done.append(f"routine {title} (switched off" + ("" if schedule else "; set its schedule") + ")")
        except routines_mod.RoutineError as error:
            skipped.append(f"routine {title}: {error}")

    _ensure()
    now = time.time()
    with hub.connect() as conn:
        for slug in tasks:
            if slug not in allowed["tasks"]:
                continue
            task = package["tasks"][slug]
            title = task["meta"].get("name") or task["meta"].get("title") or slug.replace("-", " ").title()
            body = task["body"].strip()
            comments = _comments(task)
            if comments:
                body += "\n\nComments in Paperclip:\n" + "\n".join(comments)
            assignee = str(task["meta"].get("assignee") or "")
            conn.execute("INSERT INTO imported_tasks (source, ref, title, body, project, agent, priority, created_at, updated_at) "
                         "VALUES ('paperclip', ?, ?, ?, ?, ?, ?, ?, ?)",
                         (str(task["ext"].get("identifier") or slug), title, body, project_ids.get(task["project"] or "", ""),
                          catalog_ids.get(assignee, ""), str(task["ext"].get("priority") or ""), now, now))
            done.append(f"task {title}")

    if done:
        activity.log(activity.actor(), "paperclip.imported", f"{package['company']['name']}: {len(done)} items")
    shutil.rmtree(_stage_dir(token).parent, ignore_errors=True)
    return {"done": done, "skipped": skipped}


# ---------------------------------------------------------------- imported tasks

_ready = False


def _ensure():
    global _ready
    if not _ready:
        with hub.connect() as conn:
            conn.executescript(SCHEMA)
        _ready = True


def imported_tasks(status="waiting"):
    """Tasks brought over from another tool, waiting for you to start them."""
    _ensure()
    with hub.connect() as conn:
        rows = conn.execute("SELECT * FROM imported_tasks WHERE status = ? ORDER BY id", (status,))
        return [dict(r) for r in rows]


def set_task_status(task_id, status):
    if status not in ("waiting", "started", "dismissed"):
        raise PaperclipError(f"Unknown status {status!r}")
    _ensure()
    with hub.connect() as conn:
        changed = conn.execute("UPDATE imported_tasks SET status = ?, updated_at = ? WHERE id = ?",
                               (status, time.time(), int(task_id))).rowcount
    if not changed:
        raise PaperclipError(f"There's no imported task #{task_id}")
