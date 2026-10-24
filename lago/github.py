"""GitHub for a project, through the `gh` CLI (and your existing `gh auth login`).

Repositories: open pull requests, issues and recent workflow runs. Boards: GitHub
Projects (v2) items grouped by their Status field. Results are cached briefly, since
the app polls and gh calls take a second or two.
"""

import json
import re
import subprocess
import time
from pathlib import Path

BOARD_LIMIT = 500

REPO = re.compile(r"^(?:https?://)?(?:www\.)?(?:github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


class GitHubError(Exception):
    """A problem worth showing as-is."""


def _gh(*args, timeout=30):
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as error:
        raise GitHubError("The GitHub CLI (gh) isn't installed") from error
    except subprocess.TimeoutExpired as error:
        raise GitHubError("GitHub took too long to answer") from error
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip().splitlines()
        raise GitHubError(message[-1] if message else "gh failed")
    return json.loads(proc.stdout or "null")


def repo_name(text):
    match = REPO.match((text or "").strip())
    return match.group(1) if match else None


def repos(project):
    """owner/name of every repository the project uses: its own, extra ones, its areas'."""
    names = [repo_name(project.get("repo"))]
    names += [repo_name(r) for r in (project.get("github") or {}).get("repos", [])]
    names += [repo_name(a.get("repo")) for a in (project.get("areas") or {}).values()]
    return list(dict.fromkeys(n for n in names if n))


def boards(project):
    return [b for b in (project.get("github") or {}).get("boards", []) if b.get("owner") and b.get("number")]


def repo_activity(repo, refresh=False):
    import cache
    result = cache.get(f"github:repo:{repo}", lambda: _repo_activity(repo), fresh=120, keep=86400, refresh=refresh)
    return {**result["value"], "cache": cache.meta(result)}


def _repo_activity(repo):
    if True:
        prs = _gh("pr", "list", "-R", repo, "--limit", "30", "--json",
                  "number,title,author,updatedAt,isDraft,url,headRefName,reviewDecision")
        issues = _gh("issue", "list", "-R", repo, "--limit", "50", "--json",
                     "number,title,labels,assignees,updatedAt,url")
        try:
            runs = _gh("run", "list", "-R", repo, "--limit", "8", "--json",
                       "displayTitle,status,conclusion,headBranch,createdAt,url,workflowName")
        except GitHubError:
            runs = []  # Actions may be off for this repo
        return {
            "repo": repo,
            "prs": [{**p, "author": (p.get("author") or {}).get("login", "")} for p in prs],
            "issues": [{**i, "labels": [l["name"] for l in i.get("labels", [])],
                        "assignees": [a["login"] for a in i.get("assignees", [])]} for i in issues],
            "runs": runs,
        }


def item_text(repo, number):
    """One issue or pull request: its state, body and latest comments (and a PR's checks)."""
    data = _gh("api", f"repos/{repo}/issues/{int(number)}")
    is_pr = "pull_request" in data
    lines = [f"# {repo}#{number} {data['title']} ({'PR' if is_pr else 'issue'}, {data['state']})",
             f"https://github.com/{repo}/{'pull' if is_pr else 'issues'}/{number} · by "
             f"{(data.get('user') or {}).get('login', '')} · updated {data.get('updated_at', '')[:10]}"]
    labels = [l["name"] for l in data.get("labels") or []]
    assignees = [a["login"] for a in data.get("assignees") or []]
    if labels or assignees:
        lines.append(", ".join(x for x in (", ".join(labels), "assigned " + ", ".join(assignees)
                                           if assignees else "") if x))
    if is_pr:
        pr = _gh("pr", "view", str(number), "-R", repo, "--json",
                 "isDraft,mergeable,reviewDecision,headRefName,statusCheckRollup")
        checks = pr.get("statusCheckRollup") or []
        failed = [c.get("name") or c.get("context") for c in checks
                  if (c.get("conclusion") or c.get("state")) in ("FAILURE", "ERROR", "CANCELLED")]
        lines.append(f"{pr['headRefName']}{', draft' if pr['isDraft'] else ''}, "
                     f"{pr['mergeable'].lower()}, review {pr.get('reviewDecision') or 'none'}, "
                     f"checks {len(checks) - len(failed)}/{len(checks)} ok"
                     + (f" (failed: {', '.join(failed)})" if failed else ""))
    body = (data.get("body") or "").strip()
    lines += ["", body[:1500] + ("…" if len(body) > 1500 else "") if body else "(no description)"]
    if data.get("comments"):
        comments = _gh("api", f"repos/{repo}/issues/{int(number)}/comments?per_page=100")[-3:]
        lines += ["", f"## Last {len(comments)} of {data['comments']} comments"]
        lines += [f"- {c['user']['login']} {c['created_at'][:10]}: "
                  + " ".join(c["body"].split())[:400] for c in comments]
    return "\n".join(lines)


LIST_QUERY = """query($owner:String!,$number:Int!,$cursor:String){ %s(login:$owner){ projectV2(number:$number){
  title url fields(first:50){ nodes{ ... on ProjectV2SingleSelectField { name options { name } } } }
  items(first:100, after:$cursor){ pageInfo{ hasNextPage endCursor } nodes{ id updatedAt
    content{ ... on Issue{updatedAt} ... on PullRequest{updatedAt} ... on DraftIssue{updatedAt} } } } } } }"""
DETAIL_QUERY = (Path(__file__).resolve().parent / "github_items.graphql").read_text()


def _graphql(query, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        if isinstance(value, list):
            args += [a for v in value for a in ("-f", f"{name}[]={v}")]
        elif value is not None:
            args += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
    return _gh(*args, timeout=60)["data"]


def _item(node):
    content = node.get("content") or {}
    fields = {f["field"]["name"]: f.get("name") or f.get("title") or f.get("number") or f.get("text")
              for f in node["fieldValues"]["nodes"] if f.get("field")}
    return {"id": node["id"], "title": content.get("title") or fields.get("Title", ""),
            "status": fields.get("Status") or "No status", "priority": fields.get("Priority") or "",
            "assignees": [a["login"] for a in (content.get("assignees") or {}).get("nodes", [])],
            "labels": [l["name"] for l in (content.get("labels") or {}).get("nodes", [])],
            "sprint": fields.get("Sprint") or "", "type": content.get("__typename", ""),
            "number": content.get("number"), "repo": (content.get("repository") or {}).get("nameWithOwner", ""),
            "url": content.get("url", ""), "body": (content.get("body") or "")[:6000]}


def _sync_board(owner, number, previous):
    """Fetch only what changed: list every item's timestamps (cheap), then details for
    new or changed items. `previous` is the last synced board, or None."""
    listing, stamps, cursor = None, {}, None
    for kind in ("organization", "user"):
        try:
            while True:
                data = _graphql(LIST_QUERY % kind, owner=owner, number=int(number), cursor=cursor)[kind]["projectV2"]
                listing = listing or data
                for n in data["items"]["nodes"]:
                    stamps[n["id"]] = f"{n['updatedAt']}|{(n.get('content') or {}).get('updatedAt', '')}"
                if not data["items"]["pageInfo"]["hasNextPage"]:
                    break
                cursor = data["items"]["pageInfo"]["endCursor"]
            break
        except (GitHubError, TypeError, KeyError):
            if kind == "user":
                raise
    known = {i["id"]: i for i in (previous or {}).get("items", [])}
    old_stamps = (previous or {}).get("stamps", {})
    changed = [i for i, stamp in stamps.items() if old_stamps.get(i) != stamp or i not in known]
    for start in range(0, len(changed), 50):
        for node in _graphql(DETAIL_QUERY, ids=changed[start:start + 50])["nodes"]:
            if node:
                known[node["id"]] = _item(node)
    statuses = next((f["options"] for f in listing["fields"]["nodes"] if f.get("name") == "Status"), [])
    return {"owner": owner, "number": int(number), "title": listing["title"], "url": listing["url"],
            "statuses": [o["name"] for o in statuses], "stamps": stamps,
            "items": [known[i] for i in stamps if i in known], "last_changed": len(changed)}


def board(owner, number, refresh=False):
    """A GitHub Project board from the persistent cache, synced incrementally."""
    import cache
    key = f"github:board:{owner}:{int(number)}"
    previous, _ = cache.read(key)
    result = cache.get(key, lambda: _sync_board(owner, number, cache.read(key)[0] or previous),
                       fresh=120, keep=7 * 86400, refresh=refresh)
    board_data = {k: v for k, v in result["value"].items() if k != "stamps"}
    return {**board_data, "cache": cache.meta(result)}


def board_text(owner, number, status=""):
    """A board as text for the Conductor: open items grouped by status."""
    data = board(owner, number)
    lines = [f"# {data['title']} ({data['url']})"]
    order = data["statuses"] + sorted({i["status"] for i in data["items"]} - set(data["statuses"]))
    for column in order:
        if status and column.lower() != status.lower():
            continue
        if not status and column.lower() == "done":
            continue
        items = [i for i in data["items"] if i["status"] == column]
        if not items:
            continue
        lines += ["", f"## {column} ({len(items)})"]
        for i in items:
            ref = f"{i['repo']}#{i['number']}" if i["number"] else "draft"
            extra = ", ".join(x for x in (i["priority"], i["sprint"], ", ".join(i["assignees"])) if x)
            lines.append(f"- {i['title']} [{ref}]" + (f" ({extra})" if extra else ""))
    return "\n".join(lines)
