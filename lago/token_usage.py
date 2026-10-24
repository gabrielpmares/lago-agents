#!/usr/bin/env python3
"""Token usage of Claude Code sessions, read from their transcripts.

Claude Code writes one transcript line per content block of a reply, each carrying the
same `usage`, so usage is counted once per message id. Subagent transcripts
(<session>/subagents/*.jsonl) are included.

    token_usage.py <session id | transcript path>   one session
    token_usage.py --agent <agent id>                a Lago agent's current session
    token_usage.py --project <dir> [--since DATE]    every session run in that folder
    add --json for machine-readable output
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
TOKEN_KINDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
               "output_tokens")
# What an API call would cost at Anthropic's API prices, in dollars per million tokens:
# (model id prefix, input, output, cache read). Cache writes cost 1.25x input. Plans don't
# bill this way, but it weighs models and cache use the way their limits do. First match
# wins, so a longer prefix goes before a shorter one; unknown models count as Opus.
PRICES = (("claude-fable-5-1", 10.0, 50.0, 0.25), ("claude-mythos", 10.0, 50.0, 0.25),
          ("claude-fable", 10.0, 50.0, 1.0), ("claude-opus-5-5", 4.0, 20.0, 0.2),
          ("claude-opus", 5.0, 25.0, 0.5), ("claude-sonnet-5", 2.0, 10.0, 0.2),
          ("claude-sonnet", 3.0, 15.0, 0.3), ("claude-haiku-5", 0.1, 0.5, 0.01),
          ("claude-haiku", 1.0, 5.0, 0.1))
CACHE_WRITE_RATE = 1.25


def empty():
    return {**{kind: 0 for kind in TOKEN_KINDS}, "messages": 0, "tools": Counter(),
            "graphify_calls": 0, "model": "", "cost": 0.0, "_seen": set()}


def add_entry(totals, entry):
    """Add one transcript entry (a parsed JSONL line) to `totals`."""
    if entry.get("type") != "assistant":
        return
    message = entry.get("message") or {}
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            totals["tools"][block.get("name", "?")] += 1
            command = (block.get("input") or {}).get("command", "")
            if block.get("name") == "Bash" and "graphify " in command:
                totals["graphify_calls"] += 1
            if str(block.get("name", "")).startswith("mcp__graphify"):
                totals["graphify_calls"] += 1
    message_id = message.get("id")
    if not message_id or message_id in totals["_seen"]:
        return
    totals["_seen"].add(message_id)
    usage = message.get("usage") or {}
    for kind in TOKEN_KINDS:
        totals[kind] += usage.get(kind) or 0
    totals["cost"] += cost(usage, message.get("model"))
    totals["messages"] += 1
    totals["model"] = message.get("model") or totals["model"]


def messages(path):
    """(epoch seconds, usage) for each API call in a transcript and its subagents', once each."""
    from datetime import datetime
    path, seen = Path(path), set()
    for transcript in [path, *sorted(path.with_suffix("").glob("subagents/*.jsonl"))]:
        try:
            lines = transcript.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if '"assistant"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            if entry.get("type") != "assistant" or not message.get("id") or message["id"] in seen:
                continue
            seen.add(message["id"])
            try:
                at = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                continue
            yield at, message.get("usage") or {}


def cost(usage, model):
    """Dollars one API call's `usage` would cost on `model` at API prices."""
    price = next((p for p in PRICES if (model or "").startswith(p[0])), PRICES[3])
    _, per_input, per_output, per_cache_read = price
    return ((usage.get("input_tokens") or 0) * per_input
            + (usage.get("cache_creation_input_tokens") or 0) * per_input * CACHE_WRITE_RATE
            + (usage.get("cache_read_input_tokens") or 0) * per_cache_read
            + (usage.get("output_tokens") or 0) * per_output) / 1_000_000


def session_cost(path):
    """Dollars a transcript and its subagents' transcripts would cost at API prices."""
    path, seen, total = Path(path), set(), 0.0
    for transcript in [path, *sorted(path.with_suffix("").glob("subagents/*.jsonl"))]:
        try:
            lines = transcript.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if '"assistant"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            if entry.get("type") != "assistant" or not message.get("id") or message["id"] in seen:
                continue
            seen.add(message["id"])
            total += cost(message.get("usage") or {}, message.get("model"))
    return total


def total_input(totals):
    """Every input token the model processed, cached or not."""
    return (totals["input_tokens"] + totals["cache_creation_input_tokens"]
            + totals["cache_read_input_tokens"])


def read_session(path):
    """Totals for one transcript plus its subagents' transcripts."""
    path = Path(path)
    totals = empty()
    for transcript in [path, *sorted(path.with_suffix("").glob("subagents/*.jsonl"))]:
        try:
            lines = transcript.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                add_entry(totals, json.loads(line))
            except ValueError:
                continue
    return totals


def project_dir(folder):
    """~/.claude/projects/<folder with / and . turned into ->."""
    name = str(Path(folder).expanduser().resolve()).replace("/", "-").replace(".", "-")
    return PROJECTS / name


def find_session(session_id):
    matches = list(PROJECTS.glob(f"*/{session_id}.jsonl"))
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def public(totals):
    """`totals` as plain JSON-ready values."""
    out = {key: value for key, value in totals.items() if not key.startswith("_")}
    out["tools"] = dict(totals["tools"].most_common())
    out["total_input"] = total_input(totals)
    return out


def describe(label, totals):
    tools = ", ".join(f"{name} {count}" for name, count in totals["tools"].most_common(8))
    return (f"{label}\n"
            f"  model        {totals['model'] or '?'}  ({totals['messages']} API calls)\n"
            f"  input        {totals['input_tokens']:>12,}\n"
            f"  cache write  {totals['cache_creation_input_tokens']:>12,}\n"
            f"  cache read   {totals['cache_read_input_tokens']:>12,}\n"
            f"  output       {totals['output_tokens']:>12,}\n"
            f"  total input  {total_input(totals):>12,}\n"
            f"  API price    {totals['cost']:>12,.2f} $\n"
            f"  tools        {tools or '-'}\n"
            f"  graphify     {totals['graphify_calls']} calls")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session", nargs="?", help="session id or transcript path")
    parser.add_argument("--agent", help="a Lago agent id (its current session)")
    parser.add_argument("--project", help="every session run in this folder")
    parser.add_argument("--since", help="with --project: only sessions changed on or after this date (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.agent:
        import hub
        args.session = hub.get_state(args.agent).get("session_id")
        if not args.session:
            parser.error(f"agent {args.agent!r} has no session yet")
    if args.project:
        paths = sorted(project_dir(args.project).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if args.since:
            from datetime import datetime
            cutoff = datetime.fromisoformat(args.since).timestamp()
            paths = [p for p in paths if p.stat().st_mtime >= cutoff]
    elif args.session:
        path = Path(args.session)
        paths = [path if path.suffix == ".jsonl" else find_session(args.session)]
    else:
        parser.error("give a session, --agent or --project")
    if not paths or paths[0] is None:
        sys.exit("No transcript found.")

    sessions = {p.stem: read_session(p) for p in paths}
    if args.json:
        print(json.dumps({sid: public(t) for sid, t in sessions.items()}, indent=2))
        return
    for session_id, totals in sessions.items():
        print(describe(session_id, totals))
    if len(sessions) > 1:
        grand = empty()
        for totals in sessions.values():
            for kind in (*TOKEN_KINDS, "messages", "graphify_calls", "cost"):
                grand[kind] += totals[kind]
            grand["tools"].update(totals["tools"])
        grand["model"] = ", ".join(sorted({t["model"] for t in sessions.values() if t["model"]}))
        print(describe(f"ALL {len(sessions)} sessions", grand))


if __name__ == "__main__":
    main()
