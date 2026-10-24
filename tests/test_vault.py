"""Secrets vault (#29): values only in the keyring (faked here), handed to the right agents."""

import subprocess
import sys
import unittest
from unittest import mock

import support

import config_store
import hub
import vault


class FakeKeyring:
    def __init__(self):
        self.items = {}

    def store(self, name, project, value):
        self.items[(name, project or "-")] = value

    def lookup(self, name, project):
        return self.items.get((name, project or "-"))

    def clear(self, name, project):
        self.items.pop((name, project or "-"), None)


class VaultTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.keyring = FakeKeyring()
        p = mock.patch.object(vault, "_backend", self.keyring)
        p.start()
        self.addCleanup(p.stop)
        ws = hub.create_workspace("demo", None, "task")
        hub.add_swarm_agent(f"w{ws}-impl", ws, "demo", "implementer", "Impl")
        self.agent = f"w{ws}-impl"

    def test_values_stay_in_the_keyring(self):
        vault.save("stripe", "stripe_key", "sk_live_123", project="demo")
        self.assertEqual(self.keyring.items[("stripe", "demo")], "sk_live_123")
        rows = vault.secrets()
        self.assertEqual(rows[0]["env"], "STRIPE_KEY")
        self.assertNotIn("sk_live_123", str(rows))
        with hub.connect() as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn("sk_live_123", dump)

    def test_agents_get_their_projects_secrets_and_general_ones(self):
        vault.save("stripe", "STRIPE_KEY", "sk_demo", project="demo")
        vault.save("other", "OTHER_KEY", "nope", project="elsewhere")
        vault.save("openai", "OPENAI_API_KEY", "general")
        vault.save("stripe-default", "STRIPE_KEY", "sk_general")
        env = vault.env_for(self.agent)
        self.assertEqual(env, {"STRIPE_KEY": "sk_demo", "OPENAI_API_KEY": "general"})
        self.assertEqual({a["agent_id"] for a in vault.access_log()}, {self.agent})

    def test_rules_and_remove(self):
        with self.assertRaises(vault.VaultError):
            vault.save("Bad Name", "X", "v")
        with self.assertRaises(vault.VaultError):
            vault.save("ok", "lower-case", "v")
        vault.save("a", "SAME", "1", project="demo")
        with self.assertRaisesRegex(vault.VaultError, "already the variable"):
            vault.save("b", "SAME", "2", project="demo")
        vault.remove("a", project="demo")
        self.assertEqual(self.keyring.items, {})
        with self.assertRaises(vault.VaultError):
            vault.remove("a", project="demo")

    def test_shell_quoting(self):
        self.assertEqual(vault._shell_quote("it's"), "'it'\\''s'")
        value = 'a b $HOME `id` "q" \'s'
        line = f"export X={vault._shell_quote(value)}"
        # Exactly as the launch script reads it: eval "$(vault.py --env <agent>)".
        out = subprocess.run(["bash", "-c", 'eval "$(cat)"; printf %s "$X"'], input=line + "\n",
                             capture_output=True, text=True).stdout
        self.assertEqual(out, value)


if __name__ == "__main__":
    unittest.main()
