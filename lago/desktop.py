"""The parts of the computer that differ from one system to the next (Linux distributions, and
macOS), found rather than assumed: the app's window, opening a file or link, a terminal to open
a window in, the light or dark preference, a program that records the microphone, one that
plays a sound, a sandbox for setup scripts you didn't write, what a process is, the keyring
and background services.
"""

import os
import plistlib
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

IS_MAC = sys.platform == "darwin"

# Terminals and how each runs a command line in a new window, most specific first.
# x-terminal-emulator is Debian and Ubuntu's choice of default.
TERMINALS = (
    ("ptyxis", ["--new-window", "-x"]),
    ("gnome-terminal", ["--window", "--", "sh", "-c"]),
    ("konsole", ["-e", "sh", "-c"]),
    ("xfce4-terminal", ["-x", "sh", "-c"]),
    ("kitty", ["sh", "-c"]),
    ("alacritty", ["-e", "sh", "-c"]),
    ("foot", ["sh", "-c"]),
    ("wezterm", ["start", "--", "sh", "-c"]),
    ("x-terminal-emulator", ["-e", "sh", "-c"]),
    ("xterm", ["-e", "sh", "-c"]),
)


def terminal_argv(command):
    """The argv that opens `command` (a shell command line) in a new terminal window, or None
    when no known terminal is installed. $TERMINAL wins when it's one we know how to drive.
    On macOS: iTerm if it's there, else Terminal."""
    if IS_MAC:
        quoted = command.replace("\\", "\\\\").replace('"', '\\"')
        if Path("/Applications/iTerm.app").exists():
            script = f'tell application "iTerm" to create window with default profile command "sh -c \\"{quoted}\\""'
        else:
            script = f'tell application "Terminal" to do script "{quoted}"\ntell application "Terminal" to activate'
        return ["osascript", "-e", script]
    preferred = os.path.basename(os.environ.get("TERMINAL", ""))
    for name, args in sorted(TERMINALS, key=lambda t: t[0] != preferred):
        path = shutil.which(name)
        if path:
            return [path, *args, command]
    return None


def color_scheme():
    """"dark", "light" or None: the desktop's preference, from the freedesktop setting that
    GNOME, KDE Plasma and others share, else GNOME's own; on macOS, its appearance."""
    if IS_MAC:
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        return "dark" if "Dark" in out else "light"
    try:
        out = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop",
                              "--object-path", "/org/freedesktop/portal/desktop", "--method",
                              "org.freedesktop.portal.Settings.Read", "org.freedesktop.appearance",
                              "color-scheme"], capture_output=True, text=True, timeout=3).stdout
        # "(<<uint32 1>>,)": 1 prefers dark, 2 prefers light, 0 has no preference
        if "uint32 1" in out:
            return "dark"
        if "uint32 2" in out:
            return "light"
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        scheme = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                                capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "dark" in scheme:
        return "dark"
    if "light" in scheme:
        return "light"
    return None


def recorder_argv(rate, out):
    """The argv that records the microphone as 16-bit mono at `rate` Hz into `out`: a WAV
    file's path, or "-" for raw samples on stdout. SIGINT ends it with the file complete.
    PipeWire's pw-record, else PulseAudio's parecord, else ALSA's arecord; on macOS, ffmpeg
    (from the default input) or SoX's rec. None without any."""
    if IS_MAC:
        if shutil.which("ffmpeg"):
            return ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "avfoundation", "-i", ":0",
                    "-ac", "1", "-ar", str(rate)] + (["-f", "s16le", "-"] if out == "-" else ["-y", out])
        if shutil.which("rec"):
            return ["rec", "-q", "-c", "1", "-r", str(rate), "-b", "16"] + (["-t", "raw", "-"] if out == "-" else [out])
        return None
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(rate), "--channels", "1", "--format", "s16", out]
    if shutil.which("parecord"):
        argv = ["parecord", f"--rate={rate}", "--channels=1", "--format=s16le"]
        return argv + (["--raw"] if out == "-" else ["--file-format=wav", out])  # stdout by default
    if shutil.which("arecord"):
        argv = ["arecord", "-q", "-f", "S16_LE", "-r", str(rate), "-c", "1"]
        return argv + (["-t", "raw"] if out == "-" else ["-t", "wav", out])
    return None


# Folders under your home a sandboxed script never sees, even when they're on your PATH.
PRIVATE = (".ssh", ".gnupg", ".config", ".claude", ".aws", ".kube", ".docker", ".netrc", ".password-store")


def sandbox_argv(argv, folder, env=None):
    """`argv` run inside bubblewrap, or None when it isn't installed. Inside, the script sees
    the system's programs read-only, the programs on your PATH (Node, uv...) read-only, the
    network, and `folder`, which is all it can write and is its home. Not your home folder,
    your keys, your config or your environment: only `env` (a dict) is set."""
    if IS_MAC:
        return _seatbelt_argv(argv, folder, env)
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None
    home, folder = Path.home(), Path(folder)
    args = [bwrap, "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--ro-bind-try", "/opt", "/opt", "--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for top in ("bin", "lib", "lib64", "sbin"):  # merged /usr (most distributions): links; else the folder itself
        path = Path("/") / top
        if path.is_symlink():
            args += ["--symlink", os.readlink(path), str(path)]
        elif path.is_dir():
            args += ["--ro-bind", str(path), str(path)]
    path_dirs, bound = [], set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        where = Path(entry).expanduser()
        if not where.is_absolute() or not where.is_dir():
            continue
        if where.is_relative_to(home):
            inside = where.relative_to(home)
            if not inside.parts or inside.parts[0] in PRIVATE:
                continue
            # A bin folder next to its lib (~/.npm-global, nvm's node) needs both.
            mount = where.parent if where.name == "bin" and (where.parent / "lib").is_dir() and where.parent != home / ".local" else where
            if mount not in bound:
                args += ["--ro-bind", str(mount), str(mount)]
                bound.add(mount)
        elif not str(where).startswith(("/usr", "/bin", "/sbin", "/opt")):
            continue
        path_dirs.append(str(where))
    return args + ["--bind", str(folder), str(folder), "--chdir", str(folder),
                   "--unshare-all", "--share-net", "--die-with-parent", "--new-session", "--clearenv",
                   "--setenv", "PATH", os.pathsep.join(dict.fromkeys(path_dirs + ["/usr/bin", "/bin"])),
                   "--setenv", "HOME", str(folder), "--setenv", "LANG", os.environ.get("LANG", "C.UTF-8"),
                   *[arg for name, value in (env or {}).items() for arg in ("--setenv", name, str(value))],
                   "--", *argv]


def _seatbelt_argv(argv, folder, env=None):
    """On macOS, its own sandbox (sandbox-exec): the script writes only its folder and the
    temporary folders, reads nothing private in your home (keys, config, Claude's and your
    browsers' data), and gets none of your environment. None without sandbox-exec."""
    if not shutil.which("sandbox-exec"):
        return None
    home, folder = Path.home().resolve(), Path(folder).resolve()
    quote = lambda path: '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
    private = [home / name for name in PRIVATE] + [home / "Library" / name for name in ("Keychains", "Cookies", "Mail", "Messages",
                                                                                         "Application Support", "Safari")]
    profile = "\n".join([
        "(version 1)", "(allow default)",
        f"(deny file-write* (subpath {quote(home)}))",
        f"(allow file-write* (subpath {quote(folder)}) (subpath \"/private/tmp\") (subpath \"/private/var/folders\") (literal \"/dev/null\"))",
        *[f"(deny file-read* (subpath {quote(p)}))" for p in private]])
    path = os.pathsep.join(dict.fromkeys([p for p in os.environ.get("PATH", "").split(os.pathsep)
                                          if p and not any(p.startswith(str(x)) for x in private)] + ["/usr/bin", "/bin"]))
    variables = {"PATH": path, "HOME": str(folder), "LANG": os.environ.get("LANG", "en_US.UTF-8"), **{k: str(v) for k, v in (env or {}).items()}}
    return ["sandbox-exec", "-p", profile, "env", "-i", *[f"{k}={v}" for k, v in variables.items()], *argv]


# ---------------------------------------------------------------- opening things

def open_target(target):
    """Open a URL in the browser, or a file or folder in its usual app. True if it started."""
    command = ["open", str(target)] if IS_MAC else ["xdg-open", str(target)]
    try:
        subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


# The browsers that open a page as an app window (no tabs or address bar), with where macOS keeps them.
APP_BROWSERS = (
    ("google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ("chromium-browser", None),
    ("microsoft-edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("brave-browser", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
)


def _app_browser():
    for name, mac_path in APP_BROWSERS:
        if IS_MAC and mac_path and Path(mac_path).exists():
            return mac_path
        if not IS_MAC and shutil.which(name):
            return shutil.which(name)
    return None


def app_window(url, profile_dir=None):
    """Show the app at `url` in a window of its own, and return once it closes: a native
    window when pywebview is installed (macOS's WebKit), else an app window of Chrome, Chromium,
    Edge or Brave with its own profile. Without either, the default browser opens it and this
    returns at once: False."""
    try:
        import webview  # pywebview: pip install pywebview
    except ImportError:
        webview = None
    if webview is not None:
        webview.create_window("Lago Agents", url, width=1440, height=880, min_size=(900, 600))
        webview.start(private_mode=False)
        return True
    browser = _app_browser()
    if browser:
        profile = Path(profile_dir or Path.home() / ".local" / "state" / "lago-agents" / "window")
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.run([browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                        "--no-default-browser-check", "--window-size=1440,880"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    webbrowser.open(url)
    return False


def open_app_window(url):
    """Bring up another window on an app that's already running (a second launch)."""
    browser = _app_browser()
    if browser and not _has_pywebview():
        subprocess.Popen([browser, f"--app={url}"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def _has_pywebview():
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------- processes

def process_command(pid):
    """A process's command line as a list (empty when it's gone): /proc on Linux, ps elsewhere."""
    proc = Path(f"/proc/{int(pid)}/cmdline")
    if proc.exists():
        try:
            return [part.decode(errors="replace") for part in proc.read_bytes().split(b"\0") if part]
        except OSError:
            return []
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return []
    return out.split() if out else []


def process_name(pid):
    """A process's program name ("claude"), or "" when it's gone."""
    comm = Path(f"/proc/{int(pid)}/comm")
    if comm.exists():
        try:
            return comm.read_text().strip()
        except OSError:
            return ""
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return Path(out).name if out else ""


def process_start(pid):
    """When a process started, as a comparable token (so a reused pid isn't mistaken for it),
    or None when it's gone."""
    stat = Path(f"/proc/{int(pid)}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out or None


def claude_processes():
    """Claude Code processes of yours: [{"pid", "cwd", "args" (list), "started", "lago"}] where
    "lago" says Lago Agents started it (LAGO_AGENT_ID in its environment)."""
    uid, found = os.getuid(), []
    if Path("/proc").is_dir() and not IS_MAC:
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if proc.stat().st_uid != uid or (proc / "comm").read_text().strip() != "claude":
                    continue
                found.append({"pid": int(proc.name), "cwd": os.readlink(proc / "cwd"),
                              "args": [a.decode(errors="replace") for a in (proc / "cmdline").read_bytes().split(b"\0") if a],
                              "started": proc.stat().st_mtime, "lago": b"LAGO_AGENT_ID=" in (proc / "environ").read_bytes()})
            except OSError:
                continue
        return found
    try:  # macOS: ps for the processes (with their environment, `eww`), lsof for each one's folder
        out = subprocess.run(["ps", "-x", "-o", "pid=,etime=,command=", "-ww", "-E"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, etime, command = parts
        words = command.split()
        if not words or Path(words[0]).name != "claude":
            continue
        try:
            listing = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=5).stdout
            cwd = next((l[1:] for l in listing.splitlines() if l.startswith("n")), "")
        except (OSError, subprocess.TimeoutExpired):
            cwd = ""
        found.append({"pid": int(pid), "cwd": cwd, "args": [w for w in words if "=" not in w or w.startswith("-")],
                      "started": _etime_start(etime), "lago": "LAGO_AGENT_ID=" in command})
    return found


def _etime_start(etime):
    """ps's elapsed time ([[dd-]hh:]mm:ss) as the epoch second the process started."""
    import time
    days, _, clock = etime.rpartition("-")
    parts = [int(x) for x in clock.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    seconds = (int(days or 0) * 86400) + parts[0] * 3600 + parts[1] * 60 + parts[2]
    return time.time() - seconds


# ---------------------------------------------------------------- background services

class ServiceError(Exception):
    pass


SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCH_LOGS = Path.home() / "Library" / "Logs" / "lago-agents"


def _label(name):
    return "com.lago-agents." + name.removesuffix(".service").removeprefix("lago-agents-")


def service_path(name):
    """Where a service's definition lives: a systemd user unit, or a launchd agent's plist."""
    return LAUNCH_DIR / f"{_label(name)}.plist" if IS_MAC else SYSTEMD_DIR / name


def service_write(name, argv, description, env=None, restart="always", after=""):
    """Define a service of yours that runs `argv` while you're logged in, restarting when it
    stops (`restart`: "always" or "on-failure"). It follows this checkout and this PATH."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(env or {})}
    path = service_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_MAC:
        LAUNCH_LOGS.mkdir(parents=True, exist_ok=True)
        log = str(LAUNCH_LOGS / f"{_label(name).rsplit('.', 1)[-1]}.log")
        plist = {"Label": _label(name), "ProgramArguments": [str(a) for a in argv], "EnvironmentVariables": env,
                 "RunAtLoad": True, "KeepAlive": True if restart == "always" else {"SuccessfulExit": False},
                 "ThrottleInterval": 10, "ProcessType": "Background", "StandardOutPath": log, "StandardErrorPath": log}
        path.write_bytes(plistlib.dumps(plist))
        return path
    quoted = " ".join(f'"{a}"' if " " in str(a) else str(a) for a in argv)
    lines = ["[Unit]", f"Description={description}"] + ([f"After={after}"] if after else []) + [
        "", "[Service]", f"ExecStart={quoted}"] + [f'Environment="{k}={v}"' for k, v in env.items()] + [
        f"Restart={restart}", "RestartSec=10", "Nice=5", "", "[Install]", "WantedBy=default.target", ""]
    path.write_text("\n".join(lines))
    _systemctl("daemon-reload", check=False)
    return path


def _systemctl(*args, check=True):
    try:
        proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ServiceError(f"Background services need systemd user services: {error}") from error
    if check and proc.returncode != 0:
        raise ServiceError(proc.stderr.strip() or f"systemctl {' '.join(args)} failed")
    return proc


def _launchctl(*args, check=True):
    try:
        proc = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ServiceError(f"launchctl failed: {error}") from error
    if check and proc.returncode != 0:
        raise ServiceError(proc.stderr.strip() or f"launchctl {' '.join(args)} failed")
    return proc


def _domain():
    return f"gui/{os.getuid()}"


def service_enable(name, start=True):
    """Start a defined service now and with every login."""
    if IS_MAC:
        _launchctl("bootout", f"{_domain()}/{_label(name)}", check=False)  # reload a changed plist
        _launchctl("bootstrap", _domain(), str(service_path(name)))
        _launchctl("enable", f"{_domain()}/{_label(name)}", check=False)
        return
    _systemctl("enable", *(["--now"] if start else []), name)


def service_disable(name, remove=False):
    """Stop a service and don't start it at login; `remove` deletes its definition too."""
    if IS_MAC:
        _launchctl("bootout", f"{_domain()}/{_label(name)}", check=False)
    else:
        _systemctl("disable", "--now", name, check=False)
    if remove:
        service_path(name).unlink(missing_ok=True)
        if not IS_MAC:
            _systemctl("daemon-reload", check=False)


def service_restart(name):
    if IS_MAC:
        if _launchctl("kickstart", "-k", f"{_domain()}/{_label(name)}", check=False).returncode != 0:
            service_enable(name)
        return
    _systemctl("restart", name)


def service_state(name):
    """{"active": running now, "enabled": starts at login}."""
    if IS_MAC:
        try:
            proc = _launchctl("print", f"{_domain()}/{_label(name)}", check=False)
        except ServiceError:
            return {"active": False, "enabled": False}
        loaded = proc.returncode == 0
        return {"active": loaded and "state = running" in proc.stdout, "enabled": loaded and service_path(name).exists()}
    try:
        active = _systemctl("is-active", name, check=False).stdout.strip()
        enabled = _systemctl("is-enabled", name, check=False).stdout.strip()
    except ServiceError:
        return {"active": False, "enabled": False}
    return {"active": active == "active", "enabled": enabled == "enabled"}


def services_available():
    """Whether this computer runs background services for you (systemd user services, or
    launchd on macOS)."""
    if IS_MAC:
        return shutil.which("launchctl") is not None
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def tailscale_cli():
    """Tailscale's command: on PATH (Linux, Homebrew), else inside the Mac app."""
    found = shutil.which("tailscale")
    if found:
        return found
    app = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    return str(app) if app.exists() else "tailscale"
