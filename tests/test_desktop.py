"""What differs between Linux, macOS and Windows through WSL (desktop.py, vault.py's Keychain
and DPAPI): checked here with macOS's or WSL's branch switched on, and for real on a Mac and in
WSL on Windows by CI."""

import os
import plistlib
import re
import subprocess
import unittest
from pathlib import Path
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


class WslTests(unittest.TestCase):
    """Lago in WSL on Windows: Windows' own programs open its window, links, terminals and
    keep its secrets. Here they're stand-ins that record how they were called."""

    def setUp(self):
        support.reset()
        for name, value in (("IS_WSL", True), ("IS_MAC", False)):
            patch = mock.patch.object(desktop, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.drive = support.TMP / "c-drive"
        self.drive.mkdir(exist_ok=True)
        for patch in (mock.patch.object(desktop, "windows_drive", lambda: self.drive),
                      mock.patch.object(desktop, "windows_program", lambda name: f"/mnt/c/Windows/System32/{name}"),
                      mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_terminal_window_is_windows_terminal_running_a_script_in_this_distribution(self):
        with mock.patch.object(desktop.shutil, "which", lambda name: None):
            argv = desktop.terminal_argv('tmux -L lago-agents attach -t "=la-x"; echo done')
        self.assertEqual(argv[:3], ["/mnt/c/Windows/System32/wt.exe", "-w", "new"])
        self.assertEqual(argv[3:8], ["/mnt/c/Windows/System32/wsl.exe", "-d", "Ubuntu", "-e", "sh"])
        script = Path(argv[8])
        self.addCleanup(script.unlink, missing_ok=True)
        self.assertNotIn(";", " ".join(argv))  # Windows Terminal would start another tab at a ";"
        self.assertIn('tmux -L lago-agents attach -t "=la-x"; echo done', script.read_text())
        self.assertTrue(script.read_text().startswith('rm -f "$0"'))

    def test_the_light_or_dark_preference_is_windows_app_mode(self):
        answers = {"0x0": "dark", "0x1": "light"}
        for value, scheme in answers.items():
            out = f"HKEY_CURRENT_USER\\...\\Personalize\r\n    AppsUseLightTheme    REG_DWORD    {value}\r\n"
            with mock.patch.object(desktop.subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 0, out, "")):
                self.assertEqual(desktop.color_scheme(), scheme)

    def test_files_open_on_windows_by_their_windows_path_and_links_as_they_are(self):
        started = []
        wslpath = lambda args, **k: subprocess.CompletedProcess(args, 0, "\\\\wsl.localhost\\Ubuntu\\home\\me\\a.pdf\n", "")  # noqa: E731
        with mock.patch.object(desktop.subprocess, "run", wslpath), \
                mock.patch.object(desktop.subprocess, "Popen", lambda argv, **k: started.append(argv)), \
                mock.patch.object(desktop.shutil, "which", lambda name: None):
            self.assertTrue(desktop.open_target("/home/me/a.pdf"))
            self.assertTrue(desktop.open_url("https://example.com/?a=1&b=2"))
        self.assertEqual(started, [["/mnt/c/Windows/System32/explorer.exe", "\\\\wsl.localhost\\Ubuntu\\home\\me\\a.pdf"],
                                   ["/mnt/c/Windows/System32/explorer.exe", "https://example.com/?a=1&b=2"]])

    def test_the_window_is_an_edge_app_window_with_a_windows_profile(self):
        edge = self.drive / "Program Files (x86)" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        edge.parent.mkdir(parents=True, exist_ok=True)
        edge.touch()
        runs = []

        def run(args, **kwargs):
            runs.append(args)
            if Path(str(args[0])).name == "cmd.exe":
                return subprocess.CompletedProcess(args, 0, "C:\\Users\\me\\AppData\\Local\r\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(desktop.subprocess, "run", run):
            self.assertTrue(desktop.app_window("http://127.0.0.1:5000/?t=x"))
        window = runs[-1]
        self.assertEqual(window[:2], [str(edge), "--app=http://127.0.0.1:5000/?t=x"])
        self.assertIn("--user-data-dir=C:\\Users\\me\\AppData\\Local\\lago-agents\\window", window)

    def test_the_distribution_stays_up_while_agents_run_through_a_hidden_windows_process(self):
        started = []
        with mock.patch.object(desktop.shutil, "which", lambda name: "/usr/bin/flock" if name == "flock" else None), \
                mock.patch.object(desktop.subprocess, "Popen", lambda argv, **k: started.append(argv)):
            self.assertTrue(desktop.wsl_keep_alive())
        (argv,) = started
        self.assertEqual(argv[0], "/mnt/c/Windows/System32/powershell.exe")
        script = argv[-1]
        self.assertIn("Start-Process -WindowStyle Hidden -FilePath '/mnt/c/Windows/System32/wsl.exe'", script)
        self.assertIn("-d Ubuntu -e flock -n ", script)
        self.assertIn("tmux -L lago-agents has-session", script)
        with mock.patch.object(desktop, "IS_WSL", False):
            self.assertFalse(desktop.wsl_keep_alive())

    def test_secrets_are_encrypted_by_windows_and_travel_on_stdin(self):
        import base64
        runs = []

        def powershell(args, input=None, **kwargs):
            runs.append((args, input))
            given = re.search(r"FromBase64String\('([^']*)'\)", input).group(1)
            raw = base64.b64decode(given)
            # A stand-in for DPAPI: reversible, and never the value itself.
            changed = bytes(b ^ 0x5A for b in raw)
            return subprocess.CompletedProcess(args, 0, base64.b64encode(changed).decode() + "\r\n", "")
        backend = vault.WindowsBackend(powershell="/mnt/c/powershell.exe", path=support.TMP / "vault-windows.json")
        with mock.patch.object(vault.subprocess, "run", powershell):
            backend.store("stripe", "shop", "sk_live_'quoted\"value")
            self.assertEqual(backend.lookup("stripe", "shop"), "sk_live_'quoted\"value")
            self.assertIsNone(backend.lookup("other", "shop"))
            backend.clear("stripe", "shop")
            self.assertIsNone(backend.lookup("stripe", "shop"))
        self.assertTrue(runs)
        for args, script in runs:
            self.assertEqual(args, ["/mnt/c/powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"])
            self.assertNotIn("sk_live", script)
        self.assertIn("Protect(", runs[0][1])

    def test_wsl_without_a_keyring_keeps_secrets_with_windows(self):
        with mock.patch.object(vault, "_backend", None), mock.patch.object(vault, "_keyring_usable", lambda: False), \
                mock.patch.dict(os.environ, {"LAGO_VAULT_KEY": ""}):
            self.assertIsInstance(vault.backend(), vault.WindowsBackend)


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
