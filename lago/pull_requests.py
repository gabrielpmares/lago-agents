"""Reviewing and merging pull requests under a policy you set per project.

Agents review a PR through `review()`: the verdict (approve, changes or comment) is posted on
the PR as a comment signed with the reviewer's name (GitHub doesn't let an account approve its
own PRs, and agents act as you) and recorded here. `merge()` merges only when the project's
policy allows it, checked live against GitHub:

    "merge_policy": {"enabled": true, "require_checks": true, "require_approval": true,
                     "method": "squash", "who": "human" | "conductor"}

- require_checks: every CI check finished green;
- require_approval: GitHub's review decision is APPROVED, or a Lago reviewer approved the
  PR's current head commit (an approval doesn't carry over to new pushes);
- who: "human" (the default) lets only you merge, from the app; "conductor" lets the session's
  Conductor merge too once the rest passes. Drafts and conflicting PRs never merge.
Every review and merge goes to the activity history.
"""

import subprocess
import time

import activity
import config_store
import github
import hub

DEFAULT_POLICY = {"enabled": False, "require_checks": True, "require_approval": True,
                  "method": "squash", "who": "human"}
METHODS = ("squash", "merge", "rebase")
VERDICTS = ("approve", "changes", "comment")


class PullRequestError(Exception):
    pass


def policy(project):
    return {**DEFAULT_POLICY, **(project.get("merge_policy") or {})}


def _project_of(repo):
    for pid, project in config_store.load()["projects"].items():
        if repo in github.repos(project):
            return pid, project
    raise PullRequestError(f"{repo} isn't one of your projects' repositories")


def _gh_json(*args):
    try:
        return github._gh(*args)
    except github.GitHubError as error:
        raise PullRequestError(str(error)) from error


def _gh_text(*args):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        raise PullRequestError(lines[-1] if lines else "gh failed")
    return proc.stdout.strip()


def state(repo, number):
    """What GitHub says now: head commit, checks, review decision, draft, mergeable."""
    data = _gh_json("pr", "view", str(number), "-R", repo, "--json",
                    "headRefOid,statusCheckRollup,reviewDecision,isDraft,mergeable,state,title,url")
    checks = data.get("statusCheckRollup") or []
    def outcome(c):
        return (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
    failing = [c.get("name") or c.get("context") or "?" for c in checks
               if outcome(c) not in ("SUCCESS", "NEUTRAL", "SKIPPED")]
    return {"head": data["headRefOid"], "checks": len(checks), "failing": failing,
            "review_decision": data.get("reviewDecision") or "", "draft": data.get("isDraft"),
            "mergeable": data.get("mergeable"), "state": data.get("state"), "title": data.get("title"),
            "url": data.get("url")}


def review(repo, number, verdict, body, reviewer):
    """Post a reviewer's verdict on the PR and record it against the head commit."""
    if verdict not in VERDICTS:
        raise PullRequestError(f"verdict must be one of: {', '.join(VERDICTS)}")
    body = (body or "").strip()
    if not body:
        raise PullRequestError("Say what you found")
    _project_of(repo)
    now = state(repo, number)
    heading = {"approve": "✅ Approved", "changes": "🔁 Changes requested", "comment": "💬 Review"}[verdict]
    _gh_text("pr", "comment", str(number), "-R", repo, "--body",
             f"**{heading}** by `{reviewer}` (Lago Agents), on {now['head'][:10]}\n\n{body}")
    with hub.connect() as conn:
        conn.execute("INSERT INTO pr_reviews (repo, number, head, reviewer, verdict, body, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (repo, int(number), now["head"], reviewer, verdict,
                                                     body[:4000], time.time()))
    activity.log(reviewer, f"pr.{verdict}", f"{repo}#{number} {now['title']}: {body.splitlines()[0][:200]}",
                 target=f"{repo}#{number}")
    return {"ok": True, "head": now["head"]}


def _lago_approved(repo, number, head):
    with hub.connect() as conn:
        rows = conn.execute("SELECT verdict FROM pr_reviews WHERE repo = ? AND number = ? AND head = ? "
                            "ORDER BY id", (repo, int(number), head)).fetchall()
    verdicts = [r["verdict"] for r in rows]
    return bool(verdicts) and "approve" in verdicts and verdicts[-1] != "changes"


def check(repo, number, by="you"):
    """(allowed, reasons, state): whether `by` may merge this PR now under its project's policy."""
    _, project = _project_of(repo)
    rules = policy(project)
    now = state(repo, number)
    reasons = []
    if not rules["enabled"]:
        reasons.append("merging from Lago Agents is off for this project (Settings → merge policy)")
    if by != "you" and rules["who"] != "conductor":
        reasons.append("the policy lets only the human merge")
    if now["state"] != "OPEN":
        reasons.append(f"the PR is {now['state'].lower()}")
    if now["draft"]:
        reasons.append("it's a draft")
    if now["mergeable"] == "CONFLICTING":
        reasons.append("it has merge conflicts")
    if rules["require_checks"]:
        if now["failing"]:
            reasons.append(f"checks not green: {', '.join(now['failing'][:5])}")
        elif not now["checks"]:
            reasons.append("it has no CI checks to pass")
    if rules["require_approval"] and now["review_decision"] != "APPROVED" \
            and not _lago_approved(repo, number, now["head"]):
        reasons.append("no approval on its current commit")
    return not reasons, reasons, now


def merge(repo, number, by="you"):
    allowed, reasons, now = check(repo, number, by)
    if not allowed:
        raise PullRequestError(f"{repo}#{number} can't be merged: " + "; ".join(reasons))
    _, project = _project_of(repo)
    method = policy(project)["method"]
    if method not in METHODS:
        method = "squash"
    _gh_text("pr", "merge", str(number), "-R", repo, f"--{method}", "--match-head-commit", now["head"])
    activity.log(by, "pr.merged", f"{repo}#{number} {now['title']} ({method})", target=f"{repo}#{number}")
    return {"ok": True, "message": f"Merged {repo}#{number} ({method})."}


def reviews(repo, number):
    with hub.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pr_reviews WHERE repo = ? AND number = ? ORDER BY id",
                                              (repo, int(number)))]
