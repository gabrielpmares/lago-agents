"""Bringing Paperclip over: every company, with what its people built and what it did.

Two ways in, one result. Paperclip on this computer (paperclip_local.py) gives everything: the
newest database backup plus the instance's files (instructions, skills, uploads, the key its
secrets are encrypted with). An exported package (the zip from Paperclip's Export page, a
folder from `paperclipai company export`, or a GitHub repository in the Agent Companies
format) gives what Paperclip puts in exports. Either becomes the same description of each
company (paperclip_local.snapshot()'s shape), which preview() lays out and apply() imports:

- **agents** join the catalog with their instructions (and SOUL.md, TOOLS.md…), model, Claude
  or Codex, and who they reported to; an agent that woke on a heartbeat gets a routine doing
  its heartbeat checklist every N minutes, switched off;
- **skills** install like GitHub skills (checked, scripts not executable). Paperclip's own are
  left out;
- **goals** keep their hierarchy; **projects** get their folder here (the one Paperclip used if
  it's on this computer, else cloned from the repository or made);
- **routines** keep their schedule (or a Lago webhook for a Paperclip webhook), switched off;
- **tasks**, the whole tree, go to the backlog (backlog.py): open ones to start, finished ones
  as history, with comments, documents and their revisions, labels, blockers and attachments;
- **secrets** go to the keyring (values only from this computer's Paperclip, when you say so);
- **history**: costs to Spending (never counted against budgets), approvals and the activity
  log to History with their original dates; documents that belong to no task to files.

What Lago has no place for (plugins, chat channels, case workflows…) is listed, and Lago can
be asked to study it. Importing again brings only what's new (a ledger of what came from where).

Everything is someone else's text: instructions that try to steer an agent are refused item
by item (ghimport.check_text), and a zip is unpacked with no links, no paths outside its folder
and size limits.
"""

import base64
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
import backlog
import config_store
import ghimport
import hub
import paperclip_local
import skill_sources
from team import TeamError

STAGING = config_store.STATE_DIR / "imports"
KEEP_HOURS = 24
MAX_ZIP_FILES = 5000
MAX_ZIP_TOTAL = 200 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
OPEN_STATUSES = backlog.OPEN
OWN_SKILLS = re.compile(r"^(paperclip(-|$)|para-memory-files$)")  # Paperclip's own: no use outside it
ADAPTERS = {"claude_local": "", "codex_local": "codex"}  # Paperclip adapter -> Lago runtime
WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
SOURCE = "paperclip"
DOCS_DIR = config_store.DATA_DIR / "imports" / "paperclip"
LEDGER = """
CREATE TABLE IF NOT EXISTS import_ledger (
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- agent, skill, goal, project, routine, heartbeat, task, secret, approval, activity
    ref         TEXT NOT NULL,   -- its id where it came from
    lago_id     TEXT NOT NULL,   -- what it became here
    at          REAL NOT NULL,
    PRIMARY KEY (source, kind, ref)
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


# ---------------------------------------------------------------- unpacking a package

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


def stage(data_url=None, folder=None, github=None, instance=None, backup=None):
    """Get ready to import, and return a token for preview() and apply(): from Paperclip on
    this computer (`instance`: its folder; `backup`: one of its backups, else the newest), a
    backup file on its own (`backup`), or an exported package (a zip as a data URL, a folder,
    or a GitHub repository)."""
    token, target = _new_stage()
    sub = ""
    try:
        if instance or backup:
            root = Path(instance).expanduser() if instance else None
            if root and not root.is_dir():
                raise PaperclipError(f"There's no Paperclip folder at {instance}.")
            chosen = Path(backup).expanduser() if backup else next(iter(paperclip_local.backups(root)), None)
            if not chosen or not chosen.is_file():
                raise PaperclipError("This Paperclip has no backup yet. In Paperclip, run `paperclipai db:backup`, "
                                     "or wait for its hourly one, then try again.")
            paperclip_local.snapshot(chosen, root, with_secret_values=False)  # fails now if it isn't one
            (target.parent / "source.json").write_text(json.dumps({"kind": "local", "root": str(root) if root else "", "backup": str(chosen)}))
            target.mkdir()
            return token
        if data_url:
            header, _, encoded = str(data_url).partition(",")
            if not header.endswith(";base64"):
                raise PaperclipError("That file couldn't be read.")
            data = base64.b64decode(encoded)
            if data[:2] == b"\x1f\x8b" or b"Paperclip database backup" in data[:200]:
                target.mkdir()
                (target / "backup.sql.gz").write_bytes(data)  # a backup file chosen in the browser
                paperclip_local.snapshot(target / "backup.sql.gz", None, with_secret_values=False)
                (target.parent / "source.json").write_text(json.dumps({"kind": "local", "root": "", "backup": str(target / "backup.sql.gz")}))
                return token
            _unzip(data, target)
        elif folder:
            source = Path(folder).expanduser()
            if not source.is_dir():
                raise PaperclipError(f"There's no folder {folder}.")
            if (source / "db").is_dir() or (source / "data" / "backups").is_dir() or (source / "instances").is_dir():
                shutil.rmtree(target.parent, ignore_errors=True)
                found = paperclip_local.instances([source])
                if not found:
                    raise PaperclipError(f"There's no Paperclip instance in {folder}.")
                return stage(instance=found[0]["path"])
            shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".git", "node_modules"))
            if any(p.is_symlink() for p in target.rglob("*")):
                raise PaperclipError("The folder has links in it; export the company again, or choose the zip.")
        elif github:
            sub = _github(github, target)
        else:
            raise PaperclipError("Choose Paperclip on this computer, the exported zip, a folder, or a GitHub repository.")
        root = _root(target / sub if sub else target)
    except paperclip_local.pb.BackupError as error:
        shutil.rmtree(target.parent, ignore_errors=True)
        raise PaperclipError(str(error)) from error
    except Exception:
        shutil.rmtree(target.parent, ignore_errors=True)
        raise
    (target.parent / "source.json").write_text(json.dumps({"kind": "package", "root": str(root)}))
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


def _source(token):
    folder = _stage_dir(token)
    try:
        return json.loads((folder.parent / "source.json").read_text())
    except (OSError, ValueError) as error:
        raise PaperclipError("That import has expired. Choose the package again.") from error


def load(token, with_secret_values=False):
    """The companies behind a token, read fresh from their source."""
    source = _source(token)
    if source["kind"] == "local":
        try:
            return paperclip_local.snapshot(source["backup"], source["root"] or None, with_secret_values=with_secret_values)
        except (paperclip_local.pb.BackupError, OSError) as error:
            raise PaperclipError(f"The backup can't be read any more: {error}") from error
    return {"source": "package", "companies": [company_from_package(Path(source["root"]))]}


# ---------------------------------------------------------------- an exported package, described

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


def _when(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def company_from_package(root):
    """An exported package as paperclip_local.snapshot() describes a company. Ids are the
    package's slugs; what exports leave out (costs, history, secrets) is simply empty."""
    ext = _extension(root)
    top = next((root / n for n in ("COMPANY.md", "TEAM.md") if (root / n).is_file()), None)  # a team package has no company
    meta, body = markdown(_read(top)) if top else ({}, "")
    agents = []
    for folder in sorted(p for p in (root / "agents").glob("*") if p.is_dir()) if (root / "agents").is_dir() else []:
        entry = next((folder / n for n in ("AGENTS.md", "AGENT.md") if (folder / n).is_file()), None)
        if not entry:
            continue
        a_meta, a_body = markdown(_read(entry))
        a_ext = (ext.get("agents") or {}).get(folder.name) or {}
        adapter = a_ext.get("adapter") or {}
        heartbeat = ((a_ext.get("runtime") or {}).get("heartbeat") or {})
        agents.append({"id": folder.name, "name": a_meta.get("name") or folder.name.replace("-", " ").title(),
                       "title": a_meta.get("title") or "", "role": a_meta.get("role") or a_ext.get("role") or "",
                       "reports_to": a_meta.get("reportsTo"), "capabilities": a_ext.get("capabilities") or "",
                       "adapter": adapter.get("type") or "", "model": str((adapter.get("config") or {}).get("model") or a_meta.get("model") or ""),
                       "instructions": a_body, "extras": {p.name: _read(p) for p in sorted(folder.glob("*.md")) if p != entry},
                       "status": "", "heartbeat_minutes": round(heartbeat["intervalSec"] / 60) if heartbeat.get("enabled") and heartbeat.get("intervalSec") else 0,
                       "budget_cents": a_ext.get("budgetMonthlyCents") or 0, "skills": [str(s) for s in a_meta.get("skills") or []],
                       "permissions": a_ext.get("permissions") or {}})
    skills = []
    for path in sorted((root / "skills").rglob("SKILL.md")) if (root / "skills").is_dir() else []:
        s_meta, s_body = markdown(_read(path))
        metadata = s_meta.get("metadata") if isinstance(s_meta.get("metadata"), dict) else {}
        sources = metadata.get("sources") or []
        files = {} if not s_body.strip() and sources else {p.relative_to(path.parent).as_posix(): p.read_bytes()
                                                              for p in sorted(path.parent.rglob("*")) if p.is_file() and not p.is_symlink()}
        skills.append({"id": path.parent.name, "slug": path.parent.name, "name": s_meta.get("name") or path.parent.name,
                       "description": s_meta.get("description") or "", "files": files,
                       "source": next((s.get("url") or s.get("repo") or "" for s in sources if isinstance(s, dict)), ""),
                       "source_kind": "referenced" if not files else "package"})
    projects = []
    for path in sorted((root / "projects").glob("*/PROJECT.md")) if (root / "projects").is_dir() else []:
        p_meta, p_body = markdown(_read(path))
        p_ext = (ext.get("projects") or {}).get(path.parent.name) or {}
        spaces = p_ext.get("workspaces") or {}
        spaces = [w for w in (spaces.values() if isinstance(spaces, dict) else spaces) if isinstance(w, dict)]
        primary = next((w for w in spaces if w.get("isPrimary")), spaces[0] if spaces else {})
        projects.append({"id": path.parent.name, "name": p_meta.get("name") or path.parent.name.replace("-", " ").title(),
                         "description": p_meta.get("description") or _first_sentence(p_body, 300), "status": p_ext.get("status") or "",
                         "lead": p_meta.get("owner") or p_ext.get("leadAgentSlug"), "goals": [], "folder": "", "recorded_folder": "",
                         "repo": primary.get("repoUrl") or "", "repo_ref": primary.get("repoRef") or "", "archived": False})
    issues, routines = [], []
    for path in sorted(root.rglob("TASK.md")):
        t_meta, t_body = markdown(_read(path))
        slug = path.parent.name
        project = t_meta.get("project") or (path.parts[-4] if len(path.parts) >= 5 and path.parts[-3] == "tasks" and path.parts[-5] == "projects" else None)
        recurring = bool(t_meta.get("recurring")) or slug in (ext.get("routines") or {}) or bool(t_meta.get("schedule"))
        if recurring:
            r_ext = (ext.get("routines") or {}).get(slug) or {}
            routines.append({"id": slug, "title": t_meta.get("name") or slug.replace("-", " ").title(), "description": t_body,
                             "assignee": t_meta.get("assignee"), "project": project, "goal": None, "status": r_ext.get("status") or "active",
                             "triggers": [{"kind": t.get("kind"), "cron": t.get("cronExpression"), "timezone": t.get("timezone"),
                                           "enabled": t.get("enabled", True) is not False, "label": t.get("label") or ""}
                                          for t in r_ext.get("triggers") or [] if isinstance(t, dict)],
                             "documents": []})
            continue
        t_ext = (ext.get("tasks") or {}).get(slug) or {}
        issues.append({"id": slug, "identifier": str(t_ext.get("identifier") or ""), "title": t_meta.get("name") or t_meta.get("title") or slug.replace("-", " ").title(),
                       "description": t_body, "status": str(t_ext.get("status") or "todo"), "priority": str(t_ext.get("priority") or ""),
                       "assignee": t_meta.get("assignee"), "parent": t_ext.get("parent"), "project": project, "goal": None,
                       "labels": [str(x) for x in t_ext.get("labels") or []], "blocked_by": [str(x) for x in t_ext.get("blockedBy") or []],
                       "comments": [{"by": c.get("authorAgentSlug") or ("you" if c.get("authorType") == "user" else "someone"), "text": c.get("body") or "", "at": _when(c.get("createdAt"))}
                                    for c in t_ext.get("comments") or [] if isinstance(c, dict)],
                       "documents": [], "attachments": [], "created_at": _when(t_ext.get("createdAt")),
                       "finished_at": _when(t_ext.get("completedAt") or t_ext.get("cancelledAt")), "origin": ""})
    return {"id": _slug(meta.get("slug") or meta.get("name") or root.name) or "company", "name": meta.get("name") or root.name,
            "description": meta.get("description") or _first_sentence(body), "status": "", "prefix": "", "budget_cents": 0,
            "agents": agents, "skills": skills, "goals": [], "projects": projects, "issues": issues, "routines": routines,
            "approvals": [], "costs": [], "budgets": [], "secrets": [], "activity": [], "documents": [], "other": [], "runs": 0}


# ---------------------------------------------------------------- schedules

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


def _schedule(routine):
    """Lago's schedule for a routine's triggers: (schedule or None, webhook?, why not)."""
    triggers = [t for t in routine["triggers"] if t.get("enabled", True)]
    webhook = any(t.get("kind") == "webhook" for t in triggers)
    for trigger in triggers:
        if trigger.get("kind") == "schedule":
            mapped = _cron(trigger.get("cron"), trigger.get("timezone"))
            if mapped:
                return mapped, webhook, ""
            return None, webhook, f"its schedule ({trigger.get('cron')}) is one Lago's routines can't repeat on: set when it runs"
    if webhook:
        return None, True, ""
    return None, False, "no schedule came with it: set when it runs"


# ---------------------------------------------------------------- the preview

def _ledger(conn=None):
    with hub.connect() as c:
        c.executescript(LEDGER)
        return {(r["kind"], r["ref"]): r["lago_id"] for r in c.execute("SELECT kind, ref, lago_id FROM import_ledger WHERE source = ?", (SOURCE,))}


def _remember(kind, ref, lago_id):
    with hub.connect() as conn:
        conn.executescript(LEDGER)
        conn.execute("INSERT OR REPLACE INTO import_ledger (source, kind, ref, lago_id, at) VALUES (?, ?, ?, ?, ?)",
                     (SOURCE, kind, str(ref), str(lago_id), time.time()))


def _prompt(agent):
    """An agent's instructions: its AGENTS.md, then its other notes (SOUL.md, TOOLS.md…). The
    heartbeat checklist becomes its heartbeat routine instead."""
    parts = [str(agent["instructions"] or "").strip()]
    for name, text in sorted(agent["extras"].items()):
        if Path(name).stem.upper() != "HEARTBEAT" and str(text).strip():
            parts.append(f"## {Path(name).stem.title()}\n\n{str(text).strip()}")
    if not any(parts):  # its files weren't there (a backup on its own): start from who it was
        role = agent.get("title") or agent.get("role") or ""
        parts = [f"You are {agent['name']}{', ' + role if role and role.lower() != agent['name'].lower() else ''}."
                 + (f" {agent['capabilities']}" if agent.get("capabilities") else "")]
    return "\n\n".join(p for p in parts if p)


def _heartbeat_text(agent):
    text = next((str(t).strip() for n, t in agent["extras"].items() if Path(n).stem.upper() == "HEARTBEAT" and str(t).strip()), "")
    return text or f"Do your regular check-in as {agent['name']}: review your open work, move the most important thing forward, and report what changed."


def _blocked(name, text):
    return ghimport.check_text(name, text or "")


def _suggest_folder(slug, name):
    for base in (Path.home() / "projects", Path.home() / "code", Path.home() / "src"):
        if (base / slug).is_dir():
            return f"~/{(base / slug).relative_to(Path.home())}"
    return f"~/lago-work/{slug or _slug(name)}"


def _money(cents):
    return f"${cents / 100:,.2f}"


def preview(token):
    """What each company holds and what Lago does with it: {"source", "backup_at",
    "companies": [{"id", "name", ..., "agents", "skills", "goals", "projects", "routines",
    "heartbeats", "tasks", "secrets", "history", "documents", "other", "left_out"}]}. Items carry
    "id", "name", a "note", and "blocked" (why it can't come over) or "done" (already here)."""
    data = load(token)
    ledger = _ledger()
    catalog = agentdefs.catalog()
    config = config_store.load()
    out = []
    for c in data["companies"]:
        names = {a["id"]: a["name"] for a in c["agents"]}
        agents = []
        for a in c["agents"]:
            runtime = ADAPTERS.get(a["adapter"], "")
            note = [a["title"], "on Codex" if runtime == "codex" else "", a["model"],
                    f"reported to {names[a['reports_to']]}" if a.get("reports_to") in names else ""]
            if a["adapter"] and a["adapter"] not in ADAPTERS:
                note.append(f"runs on {a['adapter']} in Paperclip: here it runs on Claude")
            prompt = _prompt(a)
            blocked = _blocked(f"agent {a['name']}", prompt)
            if not str(a["instructions"] or "").strip():
                note.append("its instructions weren't found: it starts from its role")
            agents.append({"id": a["id"], "name": a["name"], "note": " · ".join(n for n in note if n), "blocked": blocked,
                           "done": ("agent", a["id"]) in ledger, "exists": _slug(a["name"]) in catalog})
        skills = []
        for s in c["skills"]:
            blocked = ""
            if OWN_SKILLS.match(s["slug"]) or OWN_SKILLS.match(str(s["name"])):
                blocked = "Paperclip's own skill: Lago doesn't need it"
            elif not s["files"]:
                blocked = f"only a link to {s['source'] or 'its source'}: install it from Find on GitHub"
            elif (skill_sources.SKILLS_DIR / str(s["name"])).exists() or (skill_sources.LINK_DIR / str(s["name"])).exists():
                blocked = "you already have a skill with this name"
            else:
                blocked = _blocked(f"skill {s['name']}", s["files"].get("SKILL.md", b"").decode("utf-8", "replace"))
            skills.append({"id": s["id"], "name": s["name"], "note": _first_sentence(s["description"], 140), "blocked": blocked,
                           "done": ("skill", s["id"]) in ledger})
        goal_titles = {g["id"]: g["title"] for g in c["goals"]}
        goals = [{"id": g["id"], "name": g["title"], "note": " · ".join(x for x in (g["status"], f"under {goal_titles[g['parent']]}" if g.get("parent") in goal_titles else "") if x),
                  "blocked": "", "done": ("goal", g["id"]) in ledger} for g in c["goals"]]
        projects = []
        for p in c["projects"]:
            folder = p["folder"] and str(p["folder"]).replace(str(Path.home()), "~", 1)
            projects.append({"id": p["id"], "name": p["name"], "note": _first_sentence(p["description"], 140), "repo": p["repo"],
                             "folder": folder or _suggest_folder(_slug(p["name"]), p["name"]), "found": bool(folder),
                             "exists": _slug(p["name"]) in config["projects"], "blocked": "", "done": ("project", p["id"]) in ledger})
        routines = []
        for r in c["routines"]:
            schedule, webhook, why = _schedule(r)
            blocked = _blocked(f"routine {r['title']}", r["description"]) or ("" if r.get("assignee") in names or _slug(r.get("assignee")) in catalog else "no agent does it")
            note = schedule["text"] if schedule else ("on a webhook: Lago gives it a new link" if webhook else why)
            routines.append({"id": r["id"], "name": r["title"], "agent": names.get(r.get("assignee"), r.get("assignee") or ""),
                             "note": note + (" · paused in Paperclip" if r["status"] == "paused" else ""), "blocked": blocked,
                             "done": ("routine", r["id"]) in ledger})
        heartbeats = [{"id": a["id"], "name": f"{a['name']}'s heartbeat", "note": f"every {a['heartbeat_minutes']} minutes",
                       "blocked": "" if a["heartbeat_minutes"] >= 5 else "more often than every 5 minutes",
                       "done": ("heartbeat", a["id"]) in ledger} for a in c["agents"] if a["heartbeat_minutes"]]
        open_tasks = [i for i in c["issues"] if i["status"] in OPEN_STATUSES]
        tasks = {"open": len(open_tasks), "finished": len(c["issues"]) - len(open_tasks),
                 "comments": sum(len(i["comments"]) for i in c["issues"]), "documents": sum(len(i["documents"]) for i in c["issues"]),
                 "already": sum(1 for i in c["issues"] if ("task", i["id"]) in ledger),
                 "sample": [i["title"] for i in sorted(open_tasks, key=lambda i: ({"critical": 0, "urgent": 0, "high": 1}.get(i["priority"], 2)))][:5],
                 "blocked": [i["title"] for i in c["issues"] if _blocked("task", i["description"])]}
        user_secrets = [s for s in c["secrets"] if not s["internal"]]
        secrets_out = [{"id": s["id"], "name": s["name"], "note": " · ".join(x for x in (_env_name(s), "value can come over" if s["has_value"] else "name only: add its value here") if x),
                        "has_value": s["has_value"], "blocked": "", "done": ("secret", s["id"]) in ledger} for s in user_secrets]
        spent = sum(x["cents"] for x in c["costs"])
        history = {"costs": len(c["costs"]), "spent": _money(spent), "approvals": len(c["approvals"]), "activity": len(c["activity"]),
                   "runs": c["runs"], "done": ("history", c["id"]) in ledger}
        documents = [{"id": d["key"], "name": d["title"], "note": f"{len(d['revisions'])} revisions", "blocked": "", "done": ("document", d["key"]) in ledger}
                     for d in c["documents"]]
        left_out = ["Who reports to whom is kept on each agent: in Lago, each task gets a lead that hands the work out."]
        for b in c["budgets"]:
            left_out.append(f"A monthly budget of {_money(b['cents'])} for the {b['scope']}: Lago's budgets are per task and per conversation "
                            "(Accounts & Budgets).")
        if any(a["permissions"] for a in c["agents"]):
            left_out.append("Agents' permissions in Paperclip (creating agents, skills): in Lago, set what agents may do under Permissions.")
        if c["runs"]:
            left_out.append(f"The {c['runs']} heartbeat runs' logs stay in Paperclip; their costs and outcomes come over with the history.")
        left_out += [f"{o['count']} {o['label']}: Lago has no place for these yet. Ask Lago to look at them after the import." for o in c["other"]]
        out.append({"id": c["id"], "name": c["name"], "description": c["description"], "agents": agents, "skills": skills, "goals": goals,
                    "projects": projects, "routines": routines, "heartbeats": heartbeats, "tasks": tasks, "secrets": secrets_out,
                    "history": history, "documents": documents, "other": c["other"], "left_out": left_out})
    out.sort(key=lambda x: -(len(x["agents"]) + x["tasks"]["open"] + x["tasks"]["finished"] + len(x["routines"])))  # the main company first
    return {"source": data["source"], "backup": data.get("backup", ""), "backup_at": data.get("backup_at"), "companies": out}


def _env_name(secret):
    return re.sub(r"[^A-Z0-9_]+", "_", str(secret.get("key") or secret["name"]).upper()).strip("_")


# ---------------------------------------------------------------- importing

def _clone_into(url, folder):
    proc = subprocess.run(["git", "clone", "--quiet", url, str(folder)], capture_output=True, text=True,
                          timeout=ghimport.CLONE_TIMEOUT)
    if proc.returncode != 0:
        raise PaperclipError(f"couldn't clone {url}: {(proc.stderr or proc.stdout).strip()[-160:]}")


def _add_project(project, folder_text, agent_ids):
    """The project in config.json, its folder made (or cloned) if it isn't there yet."""
    import api
    config = config_store.without_swarm(config_store.load())
    base = _slug(project["name"]) or "project"
    project_id, n = base, 1
    while project_id in config["projects"]:
        n += 1
        project_id = f"{base[:44]}-{n}"
    folder = Path(str(folder_text or "").strip() or _suggest_folder(base, project["name"])).expanduser()
    if not folder.is_absolute():
        raise PaperclipError(f"give {project['name']} a full folder path")
    if not folder.exists():
        if project["repo"] and re.match(r"^(https://|git@)", project["repo"]):
            folder.parent.mkdir(parents=True, exist_ok=True)
            _clone_into(project["repo"], folder)
        else:
            folder.mkdir(parents=True)
    config["projects"][project_id] = {
        "name": project["name"], "icon": "cube", "cwd": str(folder).replace(str(Path.home()), "~", 1), "repo": project["repo"],
        "branch": project.get("repo_ref") or "main", "description": _first_sentence(project["description"], 300), "stack": "",
        "context": [], "vars": {}, "agents": sorted(set(agent_ids)), "areas": {}, "links": [], "services": [], "commands": {},
        "github": {"repos": [], "boards": []}, "mcp_servers": {}}
    api._validate_projects(config["projects"])
    config_store.save(config)
    return project_id


def apply(token, company, agents=(), skills=(), goals=(), projects=None, routines=(), heartbeats=(), tasks="open",
          secrets=(), secret_values=False, history=False, documents=()):
    """Import what you ticked from one company. `projects` maps a project's id to its folder
    here; `tasks` is "open", "all" or "none". Returns {"done": [lines], "skipped": [lines],
    "left": [lines for Lago to look at]}."""
    data = load(token, with_secret_values=bool(secret_values))
    c = next((x for x in data["companies"] if x["id"] == company), None)
    if not c:
        raise PaperclipError("That company isn't in this import.")
    shown = next(x for x in preview(token)["companies"] if x["id"] == company)
    usable = {kind: {i["id"] for i in shown[kind] if not i["blocked"]} for kind in ("agents", "skills", "routines", "heartbeats")}
    ledger = _ledger()
    done, skipped = [], []
    projects = projects or {}

    # skills first: agents name them
    skill_names = {}
    for s in c["skills"]:
        if ("skill", s["id"]) in ledger:
            skill_names[s["slug"]] = ledger[("skill", s["id"])]
    for s in (x for x in c["skills"] if x["id"] in skills and x["id"] in usable["skills"] and ("skill", x["id"]) not in ledger):
        try:
            name = _install_skill(s)
            skill_names[s["slug"]] = name
            _remember("skill", s["id"], name)
            done.append(f"skill {name}")
        except (skill_sources.SkillSourceError, TeamError, OSError) as error:
            skipped.append(f"skill {s['name']}: {error}")

    # agents
    ids = {a["id"]: ledger[("agent", a["id"])] for a in c["agents"] if ("agent", a["id"]) in ledger}
    chosen = [a for a in c["agents"] if a["id"] in agents and a["id"] in usable["agents"] and a["id"] not in ids]
    for a in chosen:
        ids[a["id"]] = ghimport._free_id(_slug(a["name"]) or "agent")
        while ids[a["id"]] in [ids[x["id"]] for x in chosen if x is not a and x["id"] in ids]:
            ids[a["id"]] += "-2"
    for a in chosen:
        runtime = ADAPTERS.get(a["adapter"], "")
        wanted = [skill_names.get(s, s) for s in a["skills"] if not OWN_SKILLS.match(s)]
        try:
            agentdefs.write_agent(ids[a["id"]], a["name"], a["title"] or _first_sentence(a["capabilities"] or a["instructions"]) or a["name"],
                                  prompt=_prompt(a), domain="imported", model="" if runtime == "codex" else a["model"],
                                  source=SOURCE, skills=", ".join(wanted), runtime=runtime,
                                  codex_model=a["model"] if runtime == "codex" else "",
                                  reports_to=ids.get(a.get("reports_to"), ""), tags=[c["name"]])
            _remember("agent", a["id"], ids[a["id"]])
            done.append(f"agent {a['name']}")
        except ValueError as error:
            ids.pop(a["id"], None)
            skipped.append(f"agent {a['name']}: {error}")

    # goals, parents first
    import goals as goals_mod
    goal_ids = {g["id"]: int(ledger[("goal", g["id"])]) for g in c["goals"] if ("goal", g["id"]) in ledger}
    pending = [g for g in c["goals"] if g["id"] in goals and g["id"] not in goal_ids]
    while pending:
        ready = [g for g in pending if not g.get("parent") or g["parent"] in goal_ids or g["parent"] not in {x["id"] for x in pending}]
        for g in ready:
            pending.remove(g)
            try:
                row = goals_mod.add(g["title"], description=g["description"], parent=goal_ids.get(g.get("parent")))
                if g["status"] in ("achieved", "completed", "done"):
                    goals_mod.update(row["id"], status="achieved")
                elif g["status"] in ("cancelled", "abandoned", "dropped"):
                    goals_mod.update(row["id"], status="dropped")
                goal_ids[g["id"]] = row["id"]
                _remember("goal", g["id"], row["id"])
                done.append(f"goal {g['title']}")
            except goals_mod.GoalError as error:
                skipped.append(f"goal {g['title']}: {error}")
        if not ready:
            break

    # projects
    project_ids = {p["id"]: ledger[("project", p["id"])] for p in c["projects"] if ("project", p["id"]) in ledger}
    for p in c["projects"]:
        if p["id"] not in projects or p["id"] in project_ids:
            continue
        team = [ids[i["assignee"]] for i in c["issues"] if i.get("project") == p["id"] and i.get("assignee") in ids]
        team += [ids[p["lead"]]] if p.get("lead") in ids else []
        try:
            project_ids[p["id"]] = _add_project(p, projects[p["id"]], team)
            _remember("project", p["id"], project_ids[p["id"]])
            done.append(f"project {p['name']}")
        except (PaperclipError, OSError, subprocess.SubprocessError) as error:
            skipped.append(f"project {p['name']}: {error}")

    # routines and heartbeats, switched off
    import routines as routines_mod
    hooks = []
    for r in (x for x in c["routines"] if x["id"] in routines and x["id"] in usable["routines"] and ("routine", x["id"]) not in ledger):
        agent_id = ids.get(r.get("assignee")) or (_slug(r.get("assignee")) if _slug(r.get("assignee")) in agentdefs.catalog() else "")
        if not agent_id:
            skipped.append(f"routine {r['title']}: import its agent too")
            continue
        schedule, webhook, _ = _schedule(r)
        text = "\n\n".join([r["description"].strip() or r["title"]] + [f"## {d['title']}\n\n{d['body']}" for d in r["documents"] if d["body"].strip()])
        try:
            row = routines_mod.create(r["title"], agent_id, text, project=project_ids.get(r.get("project") or "", ""),
                                      folder="" if project_ids.get(r.get("project") or "") else "~/lago-work/routines",
                                      repeat=schedule["repeat"] if schedule else ("never" if webhook else "daily"),
                                      every_minutes=schedule["every_minutes"] if schedule else None,
                                      when=schedule["when"] if schedule else None)
            routines_mod.set_status(row["id"], "paused")
            if webhook:
                routines_mod.new_hook(row["id"])
                hooks.append(r["title"])
            _remember("routine", r["id"], row["id"])
            done.append(f"routine {r['title']} (switched off" + ("; new webhook link on its page" if webhook else "" if schedule else "; set when it runs") + ")")
        except routines_mod.RoutineError as error:
            skipped.append(f"routine {r['title']}: {error}")
    for a in (x for x in c["agents"] if x["id"] in heartbeats and x["id"] in usable["heartbeats"] and ("heartbeat", x["id"]) not in ledger):
        if a["id"] not in ids:
            skipped.append(f"{a['name']}'s heartbeat: import the agent too")
            continue
        minutes = max(5, int(a["heartbeat_minutes"]))
        try:
            row = routines_mod.create(f"{a['name']}: heartbeat", ids[a["id"]], _heartbeat_text(a), folder="~/lago-work/routines",
                                      repeat="minutes" if minutes < 60 else "hourly" if minutes < 1440 else "daily",
                                      every_minutes=minutes if minutes < 60 else None)
            routines_mod.set_status(row["id"], "paused")
            _remember("heartbeat", a["id"], row["id"])
            done.append(f"{a['name']}'s heartbeat, every {minutes if minutes < 60 else 'hour' if minutes < 1440 else 'day'}"
                        + (" minutes" if minutes < 60 else "") + " (switched off)")
        except routines_mod.RoutineError as error:
            skipped.append(f"{a['name']}'s heartbeat: {error}")

    # tasks, the whole tree
    if tasks in ("open", "all"):
        brought = _import_tasks(c, tasks, ids, project_ids, goal_ids, ledger)
        if brought:
            done.append(f"{brought} task{'s' if brought != 1 else ''} to the backlog")

    # secrets
    import vault
    taken = set()
    for s in (x for x in c["secrets"] if x["id"] in secrets and not x["internal"] and ("secret", x["id"]) not in ledger):
        name = _slug(s["name"])[:40] or "secret"
        if (name, _env_name(s)) in taken:  # STRIPE_API_KEY and stripe-api-key are one secret here
            _remember("secret", s["id"], name)
            continue
        taken.add((name, _env_name(s)))
        try:
            if secret_values and s.get("value"):
                vault.save(name, _env_name(s), s["value"])
                done.append(f"secret {name} ({_env_name(s)}) with its value")
            else:
                bound = next((ids[b["target"]] for b in s["bindings"] if b["type"] == "agent" and b["target"] in ids), "")
                _ask_for_secret(_env_name(s), s["description"], bound)
                done.append(f"secret {name} ({_env_name(s)}): add its value on the Secrets page")
            _remember("secret", s["id"], name)
        except Exception as error:  # the keyring may be locked
            skipped.append(f"secret {s['name']}: {error}")

    # history
    if history and ("history", c["id"]) not in ledger:
        _import_history(c, ids, project_ids)
        _remember("history", c["id"], c["id"])
        done.append(f"history: {len(c['costs'])} costs ({_money(sum(x['cents'] for x in c['costs']))}), "
                    f"{len(c['approvals'])} approvals, {len(c['activity'])} activity lines")
    for d in (x for x in c["documents"] if x["key"] in documents and ("document", x["key"]) not in ledger):
        path = _save_document(c, d)
        _remember("document", d["key"], path)
        done.append(f"document {d['title']}")

    if done:
        activity.log(activity.actor(), "paperclip.imported", f"{c['name']}: " + "; ".join(done)[:1500])
    left = [line for line in shown["left_out"]] + [f"skipped {line}" for line in skipped]
    return {"done": done, "skipped": skipped, "left": left, "hooks": hooks}


def _install_skill(skill):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="paperclip-skill-") as tmp:
        folder = Path(tmp) / skill["slug"]
        for rel, data in skill["files"].items():
            path = (folder / rel).resolve()
            if folder.resolve() not in path.parents:
                raise PaperclipError(f"{rel} points outside the skill")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        ghimport._check_tree(folder, ghimport._files_under(folder))
        return skill_sources.install_from(folder, SOURCE, "", f"skills/{skill['slug']}")


def _ask_for_secret(env, description, agent_id):
    """A secret whose value didn't come over: you're asked for it (secret_requests.py), on
    behalf of the agent it belonged to."""
    import secret_requests
    secret_requests.request(agent_id or SOURCE, env, f"Brought over from Paperclip{': ' + description if description else ''}. Paste its value.")


def _import_tasks(c, which, ids, project_ids, goal_ids, ledger):
    """Every task (or the open ones, with their parents for context) into the backlog."""
    by_id = {i["id"]: i for i in c["issues"]}
    keep = {i["id"] for i in c["issues"] if which == "all" or i["status"] in OPEN_STATUSES}
    for item_id in list(keep):
        parent = by_id[item_id].get("parent")
        while parent in by_id and parent not in keep:
            keep.add(parent)
            parent = by_id[parent].get("parent")
    names = {a["id"]: a["name"] for a in c["agents"]}
    backlog_ids = {i["id"]: int(ledger[("task", i["id"])]) for i in c["issues"] if ("task", i["id"]) in ledger}
    added = 0
    for i in c["issues"]:
        if i["id"] not in keep or i["id"] in backlog_ids or _blocked("task", i["description"]):
            continue
        body = i["description"]
        if i["attachments"]:
            saved = [_save_attachment(c, i, f) for f in i["attachments"]]
            body += "\n\nAttachments:\n" + "\n".join(f"- {p}" for p in saved if p)
        status = i["status"] if i["status"] in backlog.STATES else ("done" if i.get("finished_at") else "todo")
        backlog_ids[i["id"]] = backlog.add(
            SOURCE, i["id"], i["title"], identifier=i["identifier"], company=c["name"], body=body, status=status,
            priority=i["priority"], project=project_ids.get(i.get("project") or "", ""), agent=ids.get(i.get("assignee") or "", ""),
            goal_id=goal_ids.get(i.get("goal")), labels=i["labels"] + ([f"was {names[i['assignee']]}'s"] if i.get("assignee") in names and i["assignee"] not in ids else []),
            comments=i["comments"], documents=i["documents"], created_at=i.get("created_at") or time.time(), finished_at=i.get("finished_at"))
        _remember("task", i["id"], backlog_ids[i["id"]])
        added += 1
    for i in c["issues"]:
        if i["id"] in backlog_ids and (i.get("parent") in backlog_ids or i["blocked_by"]):
            backlog.link(backlog_ids[i["id"]], backlog_ids.get(i.get("parent")), [backlog_ids[b] for b in i["blocked_by"] if b in backlog_ids])
    return added


def _company_dir(c):
    return DOCS_DIR / (_slug(c["name"]) or "company")


def _save_attachment(c, issue, item):
    if not item.get("path"):
        return item.get("name")
    target = _company_dir(c) / "attachments" / (issue["identifier"] or _slug(issue["title"]))
    target.mkdir(parents=True, exist_ok=True)
    name = Path(str(item["name"])).name or Path(item["path"]).name
    shutil.copyfile(item["path"], target / name)
    return str((target / name)).replace(str(Path.home()), "~", 1)


def _save_document(c, document):
    folder = _company_dir(c) / "documents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_slug(document['title']) or 'document'}.md"
    path.write_text(document["body"], encoding="utf-8")
    return str(path)


APPROVAL_WORDS = {"hire_agent": "hiring", "approve_ceo_strategy": "the CEO's strategy"}


def _import_history(c, ids, project_ids):
    import spending
    names = {a["id"]: a["name"] for a in c["agents"]}
    spending.add_imported(SOURCE, [{"ref": x["ref"], "agent_id": ids.get(x.get("agent")) or names.get(x.get("agent")) or "paperclip",
                                    "project": project_ids.get(x.get("project") or "") or f"{c['name']} (Paperclip)", "model": x["model"],
                                    "cost": x["cents"] / 100, "at": x["at"] or time.time()} for x in c["costs"]])
    for a in c["approvals"]:
        what = APPROVAL_WORDS.get(a["type"], a["type"].replace("_", " "))
        text = f"[{c['name']}] Approval for {what}: {a['status']}" + (f" ({a['note']})" if a["note"] else "")
        plan = a["payload"].get("plan") or a["payload"].get("name") if isinstance(a["payload"], dict) else ""
        if plan:
            text += f" · {plan}"
        for comment in a["comments"]:
            text += f"\n- {comment['by']}: {comment['text']}"
        activity.log(a["by"], "paperclip.approval", text, at=a.get("decided_at") or a.get("at"))
    for line in c["activity"]:
        details = line["details"]
        subject = details.get("title") or details.get("name") or details.get("identifier") or "" if isinstance(details, dict) else ""
        activity.log(line["actor"], f"paperclip.{line['action']}"[:60],
                     f"[{c['name']}] {line['action'].replace('.', ' ').replace('_', ' ')}" + (f": {subject}" if subject else ""), at=line["at"])
