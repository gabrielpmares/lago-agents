#!/usr/bin/env python3
"""Install the Claude Code plugins listed in your plugins.json (claude-plugins/ in your data folder), through the `claude` CLI.

Adds each marketplace that's missing, installs each plugin that's missing, and switches
it on or off to match the manifest. Plugins already installed are left at the version
they have; Claude Code updates them itself. install.sh runs this.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(os.environ.get("LAGO_AGENTS_HOME") or
                Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lago-agents")
MANIFEST = DATA_DIR / "claude-plugins" / "plugins.json"


def claude(*args):
    return subprocess.run(["claude", "plugin", *args], capture_output=True, text=True)


def installed():
    result = claude("list", "--json")
    if result.returncode != 0:
        sys.exit(f"Couldn't list Claude Code plugins: {result.stderr.strip()}")
    return {plugin["id"]: plugin for plugin in json.loads(result.stdout)}


def main():
    if not MANIFEST.exists():
        return  # no plugins of your own listed
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    known = claude("marketplace", "list").stdout
    for name, repo in manifest["marketplaces"].items():
        if f"({repo})" not in known:
            result = claude("marketplace", "add", repo)
            print(f"Marketplace {name}: {'added' if result.returncode == 0 else result.stderr.strip()}")

    have = installed()
    for plugin in manifest["plugins"]:
        plugin_id = plugin["id"]
        if plugin_id not in have:
            result = claude("install", plugin_id)
            if result.returncode != 0:
                print(f"Couldn't install {plugin_id}: {result.stderr.strip()}", file=sys.stderr)
                continue
            print(f"Installed {plugin_id}.")
            have = installed()
        if have[plugin_id]["enabled"] != plugin["enabled"]:
            claude("enable" if plugin["enabled"] else "disable", plugin_id)


if __name__ == "__main__":
    main()
