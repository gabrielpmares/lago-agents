"""Skills from a git repository (GitHub or any URL git can clone), pinned and reviewed first.

fetch() clones the repository (shallow) into a draft folder, pinned to its commit, finds the
skill (the folder holding SKILL.md, or `path` within the repo) and checks it against the
Agent Skills spec with validate_skills.py. Nothing is installed: the draft
waits for you, with its files, on the Swarm page. approve() copies the skill into
claude-skills/<name>/ with a `.source.json` (repo, commit, path), and links it into
~/.claude/skills the way install.sh does. Executable files, links and large files are refused.
"""

import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import activity
import config_store

DRAFTS_DIR = config_store.STATE_DIR / "skill-drafts"
SKILLS_DIR = config_store.DATA_DIR / "claude-skills"
LINK_DIR = Path.home() / ".claude" / "skills"
MAX_FILE = 512 * 1024
MAX_FILES = 200
IMPORT_MAX_FILE = 10 * 1024 * 1024    # the one-step import from Lago's GitHub list
IMPORT_MAX_FILES = 5000
IMPORT_MAX_TOTAL = 100 * 1024 * 1024
URL = re.compile(r"^(https://|git@|file://|/)[\w.@:/~+-]+$")


class SkillSourceError(Exception):
    pass


def _validator():
    spec = importlib.util.spec_from_file_location("validate_skills", config_store.APP_DIR / "validate_skills.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*args, cwd=None):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120,
                          env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin"})
    if proc.returncode:
        raise SkillSourceError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout.strip()


def _meta(draft_id):
    path = DRAFTS_DIR / str(draft_id) / "meta.json"
    if not path.exists():
        raise SkillSourceError(f"There's no skill draft #{draft_id}")
    return json.loads(path.read_text())


def _save(meta):
    (DRAFTS_DIR / str(meta["id"]) / "meta.json").write_text(json.dumps(meta, indent=2))


def drafts(status=None):
    if not DRAFTS_DIR.exists():
        return []
    out = [_meta(p.name) for p in sorted(DRAFTS_DIR.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0)
           if p.name.isdigit() and (p / "meta.json").exists()]
    return [m for m in out if status is None or m["status"] == status]


def fetch(repo, path="", ref=""):
    """Clone, pin and check a skill. Returns the draft (status ready)."""
    repo = (repo or "").strip()
    if not URL.match(repo):
        raise SkillSourceError("Give the repository as an https:// or git@ URL")
    if repo.startswith("https://github.com/") and not repo.endswith(".git"):
        repo = repo.rstrip("/")
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    draft_id = max([int(p.name) for p in DRAFTS_DIR.iterdir() if p.name.isdigit()], default=0) + 1
    folder = DRAFTS_DIR / str(draft_id)
    clone = folder / "repo"
    folder.mkdir()
    try:
        _git("clone", "--depth", "1", *(["--branch", ref] if ref else []), repo, str(clone))
        commit = _git("rev-parse", "HEAD", cwd=clone)
        skill_dir = _find_skill(clone, path)
        files = _check_files(skill_dir)
        validator = _validator()
        # Checked under the name it declares, so a skill at the repository's root passes too.
        name = (validator.frontmatter((skill_dir / "SKILL.md").read_text()) or {}).get("name", "")
        if not validator.NAME_PATTERN.match(name):
            raise SkillSourceError(f"Its SKILL.md names it {name!r}, which isn't a valid skill name")
        staged = folder / "stage" / name
        shutil.copytree(skill_dir, staged, ignore=shutil.ignore_patterns(".git"))
        errors, warnings = validator.check(staged)
        if errors:
            raise SkillSourceError("It doesn't meet the Agent Skills spec: " + "; ".join(errors))
        if (SKILLS_DIR / name).exists() or (LINK_DIR / name).exists():
            raise SkillSourceError(f"There's already a skill called {name!r}")
        staged.rename(folder / "skill")
        shutil.rmtree(folder / "stage")
        shutil.rmtree(clone)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    meta = {"id": draft_id, "name": name, "repo": repo, "commit": commit,
            "path": skill_dir.resolve().relative_to(clone.resolve()).as_posix() if skill_dir.resolve() != clone.resolve() else "",
            "files": files, "warnings": warnings, "status": "ready", "created_at": time.time()}
    _save(meta)
    activity.log(activity.actor(), "skill.fetched", f"{name} from {repo}@{commit[:10]} (draft #{draft_id})")
    return meta


def _find_skill(clone, path):
    if path:
        target = (clone / path).resolve()
        if clone.resolve() not in target.parents and target != clone.resolve():
            raise SkillSourceError("The path must stay inside the repository")
        if not (target / "SKILL.md").exists():
            raise SkillSourceError(f"There's no SKILL.md in {path}")
        return target
    found = [p.parent for p in clone.rglob("SKILL.md") if ".git" not in p.parts]
    if not found:
        raise SkillSourceError("The repository has no SKILL.md")
    if len(found) > 1:
        raise SkillSourceError("The repository has several skills; say which with `path`: "
                               + ", ".join(sorted(p.relative_to(clone).as_posix() or "." for p in found))[:500])
    return found[0]


def _check_files(skill_dir, max_file=MAX_FILE, max_files=MAX_FILES, max_total=None,
                 allow_exec=False):
    """The skill's files, after refusing links, big files and (unless `allow_exec`) scripts
    marked executable. The one-step import (install_from) allows bigger skills: a reference
    library of a thousand images is data, not instructions."""
    files, total = [], 0
    for p in sorted(skill_dir.rglob("*")):
        if ".git" in p.parts:
            continue
        if p.is_symlink():
            raise SkillSourceError(f"{p.name} is a link; skills may only hold plain files")
        if p.is_file():
            size = p.stat().st_size
            if size > max_file:
                raise SkillSourceError(f"{p.relative_to(skill_dir)} is larger than "
                                       f"{max_file // (1024 * 1024) or max_file // 1024} "
                                       f"{'MB' if max_file >= 1024 * 1024 else 'KB'}")
            if p.stat().st_mode & 0o111 and not allow_exec:
                raise SkillSourceError(f"{p.relative_to(skill_dir)} is executable; review scripts by "
                                       "hand instead of installing them from here")
            total += size
            files.append(p.relative_to(skill_dir).as_posix())
    if len(files) > max_files:
        raise SkillSourceError(f"More than {max_files} files")
    if max_total and total > max_total:
        raise SkillSourceError(f"More than {max_total // (1024 * 1024)} MB in all")
    return files


BINS = re.compile(r"^(\s*)bins:\s*(\[[^\]]*\])?\s*$", re.M)


def programs_needed(skill_md):
    """The programs a SKILL.md says it needs (`requires: bins:` in its frontmatter, the
    convention skill packs use), in the order given."""
    head = skill_md.split("\n---", 1)[0] if skill_md.startswith("---") else ""
    found = []
    for match in BINS.finditer(head):
        if match.group(2):  # bins: [gws, jq]
            found += [b.strip(" '\"") for b in match.group(2)[1:-1].split(",")]
            continue
        for line in head[match.end():].splitlines()[1:]:  # bins:\n  - gws
            item = re.match(r"^\s*-\s*([\w.+-]+)\s*$", line)
            if not item:
                break
            found.append(item.group(1))
    return [b for b in dict.fromkeys(found) if b]


def missing_programs(skill_dir):
    """The programs a skill needs that aren't installed here."""
    try:
        text = (Path(skill_dir) / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return []
    return [b for b in programs_needed(text) if not shutil.which(b)]


def install_from(skill_dir, repo, commit, path=""):
    """Check a skill folder from a clone and install it straight away, with no draft
    (ghimport.py's one-step import). Returns its name."""
    skill_dir = Path(skill_dir)
    _check_files(skill_dir, max_file=IMPORT_MAX_FILE, max_files=IMPORT_MAX_FILES,
                 max_total=IMPORT_MAX_TOTAL, allow_exec=True)
    validator = _validator()
    name = (validator.frontmatter((skill_dir / "SKILL.md").read_text()) or {}).get("name", "")
    if not validator.NAME_PATTERN.match(name):
        raise SkillSourceError(f"its SKILL.md names it {name!r}, which isn't a valid skill name")
    target, link = SKILLS_DIR / name, LINK_DIR / name
    if target.exists() or link.exists():
        raise SkillSourceError(f"there's already a skill called {name!r}")
    with tempfile.TemporaryDirectory(prefix="skill-") as folder:
        staged = Path(folder) / name
        shutil.copytree(skill_dir, staged, ignore=shutil.ignore_patterns(".git"))
        errors, _ = validator.check(staged)
        if errors:
            raise SkillSourceError("it doesn't meet the Agent Skills spec: " + "; ".join(errors))
        shutil.copytree(staged, target)
    for file in target.rglob("*"):  # scripts stay readable, but nothing runs just by being there
        if file.is_file() and file.stat().st_mode & 0o111:
            file.chmod(file.stat().st_mode & ~0o111)
    needs = programs_needed((target / "SKILL.md").read_text(encoding="utf-8"))
    (target / ".source.json").write_text(json.dumps({"repo": repo, "commit": commit, "path": path,
                                                     "needs": needs}, indent=2))
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    activity.log(activity.actor(), "skill.installed", f"{name} from {repo}@{commit[:10]}")
    return name


def read(draft_id):
    """The draft with its files' text, for review."""
    meta = _meta(draft_id)
    folder = DRAFTS_DIR / str(draft_id) / "skill"
    texts = {}
    for name in meta["files"]:
        try:
            texts[name] = (folder / name).read_text()[:20000]
        except (UnicodeDecodeError, OSError):
            texts[name] = "(not text)"
    return {**meta, "texts": texts}


def decide(draft_id, approve):
    meta = _meta(draft_id)
    if meta["status"] != "ready":
        raise SkillSourceError(f"Skill draft #{draft_id} is {meta['status']}")
    if not approve:
        meta["status"] = "rejected"
        _save(meta)
        return "Rejected. Nothing was installed."
    target, link = SKILLS_DIR / meta["name"], LINK_DIR / meta["name"]
    if target.exists() or link.exists():
        raise SkillSourceError(f"There's already a skill called {meta['name']!r}")
    shutil.copytree(DRAFTS_DIR / str(draft_id) / "skill", target)
    (target / ".source.json").write_text(json.dumps({k: meta[k] for k in ("repo", "commit", "path")}, indent=2))
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    meta["status"] = "installed"
    _save(meta)
    activity.log(activity.actor(), "skill.installed", f"{meta['name']} from {meta['repo']}@{meta['commit'][:10]}")
    return f"Installed the skill {meta['name']}: every agent can load it now."
