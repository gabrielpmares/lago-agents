#!/usr/bin/env bash
# Adds Lago Agents to the app menu.
#   ./install.sh              install
#   ./install.sh --uninstall  remove it from the app menu (agents and history are kept)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/lago"  # the app's code
# Your own config, agents and skills: never in this folder (see config_store.py).
DATA_DIR="${LAGO_AGENTS_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/lago-agents}"
MENU_FILE="$HOME/.local/share/applications/lago-agents.desktop"
ICON_FILE="$HOME/.local/share/icons/hicolor/scalable/apps/lago-agents.svg"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$MENU_FILE" "$ICON_FILE"
    echo "Lago Agents removed from the app menu."
    echo "Running agents keep going; stop them with: tmux -L lago-agents kill-server"
    exit 0
fi

for tool in python3 tmux git claude; do
    command -v "$tool" >/dev/null || { echo "Lago Agents needs '$tool', which isn't installed." >&2; exit 1; }
done
python3 - <<'PY' || { echo "See README.md, Install, for your distribution's packages." >&2; exit 1; }
import sys
if sys.version_info < (3, 11):
    sys.exit("Lago Agents needs Python 3.11 or newer.")
try:
    import gi
    for name, version in (("Gtk", "3.0"), ("WebKit2", "4.1"), ("Vte", "2.91")):
        gi.require_version(name, version)
    from gi.repository import Gtk, WebKit2, Vte  # noqa: F401
except (ImportError, ValueError) as error:
    sys.exit(f"Lago Agents needs PyGObject with GTK 3, WebKit2GTK 4.1 and VTE 2.91: {error}")
PY
# The scheduler, the wake-phrase listener and phone access are systemd user services.
HAVE_SYSTEMD=0
systemctl --user show-environment >/dev/null 2>&1 && HAVE_SYSTEMD=1

chmod +x "$APP/lago_agents.py" "$APP/lago_hook.py" "$APP/lago_mcp.py" \
         "$APP/install_plugins.py"
mkdir -p "$(dirname "$MENU_FILE")" "$(dirname "$ICON_FILE")"
cp "$APP/ui/lago-agents-icon.svg" "$ICON_FILE"
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

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

cat > "$MENU_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Lago Agents
Comment=Run and watch teams of AI agents
Exec=/usr/bin/python3 "$APP/lago_agents.py"
Icon=lago-agents
StartupWMClass=lago-agents
Categories=Development;
DESKTOP

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

# "Hey Lago": nothing listens unless you switch it on in Lago's settings, which installs the
# listener (listen.py). If you have, rewrite its unit so it follows this checkout.
if [ "$HAVE_SYSTEMD" = 1 ] && [ -f "$HOME/.config/systemd/user/lago-agents-listen.service" ]; then
    python3 -c "import sys; sys.path.insert(0, '$APP'); import listen; listen.set_service(True)" \
        && echo "The wake-phrase listener is on (switch it off in Lago's settings)."
fi

# Lago's clockwork: reminders, recurring jobs (daily scouting) and test grading, even
# while the app is closed. It runs with this shell's PATH, so it finds claude, gh and npx.
if [ "$HAVE_SYSTEMD" = 1 ]; then
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/lago-agents-scheduler.service" <<UNIT
[Unit]
Description=Lago Agents: reminders, recurring jobs and test grading

[Service]
ExecStart=/usr/bin/python3 "$APP/background.py"
Environment="PATH=$PATH"
Restart=always
RestartSec=10
Nice=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now lago-agents-scheduler.service
echo "Lago's scheduler runs in the background (systemctl --user status lago-agents-scheduler)."
else
    echo "No systemd user session: reminders and recurring jobs run only while the app is open." >&2
fi

# Phone access (remote.py): off until you switch it on in the app's Phone page. If it's
# already on, rewrite its unit so it follows this checkout and this shell's PATH.
if [ "$HAVE_SYSTEMD" = 1 ] && systemctl --user is-enabled --quiet lago-agents-remote.service 2>/dev/null; then
    python3 -c "import sys; sys.path.insert(0, '$APP'); import remote; remote.write_unit()"
    systemctl --user restart lago-agents-remote.service
    echo "Phone access is on (systemctl --user status lago-agents-remote)."
fi

echo "Lago Agents is in the app menu."
