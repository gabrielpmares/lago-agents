"""Acceptance criteria and reviewers on pipeline steps: a step is done only when its result
meets its criteria and every reviewer approved it.

- **criteria**: the step's task lists them and asks the agent to answer each, numbered, as
  `met` or `not met` with evidence. A criterion it leaves out or marks not met fails the step,
  with what was missing, so the Conductor re-runs it.
- **reviewers**: swarm agents that review the result in order once the criteria pass. Each
  gets a review task and starts its answer with APPROVED or CHANGES. Changes send the step
  back to its agent with the findings (at most MAX_ROUNDS times, then the step fails and the
  Conductor decides); approval passes it to the next reviewer, and the last approval finishes it.

swarm.sync() drives this; the parsing and texts live here.
"""

import re

MAX_CRITERIA = 10
MAX_REVIEWERS = 3
MAX_ROUNDS = 2

_ANSWER = re.compile(r"^\s*(?:[-*]\s*)?(?:criterion\s*)?(\d+)\s*[.):-]?\s*(?:\*\*)?\s*(not met|met)\b(.*)$",
                     re.IGNORECASE | re.MULTILINE)


def criteria_block(criteria):
    """The task's lines asking for each criterion to be answered."""
    if not criteria:
        return []
    return ["", "## Acceptance criteria",
            "Your work is done only when it meets every one of these:",
            *[f"{n}. {c}" for n, c in enumerate(criteria, 1)],
            "", "End your result with a `Criteria:` list answering each by its number, as "
            "`1. met: <evidence>` or `1. not met: <why>`. A criterion you leave out counts as "
            "not met."]


def check_criteria(criteria, result):
    """(True, []) when every criterion is answered met; else (False, [what's missing])."""
    answers = {}
    for number, verdict, _ in _ANSWER.findall(result or ""):
        answers[int(number)] = verdict.lower() == "met"
    missing = []
    for n, criterion in enumerate(criteria or [], 1):
        if n not in answers:
            missing.append(f"{n}. {criterion} (not answered)")
        elif not answers[n]:
            missing.append(f"{n}. {criterion} (not met)")
    return not missing, missing


def review_body(pipeline, step, reviewer_name, result, number, total):
    lines = [f"Review step `{step['id']}` of pipeline #{pipeline['id']} \"{pipeline['title']}\" "
             f"(reviewer {number} of {total}).", "",
             "## Pipeline goal", pipeline["goal"], "",
             f"## What the step was asked to do ({step['title']})", step["task"]]
    if step.get("criteria"):
        lines += ["", "## Its acceptance criteria", *[f"{n}. {c}" for n, c in enumerate(step["criteria"], 1)]]
    lines += ["", "## Its result", result or "(no result text)", "",
              "## Your review",
              f"Check the work itself (open the files it names), as {reviewer_name}. Start your "
              "result with `APPROVED` when it's good enough to build on, or `CHANGES` followed by "
              "the specific changes it needs, most important first. Don't make the changes yourself."]
    return "\n".join(lines)


def parse_review(result):
    """("approved" | "changes", the reviewer's text). Anything not clearly approved asks for changes."""
    text = (result or "").strip()
    first = re.sub(r"^[#*\s>`_-]+", "", text).upper()
    if first.startswith("APPROVED"):
        return "approved", text
    return "changes", text
