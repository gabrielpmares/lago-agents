"""Secrets hidden on screen (redact.py): known values and shapes of secrets, nothing else."""

import json
import unittest
from unittest import mock

import support

import config_store
import redact
import transcripts


class RedactTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        redact.forget()

    def test_shapes_of_secrets(self):
        cases = {
            "key sk-ant-api03-" + "a" * 40: "key [hidden]",
            "ghp_" + "b" * 36: "[hidden]",
            "AWS AKIAABCDEFGHIJKLMNOP": "AWS [hidden]",
            "Authorization: Bearer abcdefghijklmnop1234": "Authorization: Bearer [hidden]",
            "postgres://app:s3cretpw@db:5432/x": "postgres://app:[hidden]@db:5432/x",
            "STRIPE_SECRET=sk_live_" + "c" * 24: "STRIPE_SECRET=[hidden]",
            'password: "hunter22x"': 'password: "[hidden]"',
        }
        for given, shown in cases.items():
            self.assertEqual(redact.text(given), shown, given)

    def test_ordinary_text_and_code_stay(self):
        for line in ("author: Sam", "max_tokens: 4096", "const token = getToken()",
                     "token = request.headers", "12.3k tokens", "NODE_ENV=production",
                     "https://login.tailscale.com/a/1675aedf0132b2"):
            self.assertEqual(redact.text(line), line)

    def test_known_values_from_env_files(self):
        (support.TMP / ".env").write_text("NODE_ENV=production\nDB_PASS=plainwords\nX=Abc123def456ghi789jkl0\n")
        config = config_store.load()
        config["projects"]["demo"]["cwd"] = str(support.TMP)
        config_store.save(config)
        redact.forget()
        self.assertEqual(redact.text("it is plainwords and Abc123def456ghi789jkl0 in production"),
                         "it is [hidden] and [hidden] in production")

    def test_transcripts_shown_are_hidden_but_can_be_switched_off(self):
        path = support.TMP / "t.jsonl"
        path.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Your key is ghp_" + "d" * 36}]}}) + "\n")
        self.assertIn("[hidden]", json.dumps(transcripts.read(path)["events"]))
        config = config_store.load()
        config["redact"] = False
        config_store.save(config)
        self.assertNotIn("[hidden]", json.dumps(transcripts.read(path)["events"]))

    def test_cached_transcript_hides_new_lines_and_newly_known_secrets(self):
        path = support.TMP / "grow.jsonl"
        line = lambda text: json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": text}]}}) + "\n"
        path.write_text(line("The value is Zq81mWp4Xv62Lr90Ty37"))
        self.assertNotIn("[hidden]", json.dumps(transcripts.read(path)["events"]))
        with open(path, "a") as file:
            file.write(line("Your key is ghp_" + "e" * 36))
        events = transcripts.read(path)["events"]
        self.assertEqual(len(events), 2)
        self.assertIn("[hidden]", events[1]["text"])
        (support.TMP / ".env").write_text("X=Zq81mWp4Xv62Lr90Ty37\n")
        config = config_store.load()
        config["projects"] = {"p": {"name": "P", "cwd": str(support.TMP)}}
        config_store.save(config)
        redact.forget()  # the secret is known now: the earlier line is hidden too
        self.assertIn("[hidden]", transcripts.read(path)["events"][0]["text"])


if __name__ == "__main__":
    unittest.main()
