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


if __name__ == "__main__":
    unittest.main()
