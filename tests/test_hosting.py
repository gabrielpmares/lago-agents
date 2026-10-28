"""Hosted Lago (hosting/): only requests the host's proxy signed for this container get in,
and the vault keeps its values in a file only the container's key opens."""

import http.client
import json
import threading
import time
import unittest
from unittest import mock

import support

import app_server
import vault


class HostedServerTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        with mock.patch("background.start_loops"):
            self.server = app_server.Server(0, "127.0.0.1", ["alice.lago.test"], "proxy-secret")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, headers=None, host="alice.lago.test"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request("GET", "/", headers={"Host": host, **(headers or {})})
        status = conn.getresponse().status
        conn.close()
        return status

    def signed(self, user="alice", at=None, secret="proxy-secret"):
        at = str(int(time.time()) if at is None else at)
        return {"X-Lago-User": user, "X-Lago-Time": at, "X-Lago-Proof": app_server.proxy_proof(secret, user, at)}

    def test_only_what_the_proxy_signed_just_now_gets_in(self):
        self.assertEqual(self.get(self.signed()), 200)
        self.assertEqual(self.get(), 403)
        self.assertEqual(self.get(self.signed(at=time.time() - 600)), 403)  # an old signature replayed
        self.assertEqual(self.get(self.signed(secret="another-containers-secret")), 403)
        self.assertEqual(self.get({**self.signed(), "X-Lago-User": "bob"}), 403)
        self.assertEqual(self.get(self.signed(), host="bob.lago.test"), 421)

    def test_the_window_secret_doesnt_open_a_hosted_lago(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request("GET", f"/?t={self.server.token}", headers={"Host": "alice.lago.test"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()


class FileVaultTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.addCleanup(setattr, vault, "_backend", vault._backend)
        vault._backend = vault.FileBackend("container-key")
        self.addCleanup(vault.backend().path.unlink, missing_ok=True)

    def test_values_are_kept_encrypted_and_only_the_key_opens_them(self):
        vault.save("stripe", "STRIPE_KEY", "sk_test_123")
        self.assertEqual(vault.backend().lookup("stripe", ""), "sk_test_123")
        self.assertNotIn(b"sk_test_123", vault.backend().path.read_bytes())
        self.assertEqual(vault.backend().path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(vault.VaultError):
            vault.FileBackend("someone-elses-key").lookup("stripe", "")
        vault.remove("stripe")
        self.assertIsNone(vault.backend().lookup("stripe", ""))
        self.assertEqual(json.loads(vault.backend().fernet.decrypt(vault.backend().path.read_bytes())), {})


if __name__ == "__main__":
    unittest.main()
