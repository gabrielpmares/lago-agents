#!/usr/bin/env python3
"""The `lago` MCP server: the tools an agent uses to work with its team.

Claude Code starts one per agent session (stdio, newline-delimited JSON-RPC), with
LAGO_AGENT_ID naming the agent. Leads get delegation tools; everyone can report
results and talk to their lead.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import activity  # noqa: E402
import config_store  # noqa: E402
import desktop  # noqa: E402
import hub  # noqa: E402
import notify  # noqa: E402
import transcripts  # noqa: E402
import agentdefs  # noqa: E402
import evals  # noqa: E402
import ghcatalog  # noqa: E402
import finder  # noqa: E402
import outcomes  # noqa: E402
import publish  # noqa: E402
import github  # noqa: E402
import runner  # noqa: E402
import swarm  # noqa: E402
import team  # noqa: E402

AGENT_ID = os.environ.get("LAGO_AGENT_ID", "")
# `lago_mcp.py --public`: the server other Claude sessions (a terminal, an IDE) add with
# `claude mcp add`, to see and steer Lago's work. It has no agent identity and a small tool
# set, and never opens sessions without permission checks.
PUBLIC = "--public" in sys.argv[1:] and not AGENT_ID
# The public server registered in Claude Code or Codex, started inside an agent's session:
# the agent has its own lago server, so this one offers nothing.
SILENT = "--public" in sys.argv[1:] and bool(AGENT_ID)
PROTOCOL_VERSION = "2025-06-18"
WAIT_POLL_SECONDS = 3
MAX_WAIT_MINUTES = 30

_write_lock = threading.Lock()
_workers = []


def send(message):
    with _write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


# ---------------------------------------------------------------- tools

def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


COMMON_TOOLS = {
    "complete_task": {
        "description": "Report the result of a task you were given (the #N in "
                       "\"[Lago task #N from ...]\"). Call it once per task, when it's finished "
                       "or when you can't finish it.",
        "inputSchema": _schema({
            "task_id": {"type": "integer"},
            "result": {"type": "string", "description": "What you did, where the outputs "
                       "are, and anything left open or needing a decision."},
            "status": {"type": "string", "enum": ["done", "failed"], "default": "done"},
        }, ["task_id", "result"]),
    },
    "my_tasks": {
        "description": "List the tasks assigned to you that are still open.",
        "inputSchema": _schema({}),
    },
    "comment_task": {
        "description": "Comment on a task you're doing or handed out (#N): a question, a progress "
                       "note, an answer to a comment. It reaches the other side of the task as a "
                       "message, and stays on the task's thread for the human.",
        "inputSchema": _schema({"task_id": {"type": "integer"}, "text": {"type": "string"}},
                               ["task_id", "text"]),
    },
    "request_secret": {
        "description": "Ask the human for a secret you need (an API key, a token, a password): "
                       "never ask for its value in chat. They give it or decline in the app; you "
                       "get a message saying how to load it. Carry on with other work meanwhile.",
        "inputSchema": _schema({"env": {"type": "string", "description": "The environment variable, like STRIPE_KEY."},
                                "reason": {"type": "string", "description": "What you need it for."}},
                               ["env", "reason"]),
    },
    "message_lead": {
        "description": "Send a message to the agent you report to (a question, a blocker, "
                       "an early heads-up). It arrives in their session when they're free.",
        "inputSchema": _schema({"text": {"type": "string"}}, ["text"]),
    },
}

LEAD_TOOLS = {
    "delegate": {
        "description": "Hand a task to one of your direct reports. Their session starts if "
                       "it isn't running. Returns the task id at once; the work happens in "
                       "parallel. Include everything they need: they can't see your "
                       "conversation.",
        "inputSchema": _schema({
            "agent": {"type": "string", "description": "The report's agent id or "
                      "definition name."},
            "title": {"type": "string", "description": "A short title, under 80 characters."},
            "task": {"type": "string", "description": "Full instructions: goal, inputs "
                     "(file paths), expected output and where to write it."},
            "after": {"type": "array", "items": {"type": "integer"}, "description": "Task ids "
                      "it must wait for: it stays blocked, and goes out by itself once they're "
                      "all done."},
            "labels": {"type": "array", "items": {"type": "string"}},
        }, ["agent", "title", "task"]),
    },
    "wait_for_tasks": {
        "description": "Wait until the given delegated tasks are finished (or the timeout "
                       "passes) and return their results.",
        "inputSchema": _schema({
            "task_ids": {"type": "array", "items": {"type": "integer"}},
            "timeout_minutes": {"type": "number", "default": 15,
                                "description": f"At most {MAX_WAIT_MINUTES}."},
        }, ["task_ids"]),
    },
    "team_status": {
        "description": "What each of your direct reports is doing right now, and the tasks "
                       "you've handed out that are still open or recently finished.",
        "inputSchema": _schema({}),
    },
    "message_agent": {
        "description": "Send a message to one of your direct reports without creating a "
                       "task (extra context, a correction, an answer to their question).",
        "inputSchema": _schema({"agent": {"type": "string"}, "text": {"type": "string"}},
                               ["agent", "text"]),
    },
}


_STEP_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string", "description": "Short step id, e.g. \"spec\" or \"review-api\"."},
    "agent": {"type": "string", "description": "The swarm agent id that runs this step."},
    "title": {"type": "string", "description": "What this step does, under 80 characters."},
    "task": {"type": "string", "description": "Full instructions for the agent: what to do, "
             "which files to read, where to write its output. It sees nothing else."},
    "inputs": {"type": "object", "additionalProperties": {"type": "string"},
               "description": "Values for the agent's manifest inputs (name → value). Required "
               "inputs must be here unless the project has a variable with the same name."},
    "after": {"type": "array", "items": {"type": "string"},
              "description": "Ids of earlier steps that must finish first. Steps with no "
              "unfinished dependencies can run in parallel."},
    "gate": {"type": "boolean", "description": "true: stop and show the human this step's "
             "result before any step that depends on it runs."},
    "skills": {"type": "array", "items": {"type": "string"},
               "description": "Skills (names from find_skills) the agent should load for this step."},
    "criteria": {"type": "array", "items": {"type": "string"},
                 "description": "Acceptance criteria (at most 10). The step is done only when "
                 "the agent answers every one as met."},
    "worktree": {"type": "boolean", "description": "true for a step that edits code while "
                 "other steps may too: it works in its own git worktree and branch "
                 "(lago/p<pipeline>-<step>), which you merge once it's checked."},
    "reviewers": {"type": "array", "items": {"type": "string"},
                  "description": "Swarm agents (at most 3) that review the result in order; it "
                  "goes back to the agent with their changes until each approves."},
}, "required": ["id", "agent", "title", "task"]}

CONDUCTOR_TOOLS = {
    "list_projects": {
        "description": "The projects you can work on, with their areas.",
        "inputSchema": _schema({}),
    },
    "project_profile": {
        "description": "Everything configured for a project: folder, repo, branch, stack, files "
                       "to read first, links, services with their live status, GitHub repos and "
                       "boards, variables, areas and preferred agents, plus its git state. "
                       "Without a project, your own.",
        "inputSchema": _schema({"project": {"type": "string"}}),
    },
    "find_skills": {
        "description": "Search the Claude Code skills installed here (yours, the project's and "
                       "plugins'). Assign the useful ones to steps with `skills`. Also lists the "
                       "installed plugins, which every agent has.",
        "inputSchema": _schema({"query": {"type": "string", "description": "Keywords; empty "
                                          "lists them all."}}),
    },
    "github_board": {
        "description": "A GitHub Project board of your project: its items by status column "
                       "(Done is left out unless you ask for it).",
        "inputSchema": _schema({
            "project": {"type": "string", "description": "Project id; default: yours."},
            "board": {"type": "string", "description": "Board name or number; default: the first."},
            "status": {"type": "string", "description": "Only this column, e.g. \"Todo\"."},
        }),
    },
    "github_repo": {
        "description": "Open pull requests, open issues and recent CI runs of one of your "
                       "project's repositories; with number, that issue or PR in full (body, "
                       "last comments, PR checks).",
        "inputSchema": _schema({"repo": {"type": "string", "description": "owner/name; default: "
                                         "the project's main repository."},
                                "number": {"type": "integer"},
                                "project": {"type": "string", "description": "Project id; default: yours."}}),
    },
    "find_agents": {
        "description": "Search the switched-on swarm agents by keywords and/or domain. Returns "
                       "first the agents already used for similar tasks (with how those went), "
                       "then each match's manifest card (purpose, when to use it, inputs, "
                       "outputs) and a one-line summary of every other agent. With no "
                       "arguments, cards for them all.",
        "inputSchema": _schema({
            "query": {"type": "string", "description": "Keywords or the task in a sentence, "
                                                       "e.g. \"review security api\"."},
            "domain": {"type": "string", "description": "e.g. development, quality, marketing."},
            "project": {"type": "string", "description": "Project the task is for; default: "
                                                         "yours."},
        }),
    },
    "read_manifest": {
        "description": "An agent's full manifest: inputs (required or not), outputs, what it "
                       "requires first, and when to use or avoid it.",
        "inputSchema": _schema({"agent": {"type": "string"}}, ["agent"]),
    },
    "list_pipeline_templates": {
        "description": "Proven pipeline shapes (stages, gates, retry rules) to start from.",
        "inputSchema": _schema({}),
    },
    "read_pipeline_template": {
        "description": "A pipeline template's full description.",
        "inputSchema": _schema({"template": {"type": "string"}}, ["template"]),
    },
    "pull_request": {
        "description": "Act on a pull request of this project: `status` (checks, review decision, "
                       "whether the merge policy lets it merge), `review` (post a verdict as a "
                       "signed comment: approve, changes or comment, with what you found; a review "
                       "step's agent normally does this), or `merge` (only when the project's merge "
                       "policy lets the Conductor merge and every rule passes; otherwise ask the "
                       "human, who merges from the app).",
        "inputSchema": _schema({
            "action": {"type": "string", "enum": ["status", "review", "merge"]},
            "repo": {"type": "string", "description": "owner/name"},
            "number": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["approve", "changes", "comment"]},
            "body": {"type": "string"},
            "reviewer": {"type": "string", "description": "For review: the agent whose verdict this "
                         "is (e.g. code-reviewer); default you."},
        }, ["action", "repo", "number"]),
    },
    "share_document": {
        "description": "Share a file this session produced (a spec, ADR, report) with the human: "
                       "it appears under Documents on the session's page, every change to it is "
                       "kept as a revision, and their comments on it arrive as messages. Plan files "
                       "in .claude/plans/ are shared automatically.",
        "inputSchema": _schema({"path": {"type": "string", "description": "Absolute, or relative "
                                         "to the project folder."},
                                "title": {"type": "string"}}, ["path"]),
    },
    "propose_agent": {
        "description": "When no switched-on swarm agent fits a step (find_agents found none), "
                       "draft a new one for the human to approve on the Swarm page: one narrow "
                       "job, its prompt, and at least 3 tests. Nothing joins the swarm until they "
                       "approve. Meanwhile plan with what exists, or wait.",
        "inputSchema": _schema({
            "id": {"type": "string", "description": "lowercase-with-dashes, e.g. \"sql-migrator\"."},
            "name": {"type": "string"},
            "summary": {"type": "string", "description": "Its one job, in one sentence."},
            "prompt": {"type": "string", "description": "Its instructions, as a system prompt."},
            "reason": {"type": "string", "description": "Why the swarm needs it: the step it's for "
                       "and why no existing agent fits."},
            "domain": {"type": "string"},
            "tools": {"type": "string", "description": "Claude Code tools, comma-separated "
                      "(no Agent or Task). Empty: the usual set."},
            "use_when": {"type": "string"}, "avoid_when": {"type": "string"},
            "inputs": {"type": "array", "items": {"type": "object"}},
            "outputs": {"type": "array", "items": {"type": "object"}},
            "tests": {"type": "array", "items": {"type": "object"}, "description": "At least 3: "
                      "{task, criteria} each, where criteria say what a good result contains."},
        }, ["id", "name", "summary", "prompt", "reason", "tests"]),
    },
    "propose_pipeline": {
        "description": "Save a pipeline for the human to approve in the Pipelines page. It is "
                       "checked against the manifests: every agent must be switched on and every "
                       "required input filled. Nothing runs until the human approves it.",
        "inputSchema": _schema({
            "area": {"type": "string", "description": "The project area it touches. Default: "
                     "your work session's area."},
            "title": {"type": "string"},
            "goal": {"type": "string", "description": "The request as you understood it, what "
                     "done looks like, and anything explicitly out of scope."},
            "steps": {"type": "array", "items": _STEP_SCHEMA},
        }, ["title", "goal", "steps"]),
    },
    "revise_pipeline": {
        "description": "Change a pipeline's steps, title or goal. It goes back to the human "
                       "for approval. Finished steps that keep their id and agent stay finished.",
        "inputSchema": _schema({
            "pipeline_id": {"type": "integer"},
            "title": {"type": "string"},
            "goal": {"type": "string"},
            "steps": {"type": "array", "items": _STEP_SCHEMA,
                      "description": "The complete new list of steps."},
        }, ["pipeline_id"]),
    },
    "pipeline_status": {
        "description": "A pipeline's status and each step's progress and result. Without an "
                       "id, lists this work session's open pipelines.",
        "inputSchema": _schema({"pipeline_id": {"type": "integer"}}),
    },
    "run_step": {
        "description": "Wake the step's agent in the project folder and give it the step as a "
                       "task. Only for approved pipelines, once the step's dependencies are "
                       "done. Also re-runs a failed or finished step: put what to fix in notes.",
        "inputSchema": _schema({
            "pipeline_id": {"type": "integer"},
            "step": {"type": "string", "description": "The step id."},
            "notes": {"type": "string", "description": "Extra context for this run, e.g. a "
                      "reviewer's findings to fix, or results of earlier steps."},
        }, ["pipeline_id", "step"]),
    },
    "rate_step": {
        "description": "Thumbs up or down for the agent that ran a step, once you've checked "
                       "its result: up when it did its job well, down when a check failed it, "
                       "you had to re-run it, or the output was poor. The reason is what the "
                       "agent learns from: say what was wrong or right, concretely.",
        "inputSchema": _schema({"pipeline_id": {"type": "integer"}, "step": {"type": "string"},
                                "verdict": {"type": "string", "enum": ["up", "down"]},
                                "reason": {"type": "string"}},
                               ["pipeline_id", "step", "verdict", "reason"]),
    },
    "skip_step": {
        "description": "Mark a step as not needed, with the reason. Steps after it can run.",
        "inputSchema": _schema({"pipeline_id": {"type": "integer"}, "step": {"type": "string"},
                                "reason": {"type": "string"}}, ["pipeline_id", "step", "reason"]),
    },
    "report": {
        "description": "Tell the human something: Lago records it, speaks it aloud (\"Sam, "
                       "the task on Webstore is done\") and, if Lago (the human's assistant) opened this session, "
                       "relays it there. \"solved\" when the whole task is delivered and checked "
                       "and nothing is left (the human can close the session); \"done\" when a "
                       "stage is finished but more is left (finish_pipeline reports for you); also "
                       "when you need an answer, at a gate, and when you're blocked. \"update\" "
                       "is silent progress for Lago.",
        "inputSchema": _schema({
            "kind": {"type": "string", "enum": ["solved", "done", "failed", "question", "gate",
                                                "blocked", "update"]},
            "text": {"type": "string", "description": "What happened or what you need, complete "
                     "enough to act on without reading your terminal: the question with its "
                     "options and your recommendation, or what was delivered and where."},
            "questions": {"type": "array", "description": "For a question with clear options: "
                          "up to 5, each {id, prompt, choices (up to 8; none means a written "
                          "answer), multi?, recommended?}. The human answers with a click in the "
                          "app or on their phone, and the answers arrive as a message.",
                          "items": {"type": "object"}},
        }, ["kind", "text"]),
    },
    "finish_pipeline": {
        "description": "Close a pipeline with a summary for the human, and put its agents back "
                       "to sleep.",
        "inputSchema": _schema({
            "pipeline_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["done", "failed"]},
            "summary": {"type": "string", "description": "What was delivered, where, and what "
                        "is left open."},
            "task_complete": {"type": "boolean", "description": "true only when this finishes "
                              "the session's whole task: delivered, checked, nothing left open "
                              "and no further pipeline planned. The human then hears it's fully "
                              "done and can close the session."},
        }, ["pipeline_id", "status", "summary"]),
    },
}


ORCHESTRATOR_TOOLS = {
    "list_projects": CONDUCTOR_TOOLS["list_projects"],
    "find_agents": CONDUCTOR_TOOLS["find_agents"],
    "run_agent": {
        "description": "Use one swarm agent for one task, right now: no work session, plan or "
                       "approval. For a single job one agent does (a web search, a review, a "
                       "draft). Its result comes back to you as a \"[Lago] Task #N\" message.",
        "inputSchema": _schema({
            "agent": {"type": "string", "description": "Agent id (find_agents)."},
            "task": {"type": "string", "description": "What to do, in full; it sees only this."},
            "inputs": {"type": "object", "description": "Its manifest's inputs, name → value."},
            "project": {"type": "string", "description": "Work in this project's folder."},
            "folder": {"type": "string", "description": "Or in this folder (default "
                       "~/lago-work/runs)."},
            "mode": {"type": "string", "enum": ["cheap", "certain"], "default": "cheap",
                     "description": "cheap: the agent's own model and limits. certain: a "
                     "stronger model, room to verify; slower and costs more."},
            "skills": {"type": "array", "items": {"type": "string"}, "description":
                       "Installed skills it should load first (find_skills), e.g. "
                       "[\"gws-calendar-agenda\", \"gws-shared\"]. Agents see other skills "
                       "by name only, so name the ones the task needs."},
        }, ["agent", "task"]),
    },
    "rate_run": {
        "description": "Thumbs up or down for a swarm agent you ran (run_agent), once its "
                       "result is in: up when it answered well, down when it was wrong, "
                       "incomplete, off-task or needlessly long. Also when the human says what "
                       "they thought of it. The reason is what the agent learns from.",
        "inputSchema": _schema({"task_id": {"type": "integer"},
                                "verdict": {"type": "string", "enum": ["up", "down"]},
                                "reason": {"type": "string"},
                                "by_human": {"type": "boolean", "description": "true when it's "
                                             "the human's own verdict, relayed by you."}},
                               ["task_id", "verdict", "reason"]),
    },
    "post_news": {
        "description": "Put news in the human's bell: the result of a recurring job (daily "
                       "scouting, a check) that they should see even if they aren't looking at "
                       "the chat. First line is the headline. Relay it in the chat too.",
        "inputSchema": _schema({"text": {"type": "string"}}, ["text"]),
    },
    "use_agent": {
        "description": "Work with one swarm agent only, when the human asks (\"use the "
                       "reverse engineer for this\", \"let's work with the browser agent\"): "
                       "from then on every task goes to it with run_agent, and nothing else "
                       "runs. With no agent, back to normal (\"stop using it\").",
        "inputSchema": _schema({"agent": {"type": "string", "description": "Agent id "
                                          "(find_agents); empty to stop."}}),
    },
    "project_profile": {**CONDUCTOR_TOOLS["project_profile"],
                        "inputSchema": _schema({"project": {"type": "string"}}, ["project"])},
    "github_board": CONDUCTOR_TOOLS["github_board"],
    "github_repo": CONDUCTOR_TOOLS["github_repo"],
    "open_work_session": {
        "description": "Start a work session: a Conductor in the project's folder that takes the "
                       "request, plans the work with the swarm agents and runs it once the human "
                       "approves. It reports back to you. One session per task. Without a "
                       "project it works outside them, in `folder` or a new folder of its own.",
        "inputSchema": _schema({
            "project": {"type": "string", "description": "Project id (list_projects); empty "
                        "for work that belongs to no project."},
            "folder": {"type": "string", "description": "Only without a project: where to "
                       "work (default ~/lago-work/session-N)."},
            "area": {"type": "string", "description": "Area id within the project, if the task "
                     "belongs to one. Empty: the whole project."},
            "request": {"type": "string", "description": "What the human wants, in full: the "
                        "task, links to issues or board items, what done looks like, what's out of "
                        "scope, and the answers they already gave you. The Conductor sees only "
                        "this."},
            "goal": {"type": "integer", "description": "The goal # it serves (app goal list), "
                     "if one fits."},
        }, ["request"]),
    },
    "create_project": {
        "description": "Add a new project to Lago Agents, so you can open work sessions in it. "
                       "Creates its folder if it doesn't exist (and runs `git init` there when "
                       "asked). Only when the human asks for a new project; confirm the name "
                       "and folder with them first. Services, servers, GitHub boards and the "
                       "rest are connected later in the app (the project's Settings).",
        "inputSchema": _schema({
            "name": {"type": "string", "description": "Display name, e.g. \"Webstore Mobile\"."},
            "folder": {"type": "string", "description": "The project's folder, e.g. "
                       "~/projects/webstore-mobile. An existing folder is used as it is."},
            "description": {"type": "string", "description": "One or two sentences: what it is."},
            "stack": {"type": "string", "description": "Languages and frameworks, if known."},
            "repo": {"type": "string", "description": "Its repository, e.g. "
                     "github.com/owner/name, if there is one."},
            "areas": {"type": "array", "items": {"type": "string"}, "description": "Section "
                      "names, if the human named some (e.g. api, web). Optional."},
            "git_init": {"type": "boolean", "default": False, "description": "Run git init in "
                         "a folder that isn't a git repository yet."},
        }, ["name", "folder"]),
    },
    "app": {
        "description": "Do what the human can do in the app. Actions (args):\n"
                       "approve|deny|interrupt|sleep|screen (target: session # or agent id; "
                       "approve/deny pick the agent waiting for permission; screen shows its "
                       "terminal) · ask_human (target: pass a permission prompt you were handed "
                       "on to the human, only when it's critical) · reopen (target) · services (project) · service (project, "
                       "service, do: start|stop|logs) · run_all (project, do: start|stop) · "
                       "server (project, server?) · edit_project (project, changes: fields to "
                       "set; dicts merge, null deletes, \"+key\" appends to a list) · accounts · "
                       "use_account (account) · auto_switch (on) · budget (target?, do: show|extend|stop, amount? "
                       "dollars; or agent?/session? dollars to set the defaults: an agent that "
                       "used its budget stops until extended) · budget_windows (windows?: "
                       "[{scope: all|project:<id>|agent:<id>, window: week|month, limit}] replaces "
                       "them; none shows them) · create_agent (id, name, summary, prompt; "
                       "optional domain, use_when, avoid_when, requires, inputs/outputs [{name, "
                       "required, description}], tags, tools, model, extends, replace: add a swarm "
                       "agent any project's Conductor can use) · new_conversation (when the human asks to "
                       "clear or restart this chat; after your reply it's written to your notes "
                       "and you start afresh) · history (session?, kind?: task|plan|budget|"
                       "decision|config|session|routine|watchdog, limit?: what happened, newest "
                       "first) · search (query, session?: tasks, reports, plans and history) · "
                       "config_history (revision?: list changes to the app's settings, or one "
                       "in detail) · undo_config (revision, which?: before|after) · backups "
                       "(do?: list|now) · cards (do?: list|add|refresh|archive; title, project?, "
                       "query?, id: standing status notes rewritten when what they watch changes; "
                       "\"summary\" with project gives that project's summary) · fetch_skill (repo, path?, ref?: fetch a skill from a git "
                       "repository for the human to review and install on the Swarm page) · export (teams?, catalog?, skills?, templates?: lists of "
                       "ids; path?) · import (path, apply?: false previews, true adds what's new) "
                       "· goal (do: list|add|change|achieve|drop|link|show; "
                       "project?, title, description?, parent?, target?; link takes session and "
                       "id (or none to unlink)) · hold (target: session #, on: true pauses the "
                       "session and all its agents, false lets it carry on; reason?) · routine (do: list|create|change|pause|resume|"
                       "archive|run|runs|hook|revisions|restore; id; create/change take title, "
                       "agent, instructions, project?, folder?, repeat (hourly|daily|weekdays|"
                       "weekly|minutes|never), every_minutes?, when? first time, concurrency? "
                       "skip|parallel, catch_up? skip|once, only_if_changed? git, report? "
                       "lago|failures; restore takes revision) · secrets (project?: names and "
                       "variables of the secrets agents get; values are never shown; the human "
                       "adds them in project Settings) · watchdog (on?, stall_minutes?: it tells sessions when an "
                       "agent stalls, loops or crashes) · quiet (target: session #, on: speak and ring only for "
                       "what needs the human) · settings (mode: check|decide, model, talk, "
                       "enabled, quiet_from, quiet_to, rate, volume (25-200, %), voice, effect: any of "
                       "them)",
        "inputSchema": _schema({"action": {"type": "string"}, "args": {"type": "object"}},
                               ["action"]),
    },
    "open_tasks": {
        "description": "Every open task of an agent you ran directly (run_agent or New Task), "
                       "with where it honestly stands: working, quiet for hours, waiting on "
                       "usage, stopped before finishing, waiting on the human, or not started "
                       "yet, and the agent's last words. Work sessions are in work_sessions.",
        "inputSchema": _schema({}),
    },
    "continue_task": {
        "description": "Get a run that stopped before finishing (or went quiet) going again, "
                       "on its own conversation. Fails while every account is out of usage.",
        "inputSchema": _schema({"task_id": {"type": "integer"}}, ["task_id"]),
    },
    "abandon_task": {
        "description": "Stop a run and close its task as cancelled. Only when the human said "
                       "to drop it, in either mode: say which task and why in `reason`.",
        "inputSchema": _schema({"task_id": {"type": "integer"},
                                "reason": {"type": "string", "description": "Why, in their words."}},
                               ["task_id", "reason"]),
    },
    "work_sessions": {
        "description": "The open work sessions: project, task, whether the Conductor is working "
                       "or waiting, the latest pipeline and the latest report. Add closed=true "
                       "to include closed ones.",
        "inputSchema": _schema({"closed": {"type": "boolean", "default": False}}),
    },
    "session_detail": {
        "description": "One work session in depth: its latest pipeline and steps, its recent "
                       "reports and the Conductor's last message (more=true: longer and more).",
        "inputSchema": _schema({"session": {"type": "integer"}, "more": {"type": "boolean"}},
                               ["session"]),
    },
    "message_session": {
        "description": "Send a message to a work session's Conductor: the human's answer to its "
                       "question, a go-ahead after a gate, extra context or a change of plan. It "
                       "arrives when the Conductor is free (and wakes it if it was paused).",
        "inputSchema": _schema({"session": {"type": "integer"}, "text": {"type": "string"}},
                               ["session", "text"]),
    },
    "answer_question": {
        "description": "Answer a Conductor's question that came with a form (report #N): the "
                       "human's answer they gave you, or, in Decide mode, your own on a routine or "
                       "important one (then also log it with `decide`). answers maps each "
                       "question id to a choice, a list of choices (multi) or text.",
        "inputSchema": _schema({"report": {"type": "integer"}, "answers": {"type": "object"},
                                "note": {"type": "string"}}, ["report", "answers"]),
    },
    "decide": {
        "description": "Decide mode only: take a decision for the human and act on it. With "
                       "`plan` (approve, changes or reject) it decides the plan waiting in that "
                       "session; otherwise `message` (or the decision and reason) goes to its "
                       "Conductor as the human's answer. importance: routine (decide quietly) or "
                       "important (decide, then tell the human in one line what you chose and how "
                       "to undo it). Critical decisions are never yours to take: bring them to the "
                       "human. Every decision is logged for the human to review.",
        "inputSchema": _schema({
            "session": {"type": "integer"},
            "decision": {"type": "string", "description": "One line: what was to be decided and what you chose."},
            "reason": {"type": "string", "description": "Why, in business terms."},
            "importance": {"type": "string", "enum": ["routine", "important"]},
            "plan": {"type": "string", "enum": ["approve", "changes", "reject"]},
            "message": {"type": "string", "description": "What the Conductor should hear, when it needs more than the decision."},
        }, ["session", "decision", "reason", "importance"]),
    },
    "close_work_session": {
        "description": "Close a work session: stops its Conductor and agents and cancels its open "
                       "pipelines. Only when the human asks for it.",
        "inputSchema": _schema({"session": {"type": "integer"}}, ["session"]),
    },
    "github_agents": {
        "description": "Lago's own list of agents on GitHub (MCP servers, Claude Code agents, "
                       "skills, plugins), open-source licensed only: the ones gaining the most "
                       "stars lately, by topic, or the matches for a search, each in plain "
                       "words (what it does, needs, produces, risk). Not installed: importing "
                       "one is `import_github_agent`, only with the human's yes.",
        "inputSchema": _schema({"topic": {"type": "string", "description": "One of: "
                                          + ", ".join(ghcatalog.TOPICS) + "."},
                                "query": {"type": "string", "description": "The task in a "
                                          "sentence, instead of a topic."}}),
    },
    "find_skills": {
        "description": "Search the skills installed here (Google Workspace, logo design, ...): "
                       "what each lets an agent do, and which program it still needs. Check "
                       "these before telling the human you can't reach something.",
        "inputSchema": _schema({"query": {"type": "string", "description": "Keywords; empty "
                                          "lists them all."}}),
    },
    "open_setup_terminal": {
        "description": "Open a terminal window on the human's screen running one setup command "
                       "they need to finish themselves: a sign-in that opens their browser "
                       "(e.g. `gws auth login`), or installing a program a skill needs. Only "
                       "after their yes. The command's program must be one a skill needs, or "
                       "npm, npx, pipx, uv, gcloud or gh.",
        "inputSchema": _schema({"command": {"type": "string"},
                                "why": {"type": "string", "description": "One line for the "
                                        "terminal's title, e.g. sign in to Google Calendar."}},
                               ["command", "why"]),
    },
    "explain_github_agent": {
        "description": "Read a repo from github_agents and judge it for the human: what it "
                       "does, what they'd gain, its risks, a verdict (use, try or skip) and "
                       "which of their projects it fits and how. For \"what does X do?\" or "
                       "\"should I use X?\".",
        "inputSchema": _schema({"repo": {"type": "string", "description": "owner/name"}}, ["repo"]),
    },
    "import_github_agent": {
        "description": "Install a repo from github_agents: Lago fetches it at a pinned "
                       "commit, checks its files and installs its agents, skills or MCP server "
                       "(with the careful import switched on in Settings, or careful: true, it "
                       "becomes a draft the human approves instead). If it says Lago couldn't "
                       "install it in one step, offer the careful import for this repo and call "
                       "again with careful: true when they agree. Only after the human said yes "
                       "to importing this repo, in either mode.",
        "inputSchema": _schema({"repo": {"type": "string", "description": "owner/name"},
                                "notes": {"type": "string", "description": "What they want "
                                          "it for."},
                                "careful": {"type": "boolean", "description": "Through the "
                                            "builder's sandbox test, for this repo only."}},
                               ["repo"]),
    },
    "create_agent": {
        "description": "When no agent the human has, and nothing on GitHub, fits a job that "
                       "will come back: save a new agent as a draft they approve on the Agents "
                       "page. Design it with the agent-designer first (run_agent), and build "
                       "the tests from the human's own examples (\"given this, I expect "
                       "that\"). Once they approve, it joins the swarm and its tests run.",
        "inputSchema": CONDUCTOR_TOOLS["propose_agent"]["inputSchema"],
    },
    "prepare_publish": {
        "description": "Get one of the human's agents ready to publish on GitHub for other "
                       "people: Lago scrubs secrets, emails, addresses, paths and their name "
                       "out of a copy, and lists words they should check. Only when they ask. "
                       "You can't publish: they read the exact files and press Publish on the "
                       "agent's page.",
        "inputSchema": _schema({"agent": {"type": "string"}}, ["agent"]),
    },
    "judge_result": {
        "description": "The human's verdict on a work session's whole result, when they tell "
                       "you (\"that's exactly it\", \"this is wrong\"): accepted, every agent "
                       "that did a step earns a share of the points, by how its step went; "
                       "rejected, the steps that failed or were rated down lose points. Only "
                       "their verdict, never yours. Judging again replaces it.",
        "inputSchema": _schema({"session": {"type": "integer"},
                                "accepted": {"type": "boolean"},
                                "reason": {"type": "string", "description": "In their words; "
                                           "required when rejected."}},
                               ["session", "accepted"]),
    },
    "test_drive": {
        "description": "Give the same task to 2 or 3 swarm agents at once, each in its own "
                       "scratch folder, so the human can compare their results and pick one. "
                       "For a task that matters or will come back, when find_agents shows "
                       "several good candidates and the human agrees to the extra cost. The "
                       "results arrive as \"[Lago] Task #N\" messages; show them side by side "
                       "and ask which is best.",
        "inputSchema": _schema({"agents": {"type": "array", "items": {"type": "string"}},
                                "task": {"type": "string", "description": "The full brief, "
                                         "the same for every agent."},
                                "project": {"type": "string"}},
                               ["agents", "task"]),
    },
    "pick_test_drive": {
        "description": "Record which agent's result the human picked in a test drive: a small "
                       "rating up for it, a tiny one down for the others. Without `agent`, "
                       "shows where the drive stands.",
        "inputSchema": _schema({"drive": {"type": "integer"}, "agent": {"type": "string"},
                                "reason": {"type": "string"}}, ["drive"]),
    },
    "remember": {
        "description": "Keep a lasting fact about the human in your memory (memory/about-you.md), "
                       "e.g. a preference, a person, a priority or how they like things done. One "
                       "short sentence. Not for the day's events: those are written down for you "
                       "when the conversation ends.",
        "inputSchema": _schema({"fact": {"type": "string"}}, ["fact"]),
    },
    "schedule": {
        "description": "Set a reminder or a recurring job. When it's due, Lago Agents types "
                       "\"[Lago] Reminder #N: <text>\" into your session (waking you): then tell "
                       "the human, or do the job. Write `text` as an instruction to yourself.",
        "inputSchema": _schema({
            "text": {"type": "string", "description": "E.g. \"Remind them to call the accountant\" "
                     "or \"Check Webstore production and tell them only if something is down\"."},
            "when": {"type": "string", "description": "First time, local: \"2026-10-12 09:00\" or "
                     "\"17:30\" (next 17:30)."},
            "in_minutes": {"type": "number", "description": "Or this many minutes from now."},
            "repeat": {"type": "string", "enum": ["once", "hourly", "daily", "weekdays", "weekly",
                       "minutes"], "default": "once"},
            "every_minutes": {"type": "integer", "description": "For repeat \"minutes\" (5 or more)."},
        }, ["text"]),
    },
    "list_schedules": {
        "description": "Your active reminders and recurring jobs, with their ids and next times.",
        "inputSchema": _schema({}),
    },
    "cancel_schedule": {
        "description": "Stop a reminder or recurring job.",
        "inputSchema": _schema({"id": {"type": "integer"}}, ["id"]),
    },
    "announce": {
        "description": "Say something aloud to the human (a short sentence, spoken by the robot "
                       "voice), e.g. when you need them and they may not be looking.",
        "inputSchema": _schema({"text": {"type": "string"}}, ["text"]),
    },
}


def _reads_mail(agent):
    """Swarm agents tagged `mail` in their manifest get the mail tools."""
    entry = agentdefs.catalog().get(agent.get("definition") or "") if agent.get("swarm") else None
    return bool(entry) and "mail" in entry["tags"]

def _builds_agents(agent):
    """Swarm agents tagged `agent-builder` can submit agent drafts for the human to approve."""
    entry = agentdefs.catalog().get(agent.get("definition") or "") if agent.get("swarm") else None
    return bool(entry) and "agent-builder" in entry["tags"]


PUBLIC_TOOLS = {
    "list_projects": CONDUCTOR_TOOLS["list_projects"],
    "work_sessions": ORCHESTRATOR_TOOLS["work_sessions"],
    "session_detail": ORCHESTRATOR_TOOLS["session_detail"],
    "open_work_session": ORCHESTRATOR_TOOLS["open_work_session"],
    "message_session": ORCHESTRATOR_TOOLS["message_session"],
    "tell_lago": {
        "description": "Send Lago (the human's assistant in Lago Agents) a message, as if typed in "
                       "his chat: a request, context or a note for him to act on. He wakes if he "
                       "was asleep. His answer shows in the Lago Agents app.",
        "inputSchema": _schema({"text": {"type": "string"}}, ["text"]),
    },
    "reports": {
        "description": "The latest reports from work sessions (done, questions, gates, blocked, "
                       "Lago's decisions), newest first.",
        "inputSchema": _schema({"limit": {"type": "integer", "default": 15}}),
    },
    "search": {
        "description": "Search Lago Agents' tasks, reports, plans and history for every word "
                       "of the query.",
        "inputSchema": _schema({"query": {"type": "string"}}, ["query"]),
    },
}

BUILDER_TOOLS = {
    "submit_agent_draft": {
        "description": "Submit a new swarm agent built on a GitHub repo, for the human to approve "
                       "in the app. Nothing is installed until they do. `folder` holds agent.md "
                       "(manifest + prompt), tests.json (3+ tests), and mcp.json and/or "
                       "install.sh when the agent needs them. Returns the draft id, or what to fix.",
        "inputSchema": _schema({
            "folder": {"type": "string", "description": "Absolute path of the draft folder."},
            "agent_id": {"type": "string", "description": "The new agent's id: lowercase, dashes."},
            "repo": {"type": "string", "description": "owner/name@sha it's built on."},
            "summary": {"type": "string", "description": "For the human: what the agent does, "
                        "how it uses the repo, the security and sandbox verdicts, risks."},
            "sandbox_report": {"type": "string", "description": "Path of the sandbox test report."},
        }, ["folder", "agent_id", "repo", "summary"]),
    },
}


MAIL_TOOLS = {
    "mail_inbox": {
        "description": "The human's Gmail accounts, read-only: one line per email (id, "
                       "date, sender, subject, snippet), newest first, across every connected "
                       "account unless you name one. Each id starts with its account.",
        "inputSchema": _schema({
            "query": {"type": "string", "default": "in:inbox", "description": "Gmail search, "
                      "e.g. \"is:unread newer_than:1d\" or \"from:anna subject:invoice\"."},
            "limit": {"type": "integer", "default": 15, "description": "At most 50."},
            "account": {"type": "string", "description": "Only this account (its name or "
                        "address, as the ids show it). Empty: all of them."},
        }),
    },
    "mail_read": {
        "description": "One email in full as plain text (headers, body, attachment names).",
        "inputSchema": _schema({
            "id": {"type": "string", "description": "The full id from mail_inbox, "
                   "`<account>:<id>`."},
            "max_chars": {"type": "integer", "default": 4000,
                          "description": "Cut the body after this many characters."},
        }, ["id"]),
    },
}


def available_tools():
    if SILENT:
        return {}
    if PUBLIC:
        return PUBLIC_TOOLS
    config = config_store.load()
    agent = config["agents"].get(AGENT_ID, {})
    tools = dict(COMMON_TOOLS)
    if agent.get("orchestrator"):
        return {**ORCHESTRATOR_TOOLS, **MAIL_TOOLS}
    extra = {**(MAIL_TOOLS if _reads_mail(agent) else {}),
             **(BUILDER_TOOLS if _builds_agents(agent) else {})}
    if agent.get("direct"):
        # A run Lago started answers with its last message (lago_hook.on_stop collects it).
        # Given complete_task, cheap models call it anyway and send a summary instead.
        return {**extra, "request_secret": COMMON_TOOLS["request_secret"]}
    tools.update(extra)
    if agent.get("conductor"):
        tools.update(CONDUCTOR_TOOLS)
        tools.update({name: LEAD_TOOLS[name]
                      for name in ("wait_for_tasks", "team_status", "message_agent")})
    elif config_store.reports_of(config, AGENT_ID):
        tools.update(LEAD_TOOLS)
    if not agent.get("reports_to"):
        tools.pop("message_lead")
    return tools


def _task_line(config, task, with_result=False):
    line = (f"#{task['id']} [{task['status']}] {task['title']} → "
            f"{team.label(config, task['to_agent'])}")
    if with_result and task["result"]:
        line += f"\nResult:\n{team.clip(task['result'], team.RESULT_PREVIEW)}"
    return line


def tool_complete_task(task_id, result, status="done"):
    task = hub.get_task(int(task_id))
    if task is None or task["to_agent"] != AGENT_ID:
        raise team.TeamError(f"Task #{task_id} isn't assigned to you. Use my_tasks to see yours.")
    if status not in ("done", "failed"):
        status = "done"
    team.finish(task["id"], result, status)
    if config_store.RUN_ID.match(AGENT_ID) and not hub.open_tasks_for(AGENT_ID):
        _sleep_later(AGENT_ID)  # a one-task agent Lago ran: done, so it goes back to sleep
    who = "the human" if task["from_agent"] is None else "your lead"
    return f"Task #{task_id} marked {status}. The result was passed to {who}."


def _sleep_later(agent_id, seconds=5):
    """Stop this agent's own session once this call has answered (it can't stop itself now)."""
    import subprocess
    code = (f"import sys, time; sys.path.insert(0, {str(Path(__file__).resolve().parent)!r}); "
            f"time.sleep({seconds}); import hub, team; "
            f"hub.open_tasks_for({agent_id!r}) or team.stop({agent_id!r})")
    subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def tool_my_tasks():
    config = config_store.load()
    open_tasks = hub.open_tasks_for(AGENT_ID)
    if not open_tasks:
        return "You have no open tasks."
    return "\n\n".join(f"#{t['id']} [{t['status']}] from {team.label(config, t['from_agent'])}: "
                       f"{t['title']}\n{t['body']}" for t in reversed(open_tasks))


def tool_message_lead(text):
    lead = config_store.load()["agents"][AGENT_ID].get("reports_to")
    if not lead:
        raise team.TeamError("You don't report to another agent. Ask the human by stopping "
                             "and writing your question.")
    team.message(lead, text, from_agent=AGENT_ID)
    return "Sent. It arrives in your lead's session when they finish their current step."


def tool_delegate(agent, title, task, after=None, labels=None):
    config = config_store.load()
    target = team.resolve_report(config, AGENT_ID, agent)
    task_id = team.assign(target, title.strip()[:120] or "Task", task, from_agent=AGENT_ID,
                          blocked_by=after, labels=labels)
    waits = hub.get_task(task_id)["status"] == "blocked"
    return (f"Delegated as task #{task_id} to {team.label(config, target)}"
            + (f"; it waits until {', '.join(f'#{i}' for i in after)} are done." if waits else ".")
            + f" Call wait_for_tasks with [{task_id}] (and any others) to collect the result.")


def tool_comment_task(task_id, text):
    import tickets
    task = hub.get_task(int(task_id))
    if task is None or AGENT_ID not in (task["to_agent"], task["from_agent"]):
        raise team.TeamError(f"Task #{task_id} isn't one you're doing or handed out.")
    try:
        to = tickets.comment(task_id, text, author=AGENT_ID)
    except tickets.TicketError as error:
        raise team.TeamError(str(error)) from error
    return f"Commented on task #{task_id}" + (f"; {team.label(config_store.load(), to)} gets it." if to else ".")


def tool_wait_for_tasks(task_ids, timeout_minutes=15):
    ids = [int(t) for t in task_ids]
    mine = [hub.get_task(t) for t in ids]
    unknown = [ids[i] for i, t in enumerate(mine) if t is None or t["from_agent"] != AGENT_ID]
    if unknown:
        raise team.TeamError(f"These aren't tasks you delegated: {unknown}")
    deadline = time.time() + min(float(timeout_minutes or 15), MAX_WAIT_MINUTES) * 60
    while True:
        tasks = [hub.get_task(t) for t in ids]
        pending = [t for t in tasks if t["status"] in hub.OPEN_TASK]
        if not pending or time.time() >= deadline:
            break
        time.sleep(WAIT_POLL_SECONDS)
    config = config_store.load()
    done = [t for t in tasks if t["status"] not in hub.OPEN_TASK]
    for task in done:
        hub.update_task(task["id"], result_seen=1)
    parts = [_task_line(config, t, with_result=True) for t in done]
    if pending:
        parts.append("Still open (wait again, or check team_status):\n" +
                     "\n".join(_task_line(config, t) for t in pending))
    return "\n\n".join(parts)


def tool_team_status():
    config = config_store.load()
    running = runner.running_agents()
    lines = ["Your direct reports:"]
    reports = config_store.reports_of(config, AGENT_ID)
    if config["agents"].get(AGENT_ID, {}).get("conductor"):  # only the agents that are awake
        lines = ["Swarm agents that are awake:"]
        reports = [rid for rid in reports if rid in running or hub.open_tasks_for(rid)] or []
        if not reports:
            lines.append("- None. Every swarm agent is asleep.")
    for rid in reports:
        state = hub.get_state(rid)
        status = state["status"] if rid in running else "stopped"
        detail = f" — {state['detail']}" if state["detail"] and rid in running else ""
        lines.append(f"- {team.label(config, rid)}: {status}{detail}")
    handed_out = hub.tasks("from_agent = ?", (AGENT_ID,), limit=30)
    if handed_out:
        lines += ["", "Tasks you handed out (newest first):"]
        lines += [_task_line(config, t) for t in handed_out]
    return "\n".join(lines)


def tool_message_agent(agent, text):
    config = config_store.load()
    target = team.resolve_report(config, AGENT_ID, agent)
    team.message(target, text, from_agent=AGENT_ID)
    return f"Sent to {team.label(config, target)}."


# ---------------------------------------------------------------- Conductor tools

def tool_list_projects():
    config = config_store.load()
    if not config["projects"]:
        return "There are no projects yet. Ask the human to add one in the Projects page."
    lines = []
    for pid, project in config["projects"].items():
        areas = ", ".join(f"{aid} ({a.get('name', aid)})" for aid, a in
                          (project.get("areas") or {}).items())
        lines.append(f"- `{pid}` {project.get('name', pid)}: {project.get('description', '')}"
                     f"\n  Folder: {project.get('cwd', '?')}" + (f"\n  Areas: {areas}" if areas else ""))
    return "\n".join(lines)


def _my_project(project=""):
    """The project a tool is about: the one asked for, else the Conductor's own."""
    project = (project or "").strip().strip("`")
    if project:
        swarm.get_project(config_store.load(), project)
        return project
    if AGENT_ID == config_store.ORCHESTRATOR_ID:
        raise team.TeamError("Say which project (list_projects shows their ids).")
    return swarm.workspace_of(AGENT_ID)["project"]


def tool_project_profile(project=""):
    config = config_store.load()
    project = _my_project(project)
    if not project:  # a Conductor whose session belongs to no project
        return swarm.session_text(config, swarm.workspace_of(AGENT_ID))
    return swarm.project_text(config, project)


def tool_find_skills(query=""):
    config = config_store.load()
    folder = None  # Lago has no project of his own: the skills every agent has
    if AGENT_ID != config_store.ORCHESTRATOR_ID:
        folder = swarm.project_cwd(swarm.session_project(config, swarm.workspace_of(AGENT_ID)))
    words = [w for w in (query or "").lower().split() if len(w) > 2]
    found = []
    for skill in agentdefs.skills(folder).values():
        text = f"{skill['name']} {skill['description']}".lower()
        score = sum(text.count(w) for w in words) if words else 1
        if score:
            found.append((-score, skill["name"], skill))
    lines = [f"- `{s['name']}` ({s['source']}): {s['description']}" + _needs_note(s)
             for *_, s in sorted(found, key=lambda f: f[:2])]
    plugins = [f"- {p['name']}: {p['description']}"
               + (f" (commands: {', '.join('/' + c for c in p['commands'])})" if p["commands"] else "")
               for p in agentdefs.plugins()]
    return ("Skills:\n" + ("\n".join(lines) or "- No match.")
            + ("\n\nInstalled plugins (on for every agent):\n" + "\n".join(plugins) if plugins else ""))


def _needs_note(skill):
    """" Needs `gws`, not installed yet." for a skill whose program is missing."""
    import skill_sources
    missing = skill_sources.missing_programs(Path(skill["path"]).parent)
    return (f" Needs {', '.join(f'`{b}`' for b in missing)}, not installed yet: offer to set it "
            "up." if missing else "")


SETUP_PROGRAMS = {"npm", "npx", "pipx", "uv", "gcloud", "gh"}


def tool_open_setup_terminal(command, why):
    """A setup step only the human can finish (a sign-in in their browser, an install they
    watch), run in a terminal window on their screen."""
    import shlex as shell_words
    import skill_sources
    try:
        program = shell_words.split(command)[0]
    except (ValueError, IndexError):
        raise team.TeamError("Give the command to run.")
    allowed = SETUP_PROGRAMS | {b for skill in agentdefs.skills().values()
                                for b in skill_sources.programs_needed(
                                    Path(skill["path"]).read_text(encoding="utf-8", errors="replace"))}
    if program not in allowed:
        raise team.TeamError(f"Setup terminals run the programs your skills need, or "
                             f"{', '.join(sorted(SETUP_PROGRAMS))}; not {program!r}.")
    import server
    session = "setup-" + re.sub(r"[^a-z0-9]+", "-", program.lower()).strip("-")
    server.launch(session, "", command, why)
    try:
        argv = desktop.terminal_argv(" ".join(runner.attach_argv(session)))
        if not argv:
            raise OSError("no terminal app found")
        subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shown = "A terminal window opened on their screen"
    except OSError:
        shown = f"Couldn't open a window: they can attach with `{' '.join(runner.attach_argv(session))}`"
    return (f"{shown}, running `{command}` ({why}). Tell them what to do in it (e.g. finish the "
            "sign-in in the browser it opens), and check the result once they say it's done.")


def tool_github_board(board="", status="", project=""):
    project = config_store.load()["projects"].get(_my_project(project), {})
    boards = github.boards(project)
    if not boards:
        raise team.TeamError("This project has no GitHub board. The human can add one in the "
                             "project's Settings.")
    wanted = (board or "").strip().lower()
    chosen = next((b for b in boards if wanted in (str(b["number"]), (b.get("name") or "").lower())),
                  None) if wanted else boards[0]
    if chosen is None:
        raise team.TeamError("No such board. Boards: " + ", ".join(
            f"{b.get('name') or ''} #{b['number']}" for b in boards))
    try:
        return github.board_text(chosen["owner"], chosen["number"], status.strip())
    except github.GitHubError as error:
        raise team.TeamError(f"GitHub: {error}") from error


def tool_github_repo(repo="", project="", number=None):
    project = config_store.load()["projects"].get(_my_project(project), {})
    repos = github.repos(project)
    name = github.repo_name(repo) if repo else (repos[0] if repos else None)
    if not name:
        raise team.TeamError("This project has no GitHub repository set.")
    try:
        if number:
            return github.item_text(name, number)
        data = github.repo_activity(name)
    except github.GitHubError as error:
        raise team.TeamError(f"GitHub: {error}") from error
    lines = [f"# {name} (links: https://github.com/{name}/pull/N, /issues/N)", "",
             f"## Open pull requests ({len(data['prs'])})"]
    lines += [f"- #{p['number']} {p['title']} ({p['headRefName']}, {p['author']}"
              f"{', draft' if p['isDraft'] else ''})" for p in data["prs"]] or ["- none"]
    lines += ["", f"## Open issues ({len(data['issues'])})"]
    lines += [f"- #{i['number']} {i['title']}" + (f" [{', '.join(i['labels'])}]" if i["labels"] else "")
              for i in data["issues"]] or ["- none"]
    lines += ["", "## Recent CI runs"]
    lines += [f"- {r.get('workflowName', '')}: {r.get('displayTitle', '')} on {r.get('headBranch', '')} → "
              f"{r.get('conclusion') or r.get('status')}" for r in data["runs"][:5]] or ["- none"]
    return "\n".join(lines)


def tool_find_agents(query="", domain="", project=""):
    config = config_store.load()
    found = swarm.search(config, query, domain.strip())
    enabled = swarm.enabled_agents(config)
    used = _used_before(query, project, enabled)
    domains = sorted({e["domain"] for e in enabled.values()})
    head = (f"{len(found)} of {len(enabled)} switched-on agents match. Domains: {', '.join(domains)}. "
            f"{len(swarm.disabled(config))} agent(s) are switched off.")
    shown = {e["id"] for e in found}
    cards = {c["id"]: c for c in evals.ranking()}
    others = [f"- `{e['id']}`{_score_note(cards.get(e['id']))}: {e['summary']}"
              for e in enabled.values() if e["id"] not in shown]
    tail = ("\n\nThe other switched-on agents (`read_manifest` for one that could take on more "
            "of the job):\n" + "\n".join(others)) if others else ""
    tail += _on_github(query)
    if not found:
        return used + head + "\nNo match: try other words, or no domain." + tail
    return used + head + "\n\n" + "\n".join(swarm.card(e) + _score_line(cards.get(e["id"]))
                                              for e in found) + tail


def _on_github(query):
    """For Lago: the best matches in the GitHub list, last, since they're not installed."""
    if AGENT_ID != config_store.ORCHESTRATOR_ID or not (query or "").strip():
        return ""
    try:
        have = ghcatalog.installed()
        found = [c for c in ghcatalog.search(query, limit=6) if c["repo"].lower() not in have][:3]
    except Exception:  # the list is a bonus: never lose the search over it
        return ""
    if not found:
        return ""
    return ("\n\nOn GitHub, not installed (only when nothing above fits; importing needs the "
            "human's yes, then import_github_agent):\n"
            + "\n".join(ghcatalog.card_text(c) for c in found))


def _used_before(query, project, enabled):
    """The switched-on agents that did tasks like this one before, as a section that comes
    first: what worked for you outweighs a manifest's promise, when it really is the same job."""
    project = (project or "").strip().strip("`")
    if not project and AGENT_ID != config_store.ORCHESTRATOR_ID:
        try:
            project = (swarm.workspace_of(AGENT_ID) or {}).get("project", "")
        except team.TeamError:
            project = ""
    used = [f for f in finder.history(query, project) if f["catalog_id"] in enabled]
    if not used:
        return ""
    return ("Used before for similar tasks (weigh these first, but only when the job really is "
            "the same: same kind of input and result, not just shared words):\n"
            + finder.history_text(used) + "\n\n")


def _score_note(card):
    rated = card and (card["ups"] + card["downs"] or card["tests_run"])
    return f" ({card['score']}/100)" if rated else ""


def _score_line(card):
    """The agent's record from ratings and tests, when it has one."""
    if not card or not (card["ups"] + card["downs"] or card["tests_run"]):
        return ""
    tests = f", tests {card['tests_passed']}/{card['tests_run']} passed" if card["tests_run"] else ""
    return (f"\n  Score: {card['score']}/100, #{card['rank']} of the swarm "
            f"({card['ups']} up, {card['downs']} down{tests})")


def tool_read_manifest(agent):
    config = config_store.load()
    entry = agentdefs.catalog().get(agent.strip().strip("`"))
    if entry is None:
        raise team.TeamError(f"There's no swarm agent {agent!r}. Use find_agents to see them.")
    off = " (switched OFF: the human must switch it on before a pipeline can use it)" \
        if entry["id"] in swarm.disabled(config) else ""
    card = next((c for c in evals.ranking() if c["id"] == entry["id"]), None)
    return swarm.manifest_text(entry) + _score_line(card).replace("\n  ", "\n\n") + off


def tool_list_pipeline_templates():
    return swarm.templates_text()


def tool_read_pipeline_template(template):
    found = agentdefs.pipeline_templates().get(template.strip().strip("`"))
    if found is None:
        raise team.TeamError(f"There's no template {template!r}.\n{swarm.templates_text()}")
    return f"# {found['name']}\nAgents: {', '.join(found['agents'])}\n\n{found['body']}"


def tool_propose_pipeline(title, goal, steps, area="", project=""):
    workspace = swarm.workspace_of(AGENT_ID)
    pipeline_id = swarm.propose(workspace["id"], (area or "").strip(), title, goal, steps)
    return (f"Saved as pipeline #{pipeline_id}, waiting for the human's approval in the Pipelines "
            "page. Tell the human it's ready (a short summary of the steps), then end your turn. "
            "Their decision arrives as a \"[Lago] Pipeline\" message.\n\n"
            + swarm.status_text(swarm.get(pipeline_id)))


def _mine(pipeline_id):
    """Conductors only touch their own work session's pipelines."""
    pipeline = hub.get_pipeline(int(pipeline_id))
    if pipeline is None or pipeline["workspace"] != swarm.workspace_of(AGENT_ID)["id"]:
        raise team.TeamError(f"Pipeline #{pipeline_id} isn't one of this work session's. "
                             "Use pipeline_status to see yours.")
    return int(pipeline_id)


def tool_revise_pipeline(pipeline_id, steps=None, title=None, goal=None):
    pipeline = swarm.revise(_mine(pipeline_id), steps, title, goal)
    return ("Revised. It's back with the human for approval; end your turn and wait for their "
            "decision.\n\n" + swarm.status_text(pipeline))


def tool_pipeline_status(pipeline_id=None):
    if pipeline_id is not None:
        return swarm.status_text(swarm.get(_mine(pipeline_id)))
    open_ones = hub.pipelines("workspace = ? AND status IN ('draft', 'changes_requested', "
                              "'approved', 'running')", (swarm.workspace_of(AGENT_ID)["id"],))
    if not open_ones:
        return "No open pipelines."
    return "\n\n".join(swarm.status_text(swarm.sync(p)) for p in open_ones)


def tool_run_step(pipeline_id, step, notes=""):
    task_id, agent_id = swarm.run_step(_mine(pipeline_id), step.strip(), notes)
    return (f"Woke `{agent_id}` and sent step `{step}` as task #{task_id}. Run other ready steps "
            f"too, then call wait_for_tasks with the task ids.")


def tool_rate_step(pipeline_id, step, verdict, reason):
    pipeline = swarm.get(_mine(pipeline_id))
    found = next((s for s in pipeline["steps"] if s["id"] == step), None)
    if found is None or not found.get("task_id"):
        raise team.TeamError(f"Step `{step}` of pipeline #{pipeline_id} hasn't run yet.")
    return evals.rate(found["task_id"], verdict, "conductor", reason)


def tool_rate_run(task_id, verdict, reason, by_human=False):
    return evals.rate(task_id, verdict, "human" if by_human else "lago", reason)


def tool_skip_step(pipeline_id, step, reason):
    swarm.skip_step(_mine(pipeline_id), step.strip(), reason)
    return f"Step `{step}` skipped."


def tool_finish_pipeline(pipeline_id, status, summary, task_complete=False):
    slept = swarm.finish(_mine(pipeline_id), status, summary, bool(task_complete))
    return (f"Pipeline #{pipeline_id} closed as {status}. "
            + (f"Put back to sleep: {', '.join(slept)}." if slept else "No agents were awake.")
            + " Tell the human what was delivered.")


def tool_request_secret(env, reason):
    import secret_requests
    try:
        return secret_requests.request(AGENT_ID, env, reason)
    except secret_requests.SecretRequestError as error:
        raise team.TeamError(str(error)) from error


def tool_report(kind, text, questions=None):
    import questions as forms
    if kind not in ("solved", "done", "failed", "question", "gate", "blocked", "update"):
        raise team.TeamError("kind must be solved, done, failed, question, gate, blocked or update.")
    if questions and kind != "question":
        raise team.TeamError("Only a question report takes questions.")
    try:
        form = forms.clean_form(questions)
    except forms.QuestionError as error:
        raise team.TeamError(str(error)) from error
    workspace = swarm.workspace_of(AGENT_ID)
    notify.report(workspace["id"], kind, text, form)
    spoken = kind != "update"
    if workspace.get("orchestrated"):
        return ("Reported to Lago" + (" and announced aloud. " if spoken else ". ")
                + ("End your turn; the answer arrives as a message." if kind == "question" else ""))
    return ("Recorded" + (" and announced aloud" if spoken else "") + ". The human answers in "
            "this terminal" + ("; end your turn and wait." if kind == "question" else "."))


# ---------------------------------------------------------------- Orchestrator tools

def _session_line(config, ws, running, states):
    cid = config_store.conductor_id(ws["id"])
    state = states.get(cid) or {}
    status = (state.get("status") or "stopped") if cid in running else \
        ("closed" if ws["status"] == "closed" else "paused")
    _, place = notify.where(config, ws)
    line = f"- #{ws['id']} {place}: \"{ws['title']}\" · Conductor {status}"
    if cid in running and state.get("detail"):
        line += f" ({state['detail']})"
    if not ws.get("orchestrated"):
        line += " · opened by the human in the app"
    pipelines = hub.pipelines("workspace = ?", (ws["id"],), limit=1)
    if pipelines:
        p = swarm.sync(pipelines[0])
        done = sum(s["status"] in swarm.DONE_STEP for s in p["steps"])
        line += f"\n  Pipeline #{p['id']} \"{p['title']}\": {p['status']}, {done}/{len(p['steps'])} steps"
    events = hub.events("workspace = ?", (ws["id"],), limit=1)
    if events:
        e = events[0]
        line += f"\n  Last report ({e['kind']}, {time.strftime('%H:%M', time.localtime(e['created_at']))}): " \
                + team.clip(e["text"].replace("\n", " "), 200)
    return line


def tool_work_sessions(closed=False):
    config = config_store.load()
    rows = hub.workspaces() if closed else hub.workspaces("status = 'open'")
    if not rows:
        return "No work sessions are open. Open one with open_work_session."
    running, states = runner.running_agents(), hub.all_states()
    return "\n".join(_session_line(config, ws, running, states) for ws in rows)


def _session(session):
    return swarm.get_workspace(int(session))


def tool_session_detail(session, more=False):
    config = config_store.load()
    pipelines, reports, report_chars, last_chars, result_chars = \
        (3, 6, 600, 3000, 1500) if more else (1, 3, 300, 800, 200)
    ws = _session(session)
    cid = config_store.conductor_id(ws["id"])
    running = runner.running_agents()
    parts = [_session_line(config, ws, running, hub.all_states())]
    for p in hub.pipelines("workspace = ?", (ws["id"],), limit=pipelines):
        parts.append(swarm.status_text(swarm.sync(p), result_chars))
    events = hub.events("workspace = ?", (ws["id"],), limit=reports)
    if events:
        parts.append("Recent reports (newest first):\n" + "\n".join(
            f"- {time.strftime('%H:%M', time.localtime(e['created_at']))} {e['kind']}: "
            + team.clip(e["text"], report_chars).replace("\n", "\n  ") for e in events))
    last = team.last_assistant_text(hub.get_state(cid).get("session_id"))
    if last:
        parts.append("The Conductor's last message:\n" + team.clip(last, last_chars))
    status = hub.get_state(cid)["status"]
    if cid in running and status == "needs_approval":
        parts.append("It's waiting for the human to allow a tool in its terminal (the app shows "
                     "Approve/Deny on the session's page).")
    return "\n\n".join(parts)


def tool_create_project(name, folder, description="", stack="", repo="", areas=None,
                        git_init=False):
    project_id, notes = swarm.create_project(name, folder, description, stack, repo, areas, git_init)
    return (f"Added project `{project_id}` ({name.strip()}) in {folder.strip()}: {', '.join(notes)}. You "
            "can open work sessions in it now. The human connects services, servers and GitHub in "
            "the app (Projects → " + name.strip() + " → Settings).")


def tool_open_work_session(request, project="", area="", folder="", goal=None):
    project = (project or "").strip().strip("`")
    if project:
        swarm.get_project(config_store.load(), project)
    # Like you, or unrestricted anyway once the human gave their password since the app opened.
    unrestricted = not PUBLIC and (config_store.ORCHESTRATOR_UNRESTRICTED.exists()
                                   or config_store.bypass_ready(config_store.load()))
    workspace_id = swarm.open_workspace(project, (area or "").strip().strip("`"), request,
                                        unrestricted=unrestricted, orchestrated=True,
                                        folder=folder, goal=goal)
    return (f"Opened work session #{workspace_id}. Its Conductor is starting with your request "
            "and reports back to you here. The human can watch it in the app (Projects, or the "
            "session list on the Talk to Lago page).")


def tool_run_agent(agent, task, inputs=None, project="", folder="", mode="cheap", skills=None):
    focused = config_store.focus()
    if focused and (agent or "").strip().strip("`") != focused \
            and not _scheduled((agent or "").strip().strip("`")):
        raise team.TeamError(f"The human is working with `{focused}` only, so it does this, "
                             f"not `{agent}`. If `{focused}` can't do it, tell them and ask "
                             "whether to stop using it.")
    project = (project or "").strip().strip("`")
    unrestricted = config_store.ORCHESTRATOR_UNRESTRICTED.exists() \
        or config_store.bypass_ready(config_store.load())
    skills = [name.strip().strip("`") for name in skills or [] if name.strip()]
    if skills:
        unknown = [name for name in skills if name not in agentdefs.skills()]
        if unknown:
            raise team.TeamError(f"Not installed: {', '.join(unknown)}. find_skills lists them.")
        task += ("\n\nBefore you start, load these skills with the Skill tool: "
                 + ", ".join(f"`{name}`" for name in skills) + ".")
    task_id, agent_id, model = swarm.run_agent(agent, task, project, folder, inputs,
                                               unrestricted=unrestricted, from_agent=AGENT_ID,
                                               mode=mode)
    return (f"Task #{task_id} sent to `{agent_id}` ({mode}, {model}). Its result arrives here "
            "as a message; tell the human when it does.")


def tool_submit_agent_draft(folder, agent_id, repo, summary, sandbox_report=""):
    import agent_drafts
    draft_id = agent_drafts.submit(folder, agent_id.strip(), repo.strip(), summary, sandbox_report,
                                   from_agent=AGENT_ID)
    return (f"Submitted as agent draft #{draft_id}. The human reviews and approves it on the Swarm "
            "page; it's installed then. Say so in your final message.")


def tool_post_news(text):
    text = team.clip((text or "").strip(), 2000)
    if not text:
        raise team.TeamError("Write the news.")
    hub.add_event(None, None, "news", text)
    return "Posted to the bell."


def _scheduled(agent):
    """Whether a recurring job that just fired names this agent: it may run then, even while
    the human is working with another agent."""
    import schedules
    recent = time.time() - 1800
    return any((row.get("last_at") or 0) > recent and agent in row["text"]
               for row in schedules.active())


def tool_use_agent(agent=""):
    agent = (agent or "").strip().strip("`")
    if not agent:
        was = config_store.focus()
        config_store.set_focus(None)
        return (f"Stopped using `{was}`: back to normal." if was else "No agent was picked.")
    entry = swarm.enabled_agents(config_store.load()).get(agent)
    if entry is None:
        raise team.TeamError(f"There's no switched-on swarm agent {agent!r}. find_agents lists them.")
    config_store.set_focus(agent)
    return swarm.focus_brief(entry) + ("\n\nIf they haven't said what it should do, ask them in one "
                                 "short line.")


def tool_message_session(session, text):
    ws = _session(session)
    if ws["status"] == "closed":
        raise team.TeamError(f"Work session #{ws['id']} is closed. Ask the human to reopen it in "
                             "the app, or open a new one.")
    team.message(config_store.conductor_id(ws["id"]), text, from_agent=AGENT_ID)
    return f"Sent to the Conductor of work session #{ws['id']}."


def tool_decide(session, decision, reason, importance, plan=None, message=None):
    if config_store.lago_mode(config_store.load()) != "decide":
        raise team.TeamError("You're in Check mode: the human decides. Bring it to them, ready to "
                             "answer (the decision, your recommendation, the options).")
    if importance not in ("routine", "important"):
        raise team.TeamError("Critical decisions are the human's, in every mode: bring this one to "
                             "them, ready to answer.")
    ws = _session(session)
    if ws["status"] == "closed":
        raise team.TeamError(f"Work session #{ws['id']} is closed.")
    note = (message or f"{decision}\nWhy: {reason}").strip()
    if plan:
        drafts = hub.pipelines("workspace = ? AND status IN ('draft', 'changes_requested')",
                               (ws["id"],), limit=1)
        if not drafts:
            raise team.TeamError(f"Work session #{ws['id']} has no plan waiting for a decision.")
        swarm.decide(drafts[0]["id"], plan, note, by_lago=True)
        done = f"Plan #{drafts[0]['id']}: {plan}."
    else:
        team.message(config_store.conductor_id(ws["id"]),
                     f"[Decided by Lago for the human (Decide mode)]\n{note}", from_agent=AGENT_ID)
        done = f"Sent to the Conductor of work session #{ws['id']}."
    hub.add_event(ws["id"], ws["project"], "decided", f"{decision}\nWhy: {reason}\n({importance})")
    activity.log(AGENT_ID or "orchestrator", "decision.made", f"{decision} (why: {reason})",
                 target=f"session #{ws['id']}", workspace=ws["id"], project=ws["project"] or None)
    return done + (" Logged. Tell the human in one line what you chose and how to undo it."
                   if importance == "important" else " Logged for the human to review.")


def tool_answer_question(report, answers, note=""):
    import questions
    try:
        return questions.answer(report, answers, note, by=AGENT_ID or "orchestrator")["message"]
    except questions.QuestionError as error:
        raise team.TeamError(str(error)) from error


def tool_tell_lago(text):
    if not PUBLIC:
        raise team.TeamError("tell_lago is for other Claude sessions.")
    notify.tell_orchestrator(f"[Lago] A message from another Claude session (not the human "
                             f"typing to you here):\n{text.strip()}")
    return "Sent to Lago. His reply shows in the Lago Agents app."


def tool_reports(limit=15):
    rows = hub.events("kind != 'update'", limit=min(int(limit), 50))
    return "\n".join(f"#{e['id']} {_ago(e['created_at'])} · {e['kind']}"
                     f"{' · session #' + str(e['workspace']) if e['workspace'] else ''}: "
                     f"{e['text'].splitlines()[0][:200] if e['text'] else ''}" for e in rows) or "No reports yet."


def tool_search(query):
    return _search(query)


def tool_pull_request(action, repo, number, verdict="", body="", reviewer=""):
    import pull_requests
    try:
        if action == "status":
            allowed, reasons, now = pull_requests.check(repo, number, by=AGENT_ID)
            return (f"{repo}#{number} {now['title']} · {now['checks']} checks"
                    + (f" (not green: {', '.join(now['failing'])})" if now["failing"] else " green")
                    + f" · review: {now['review_decision'] or 'none'} · "
                    + ("you may merge it." if allowed else "not mergeable by you: " + "; ".join(reasons)))
        if action == "review":
            pull_requests.review(repo, number, verdict, body, reviewer or AGENT_ID)
            return f"Posted the {verdict} review on {repo}#{number}."
        if action == "merge":
            return pull_requests.merge(repo, number, by=AGENT_ID)["message"]
    except pull_requests.PullRequestError as error:
        raise team.TeamError(str(error)) from error
    raise team.TeamError(f"Unknown action {action!r}")


def tool_share_document(path, title=""):
    import documents
    try:
        doc = documents.share(swarm.workspace_of(AGENT_ID)["id"], path, title)
    except documents.DocumentError as error:
        raise team.TeamError(str(error)) from error
    return f"Shared {doc['title']} (document #{doc['id']}). Its revisions are kept; comments arrive as messages."


def tool_propose_agent(id, name, summary, prompt, reason, tests, domain="general", tools="",  # noqa: A002
                       use_when="", avoid_when="", inputs=None, outputs=None):
    import agent_drafts
    draft_id = agent_drafts.propose(id, reason, tests, from_agent=AGENT_ID, name=name, summary=summary,
                                    prompt=prompt, domain=domain, tools=tools, use_when=use_when,
                                    avoid_when=avoid_when, inputs=inputs, outputs=outputs)
    return (f"Draft #{draft_id} for `{id}` is waiting for the human on the Swarm page. Tell them "
            "what it's for in one line; it can be used once they approve it.")


def tool_create_agent(**fields):
    return tool_propose_agent(**fields).replace("on the Swarm page", "on the Agents page")


def tool_prepare_publish(agent):
    agent = agent.strip().strip("`")
    prepared = publish.prepare(agent)
    removed = [f for f in prepared["findings"] if f["action"] == "removed"]
    check = sorted({f["text"] for f in prepared["findings"] if f["action"] == "check"})
    return (f"Ready to become github.com/{prepared['repo']} ({len(prepared['files'])} files). "
            f"Removed {len(removed)} thing(s) (secrets, emails, addresses, paths, their name)."
            + (f" For them to check: {', '.join(check)}." if check else "")
            + " Tell them to open the agent's page (Agents, then the agent) and press Publish "
              "to GitHub to read the files and publish; nothing goes out before that.")


def tool_github_agents(topic="", query=""):
    topic = (topic or "").strip().lower()
    if topic and topic not in ghcatalog.TOPICS:
        raise team.TeamError(f"Topics: {', '.join(ghcatalog.TOPICS)}.")
    found = ghcatalog.search(query, limit=8) if (query or "").strip() \
        else ghcatalog.trending(topic, limit=8)
    have = ghcatalog.installed()
    found = [{**c, "have": have.get(c["repo"].lower())} for c in found]
    if not found:
        return ("Nothing in Lago's GitHub list matches." if ghcatalog.cards()
                else "Lago's GitHub list is empty: it fills on its daily refresh.")
    head = (f"Matches for \"{query.strip()}\"" if (query or "").strip()
            else f"Gaining the most stars lately{' in ' + ghcatalog.TOPICS[topic] if topic else ''}")
    return (head + " (not installed; importing needs the human's yes, then the builder's "
            "security check and their approval of the draft):\n"
            + "\n".join(ghcatalog.card_text(c) for c in found))


def tool_explain_github_agent(repo):
    d = ghcatalog.explain(github.repo_name(repo) or repo.strip())
    fits = "\n".join(f"- {f['name']}: {f['how']}" for f in d["fits"]) or "- none of their projects"
    return (f"{d['repo']} (commit {d['sha'][:7]}): verdict {d['verdict']}, risk {d['risk']}. "
            f"{d['why']}\nWhat it does: {d['what']}\nGain: {d['reward']}\nRisks:\n"
            + "\n".join(f"- {r}" for r in d["risks"]) + f"\nFits:\n{fits}"
            + (f"\nThey already have: {', '.join(d['similar'])}." if d["similar"] else ""))


def tool_import_github_agent(repo, notes="", careful=False):
    done = ghcatalog.import_repo(github.repo_name(repo) or repo.strip(), notes, bool(careful))
    if isinstance(done, str):
        return done + " Tell them in a line; it can be used now (find_agents shows it)."
    return (f"The github-agent-builder is checking it (task #{done}). Its draft waits for the "
            "human's approval on the Agents page; tell them when its result arrives.")


def tool_judge_result(session, accepted, reason=""):
    return outcomes.judge_session(_session(session)["id"], bool(accepted), reason)


def tool_test_drive(agents, task, project=""):
    drive_id, started = outcomes.start_drive(agents, task, project)
    return (f"Test drive #{drive_id} started: "
            + ", ".join(f"`{c}` (task #{t})" for c, t in started.items())
            + ". Their results arrive as messages; when all are in, show them side by side and "
              f"ask which is best, then pick_test_drive {drive_id}.")


def tool_pick_test_drive(drive, agent="", reason=""):
    if not (agent or "").strip():
        return outcomes.drive_text(drive)
    return outcomes.pick(drive, agent, reason)


def tool_open_tasks():
    import open_tasks
    return open_tasks.brief()


def tool_continue_task(task_id):
    import open_tasks
    return open_tasks.continue_run(task_id, actor=config_store.ORCHESTRATOR_ID)


def tool_abandon_task(task_id, reason):
    import open_tasks
    return open_tasks.abandon(task_id, reason, actor=config_store.ORCHESTRATOR_ID)


def tool_close_work_session(session):
    ws = _session(session)
    swarm.close_workspace(ws["id"])
    return f"Closed work session #{ws['id']}. Its agents are asleep."


# ---------------------------------------------------------------- the app's own actions

def _api():
    import api
    return api.Api(None)


def _gb(n):
    return f"{n / 1e9:.1f}GB"


def _agent_of(target, waiting=False):
    """An agent id from a session number or an id; with `waiting`, the one in that session
    stuck on a permission prompt."""
    target = str(target).strip().lstrip("#")
    if target == config_store.ORCHESTRATOR_ID:
        raise team.TeamError("That's you. Targets are work sessions and their agents.")
    if not target.isdigit():
        return target
    ws = _session(target)
    cid = config_store.conductor_id(ws["id"])
    if waiting:
        states = hub.all_states()
        stuck = [aid for aid, st in states.items() if st.get("status") == "needs_approval"
                 and (aid == cid or aid.startswith(cid + "-")) and runner.is_running(aid)]
        if stuck:
            return stuck[0]
    return cid


def _press(key):
    def act(target):
        agent = _agent_of(target, waiting=key != "interrupt")
        return f"{agent}: " + _api().press(agent, key)["message"]
    return act


def _ask_human(target):
    """Pass a permission prompt Lago was handed on to the human: spoken, in the bell and on
    their phone."""
    import lago_hook
    agent = _agent_of(target, waiting=True)
    state = hub.get_state(agent)
    if state["status"] != "needs_approval":
        return f"{agent} isn't waiting for permission."
    hub.set_state(agent, lago_review=None)
    lago_hook.tell_human(agent, "permission", state["detail"])
    return (f"The human is asked about {agent}'s prompt. Tell them in plain words what it wants "
            "to do, why it needs them, and what you'd choose; then approve or deny as they say.")


def _screen(target):
    agent = _agent_of(target, waiting=True)
    if not runner.is_running(agent):
        return f"{agent} isn't running."
    lines = [line.rstrip() for line in runner.capture(agent, 30).splitlines()]
    return team.clip("\n".join(line for i, line in enumerate(lines)
                               if line or (i and lines[i - 1])).strip(), 2500)


def _sleep(target):
    agent = _agent_of(target)
    team.stop(agent)
    return f"{agent} is asleep (its conversation is kept)."


def _reopen(target):
    return _api().reopen_workspace(int(str(target).lstrip("#")))["message"]


def _services(project):
    rows = _api().services_status(_my_project(project))
    return "\n".join(f"{sid}: {st['status']}" + (f" ({st['detail']})" if st.get("detail") else "")
                     for sid, st in rows.items() if not sid.startswith("_")) or "No services."


def _service(project, service, do):
    api, project = _api(), _my_project(project)
    if do == "logs":
        log = api.service_logs(project, service)
        return team.clip(log["text"][-3000:], 3000) or "No log yet."
    return api.service_action(project, service, do)["message"]


def _run_all(project, do):
    return _api().project_command(_my_project(project), f"{do}_all")["message"]


def _server(project, server=""):
    st = _api().server_status(_my_project(project), server)
    if st.get("error"):
        return f"Error: {st['error']}"
    lines = [f"{st.get('host')}: up {st.get('uptime', 0) / 86400:.0f}d, load "
             f"{' '.join(f'{x:.2f}' for x in st.get('load') or [])}, memory "
             f"{_gb(st['memory']['available'])} free of {_gb(st['memory']['total'])}, disk "
             f"{_gb(st['disk']['used'])} of {_gb(st['disk']['total'])}"
             if st.get("memory") and st.get("disk") else str(st.get("host", "?"))]
    for c in st.get("containers") or []:
        lines.append(f"- {c.get('Names')}: {c.get('Status') or c.get('State')}")
    if st.get("failed_units"):
        lines.append("Failed units: " + ", ".join(st["failed_units"]))
    git = st.get("git") or {}
    if git:
        lines.append(f"Git: {git.get('branch')} {str(git.get('head', ''))[:8]} "
                     f"{git.get('subject', '')}; {len(git.get('changed') or [])} changed files")
    if st.get("drift"):
        lines.append("Drift: " + team.clip(json.dumps(st["drift"], separators=(",", ":")), 600))
    return "\n".join(lines)


def _merge(target, changes):
    for key, value in changes.items():
        if key.startswith("+"):
            target[key[1:]] = list(target.get(key[1:]) or []) + list(value or [])
        elif value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _edit_project(project, changes):
    import api
    project = _my_project(project)
    if not isinstance(changes, dict) or not changes:
        raise team.TeamError("changes must be an object of fields to set.")
    projects = config_store.without_swarm(config_store.load())["projects"]
    _merge(projects[project], changes)
    try:
        _api().save_projects(projects)
    except api.ApiError as error:
        raise team.TeamError(str(error)) from error
    return f"Saved {project}: {', '.join(k.lstrip('+') for k in changes)}."


def _accounts():
    data = _api().accounts()
    lines = []
    for a in data["accounts"]:
        st = a["status"]
        limits = ", ".join(f"{l['window']} {l['percent']}%" for l in
                           (a.get("usage") or {}).get("limits", []))
        lines.append(f"{a['id']} ({a['label']}){' ACTIVE' if a['active'] else ''}: "
                     + (f"{st.get('email')} {st.get('plan')}" if st.get("logged_in")
                        else "not logged in") + (f"; {limits}" if limits else "")
                     + f"; {a['running']} running")
    lines.append(f"Auto-switch at {data['switch_at']}%: {'on' if data['auto_switch'] else 'off'}")
    return "\n".join(lines)


def _budget(target="", do="show", amount=None, agent=None, session=None):
    """Show, extend or stop what a budget stopped; with agent/session (dollars), set the
    default budgets."""
    import budgets
    api = _api()
    if agent is not None or session is not None:
        now = budgets.limits(config_store.load())
        return api.save_budgets(now["agent"] if agent is None else agent,
                                now["session"] if session is None else session)["message"]
    if not target:
        now = budgets.limits(config_store.load())
        windows = "; ".join(f"{w['window']}ly {w['scope']}: ${w['spent']:.2f} of ${w['limit'] + w['extended']:.2f}"
                            for w in api.budget_windows())
        return (f"Budgets: ${now['agent']:.2f} per agent conversation, ${now['session']:.2f} per "
                "work session (0: none), at API prices." + (f" Windows: {windows}." if windows else ""))
    agent_id = _agent_of(target)
    if do == "extend":
        return api.extend_budget(agent_id, amount)["message"]
    if do == "stop":
        return api.stop_over_budget(agent_id)["message"]
    rows = budgets.state(agent_id)
    return "\n".join(f"{b['kind']}: ${b['spent']:.2f} of " + (f"${b['limit']:.2f}" if b["limit"] else "no limit")
                     + (" (stopped)" if b["paused"] else "") for b in rows) or "No budget applies."


def _settings(**changes):
    import voice
    api, done = _api(), []
    if "model" in changes:
        api.set_lago_model(changes.pop("model"))
        done.append("model (from your next start)")
    if "talk" in changes:
        api.set_talk(bool(changes.pop("talk")))
        done.append("talk")
    if "mode" in changes:
        api.set_lago_mode(changes.pop("mode"), tell=False)
        done.append("mode")
    if changes:
        api.save_voice({**voice.settings(config_store.load()), **changes})
        done += list(changes)
    return "Saved: " + ", ".join(done) if done else "Nothing to change."


def _create_agent(agent_id, **fields):
    try:
        path = agentdefs.write_agent(agent_id, **fields)
    except ValueError as error:
        raise team.TeamError(str(error)) from error
    entry = agentdefs.catalog().get(agent_id)
    if not entry:
        raise team.TeamError(f"Wrote {path}, but the catalog can't read it back.")
    return (f"Added swarm agent `{agent_id}` ({entry['name']}, {entry['domain']}) at {path}. It's "
            "switched on: Conductors in every project can now pick it. The human sees it on the "
            "Swarm page.")


def _ago(at):
    seconds = max(0, time.time() - (at or 0))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def _history(session=None, kind=None, limit=25):
    rows = activity.timeline(workspace=_session(session)["id"] if session else None, kind=kind,
                             limit=min(int(limit), 100))
    return "\n".join(f"{_ago(r['created_at'])} · {r['actor']} · {r['kind']} · {r['text'][:200]}"
                     for r in rows) or "Nothing recorded yet."


def _search(query, session=None):
    rows = activity.search(query, workspace=_session(session)["id"] if session else None, limit=20)
    return "\n".join(f"{r['kind']} #{r['id']} ({_ago(r['at'])}"
                     f"{', session #' + str(r['workspace']) if r.get('workspace') else ''}): "
                     f"{r['title'][:80]} — {r['snippet']}" for r in rows) or f"Nothing matches {query!r}."


def _config_history(revision=None):
    import revisions
    try:
        if revision is None:
            return "\n".join(f"#{r['id']} {_ago(r['created_at'])} by {r['source']}: "
                             f"{', '.join(r['changed'][:6])}" for r in revisions.recent(20)) \
                or "No changes recorded yet."
        return "\n".join(f"{d['path']}:\n  before: {json.dumps(d['before'])[:400]}\n"
                         f"  after:  {json.dumps(d['after'])[:400]}" for d in revisions.diff(int(revision)))
    except revisions.RevisionError as error:
        raise team.TeamError(str(error)) from error


def _undo_config(revision, which="before"):
    import revisions
    try:
        revisions.restore(int(revision), which)
    except revisions.RevisionError as error:
        raise team.TeamError(str(error)) from error
    return f"Settings put back to {which} revision #{revision} (itself undoable: see config_history)."


def _watchdog(on=None, stall_minutes=None):
    import watchdog
    config = config_store.load()
    current = watchdog.settings(config)
    if on is None and stall_minutes is None:
        return (f"Watchdog {'on' if current['enabled'] else 'off'}: stalled after "
                f"{current['stall_minutes']} minutes without progress, loops, crashes.")
    if stall_minutes is not None and int(stall_minutes) < 5:
        raise team.TeamError("Give it at least 5 minutes: builds and test runs take a while.")
    config["watchdog"] = {**(config.get("watchdog") or {}),
                          **({"enabled": bool(on)} if on is not None else {}),
                          **({"stall_minutes": int(stall_minutes)} if stall_minutes is not None else {})}
    config_store.save(config)
    return _watchdog()


def _routine_line(r):
    import routines
    import schedules
    when = f", next {schedules.clock(r['next_at'])}" if r.get("next_at") and r["status"] == "active" else ""
    return (f"#{r['id']} {r['title']} · {r['agent']} · {routines.describe(r)}{when} · {r['status']}"
            f"{' · only if git changed' if r['only_if_changed'] else ''}"
            f"{' · hook' if r.get('hook_hash') else ''}")


def _routine(do="list", id=None, revision=None, **fields):  # noqa: A002 - the tool's argument name
    import routines
    try:
        if do == "list":
            return "\n".join(_routine_line(r) for r in routines.all_routines()) or "No routines yet."
        if do == "create":
            return "Created " + _routine_line(routines.create(actor=AGENT_ID or "orchestrator", **fields))
        if id is None:
            raise team.TeamError(f"routine do={do} needs id")
        if do == "change":
            return "Changed " + _routine_line(routines.update(id, actor=AGENT_ID or "orchestrator", **fields))
        if do in ("pause", "resume", "archive"):
            status = {"pause": "paused", "resume": "active", "archive": "archived"}[do]
            return _routine_line(routines.set_status(id, status, actor=AGENT_ID or "orchestrator"))
        if do == "run":
            run = routines.fire(id, "manual", fields.get("payload", ""), force=True)
            return f"Run #{run['id']}: {run['status']}" + (f" ({run['reason']})" if run["reason"] else "") \
                + (f" as {run['agent_id']}, task #{run['task_id']}" if run["agent_id"] else "")
        if do == "runs":
            return "\n".join(f"run #{r['id']} {_ago(r['created_at'])} · {r['trigger']} · {r['status']}"
                             f"{' · ' + r['reason'] if r['reason'] else ''}"
                             f"{' · task #' + str(r['task_id']) if r['task_id'] else ''}"
                             f"{' · +' + str(r['coalesced']) + ' skipped while running' if r['coalesced'] else ''}"
                             for r in routines.runs(id, 15)) or "No runs yet."
        if do == "hook":
            secret = routines.new_hook(id, actor=AGENT_ID or "orchestrator")
            return (f"Webhook for routine #{id} (shown once; any old one stops working): POST to "
                    f"https://<this computer's Tailscale address>:<phone port>/v1/hooks/{secret} with "
                    "an optional JSON body. Reachable over Tailscale only.")
        if do == "revisions":
            return "\n".join(f"revision {r['revision']} {_ago(r['created_at'])} by {r['actor']}: "
                             f"{r['snapshot']['repeat']}, {r['snapshot']['agent']}, "
                             f"{r['snapshot']['instructions'][:80]}" for r in routines.revisions(id))
        if do == "restore":
            return "Restored " + _routine_line(routines.restore(id, revision, actor=AGENT_ID or "orchestrator"))
    except routines.RoutineError as error:
        raise team.TeamError(str(error)) from error
    raise team.TeamError(f"Unknown routine action {do!r}")


def _goal_line(g, indent=""):
    return f"{indent}#{g['id']} {g['title']}" + (f" (by {g['target']})" if g["target"] else "") \
        + (f" [{g['status']}]" if g["status"] != "active" else "")


def _goal(do="list", id=None, project=None, title=None, description="", parent=None, target="",  # noqa: A002
          session=None):
    import goals
    try:
        if do == "list":
            rows = goals.all_goals(project)
            ids = {g["id"] for g in rows}
            children = {}
            for g in rows:  # a goal whose parent isn't active shows at the top
                children.setdefault(g["parent_id"] if g["parent_id"] in ids else None, []).append(g)
            lines = []

            def walk(parent, depth):
                for g in children.get(parent, []):
                    where = f" · {g['project']}" if not project and g["project"] else ""
                    lines.append(_goal_line(g, "  " * depth) + where)
                    walk(g["id"], depth + 1)
            walk(None, 0)
            return "\n".join(lines) or "No active goals."
        if do == "add":
            return "Added " + _goal_line(goals.add(title, project or "", description, parent, target))
        if do == "link":
            goals.link(_session(session)["id"], None if id in (None, "none") else int(id))
            return f"Linked session #{session}" + (f" to goal #{id}." if id not in (None, "none") else ": unlinked.")
        if id is None:
            raise team.TeamError(f"goal do={do} needs id")
        if do in ("achieve", "drop"):
            return _goal_line(goals.update(id, status={"achieve": "achieved", "drop": "dropped"}[do]))
        if do == "change":
            changes = {k: v for k, v in (("title", title), ("description", description or None),
                                         ("target", target or None), ("parent_id", parent)) if v is not None}
            return "Changed " + _goal_line(goals.update(id, **changes))
        if do == "show":
            p = goals.progress(id)
            c = p["counts"]
            return "\n".join([_goal_line(p["goal"]) + (f" (under {' → '.join(p['parents'])})" if p["parents"] else ""),
                              f"Sessions: {c['open']} open, {c['solved']} solved, {c['closed']} closed",
                              *[f"- session #{s['id']} {s['title']} [{'solved' if s['solved'] else s['status']}]" for s in p["sessions"]],
                              *[_goal_line(g, "  sub-goal ") for g in p["children"]]])
    except goals.GoalError as error:
        raise team.TeamError(str(error)) from error
    raise team.TeamError(f"Unknown goal action {do!r}")


def _cards(do="list", id=None, title="", project="", query=""):  # noqa: A002
    import statuscards
    try:
        if do == "list":
            return "\n\n".join(f"#{c['id']} {c['title']} ({_ago(c['generated_at']) if c['generated_at'] else 'not written yet'}):\n"
                                f"{c['text'] or '(empty)'}" for c in statuscards.cards()) or "No status cards."
        if do == "add":
            card = statuscards.add(title, project, query)
            return f"Added card #{card['id']}: " + statuscards.refresh(card["id"], force=True)[0]["text"]
        if do == "summary":
            card = statuscards.project_summary(project)
            return statuscards.refresh(card["id"])[0]["text"] or "(empty)"
        if id is None:
            raise team.TeamError(f"cards do={do} needs id")
        if do == "refresh":
            return statuscards.refresh(id, force=True)[0]["text"]
        if do == "archive":
            statuscards.archive(id)
            return f"Archived card #{id}."
    except statuscards.CardError as error:
        raise team.TeamError(str(error)) from error
    raise team.TeamError(f"Unknown cards action {do!r}")


def _backups(do="list"):
    import backups
    if do == "now":
        return f"Backed up to {backups.backup().name}."
    rows = backups.backups()
    return "\n".join(f"{b['name']} ({b['size'] // 1024} KB, {_ago(b['at'])})" for b in rows) \
        or "No backups yet (one is made each day)."


APP_ACTIONS = {
    "history": _history, "search": _search, "config_history": _config_history,
    "undo_config": _undo_config, "backups": _backups, "watchdog": _watchdog, "routine": _routine,
    "goal": _goal, "cards": _cards,
    "secrets": lambda project=None: "\n".join(f"{s['name']} → ${s['env']} ({s['project'] or 'every agent'})"
                                               for s in _api().secrets(project)) or "No secrets.",
    "fetch_skill": lambda repo, path="", ref="": _api().fetch_skill(repo, path, ref)["message"]
    + " It waits for the human on the Swarm page; you can't install it for them.",
    "export": lambda **kw: _api().export_team(**kw)["message"],
    "import": lambda path, apply=False: _api().apply_import(path)["message"] if apply
    else json.dumps(_api().preview_import(path))[:3000],
    "hold": lambda target, on=True, reason="": _api().hold_session(_session(target)["id"], bool(on), reason)["message"],
    "quiet": lambda target, on=True: _api().set_session_quiet(_session(target)["id"], bool(on))["message"],
    "approve": _press("approve"), "deny": _press("deny"), "interrupt": _press("interrupt"),
    "sleep": _sleep, "screen": _screen, "reopen": _reopen, "ask_human": _ask_human,
    "services": _services, "service": _service, "run_all": _run_all, "server": _server,
    "edit_project": _edit_project, "accounts": _accounts,
    "use_account": lambda account: _api().set_active_account(account)["message"],
    "auto_switch": lambda on: _api().set_auto_switch(bool(on))["message"],
    "settings": _settings, "budget": _budget,
    "budget_windows": lambda windows=None: _api().save_budget_windows(windows)["message"]
    if windows is not None else _budget(),
    "new_conversation": lambda: _api().new_conversation()["message"],
    "create_agent": lambda id, **fields: _create_agent(id, **fields),
}


def tool_app(action, args=None):
    import api
    fn = APP_ACTIONS.get(action)
    if fn is None:
        raise team.TeamError(f"Unknown action {action!r}. Actions: {', '.join(APP_ACTIONS)}")
    try:
        return fn(**(args or {}))
    except api.ApiError as error:
        raise team.TeamError(str(error)) from error


def _gmail(call):
    """Run `call(gmail)`, turning Gmail's errors into ones the agent reads."""
    import gmail
    try:
        return call(gmail)
    except gmail.GmailError as error:
        raise team.TeamError(str(error)) from error


def tool_mail_inbox(query="in:inbox", limit=15, account=""):
    return _gmail(lambda gmail: gmail.inbox(query, limit, account.strip()))


def tool_mail_read(id, max_chars=4000):
    return _gmail(lambda gmail: gmail.read(id, max_chars))


def tool_remember(fact):
    import memory
    try:
        memory.remember(fact)
    except ValueError as error:
        raise team.TeamError(str(error)) from error
    return "Remembered."


def tool_schedule(text, when=None, in_minutes=None, repeat="once", every_minutes=None):
    import schedules
    try:
        row = schedules.add(text, when, in_minutes, repeat, every_minutes)
    except schedules.ScheduleError as error:
        raise team.TeamError(str(error)) from error
    return f"Scheduled #{row['id']}: {schedules.describe(row)}, next {schedules.clock(row['next_at'])}."


def tool_list_schedules():
    import schedules
    rows = schedules.active()
    return "\n".join(f"- #{r['id']} {schedules.describe(r)}, next {schedules.clock(r['next_at'])}: "
                     f"{r['text']}" for r in rows) or "None."


def tool_cancel_schedule(id):  # noqa: A002 - the tool's argument name
    import schedules
    try:
        schedules.cancel(id)
    except schedules.ScheduleError as error:
        raise team.TeamError(str(error)) from error
    return f"Cancelled #{id}."


def tool_announce(text):
    import voice
    config = config_store.load()
    settings = voice.settings(config)
    if not settings["enabled"]:
        return "The voice is switched off in the app, so nothing was said."
    if voice.quiet_now(settings):
        return "It's the human's quiet hours, so nothing was said."
    text = team.clip(text.strip(), 300)
    if not voice.say(text, settings, "needs_you"):  # announce is for when he needs the human
        raise team.TeamError("No speech engine (spd-say) is available.")
    return "Said."


HANDLERS = {name.removeprefix("tool_"): fn for name, fn in globals().items()
            if name.startswith("tool_")}


# ---------------------------------------------------------------- protocol

def call_tool(request_id, params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    try:
        if name not in available_tools():
            raise team.TeamError(f"Unknown tool {name!r}")
        text, is_error = HANDLERS[name](**arguments), False
    except team.TeamError as error:
        text, is_error = str(error), True
    except TypeError as error:
        text, is_error = f"Bad arguments: {error}", True
    except Exception as error:  # report instead of leaving the agent hanging
        text, is_error = f"Lago Agents hit an internal error: {error!r}", True
    send({"jsonrpc": "2.0", "id": request_id,
          "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def handle(message):
    method, request_id = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if request_id is None:
        return  # a notification, e.g. notifications/initialized
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "lago", "version": "1.0"},
            "instructions": "Lago Agents: work with your team of agents. Each agent is a "
                            "live session; use these tools to delegate and report."}})
    elif method == "tools/list":
        tools = [{"name": name, **spec} for name, spec in available_tools().items()]
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
        if AGENT_ID:
            hub.set_state(AGENT_ID, tools_at=time.time())  # messages held for this go out now
    elif method == "tools/call":
        # Waiting can take minutes, so each call gets its own thread.
        worker = threading.Thread(target=call_tool, args=(request_id, params), daemon=True)
        _workers.append(worker)
        worker.start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": request_id,
              "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        handle(message)
    for worker in _workers:  # input closed: finish the calls already made
        worker.join()


if __name__ == "__main__":
    main()
