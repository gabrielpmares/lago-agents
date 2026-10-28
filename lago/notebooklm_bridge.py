"""Runs inside NotebookLM's own virtualenv (~/lago-work/tools/notebooklm/.venv), so the
unofficial notebooklm-py library and its browser dependencies stay out of the app's Python.
Reads one JSON request on stdin, prints one JSON reply: {"ok": true, ...} or {"error": "..."}.
notebooklm.py is the app's side of it."""

import asyncio
import json
import sys
from pathlib import Path

from notebooklm import ChatGoal, NotebookLMClient

MAX_SOURCES = 45  # NotebookLM's free plan holds 50 sources a notebook: leave room for yours
NOTE_PREFIX = "Lago notes: "


async def check(_request):
    async with NotebookLMClient.from_storage() as client:
        notebooks = await client.notebooks.list()
    return {"notebooks": len(notebooks)}


async def sync(request):
    """Create the notebook (or reuse it), replace our notes in it, add the role's official
    sources it doesn't have yet, and switch its chat to the learning guide."""
    async with NotebookLMClient.from_storage() as client:
        notebook = None
        if request.get("notebook_id"):
            notebook = await client.notebooks.get_or_none(request["notebook_id"])
        if notebook is None:
            notebook = await client.notebooks.create(request["title"])
        existing = await client.sources.list(notebook.id)
        ours = [s.id for s in existing if (s.title or "").startswith(NOTE_PREFIX)]
        if ours:
            await client.sources.delete_many(notebook.id, ours)
        count = len(existing) - len(ours)
        failed = []
        for path in request["files"]:
            if count >= MAX_SOURCES:
                break
            try:
                await client.sources.add_file(notebook.id, path, mime_type="text/markdown",
                                              title=NOTE_PREFIX + Path(path).stem)
                count += 1
            except Exception as error:  # noqa: BLE001 - one file must not stop the rest
                failed.append(f"{Path(path).name}: {error}")
        have = {s.url for s in existing if s.url}
        for url in request.get("urls", []):
            if count >= MAX_SOURCES:
                break
            if url in have:
                continue
            try:
                await client.sources.add_url(notebook.id, url)
                count += 1
            except Exception as error:  # noqa: BLE001 - a page NotebookLM can't read is skipped
                failed.append(f"{url}: {error}")
        await client.chat.configure(notebook.id, goal=ChatGoal.LEARNING_GUIDE)
    return {"notebook_id": notebook.id, "sources": count, "failed": failed[:10]}


async def ask(request):
    async with NotebookLMClient.from_storage() as client:
        result = await client.chat.ask(request["notebook_id"], request["question"])
        titles = {s.id: s.title for s in await client.sources.list(request["notebook_id"])}
    cited = []
    for ref in result.references or []:
        title = titles.get(getattr(ref, "source_id", None))
        if title and title not in cited:
            cited.append(title)
    return {"answer": result.answer, "sources": cited}


GENERATORS = {"audio": "generate_audio", "quiz": "generate_quiz", "flashcards": "generate_flashcards",
              "study_guide": "generate_study_guide", "slide_deck": "generate_slide_deck"}


async def generate(request):
    async with NotebookLMClient.from_storage() as client:
        method = getattr(client.artifacts, GENERATORS[request["kind"]])
        status = await method(request["notebook_id"])
    return {"task_id": getattr(status, "task_id", "")}


OPS = {"check": check, "sync": sync, "ask": ask, "generate": generate}


def main():
    request = json.loads(sys.stdin.read() or "{}")
    try:
        reply = {"ok": True, **asyncio.run(OPS[request["op"]](request))}
    except Exception as error:  # noqa: BLE001 - every failure goes back as text
        reply = {"error": f"{type(error).__name__}: {error}"[:600]}
    print(json.dumps(reply))


if __name__ == "__main__":
    main()
