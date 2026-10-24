#!/usr/bin/env bash
# Installs Lago Agents: on Linux, in the app menu; on macOS, as an app in ~/Applications.
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
    echo "Lago Agents removed from the app menu."
    echo "Running agents keep going; stop them with: tmux -L lago-agents kill-server"
    exit 0
fi

missing=()
for tool in python3 tmux git claude; do
    command -v "$tool" >/dev/null || missing+=("$tool")
done
if (( ${#missing[@]} )); then
    echo "Lago Agents needs ${missing[*]}, which isn't installed." >&2
    if [[ "$OS" == Darwin ]]; then
        echo "With Homebrew (https://brew.sh): brew install python tmux git; then Claude Code: npm install -g @anthropic-ai/claude-code" >&2
    fi
    exit 1
fi
python3 - "$OS" <<'PY' || { echo "See README.md, Install." >&2; exit 1; }
import sys
if sys.version_info < (3, 11):
    sys.exit("Lago Agents needs Python 3.11 or newer" + (" (brew install python)" if sys.argv[1] == "Darwin" else "") + ".")
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
PYTHON=python3

if [[ "$OS" == Darwin ]]; then
    # The window: a native one through pywebview, in a venv of the app's own (Homebrew's Python
    # won't take packages system-wide). Without it, Lago opens in Chrome (or your browser).
    VENV="$STATE_DIR/app-venv"
    mkdir -p "$STATE_DIR"
    if [[ ! -x "$VENV/bin/python" ]]; then
        python3 -m venv "$VENV"
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
python3 "$APP/validate_skills.py" "$DATA_DIR/claude-skills" \
    || echo "Some skills don't follow the Agent Skills spec (see above)." >&2

# Links into your data folder whose target is gone (a renamed or removed agent or skill).
for link in "$HOME"/.claude/agents/*.md "$HOME"/.claude/skills/*; do
    if [[ -L "$link" && ! -e "$link" && "$(readlink "$link")" == "$DATA_DIR"/* ]]; then
        rm "$link"
    fi
done

# Plugins: install the ones in your claude-plugins/plugins.json that are missing.
python3 "$APP/install_plugins.py"

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
python3 - "$APP" <<'PY' || true
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
