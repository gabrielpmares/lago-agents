"""Work sessions telling you things: recorded, spoken aloud and relayed to the Orchestrator.

A Conductor reports with its `report` tool; Lago reports for it when a pipeline waits for
approval or finishes, and the hooks report when a session sits waiting for you or asks
for permission. Sessions the Orchestrator opened also send every report to it, so you
can answer from one place.
"""

import time

import config_store
import hub
import memory
import runner
import team
import voice

ORCHESTRATOR = config_store.ORCHESTRATOR_ID
# "solved": the whole task is delivered and checked, nothing left: the session can close.
# "done": one pipeline (a stage) finished, and there may be more to do.
KINDS = ("solved", "done", "failed", "question", "gate", "approval", "blocked", "permission",
         "waiting", "update")
# The same kind of news from the same session isn't spoken twice within this window
# (say, finish_pipeline and the Conductor's own "done" report).
REPEAT_SECONDS = 90
TEXT_LIMIT = 4000


def where(config, workspace):
    project = config["projects"].get(workspace["project"], {})
    name = project.get("name", workspace["project"]) if workspace["project"] \
        else config_store.NO_PROJECT
    area = (project.get("areas") or {}).get(workspace["area"] or "", {})
    return name, f"{name} / {area.get('name', workspace['area'])}" if area else name


def report(workspace_id, kind, text, form=None):
    """Record what a work session has to say, speak it and relay it. Returns the event id.
    `form`: a question's choices (questions.clean_form), answered with a click in the app."""
    if kind not in KINDS:
        raise team.TeamError(f"Unknown kind {kind!r}. Use one of: {', '.join(KINDS)}")
    workspace = hub.get_workspace(int(workspace_id))
    if workspace is None:
        raise team.TeamError(f"There's no work session #{workspace_id}")
    config = config_store.load()
    project_name, place = where(config, workspace)
    text = team.clip((text or "").strip(), TEXT_LIMIT)
    repeated = hub.events("workspace = ? AND kind = ? AND created_at > ?",
                          (workspace["id"], kind, time.time() - REPEAT_SECONDS), limit=1)
    import company
    # A project someone else handles asks them (their bell), not your speakers.
    muted = bool(company.handlers(config).get(workspace["project"])) or \
        (workspace.get("quiet") and voice.KIND_CATEGORY.get(kind) != "needs_you")
    spoken = "" if repeated or muted else voice.announce(config, kind, project_name)
    event_id = hub.add_event(workspace["id"], workspace["project"], kind, text, spoken, form)
    if workspace.get("orchestrated"):
        if form:
            import questions
            text = (f"{text}\n\nIts form (report #{event_id}; answer with `answer_question`, or "
                    f"pass it to the human):\n{questions.form_text(form)}")
        tell_orchestrator(f"[Lago] Report from work session #{workspace['id']} ({place}, "
                          f"\"{workspace['title']}\"): {kind}\n\n{text or '(no details)'}",
                          start=False)
    return event_id


def tell_orchestrator(text, start=True):
    """Type `text` into the Orchestrator once it's at its prompt. With `start`, a stopped
    Orchestrator starts with it; otherwise it waits in the inbox until it next runs."""
    hub.queue_text(ORCHESTRATOR, text)
    if _fresh_conversation_due():
        memory.rotate_later()  # writes down the old conversation, then delivers this
        return
    try:
        team.pump(ORCHESTRATOR, auto_start=start)
    except team.TeamError:
        if start:
            raise


def _fresh_conversation_due():
    """Lago is idle in a conversation from an earlier day, or one that's grown long."""
    if not runner.is_running(ORCHESTRATOR) or config_store.rotating():
        return False
    state = hub.get_state(ORCHESTRATOR)
    return state["status"] == "idle" and memory.rotation_due(state.get("session_id"))


def quiet_since(workspace_id, since):
    """True when the session hasn't reported anything since `since`."""
    return not hub.events("workspace = ? AND created_at > ?", (workspace_id, since or 0), limit=1)
