"""What differs between Linux and macOS (desktop.py, vault.py's Keychain): checked here with
macOS's branch switched on, and for real on a Mac by CI."""

import os
import plistlib
import subprocess
import unittest
from unittest import mock

import support

import desktop
import vault


class MacTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        patch = mock.patch.object(desktop, "IS_MAC", True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_service_is_a_launchd_agent_that_keeps_running_with_its_path(self):
        path = desktop.service_write("lago-agents-scheduler.service", ["/opt/homebrew/bin/python3", "/x/background.py"],
                                     "Lago's clockwork", env={"EXTRA": "1"})
        self.assertEqual(path.parent, desktop.LAUNCH_DIR)
        plist = plistlib.loads(path.read_bytes())
        self.assertEqual(plist["Label"], "com.lago-agents.scheduler")
        self.assertEqual(plist["ProgramArguments"], ["/opt/homebrew/bin/python3", "/x/background.py"])
        self.assertEqual((plist["RunAtLoad"], plist["KeepAlive"]), (True, True))
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], os.environ["PATH"])
        calls = []
        with mock.patch.object(desktop, "_launchctl", lambda *a, check=True: calls.append(a) or subprocess.CompletedProcess(a, 0, "", "")):
            desktop.service_enable("lago-agents-scheduler.service")
            desktop.service_disable("lago-agents-scheduler.service", remove=True)
        uid = os.getuid()
        self.assertIn(("bootstrap", f"gui/{uid}", str(path)), calls)
        self.assertIn(("bootout", f"gui/{uid}/com.lago-agents.scheduler"), calls)
        self.assertFalse(path.exists())
        on_failure = plistlib.loads(desktop.service_write("x.service", ["a"], "d", restart="on-failure").read_bytes())
        self.assertEqual(on_failure["KeepAlive"], {"SuccessfulExit": False})

    def test_a_reloaded_service_waits_for_the_old_one_to_go(self):
        desktop.service_write("lago-agents-scheduler.service", ["a"], "d")
        failures = iter(["Bootstrap failed: 5: Input/output error"] * 2)

        def launchctl(*args, check=True):
            if args[0] == "bootstrap" and (error := next(failures, None)):
                raise desktop.ServiceError(error)
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(desktop, "_launchctl", launchctl), mock.patch.object(desktop.time, "sleep") as sleep:
            desktop.service_enable("lago-agents-scheduler.service")
        self.assertEqual(sleep.call_count, 2)

    def test_the_keychain_gets_the_value_on_stdin_never_on_a_command_line(self):
        runs = []

        def fake(args, **kwargs):
            runs.append((args, kwargs.get("input")))
            return subprocess.CompletedProcess(args, 0, "s3cret\n" if "find-generic-password" in args else "", "")
        keychain = vault.MacKeychainBackend()
        with mock.patch.object(vault.subprocess, "run", fake):
            keychain.store("stripe", "shop", 'va"lue\\with quotes')
            self.assertEqual(keychain.lookup("stripe", "shop"), "s3cret")
            keychain.clear("stripe", "shop")
        (store_args, store_input), (find_args, _), (delete_args, _) = runs
        self.assertEqual(store_args, ["security", "-i"])
        self.assertIn('-a "shop/stripe"', store_input)
        self.assertIn('-w "va\\"lue\\\\with quotes"', store_input)
        self.assertTrue(all("va" not in str(a) for a in find_args + delete_args))
        self.assertEqual(find_args[:2], ["security", "find-generic-password"])

    def test_a_terminal_window_and_the_microphone_use_the_macs_own(self):
        argv = desktop.terminal_argv('claude --resume "x"')
        self.assertEqual(argv[0], "osascript")
        self.assertIn('claude --resume \\"x\\"', argv[2])
        with mock.patch.object(desktop.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}" if name == "ffmpeg" else None):
            self.assertEqual(desktop.recorder_argv(16000, "-")[:8], ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "avfoundation", "-i", ":0"])
            self.assertEqual(desktop.recorder_argv(16000, "/tmp/a.wav")[-2:], ["-y", "/tmp/a.wav"])

    def test_processes_are_read_without_proc(self):
        with mock.patch.object(desktop.Path, "exists", lambda self: False), \
                mock.patch.object(desktop.subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 0, "/usr/local/bin/claude\n", "")):
            self.assertEqual(desktop.process_name(123), "claude")
        self.assertAlmostEqual(desktop._etime_start("01-02:03:04"), __import__("time").time() - (86400 + 7384), delta=5)


class CertificateTests(unittest.TestCase):
    """The phone server's certificate, made with whatever openssl this system has (LibreSSL on
    macOS)."""

    def setUp(self):
        support.reset()

    @unittest.skipUnless(__import__("shutil").which("openssl"), "openssl isn't installed")
    def test_the_certificate_loads_and_names_this_computer(self):
        import ssl
        import remote
        for path in (remote.CERT_PATH, remote.KEY_PATH):
            path.unlink(missing_ok=True)
        remote.ensure_certificate(["100.101.102.103"])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(remote.CERT_PATH, remote.KEY_PATH)
        text = subprocess.run(["openssl", "x509", "-in", str(remote.CERT_PATH), "-noout", "-text"],
                              capture_output=True, text=True).stdout
        self.assertIn("100.101.102.103", text)
        self.assertIn("127.0.0.1", text)
        self.assertEqual(oct(remote.KEY_PATH.stat().st_mode & 0o777), "0o600")



class MacSandboxTests(unittest.TestCase):
    """A draft agent's install script in macOS's sandbox: checked on a Mac (CI)."""

    def setUp(self):
        support.reset()
        self.folder = support.TMP / "tool"
        self.folder.mkdir(parents=True, exist_ok=True)

    def test_the_profile_keeps_the_script_in_its_folder(self):
        with mock.patch.object(desktop, "IS_MAC", True), mock.patch.object(desktop.shutil, "which", lambda name: "/usr/bin/" + name):
            argv = desktop.sandbox_argv(["bash", "x.sh"], self.folder, {"TOOL_DIR": self.folder})
        self.assertEqual(argv[:2], ["sandbox-exec", "-p"])
        profile = argv[2]
        self.assertIn(f'(allow file-write* (subpath "{self.folder.resolve()}")', profile)
        self.assertIn(f'(deny file-read* (subpath "{desktop.Path.home().resolve() / ".ssh"}"))', profile)
        self.assertIn(f"HOME={self.folder.resolve()}", argv)
        self.assertIn(str(self.folder.resolve()), argv[3:8])  # it starts in its folder
        self.assertIn(f"TOOL_DIR={self.folder}", argv)
        self.assertEqual(argv[-2:], ["bash", "x.sh"])

    @unittest.skipUnless(desktop.IS_MAC and desktop.shutil.which("sandbox-exec"), "macOS's sandbox is only on a Mac")
    def test_a_script_cannot_write_outside_its_folder_or_read_your_keys(self):
        outside = desktop.Path.home() / f"lago-sandbox-check-{os.getpid()}"
        secret_dir = desktop.Path.home() / ".ssh"
        script = (f"echo made > made.txt; echo nope > {outside} 2>/dev/null || echo blocked; "
                  f"ls {secret_dir} >/dev/null 2>&1 && echo read || echo hidden; echo $HOME")
        out = subprocess.run(desktop.sandbox_argv(["bash", "-c", script], self.folder), capture_output=True, text=True, timeout=30)
        self.assertFalse(outside.exists())
        outside.unlink(missing_ok=True)
        lines = out.stdout.split()
        self.assertEqual(lines[0], "blocked", out.stderr)
        self.assertEqual(lines[-1], str(self.folder.resolve()))
        self.assertEqual((self.folder / "made.txt").read_text(), "made\n")
        if secret_dir.exists():
            self.assertEqual(lines[1], "hidden")


if __name__ == "__main__":
    unittest.main()
