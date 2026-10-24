"""Paperclip on this computer: finding it, and reading everything a company holds.

Paperclip keeps an instance in `$PAPERCLIP_HOME/instances/<name>` (`~/.paperclip` by
default): its settings (config.json), hourly database backups (data/backups/*.sql.gz), uploaded
files (data/storage), agents' instruction files (companies/<company>/agents/<agent>/
instructions), company skills (skills/<company>/<skill>) and the key its secrets are encrypted
with (secrets/master.key). A Docker install keeps the same tree in its data folder.

instances() lists what's there; snapshot() reads one backup (and the instance's files, when
it's on hand) into a plain description of each company, the same shape paperclip_import
builds from an exported package: companies, agents, skills, goals, projects, issues, routines,
approvals, costs, budgets, secrets, activity, documents, and "other" for the kinds of things
Lago has no place for yet. Nothing here writes to Paperclip.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import paperclip_backup as pb

BUNDLED = ("paperclip_bundled", "bundled")  # Paperclip's own skills, installed in every company
HIDDEN_ACTIONS = re.compile(r"^(agent\.identity|primary_agent|heartbeat|agent\.wakeup|run\.|lifecycle|resource)")
OTHER_TABLES = {  # what Lago lists but has no place for, with what a person would call it
    "plugins": "plugins", "chat_channels": "chat channels (Slack, Discord, Telegram…)",
    "tool_connection_app_snapshots": "tool connections",
    "public_mcp": "MCP endpoints", "workflows": "case workflows", "pipelines": "pipelines",
    "pipeline_cases": "cases", "status_cards": "status cards", "decision_queues": "decision queues",
    "email": "email settings", "remote_agent_profiles": "remote agent profiles",
    "invites": "invites", "join_requests": "join requests",
    "agent_api_keys": "agent API keys", "board_api_keys": "board API keys",
}


class LocalError(Exception):
    pass


def homes():
    """Where Paperclip keeps its data: $PAPERCLIP_HOME, then ~/.paperclip."""
    found = []
    for raw in (os.environ.get("PAPERCLIP_HOME"), str(Path.home() / ".paperclip")):
        if raw:
            path = Path(raw).expanduser()
            if path.is_dir() and path not in found:
                found.append(path)
    return found


def _config(root):
    try:
        return json.loads((root / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _backup_dir(root):
    configured = ((_config(root).get("database") or {}).get("backup") or {}).get("dir")
    return Path(configured).expanduser() if configured else root / "data" / "backups"


def backups(root):
    """The instance's backups, newest first: [Path]."""
    folders = [_backup_dir(Path(root)), Path(root) / "data" / "backups", Path(root).parent.parent / "backups"]
    seen = {}
    for folder in folders:
        if folder.is_dir():
            for path in folder.glob("*.sql*"):
                if path.suffix in (".sql", ".gz") and path.is_file():
                    seen[path.resolve()] = path
    return sorted(seen.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def _port(root):
    server = _config(root).get("server") or {}
    return server.get("port") or 3100


def instance(root):
    """One instance: {"path", "name", "backups": [{"path", "at", "bytes"}], "port", "running"}."""
    root = Path(root).expanduser()
    found = backups(root)
    return {"path": str(root), "name": root.name, "port": _port(root), "running": _running(_port(root)),
            "backups": [{"path": str(p), "at": p.stat().st_mtime, "bytes": p.stat().st_size} for p in found[:10]]}


def instances(extra=()):
    """Every Paperclip instance found on this computer (and in `extra` folders: a Docker data
    folder, a copied instance), newest backup first."""
    out, seen = [], set()
    for base in [*homes(), *(Path(e).expanduser() for e in extra if e)]:
        candidates = [base] if (base / "db").is_dir() or (base / "data" / "backups").is_dir() else []
        candidates += sorted(p for p in (base / "instances").glob("*") if p.is_dir()) if (base / "instances").is_dir() else []
        for root in candidates:
            if root.resolve() not in seen:
                seen.add(root.resolve())
                out.append(instance(root))
    return sorted(out, key=lambda i: -(i["backups"][0]["at"] if i["backups"] else 0))


def _running(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/api/health", timeout=1.5) as response:
            return json.loads(response.read().decode()).get("status") == "ok"
    except Exception:
        return False


def fresh_backup(root):
    """Ask Paperclip's own command for a backup now. Returns its path, or raises LocalError
    saying why it couldn't (the newest automatic backup is used instead)."""
    root = Path(root).expanduser()
    cli = shutil.which("paperclipai")
    command = [cli] if cli else ([shutil.which("npx"), "--yes", "paperclipai"] if shutil.which("npx") else None)
    if not command:
        raise LocalError("Paperclip's command isn't installed here, so the newest automatic backup is used.")
    target = _backup_dir(root)
    home = root.parent.parent if root.parent.name == "instances" else root
    env = {**os.environ, "PAPERCLIP_HOME": str(home), "PAPERCLIP_INSTANCE_ID": root.name}
    before = {p.resolve() for p in backups(root)}
    proc = subprocess.run([*command, "db:backup", "--dir", str(target)], capture_output=True, text=True, timeout=300, env=env)
    after = [p for p in backups(root) if p.resolve() not in before]
    if proc.returncode != 0 or not after:
        raise LocalError("Paperclip couldn't make a backup now (is it running?): "
                         + " ".join((proc.stderr or proc.stdout).split())[-200:])
    return after[0]


# ---------------------------------------------------------------- reading files

def _local(path, root):
    """A path Paperclip recorded (maybe inside its Docker container), on this computer."""
    if not path:
        return None
    path = Path(str(path))
    if path.exists():
        return path
    if root:
        match = re.search(r"/instances/[^/]+/(.+)$", str(path))
        if match and (Path(root) / match.group(1)).exists():
            return Path(root) / match.group(1)
    return None


def _master_key(root):
    if not root:
        return None
    configured = ((_config(root).get("secrets") or {}).get("localEncrypted") or {}).get("keyFilePath")
    path = Path(configured).expanduser() if configured else Path(root) / "secrets" / "master.key"
    raw = os.environ.get("PAPERCLIP_SECRETS_MASTER_KEY")
    if not raw:
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return bytes.fromhex(raw)
    try:
        decoded = base64.b64decode(raw, validate=True)
        if len(decoded) == 32:
            return decoded
    except ValueError:
        pass
    return raw.encode() if len(raw.encode()) == 32 else None


NODE_DECRYPT = """
const c = require("crypto");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const key = Buffer.from(input.key, "base64"), out = {};
for (const [id, m] of Object.entries(input.items)) {
  try {
    const d = c.createDecipheriv("aes-256-gcm", key, Buffer.from(m.iv, "base64"));
    d.setAuthTag(Buffer.from(m.tag, "base64"));
    out[id] = Buffer.concat([d.update(Buffer.from(m.ciphertext, "base64")), d.final()]).toString("utf8");
  } catch (e) {}
}
process.stdout.write(JSON.stringify(out));
"""


def decrypt(key, materials):
    """{id: value} for the secrets that decrypt with `key` (Paperclip's AES-256-GCM). Python's
    standard library has no AES, so this uses the `cryptography` package when it's installed,
    else Node (which Paperclip itself runs on)."""
    if not key or not materials:
        return {}
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        out = {}
        for item_id, m in materials.items():
            try:
                out[item_id] = AESGCM(key).decrypt(base64.b64decode(m["iv"]), base64.b64decode(m["ciphertext"]) + base64.b64decode(m["tag"]), None).decode()
            except Exception:
                continue
        return out
    except ImportError:
        pass
    node = shutil.which("node")
    if not node:
        return {}
    proc = subprocess.run([node, "-e", NODE_DECRYPT], input=json.dumps({"key": base64.b64encode(key).decode(), "items": materials}),
                          capture_output=True, text=True, timeout=60)
    try:
        return json.loads(proc.stdout) if proc.returncode == 0 else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------- the snapshot

def _when(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _by(rows, key):
    out = {}
    for row in rows:
        out.setdefault(row.get(key), []).append(row)
    return out


def snapshot(backup, root=None, with_secret_values=True):
    """Everything in a backup, per company. `root` is the instance folder, for the files that
    live beside the database (instructions, skills, uploads, the secrets key)."""
    rows = pb.read(backup)
    root = Path(root) if root else None
    t = lambda name: rows.get(name, [])  # noqa: E731
    users = {u.get("id"): u.get("name") or u.get("email") for u in t("user")}
    companies = []
    for company in t("companies"):
        cid = company["id"]
        mine = lambda name: [r for r in t(name) if r.get("company_id") == cid]  # noqa: E731
        agent_rows = mine("agents")
        agent_names = {a["id"]: a["name"] for a in agent_rows}
        who = lambda agent_id=None, user_id=None: agent_names.get(agent_id) or users.get(user_id) or ("you" if user_id else "someone")  # noqa: E731

        bundled = {s["slug"] for s in mine("company_skills") if (pb.json_value(s.get("metadata"), {}) or {}).get("sourceKind") in BUNDLED}
        agents = []
        for a in agent_rows:
            config = pb.json_value(a.get("adapter_config"), {}) or {}
            runtime = pb.json_value(a.get("runtime_config"), {}) or {}
            heartbeat = runtime.get("heartbeat") or {}
            files = {}
            folder = _local(config.get("instructionsRootPath"), root) or (
                root / "companies" / cid / "agents" / a["id"] / "instructions" if root else None)
            if folder and folder.is_dir():
                for path in sorted(folder.rglob("*.md")):
                    files[path.relative_to(folder).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
            entry = Path(str(config.get("instructionsFilePath") or "AGENTS.md")).name
            body = files.pop(entry, None) or files.pop("AGENTS.md", None) or config.get("promptTemplate") or ""
            agents.append({
                "id": a["id"], "name": a["name"], "title": a.get("title") or "", "role": a.get("role") or "",
                "reports_to": a.get("reports_to"), "capabilities": a.get("capabilities") or "",
                "adapter": a.get("adapter_type") or "", "model": config.get("model") or "",
                "instructions": body, "extras": files, "status": a.get("status") or "",
                "heartbeat_minutes": round(heartbeat["intervalSec"] / 60) if heartbeat.get("enabled") and heartbeat.get("intervalSec") else 0,
                "budget_cents": a.get("budget_monthly_cents") or 0,
                "skills": [k.split("/")[-1] for k in ((config.get("paperclipSkillSync") or {}).get("desiredSkills") or [])
                           if k.split("/")[-1] not in bundled],
                "permissions": pb.json_value(a.get("permissions"), {}) or {}})

        skills = []
        for s in mine("company_skills"):
            meta = pb.json_value(s.get("metadata"), {}) or {}
            if meta.get("sourceKind") in BUNDLED:
                continue
            files = {}
            folder = (root / "skills" / cid / s["slug"]) if root else None
            folder = folder if folder and folder.is_dir() else _local(s.get("source_locator"), root)
            if folder and Path(folder).is_dir():
                for path in sorted(Path(folder).rglob("*")):
                    if path.is_file() and not path.is_symlink() and path.stat().st_size < 10 * 1024 * 1024:
                        files[path.relative_to(folder).as_posix()] = path.read_bytes()
            if "SKILL.md" not in files and s.get("markdown"):
                files["SKILL.md"] = s["markdown"].encode()
            skills.append({"id": s["id"], "slug": s["slug"], "name": s.get("name") or s["slug"],
                           "description": s.get("description") or "", "source": s.get("source_locator") or "",
                           "source_kind": meta.get("sourceKind") or s.get("source_type") or "", "files": files})

        goals = [{"id": g["id"], "title": g["title"], "description": g.get("description") or "", "level": g.get("level") or "",
                  "status": g.get("status") or "active", "parent": g.get("parent_id"), "owner": g.get("owner_agent_id")}
                 for g in mine("goals")]
        project_goals = _by(mine("project_goals"), "project_id")
        workspaces = _by(mine("project_workspaces"), "project_id")
        projects = []
        for p in mine("projects"):
            spaces = sorted(workspaces.get(p["id"], []), key=lambda w: not w.get("is_primary"))
            primary = spaces[0] if spaces else {}
            folder = primary.get("cwd") or ""
            projects.append({"id": p["id"], "name": p["name"], "description": p.get("description") or "",
                             "status": p.get("status") or "", "lead": p.get("lead_agent_id"),
                             "goals": [x["goal_id"] for x in project_goals.get(p["id"], [])] + ([p["goal_id"]] if p.get("goal_id") else []),
                             "folder": folder if folder and Path(folder).is_dir() else "",
                             "recorded_folder": folder, "repo": primary.get("repo_url") or "",
                             "repo_ref": primary.get("repo_ref") or primary.get("default_ref") or "",
                             "archived": bool(p.get("archived_at"))})

        labels = {l["id"]: l["name"] for l in mine("labels")}
        issue_labels = _by(mine("issue_labels"), "issue_id")
        comments = _by(sorted(mine("issue_comments"), key=lambda c: c.get("created_at") or ""), "issue_id")
        relations = _by(mine("issue_relations"), "related_issue_id")
        docs = {d["id"]: d for d in mine("documents")}
        revisions = _by(sorted(mine("document_revisions"), key=lambda r: r.get("revision_number") or 0), "document_id")
        attached = set()

        def document(link):
            d = docs.get(link["document_id"])
            if not d:
                return None
            attached.add(d["id"])
            return {"key": link.get("key") or "", "title": d.get("title") or link.get("key") or "", "body": d.get("latest_body") or "",
                    "revisions": [{"number": r.get("revision_number"), "body": r.get("body") or "", "at": _when(r.get("created_at")),
                                   "by": who(r.get("created_by_agent_id"), r.get("created_by_user_id")), "note": r.get("change_summary") or ""}
                                  for r in revisions.get(d["id"], [])]}
        issue_docs = _by(mine("issue_documents"), "issue_id")
        storage = _storage_dir(root)
        assets = {a["id"]: a for a in mine("assets")}
        attachments = _by(mine("issue_attachments"), "issue_id")
        issues = []
        for i in mine("issues"):
            if i.get("hidden_at"):
                continue
            files = []
            for att in attachments.get(i["id"], []):
                asset = assets.get(att.get("asset_id"))
                path = storage / asset["object_key"] if storage and asset and asset.get("object_key") else None
                files.append({"name": (asset or {}).get("original_filename") or att.get("asset_id"), "path": str(path) if path and path.is_file() else ""})
            issues.append({
                "id": i["id"], "identifier": i.get("identifier") or "", "title": i["title"], "description": i.get("description") or "",
                "status": i.get("status") or "todo", "priority": i.get("priority") or "", "assignee": i.get("assignee_agent_id"),
                "parent": i.get("parent_id"), "project": i.get("project_id"), "goal": i.get("goal_id"),
                "labels": [labels[x["label_id"]] for x in issue_labels.get(i["id"], []) if x["label_id"] in labels],
                "blocked_by": [r["issue_id"] for r in relations.get(i["id"], []) if r.get("type") == "blocks"],
                "comments": [{"by": who(c.get("author_agent_id"), c.get("author_user_id")), "text": c.get("body") or "", "at": _when(c.get("created_at"))}
                             for c in comments.get(i["id"], []) if not c.get("deleted_at")],
                "documents": [d for d in (document(link) for link in issue_docs.get(i["id"], [])) if d],
                "attachments": files, "created_at": _when(i.get("created_at")),
                "finished_at": _when(i.get("completed_at") or i.get("cancelled_at")),
                "origin": i.get("origin_kind") or ""})

        triggers = _by(mine("routine_triggers"), "routine_id")
        routine_docs = _by(mine("routine_documents"), "routine_id")
        routines = [{"id": r["id"], "title": r["title"], "description": r.get("description") or "", "assignee": r.get("assignee_agent_id"),
                     "project": r.get("project_id"), "goal": r.get("goal_id"), "status": r.get("status") or "active",
                     "triggers": [{"kind": x.get("kind"), "cron": x.get("cron_expression"), "timezone": x.get("timezone"),
                                   "enabled": x.get("enabled") is not False and not x.get("archived"), "label": x.get("label") or ""}
                                  for x in triggers.get(r["id"], [])],
                     "documents": [d for d in (document(link) for link in routine_docs.get(r["id"], [])) if d]}
                    for r in mine("routines") if r.get("status") != "archived"]

        approval_comments = _by(mine("approval_comments"), "approval_id")
        approvals = [{"id": a["id"], "type": a.get("type") or "", "status": a.get("status") or "", "payload": pb.json_value(a.get("payload"), {}),
                      "note": a.get("decision_note") or "", "by": who(a.get("requested_by_agent_id"), a.get("requested_by_user_id")),
                      "at": _when(a.get("created_at")), "decided_at": _when(a.get("decided_at")),
                      "comments": [{"by": who(c.get("author_agent_id"), c.get("author_user_id")), "text": c.get("body") or "", "at": _when(c.get("created_at"))}
                                   for c in approval_comments.get(a["id"], [])]}
                     for a in mine("approvals")]

        costs = [{"ref": c["id"], "agent": c.get("agent_id"), "project": c.get("project_id"), "model": c.get("model") or "",
                  "provider": c.get("provider") or "", "cents": float(c.get("cost_cents") or 0), "at": _when(c.get("occurred_at"))}
                 for c in mine("cost_events")]
        budgets = [{"scope": b.get("scope_type"), "target": b.get("scope_id"), "cents": float(b.get("amount") or 0), "window": b.get("window_kind") or "",
                    "hard_stop": bool(b.get("hard_stop_enabled"))} for b in mine("budget_policies") if b.get("is_active")]

        secret_rows = [s for s in mine("company_secrets") if not s.get("deleted_at")]
        versions = {}
        for v in t("company_secret_versions"):
            if v.get("revoked_at"):
                continue
            if v["secret_id"] not in versions or (v.get("version") or 0) > (versions[v["secret_id"]].get("version") or 0):
                versions[v["secret_id"]] = v
        bindings = _by(mine("company_secret_bindings"), "secret_id")
        materials = {}
        for s in secret_rows:
            material = pb.json_value((versions.get(s["id"]) or {}).get("material"), None)
            if s.get("provider") == "local_encrypted" and isinstance(material, dict) and material.get("ciphertext"):
                materials[s["id"]] = material
        key = _master_key(root)
        values = decrypt(key, materials) if with_secret_values else {}
        secrets = [{"id": s["id"], "name": s["name"], "key": s.get("key") or s["name"], "description": s.get("description") or "",
                    "provider": s.get("provider") or "", "value": values.get(s["id"]),
                    "has_value": s["id"] in values if with_secret_values else bool(key) and s["id"] in materials,
                    "internal": any(b.get("target_type") == "routine" and str(b.get("config_path", "")).startswith("webhookSecret") for b in bindings.get(s["id"], [])),
                    "bindings": [{"type": b.get("target_type"), "target": b.get("target_id"), "path": b.get("config_path") or ""} for b in bindings.get(s["id"], [])]}
                   for s in secret_rows]

        activity = [{"actor": "you" if a.get("actor_type") == "user" else agent_names.get(a.get("actor_id")) or a.get("actor_type") or "system",
                     "action": a.get("action") or "", "entity": a.get("entity_type") or "", "details": pb.json_value(a.get("details"), {}) or {},
                     "at": _when(a.get("created_at"))}
                    for a in mine("activity_log") if not HIDDEN_ACTIONS.match(a.get("action") or "")]

        loose_docs = [{"key": d["id"], "title": d.get("title") or "Untitled", "body": d.get("latest_body") or "",
                       "revisions": [{"number": r.get("revision_number"), "body": r.get("body") or "", "at": _when(r.get("created_at"))}
                                     for r in revisions.get(d["id"], [])]}
                      for d in docs.values() if d["id"] not in attached]

        other = []
        for table, label in OTHER_TABLES.items():
            count = len([r for r in t(table) if r.get("company_id") == cid])
            if count:
                other.append({"kind": table, "label": label, "count": count})
        runs = len(mine("heartbeat_runs"))
        companies.append({
            "id": cid, "name": company["name"], "description": company.get("description") or "", "status": company.get("status") or "",
            "prefix": company.get("issue_prefix") or "", "budget_cents": company.get("budget_monthly_cents") or 0,
            "agents": agents, "skills": skills, "goals": goals, "projects": projects, "issues": issues, "routines": routines,
            "approvals": approvals, "costs": costs, "budgets": budgets, "secrets": secrets, "activity": activity,
            "documents": loose_docs, "other": other, "runs": runs})
    return {"source": "backup", "backup": str(backup), "backup_at": Path(backup).stat().st_mtime, "root": str(root) if root else "",
            "companies": companies, "read_at": time.time()}


def _storage_dir(root):
    if not root:
        return None
    configured = (((_config(root).get("storage") or {}).get("localDisk") or {}).get("baseDir"))
    path = Path(configured).expanduser() if configured else Path(root) / "data" / "storage"
    return path if path.is_dir() else None
