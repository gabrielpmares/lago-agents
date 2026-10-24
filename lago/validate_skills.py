#!/usr/bin/env python3
"""Check every skill in a folder (default: claude-skills/ in your data folder) against the
Agent Skills spec (agentskills.io).

Errors break the spec: a bad or mismatched name, a missing or too long description.
Warnings are the spec's size advice: SKILL.md over 500 lines.
Exits 1 when any skill has an error.
"""
import os
import re
import sys
from pathlib import Path

SKILLS_DIR = Path(os.environ.get("LAGO_AGENTS_HOME") or
                  Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lago-agents") \
    / "claude-skills"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME, MAX_DESCRIPTION, MAX_COMPATIBILITY, MAX_LINES = 64, 1024, 500, 500


def frontmatter(text):
    """The top-level `key: value` pairs of the YAML frontmatter, or None if there is none."""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return None
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


def check(skill_dir):
    """(errors, warnings) for one skill folder."""
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        return ["no SKILL.md"], []
    text = path.read_text()
    fields = frontmatter(text)
    if fields is None:
        return ["no YAML frontmatter"], []

    errors, warnings = [], []
    name = fields.get("name", "")
    if not NAME_PATTERN.match(name) or len(name) > MAX_NAME:
        errors.append(f"name '{name}' must be 1-{MAX_NAME} lowercase letters, digits and single hyphens")
    if name != skill_dir.name:
        errors.append(f"name '{name}' must match the folder name '{skill_dir.name}'")
    description = fields.get("description", "")
    if not 1 <= len(description) <= MAX_DESCRIPTION:
        errors.append(f"description must be 1-{MAX_DESCRIPTION} characters (has {len(description)})")
    if "compatibility" in fields and not 1 <= len(fields["compatibility"]) <= MAX_COMPATIBILITY:
        errors.append(f"compatibility must be 1-{MAX_COMPATIBILITY} characters")
    lines = text.count("\n")
    if lines > MAX_LINES:
        warnings.append(f"SKILL.md has {lines} lines; move detail into references/ (spec advises < {MAX_LINES})")
    return errors, warnings


def main():
    failed = False
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else SKILLS_DIR
    for skill_dir in sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []:
        errors, warnings = check(skill_dir)
        for message in errors:
            print(f"claude-skills/{skill_dir.name}: {message}", file=sys.stderr)
        for message in warnings:
            print(f"claude-skills/{skill_dir.name}: warning: {message}", file=sys.stderr)
        failed = failed or bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
