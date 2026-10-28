# Specialists

A specialist is a swarm agent that works and teaches like a professional in one role, for
example a .NET cloud engineer on Azure, a Kubernetes platform engineer or a web
application pentester. Build one on the **Specialists** tab of the Agents page. You can also
ask Lago, who calls `create_specialist`.

Nothing is fine-tuned. A specialist is three things:
- researched notes it reads before it answers or builds;
- a working method: how a senior in the role approaches work, their quality bar, and what
  they escalate;
- an exam it has to pass, which then stays as its test suite.

## How one is built

1. **Pick the role.** You narrow a field down: Field › Area › Role › Focus. The list is in
   `lago/prompts/specialists/fields.json`. **Suggest More** asks a cheap model for more
   options at any level, and you can type your own. Your notes ("I'm targeting a bank:
   Azure DevOps, Bicep, private networking") steer everything after this.
2. **Role map.** The `specialist-builder` reads current job postings, the certification
   skill outlines that match the role (AZ-204/305/400, CKA, AWS exam guides…), official
   learning paths and practitioner sources. It writes `role-map.json`: day-to-day work,
   deliverables, what makes a senior, and the topics, each marked core, working or
   awareness. **You approve the map**: untick topics, add your own, add notes. Nothing is
   studied before that.
3. **Study.** One `specialist-researcher` run per topic, three at a time, writes
   `knowledge/<topic>.md`. Each note covers concepts, step-by-step playbooks with complete
   code or config, trade-offs, pitfalls, security/cost/operations, check-yourself questions
   and sources. Primary sources come first, and every note says which versions it covers
   and when it was checked. A failed study is tried once more.
4. **Assemble.** The builder writes the exam *before* it reads the notes, so the exam tests
   the field and not the notes: two questions per core topic and one per working topic. About
   60% are teaching questions and 40% are hands-on tasks that need no cloud account. Then it
   writes `knowledge/index.md` and `prompt.md` (the working method).
5. **Exam.** The specialist joins the catalog switched off, and its exam runs as its test
   suite (`evals.py`: each answer is graded against strict criteria). It passes with an
   average of 80% or more and no more than one answer in six below 70%. If it fails, the
   topics behind the weak answers are studied again, with what the grader saw, and the exam
   runs once more. If it fails again it stops, and **Try Again** gives it another round.
6. **Live.** It's switched on, and you hear it on the bell.

Building one uses real usage. The builder and the specialist run on Opus, the researchers
on Sonnet. A focused specialist (8-12 topics) takes a few hours. A thorough one (14-22) takes
most of a day.

## Using one

- **Ask** (its page, or Lago's `ask_specialist`): a conversation of its own, the
  `sp-<id>` session, kept from one question to the next. It explains at your level, gives
  the mental model before the detail, points to where facts come from, and can quiz you.
- **Files**: attach Markdown, PDFs, text files or pictures to a question (**Attach**, or drop
  them on the box), or give Lago a file and ask him to pass it on (`ask_specialist` takes
  paths). It reads all of each one and says where in it each fact is. The files are kept in
  its `files/` folder until you remove the specialist, so you can come back to them.
- **Case study**: switch on **Case study** before you ask, with the case attached. It solves
  the case and writes a walkthrough you keep, in `cases/<case>.md`: the case in brief (the
  facts that decide it, the ones that don't, the assumptions), the concepts it tests, then
  every step with what to do, why, the alternatives and why they lose, the usual mistake and
  **Say it like this** (how you'd explain the step yourself), the full answer, the follow-up
  questions you'd get with model answers, and check-yourself questions. Then it offers to
  drill you: explain a step back and it tells you what's right, missing or wrong. The
  walkthroughs and your files are under **Case files** at the top of the Ask tab.
- **Give It Work** (or Lago's `specialist_work`): a work session it leads. It writes the
  plan and acceptance criteria, other agents implement where that's cheaper, and it reviews
  every step that produces code or infrastructure. You approve the plan first, as with any
  session.
- Conductors and Lago also find it with `find_agents`, like any agent. Its domain is
  `specialist`.

## Keeping it current

Every `swarm.specialist_refresh_days` (config.json, default 30; 0 turns it off) the builder
checks what changed in the field since the notes were written: new versions, retirements,
new practices, advisories. If something material changed, a refresh waits for your OK on
its **Updates** tab. With your yes, the changed topics are studied into `knowledge-next/`.
The new notes replace the old ones only if the specialist passes its exam on them.
Otherwise it keeps the old notes. **Check for Updates Now** runs the check at once.

Thumbs-down ratings improve its prompt the same way as any agent's (see agent-catalog.md),
and the exam is the regression gate.

## Research sources

- **Built in:** Claude's web search and page fetching.
- **Perplexity** (optional): add a key with **Add Perplexity**. It's saved as the secret
  `perplexity` (`PERPLEXITY_API_KEY`, for every agent). The builder and researchers then get
  Perplexity's MCP server (`@perplexity-ai/mcp-server`, pinned) and use `perplexity_research`
  for wide overviews before confirming details at primary sources. Perplexity bills you per
  use.

## NotebookLM

Google has no public NotebookLM API for personal accounts, so this uses the unofficial
[notebooklm-py](https://github.com/teng-lin/notebooklm-py) (pinned in `notebooklm.py`). It
can stop working whenever Google changes NotebookLM. Nothing else depends on it.

- **Connect** opens a terminal that installs it in `~/lago-work/tools/notebooklm/.venv` and
  runs its Google sign-in in a browser window. **Check** confirms the sign-in works. The
  sign-in is kept by notebooklm-py in `~/.notebooklm`; Lago never reads it.
- When a specialist goes live, or is refreshed, its notes and the official sources from its
  role map go into a notebook of its own, and the notebook's chat is set to the learning
  guide.
- On its **NotebookLM** tab you can make an audio overview, a quiz, flashcards, a study
  guide or a slide deck. They appear in the notebook's Studio panel.
- The specialist gets an `ask_notebook` tool, for answers grounded in those sources.

## Files

- `catalog/specialists/<id>/`: `specialist.json` (its state), `role-map.json`,
  `knowledge/`, `exam.json`, `prompt.md`.
- `catalog/agents/<id>.md`: the agent itself. `knowledge: <id>/knowledge` lets it read its
  notes.
- `catalog/tests/<id>.json`: its exam.
- `~/lago-work/specialists/<id>/`: the folder its conversation works in (exercises), with
  `files/` (what you gave it) and `cases/` (its case walkthroughs).

Exporting a specialist (Teams › Export) takes its agent and exam, not its notes.
