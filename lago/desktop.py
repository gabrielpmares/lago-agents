"""The parts of a Linux desktop that differ from one distribution to the next, found rather
than assumed: a terminal to open a window in, the light or dark preference, a program that
records the microphone, and a sandbox for setup scripts you didn't write.
"""

import os
import shutil
import subprocess
from pathlib import Path

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
    when no known terminal is installed. $TERMINAL wins when it's one we know how to drive."""
    preferred = os.path.basename(os.environ.get("TERMINAL", ""))
    for name, args in sorted(TERMINALS, key=lambda t: t[0] != preferred):
        path = shutil.which(name)
        if path:
            return [path, *args, command]
    return None


def color_scheme():
    """"dark", "light" or None: the desktop's preference, from the freedesktop setting that
    GNOME, KDE Plasma and others share, else GNOME's own."""
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
    PipeWire's pw-record, else PulseAudio's parecord, else ALSA's arecord; None without any."""
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
