"""Reviewing and merging pull requests under a project's policy (#30). GitHub is faked."""

import unittest
from unittest import mock

import support

import config_store
import github
import pull_requests

REPO = "acme/shop"


class FakeGitHub:
    def __init__(self):
        self.pr = {"headRefOid": "a" * 40, "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
                   "reviewDecision": "", "isDraft": False, "mergeable": "MERGEABLE", "state": "OPEN",
                   "title": "Fix cart", "url": "https://github.com/acme/shop/pull/7"}
        self.calls = []

    def gh(self, *args, timeout=30):
        return dict(self.pr)

    def text(self, *args):
        self.calls.append(args)
        return ""


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        config = config_store.load()
        config["projects"]["demo"]["github"] = {"repos": [REPO], "boards": []}
        config["projects"]["demo"]["merge_policy"] = {"enabled": True, "who": "human"}
        config_store.save(config)
        self.fake = FakeGitHub()
        patches = [mock.patch.object(github, "_gh", side_effect=self.fake.gh),
                   mock.patch.object(github, "repos", side_effect=lambda p: (p.get("github") or {}).get("repos", [])),
                   mock.patch.object(pull_requests, "_gh_text", side_effect=self.fake.text)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def set_policy(self, **rules):
        config = config_store.load()
        config["projects"]["demo"]["merge_policy"] = {"enabled": True, **rules}
        config_store.save(config)

    def test_needs_an_approval_on_the_current_commit(self):
        allowed, reasons, _ = pull_requests.check(REPO, 7)
        self.assertFalse(allowed)
        self.assertIn("no approval on its current commit", reasons)
        pull_requests.review(REPO, 7, "approve", "Looks right", "code-reviewer")
        self.assertTrue(pull_requests.check(REPO, 7)[0])
        self.assertIn("Approved", self.fake.calls[0][-1])
        self.fake.pr["headRefOid"] = "b" * 40  # a new push: the approval doesn't carry over
        self.assertFalse(pull_requests.check(REPO, 7)[0])

    def test_changes_after_approval_block(self):
        pull_requests.review(REPO, 7, "approve", "ok", "code-reviewer")
        pull_requests.review(REPO, 7, "changes", "found a bug", "security-auditor")
        self.assertFalse(pull_requests.check(REPO, 7)[0])

    def test_failing_checks_drafts_conflicts(self):
        self.fake.pr["reviewDecision"] = "APPROVED"
        self.fake.pr["statusCheckRollup"] = [{"name": "ci", "conclusion": "FAILURE"}]
        self.assertIn("checks not green: ci", pull_requests.check(REPO, 7)[1])
        self.fake.pr["statusCheckRollup"] = []
        self.assertIn("it has no CI checks to pass", pull_requests.check(REPO, 7)[1])
        self.set_policy(require_checks=False)
        self.fake.pr.update(isDraft=True, mergeable="CONFLICTING")
        self.assertEqual(set(pull_requests.check(REPO, 7)[1]), {"it's a draft", "it has merge conflicts"})

    def test_who_may_merge(self):
        self.fake.pr["reviewDecision"] = "APPROVED"
        with self.assertRaisesRegex(pull_requests.PullRequestError, "only the human"):
            pull_requests.merge(REPO, 7, by="w3")
        pull_requests.merge(REPO, 7)
        self.assertEqual(self.fake.calls[-1][:6], ("pr", "merge", "7", "-R", REPO, "--squash"))
        self.assertIn("--match-head-commit", self.fake.calls[-1])
        self.set_policy(who="conductor", method="rebase")
        pull_requests.merge(REPO, 7, by="w3")
        self.assertIn("--rebase", self.fake.calls[-1])

    def test_off_by_default_and_unknown_repos(self):
        config = config_store.load()
        del config["projects"]["demo"]["merge_policy"]
        config_store.save(config)
        self.fake.pr["reviewDecision"] = "APPROVED"
        self.assertIn("merging from Lago Agents is off for this project (Settings → merge policy)",
                      pull_requests.check(REPO, 7)[1])
        with self.assertRaises(pull_requests.PullRequestError):
            pull_requests.check("someone/else", 1)


if __name__ == "__main__":
    unittest.main()
