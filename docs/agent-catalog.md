# The swarm catalog

Every file in `catalog/agents/` of your data folder (`~/.config/lago-agents`) is one swarm
agent; its tests are in `catalog/tests/`, MCP servers in `catalog/mcp/` and pipeline shapes
in `catalog/pipelines/`. Agents sleep: no session runs until the
Conductor wakes one for a step of a pipeline you approved. Switch agents on or off in the
Swarm page. A switched-off agent is invisible to the Conductor.

The frontmatter of each file is the agent's **manifest**: a public, atomic description
of what the agent does, what it needs and what it produces. Lago Agents shows it in the
app, and the Conductor reads it to pick agents and to check a pipeline before it runs.

## Manifest format

```markdown
---
name: Architect                  # display name
domain: development              # development, quality, product, marketing, ops, meta, ...
summary: >                       # one sentence: the single job this agent does
  Maps the impact of an approved user story and writes the ADR.
extends: architect               # optional: a Claude Code agent definition (~/.claude/agents
                                 # or the project's .claude/agents) whose prompt, tools and
                                 # model this agent runs with
inputs:                          # what the Conductor must give it
  - name: plan_file
    required: true
    description: Plan file holding the approved user story.
  - name: base_branch
    description: Branch to compare against (optional inputs leave out `required`).
outputs:                         # what it leaves behind, and where
  - name: adr
    description: ADR written into the plan file under "Architecture Decision Record".
requires: >                      # what must already be true before it runs
  An approved user story in the plan file.
use_when: >                      # when the Conductor should pick it
  Before any code is written for a feature or non-trivial fix.
avoid_when: >                    # when it shouldn't
  One-line fixes, or documentation-only changes.
tags: [architecture, adr, impact-analysis]
tools: Read, Grep                # optional: overrides the extended definition's tools
model: opus                      # optional: overrides the extended definition's model
source: owner/repo               # optional: the GitHub project it's built on (its stars
                                 # give it a head start in the ranking)
knowledge: <id>/knowledge        # optional: a specialist's notes, a folder under
                                 # catalog/specialists/ it may read (docs/specialists.md)
---

Optional instructions. With `extends`, they are added after the extended definition's
prompt. Without it, they are the agent's whole prompt.
```

Required inputs are checked when the Conductor proposes a pipeline. A step is rejected
unless it fills every required input, or the project has a variable of the same name
(set in the Projects page, e.g. `brief_file`).

## What the agent gets when it wakes

Each agent runs as `<project>-<agent id>`, a Claude Code session in the project's
folder, with a fresh conversation. Its task contains the project profile (folder,
repository, branch, stack, files to read first, the area it works in), the pipeline goal,
the step's instructions, its inputs, and the outputs its manifest promises.

## Ratings, tests and improvements

The Conductor rates each step it checks (`rate_step`), Lago each agent it runs (`rate_run`),
and you can rate any step in the app; your rating of a task replaces theirs and counts
three times as much.

Each agent has a test suite in `tests/<agent id>.json`
(`{"tests": [{"id", "name", "kind", "task", "inputs", "criteria"}]}`; `kind` is
`regression`, its everyday job, or `fix`, a failure it must not repeat). Edit it on the
agent's Evaluation page (Swarm, then the agent's score). Each test runs the agent for real
in `~/lago-work/evals/`, and a cheap model grades it against the criteria.

After `swarm.improve_after_downs` thumbs down on the current prompt (default 2, 0 turns it
off; set it in config.json), the agent gets an improvement:
1. The suite is filled up: a fix test per thumbs down, and regression tests up to 3.
2. The agent-designer rewrites the prompt. It sees the feedback and the fix tests, never
   the regression tests, so it can't tune the prompt to them.
3. The regression gate: the whole suite runs on the current and the new prompt in the same
   batch, `swarm.eval_repeats` times each (default 2, averaged). The new prompt must pass
   every test (70+), lose no more than 15 points on any, and score at least as well overall.
4. If it falls short, the designer gets one more try, told which tests fell short. After
   that, the agent keeps its prompt.
5. Only a rewrite that passed reaches you. You approve it, and can revert it later.

Only the prompt changes: the frontmatter, tools and model stay. See `evals.py`.

## Agents built from GitHub repos

The `github-agent-builder` agent turns a repo into a draft: `agent.md`, `tests.json`, and
how it runs the repo. That's `mcp.json` (a pinned MCP server launch) and/or `install.sh`
(sets the repo up in `~/lago-work/tools/<id>`). You approve the draft on the Swarm page.
Only then does the install script run, and the agent goes into `agents/`, its tests into
`tests/` and its MCP server into `mcp/<id>.json`:

```json
{"command": "npx", "args": ["-y", "some-mcp@1.4.0"], "blocked_tools": ["write_file"],
 "source": "owner/repo@sha"}
```

The command must be npx, uvx, node, python3, docker, or a program under
`~/lago-work/tools`. `{run_dir}` in an argument becomes the session's run folder. An agent
uses the server by naming it in `mcp:`.

## Pipeline templates

`pipelines/*.md` are proven pipeline shapes: stages, gates and retry rules. The
Conductor starts from a template when one fits and drops the stages a request doesn't
need. Frontmatter: `name`, `summary`, `domain` and `agents` (the agent ids it uses). The
body describes the stages.

## The Conductor

`lago/prompts/conductor.md` is the Conductor's own prompt. It isn't a catalog agent: it's the one
agent that is always available to talk to.
