#!/usr/bin/env bash
# Installs Lago Agents: on Linux, in the app menu; on macOS, as an app in ~/Applications; on
# Windows, inside WSL (run this there), in the Start menu.
#   ./install.sh              install
#   ./install.sh --uninstall  remove it from the app menu (agents and history are kept)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/lago"  # the app's code
OS="$(uname -s)"
# Your own config, agents and skills: never in this folder (see config_store.py).
DATA_DIR="${LAGO_AGENTS_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/lago-agents}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/lago-agents"
MENU_FILE="$HOME/.local/share/applications/lago-agents.desktop"
ICON_FILE="$HOME/.local/share/icons/hicolor/scalable/apps/lago-agents.svg"
MAC_APP="$HOME/Applications/Lago Agents.app"
# Windows: Lago runs in a WSL distribution, and Windows gets a Start menu shortcut to it.
WSL=
if [[ "$OS" == Linux ]] && { [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; }; then
    WSL=1
fi
WSL_LAUNCHER="$STATE_DIR/windows-launch.sh"
# The Start menu folder, as WSL sees it ("" when Windows can't be asked).
win_start_menu() {
    local appdata
    appdata="$(cd / && cmd.exe /d /c 'echo %APPDATA%' 2>/dev/null | tr -d '\r' | tail -1)"
    [[ -n "$appdata" && "$appdata" != "%APPDATA%" ]] || return 0
    wslpath -u "$appdata\\Microsoft\\Windows\\Start Menu\\Programs" 2>/dev/null || true
}
if [[ "$OS" == Darwin ]]; then
    # Homebrew's tools first, even in a shell Homebrew's "Next steps" never set up: macOS's own
    # python3 is too old.
    for brew_bin in /usr/local/bin /opt/homebrew/bin; do
        [[ -d "$brew_bin" ]] && PATH="$brew_bin:$PATH"
    done
    export PATH
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$MENU_FILE" "$ICON_FILE"
    rm -rf "$MAC_APP"
    if [[ -n "$WSL" ]]; then
        start_menu="$(win_start_menu)"
        [[ -n "$start_menu" ]] && rm -f "$start_menu/Lago Agents.lnk"
        rm -f "$WSL_LAUNCHER"
    fi
    echo "Lago Agents removed from the app menu."
    echo "Running agents keep going; stop them with: tmux -L lago-agents kill-server"
    exit 0
fi

missing=()
for tool in python3 tmux git claude; do
    command -v "$tool" >/dev/null || missing+=("$tool")
done
if [[ -n "$WSL" ]]; then
    # WSL puts Windows' PATH after Linux's: a `claude` or `python3` found under /mnt/c is
    # Windows' own, which can't run agents in WSL. Each of them is needed inside WSL.
    for tool in python3 tmux git claude; do
        found="$(command -v "$tool" || true)"
        if [[ -n "$found" && "$found" == /mnt/* ]]; then
            echo "$tool here is Windows' own ($found): Lago Agents needs it installed inside WSL." >&2
            missing+=("$tool")
        fi
    done
fi
if (( ${#missing[@]} )); then
    echo "Lago Agents needs ${missing[*]}, which isn't installed." >&2
    if [[ "$OS" == Darwin ]]; then
        echo "With Homebrew (https://brew.sh): brew install python tmux git; then Claude Code: npm install -g @anthropic-ai/claude-code" >&2
    elif [[ -n "$WSL" ]]; then
        echo "Inside WSL (Ubuntu, Debian): sudo apt install python3 tmux git; then Claude Code: curl -fsSL https://claude.ai/install.sh | bash" >&2
    fi
    exit 1
fi
if [[ -n "$WSL" && "$SCRIPT_DIR" == /mnt/* ]]; then
    echo "Note: this checkout is on a Windows drive ($SCRIPT_DIR), which WSL reads slowly." >&2
    echo "      It runs faster cloned inside WSL, for example in ~/lago-agents." >&2
fi
# The Python everything here runs with: python3, unless it's too old or broken. Homebrew's
# Pythons can be built for a newer macOS than this one, and then can't load their XML module
# (pyexpat, which pip needs) against this macOS's libexpat; so on a Mac every python3 and
# python3.1x on the PATH is tried, python.org's (in /usr/local/bin) among them.
PY=python3
if [[ "$OS" == Darwin ]]; then
    for candidate in $(type -ap python3 python3.15 python3.14 python3.13 python3.12 python3.11); do
        if "$candidate" -c 'import sys, pyexpat; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    done
    if [[ "$PY" != python3 ]]; then
        echo "Using $("$PY" -V) ($PY): python3 is too old or can't load its XML module, which pip needs."
    fi
fi
"$PY" - "$OS" <<'PY' || { echo "See README.md, Install." >&2; exit 1; }
import sys
if sys.version_info < (3, 11):
    sys.exit("Lago Agents needs Python 3.11 or newer" + (" (brew install python)" if sys.argv[1] == "Darwin" else "") + ".")
if sys.argv[1] == "Darwin":
    try:
        import pyexpat  # noqa: F401
    except ImportError as error:
        sys.exit(f"This Python can't load its XML module, which pip needs ({error}). "
                 "Update macOS, or install Python from https://www.python.org/downloads/macos/")
if sys.argv[1] != "Darwin":
    try:
        import gi
        for name, version in (("Gtk", "3.0"), ("WebKit2", "4.1"), ("Vte", "2.91")):
            gi.require_version(name, version)
        from gi.repository import Gtk, WebKit2, Vte  # noqa: F401
    except (ImportError, ValueError) as error:
        print(f"No GTK window ({error}): Lago Agents opens in a browser window instead.", file=sys.stderr)
PY

chmod +x "$APP/lago_agents.py" "$APP/lago_hook.py" "$APP/lago_mcp.py" "$APP/install_plugins.py" "$SCRIPT_DIR/lago-agents"
PYTHON="$PY"

if [[ "$OS" == Darwin ]]; then
    # The window: a native one through pywebview, in a venv of the app's own (Homebrew's Python
    # won't take packages system-wide). Without it, Lago opens in Chrome (or your browser).
    VENV="$STATE_DIR/app-venv"
    mkdir -p "$STATE_DIR"
    # A venv left by an earlier install whose Python broke (or that died before pip) is made again.
    if ! "$VENV/bin/python" -c 'import pyexpat, pip' 2>/dev/null; then
        rm -rf "$VENV"
        "$PY" -m venv "$VENV"
    fi
    if "$VENV/bin/python" -m pip install --quiet --upgrade pywebview; then
        PYTHON="$VENV/bin/python"
        echo "Lago Agents opens in a window of its own."
    else
        echo "Couldn't install pywebview: Lago Agents opens in Chrome or your browser instead." >&2
        PYTHON="$VENV/bin/python"
    fi
fi

# Your Claude Code agent definitions: link each one into ~/.claude/agents, so your data
# folder is the copy that's edited and versioned. A different file already there is left alone.
mkdir -p "$DATA_DIR" "$HOME/.claude/agents"
shopt -s nullglob
for def in "$DATA_DIR"/claude-agents/*.md; do
    target="$HOME/.claude/agents/$(basename "$def")"
    if [[ -e "$target" && ! -L "$target" ]] && ! cmp -s "$def" "$target"; then
        echo "Kept your own $target (differs from claude-agents/$(basename "$def"))." >&2
        continue
    fi
    ln -sfn "$def" "$target"
done

# Skills: link each skill folder into ~/.claude/skills the same way.
mkdir -p "$HOME/.claude/skills"
for skill in "$DATA_DIR"/claude-skills/*/; do
    skill="${skill%/}"
    target="$HOME/.claude/skills/$(basename "$skill")"
    if [[ -e "$target" && ! -L "$target" ]] && ! diff -rq "$skill" "$target" >/dev/null; then
        echo "Kept your own $target (differs from claude-skills/$(basename "$skill"))." >&2
        continue
    fi
    [[ -d "$target" && ! -L "$target" ]] && rm -rf "$target"
    ln -sfn "$skill" "$target"
done
"$PY" "$APP/validate_skills.py" "$DATA_DIR/claude-skills" \
    || echo "Some skills don't follow the Agent Skills spec (see above)." >&2

# Links into your data folder whose target is gone (a renamed or removed agent or skill).
for link in "$HOME"/.claude/agents/*.md "$HOME"/.claude/skills/*; do
    if [[ -L "$link" && ! -e "$link" && "$(readlink "$link")" == "$DATA_DIR"/* ]]; then
        rm "$link"
    fi
done

# Plugins: install the ones in your claude-plugins/plugins.json that are missing.
"$PY" "$APP/install_plugins.py"

if [[ "$OS" == Darwin ]]; then
    # An app in ~/Applications (Spotlight, Launchpad, the Dock). Apps opened from the Finder get
    # a bare PATH, so it carries this shell's: that's how it finds tmux, claude, gh and npx.
    rm -rf "$MAC_APP"
    mkdir -p "$MAC_APP/Contents/MacOS" "$MAC_APP/Contents/Resources"
    cat > "$MAC_APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Lago Agents</string>
  <key>CFBundleDisplayName</key><string>Lago Agents</string>
  <key>CFBundleIdentifier</key><string>com.lago-agents.app</string>
  <key>CFBundleExecutable</key><string>lago-agents</string>
  <key>CFBundleIconFile</key><string>lago-agents</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Lago listens when you talk to him.</string>
</dict></plist>
PLIST
    cat > "$MAC_APP/Contents/MacOS/lago-agents" <<LAUNCH
#!/bin/sh
export PATH="$PATH"
exec "$SCRIPT_DIR/lago-agents" "\$@"
LAUNCH
    chmod +x "$MAC_APP/Contents/MacOS/lago-agents"
    # Its icon, drawn from the app's SVG with the Mac's own tools (left plain if they can't).
    ICONSET="$(mktemp -d)/lago-agents.iconset"
    mkdir -p "$ICONSET"
    if qlmanage -t -s 1024 -o "$ICONSET" "$APP/ui/lago-agents-icon.svg" >/dev/null 2>&1 \
        && [[ -f "$ICONSET/lago-agents-icon.svg.png" ]]; then
        for size in 16 32 128 256 512; do
            sips -z $size $size "$ICONSET/lago-agents-icon.svg.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
            sips -z $((size * 2)) $((size * 2)) "$ICONSET/lago-agents-icon.svg.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
        done
        rm "$ICONSET/lago-agents-icon.svg.png"
        iconutil -c icns "$ICONSET" -o "$MAC_APP/Contents/Resources/lago-agents.icns" 2>/dev/null || true
    fi
    echo "Lago Agents is in ~/Applications (open it from Spotlight or Launchpad)."
elif [[ -n "$WSL" ]]; then
    # A Start menu shortcut that starts Lago in this WSL distribution, with this shell's PATH
    # (a shortcut's shell doesn't read ~/.bashrc, where claude's folder usually goes on the PATH).
    mkdir -p "$STATE_DIR"
    cat > "$WSL_LAUNCHER" <<LAUNCH
#!/bin/sh
export PATH="$PATH"
exec "$SCRIPT_DIR/lago-agents" "\$@"
LAUNCH
    chmod +x "$WSL_LAUNCHER"
    start_menu="$(win_start_menu)"
    if [[ -n "$start_menu" ]] && command -v powershell.exe >/dev/null; then
        # wslg.exe starts it without a console window; plain wsl.exe (older WSL) minimized.
        wsl_exe='C:\Windows\System32\wsl.exe'
        style=7
        if [[ -e "$(wslpath -u 'C:\Program Files\WSL\wslg.exe' 2>/dev/null)" ]]; then
            wsl_exe='C:\Program Files\WSL\wslg.exe'
            style=1
        fi
        icon=""
        if command -v rsvg-convert >/dev/null; then
            # Windows wants an .ico: one 256-pixel PNG inside the icon format's header.
            ico="$STATE_DIR/lago-agents.ico"
            if rsvg-convert -w 256 -h 256 "$APP/ui/lago-agents-icon.svg" -o "$STATE_DIR/lago-agents-256.png" 2>/dev/null; then
                "$PY" - "$STATE_DIR/lago-agents-256.png" "$ico" <<'PY' && icon="$(wslpath -w "$ico")"
import struct, sys
png = open(sys.argv[1], "rb").read()
with open(sys.argv[2], "wb") as out:
    out.write(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22) + png)
PY
            fi
        fi
        lnk="$(wslpath -w "$start_menu")\Lago Agents.lnk"
        arguments="${WSL_DISTRO_NAME:+-d $WSL_DISTRO_NAME }--cd ~ -- '$WSL_LAUNCHER'"
        q="'"
        ps_quote() { printf "'%s'" "${1//$q/$q$q}"; }
        shortcut="\$s = (New-Object -ComObject WScript.Shell).CreateShortcut($(ps_quote "$lnk"));"
        shortcut+=" \$s.TargetPath = $(ps_quote "$wsl_exe"); \$s.Arguments = $(ps_quote "$arguments");"
        shortcut+=" \$s.WindowStyle = $style; \$s.Description = 'Run and watch teams of AI agents';"
        [[ -n "$icon" ]] && shortcut+=" \$s.IconLocation = $(ps_quote "$icon");"
        shortcut+=" \$s.Save()"
        if (cd / && powershell.exe -NoProfile -NonInteractive -Command "$shortcut" >/dev/null); then
            echo "Lago Agents is in the Windows Start menu (it runs in WSL, $WSL_DISTRO_NAME)."
        else
            echo "Couldn't make the Start menu shortcut; start Lago Agents with: $SCRIPT_DIR/lago-agents" >&2
        fi
    else
        echo "Windows isn't reachable from WSL (interop off?): start Lago Agents with $SCRIPT_DIR/lago-agents" >&2
    fi
else
    mkdir -p "$(dirname "$MENU_FILE")" "$(dirname "$ICON_FILE")"
    cp "$APP/ui/lago-agents-icon.svg" "$ICON_FILE"
    gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    cat > "$MENU_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Lago Agents
Comment=Run and watch teams of AI agents
Exec="$SCRIPT_DIR/lago-agents"
Icon=lago-agents
StartupWMClass=lago-agents
Categories=Development;
DESKTOP
    echo "Lago Agents is in the app menu."
fi

# Claude Code or Codex using Lago Agents as an MCP server: point them at this checkout.
"$PY" - "$APP" <<'PY' || true
import sys
sys.path.insert(0, sys.argv[1])
import connectors
for which, has in (("claude", connectors._claude_has), ("codex", connectors._codex_has)):
    if connectors._installed_cli(which) and has():
        connectors.connect_assistant(which, False)
        connectors.connect_assistant(which, True)
        print(f"{'Claude Code' if which == 'claude' else 'Codex'} uses Lago Agents from this folder.")
PY

# Background services (systemd user services on Linux, launchd agents on macOS), with this
# shell's PATH so they find claude, gh and npx:
# - Lago's clockwork: reminders, recurring jobs and test grading, even while the app is closed;
# - "Hey Lago" and phone access, if you switched them on: rewritten to follow this checkout.
"$PYTHON" - "$APP" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import desktop
if not desktop.services_available():
    print("No background services here: reminders and recurring jobs run only while the app is open.", file=sys.stderr)
    sys.exit(0)
name = "lago-agents-scheduler.service"
desktop.service_write(name, [sys.executable, f"{sys.argv[1]}/background.py"], "Lago Agents: reminders, recurring jobs and test grading")
try:
    desktop.service_enable(name)
    print("Lago's scheduler runs in the background.")
except desktop.ServiceError as error:
    print(f"Lago's scheduler couldn't start in the background ({error}): reminders and recurring jobs run while the app is open.", file=sys.stderr)
if desktop.service_path("lago-agents-listen.service").exists():
    import listen
    listen.set_service(True)
    print("The wake-phrase listener is on (switch it off in Lago's settings).")
if desktop.service_state("lago-agents-remote.service")["enabled"]:
    import remote
    remote.write_unit()
    desktop.service_restart("lago-agents-remote.service")
    print("Phone access is on.")
PY
