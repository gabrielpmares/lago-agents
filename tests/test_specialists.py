"""Specialists: a role mapped, approved, studied, examined, then asked, refreshed and removed.
The builder and researcher runs are faked: each "run" is a real task the test finishes after
writing the files that agent would have written."""

import json
import unittest
from pathlib import Path
from unittest import mock

import support

import agentdefs
import config_store
import evals
import hub
import specialists
import swarm
import team
import vault

ROLE = ["IT & Software", "Backend Engineering", ".NET Backend Engineer",
        ".NET cloud engineer on Azure (Bicep, AKS)"]
NOTE = "# Topic\n\n" + "A long enough note. " * 40


class Runs:
    """Stands in for swarm.run_agent: records each run as an open task."""

    def __init__(self):
        self.calls = []

    def __call__(self, agent, task, project_id="", folder="", inputs=None, **kwargs):
        task_id = hub.create_task(f"r{len(self.calls) + 1}-{agent}", task[:80], task)
        self.calls.append({"agent": agent, "task": task, "folder": Path(folder), "inputs": inputs or {},
                           "task_id": task_id, **kwargs})
        return task_id, f"r{len(self.calls)}-{agent}", "model"

    def open(self, agent):
        return [c for c in self.calls if c["agent"] == agent
                and hub.get_task(c["task_id"])["status"] in hub.OPEN_TASK]

    def finish(self, call, status="done", result="Done."):
        hub.update_task(call["task_id"], status=status, result=result)


def role_map(topics=5):
    return {"title": ".NET Cloud Engineer (Azure)", "summary": "Builds and runs .NET on Azure.",
            "day_to_day": ["Deploys with Bicep"], "deliverables": ["Bicep modules"], "senior_bar": ["Owns reliability"],
            "certifications": [{"name": "AZ-204", "url": "https://learn.microsoft.com/az-204"}],
            "topics": [{"id": f"topic-{i}", "name": f"Topic {i}", "area": "Cloud", "depth": "core" if i < 2 else "working",
                        "priority": 1, "why": "Postings ask for it."} for i in range(topics)],
            "sources": [{"title": "Docs", "url": "https://learn.microsoft.com/azure"}]}


class SpecialistTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        import shutil
        shutil.rmtree(specialists.DIR, ignore_errors=True)
        shutil.rmtree(evals.TESTS_DIR, ignore_errors=True)
        self.runs = Runs()
        self._patch(swarm, "run_agent", self.runs)
        self._patch(specialists, "WORK_DIR", support.TMP / "spec-work")
        self._patch(swarm, "_trust", lambda folder: None)
        self._patch(team, "pump", lambda *a, **k: None)
        self._patch(evals, "run_tests", self._run_tests)
        self.scores = {}
        self.exams = []

    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_tests(self, catalog_id, proposal=None, variant=None, only=None, repeats=1):
        ids = [hub.add_test_run(catalog_id, t["id"], "v1", str(support.TMP), proposal, str(variant) if variant else None)
               for t in evals.tests(catalog_id)]
        self.exams.append({"ids": ids, "variant": variant})
        return ids

    def grade(self, default=0.9):
        for run_id in self.exams[-1]["ids"]:
            run = hub.test_runs("id = ?", (run_id,))[0]
            hub.update_test_run(run_id, status="graded", score=self.scores.get(run["test_id"], default),
                                notes=f"notes on {run['test_id']}")

    def build_to_exam(self, sid, topics=5):
        """Map, approve and study a specialist, and have the builder assemble it."""
        call = self.runs.open("specialist-builder")[0]
        (call["folder"] / "role-map.json").write_text(json.dumps(role_map(topics)))
        self.runs.finish(call)
        specialists.tick()
        self.assertEqual(specialists.get(sid)["status"], "map_ready")
        specialists.approve_map(sid)
        while self.runs.open("specialist-researcher"):
            for study in self.runs.open("specialist-researcher"):
                Path(study["inputs"]["output"]).write_text(NOTE)
                self.runs.finish(study)
            specialists.tick()
        self.assertEqual(specialists.get(sid)["status"], "assembling")
        call = self.runs.open("specialist-builder")[0]
        folder = call["folder"]
        exam = {"tests": [{"name": f"Q{i}", "topic": f"topic-{i % topics}", "task": f"Explain thing {i}",
                           "criteria": "- says why"} for i in range(6)]}
        (folder / "exam.json").write_text(json.dumps(exam))
        (folder / "prompt.md").write_text("**Principles**: keep it simple.")
        (folder / "knowledge" / "index.md").write_text("- [Topic 0](topic-0.md)")
        self.runs.finish(call)
        specialists.tick()

    def test_a_role_goes_from_map_to_live_only_after_its_exam(self):
        sid = specialists.create(ROLE, notes="Targeting a bank", depth="focused")
        self.assertEqual(sid, "net-cloud-engineer-on-azure")
        meta = specialists.get(sid)
        self.assertEqual((meta["status"], meta["title"]), ("mapping", ".NET cloud engineer on Azure"))
        self.assertTrue((agentdefs.CATALOG_AGENTS / "specialist-builder.md").exists())  # shipped, now yours
        self.assertIn("between 8 and 12", self.runs.calls[0]["task"])
        self.assertIn("Targeting a bank", self.runs.calls[0]["task"])

        call = self.runs.open("specialist-builder")[0]
        (call["folder"] / "role-map.json").write_text(json.dumps(role_map()))
        self.runs.finish(call)
        specialists.tick()
        meta = specialists.get(sid)
        self.assertEqual((meta["status"], meta["title"]), ("map_ready", ".NET Cloud Engineer (Azure)"))
        self.assertFalse(self.runs.open("specialist-researcher"))  # nothing studied before your OK

        specialists.approve_map(sid, keep=["topic-0", "topic-1", "topic-2", "topic-3"],
                                added=[{"name": "Azure API Management", "depth": "core"}])
        meta = specialists.get(sid)
        self.assertEqual(sorted(meta["topics"]), ["azure-api-management", "topic-0", "topic-1", "topic-2", "topic-3"])
        self.assertEqual(len(self.runs.open("specialist-researcher")), specialists.MAX_STUDYING)

        while self.runs.open("specialist-researcher"):
            for study in self.runs.open("specialist-researcher"):
                Path(study["inputs"]["output"]).write_text(NOTE)
                self.runs.finish(study)
            specialists.tick()
        self.assertEqual(specialists.get(sid)["status"], "assembling")

        call = self.runs.open("specialist-builder")[0]
        self.assertIn("Mode: assemble", call["task"])
        (call["folder"] / "exam.json").write_text(json.dumps({"tests": [
            {"name": f"Q{i}", "topic": "topic-0", "task": f"Explain {i}", "criteria": "- right"} for i in range(6)]}))
        (call["folder"] / "prompt.md").write_text("**Principles**: least privilege.")
        (call["folder"] / "knowledge" / "index.md").write_text("- [Topic 0](topic-0.md)")
        self.runs.finish(call)
        specialists.tick()

        meta = specialists.get(sid)
        self.assertEqual(meta["status"], "examining")
        self.assertIn(sid, swarm.disabled(config_store.load()))  # off until it passes
        manifest = agentdefs.catalog()[sid]
        self.assertEqual((manifest["domain"], manifest["knowledge"], manifest["model"]),
                         ("specialist", f"{sid}/knowledge", specialists.MODEL))
        self.assertIn("least privilege", manifest["body"])
        self.assertEqual(len(evals.tests(sid)), 6)

        self.grade(0.9)
        specialists.tick()
        meta = specialists.get(sid)
        self.assertEqual(meta["status"], "live")
        self.assertNotIn(sid, swarm.disabled(config_store.load()))
        self.assertIn("passed its exam", hub.events()[0]["text"])

    def test_a_failed_exam_studies_the_weak_topics_again_then_stops(self):
        sid = specialists.create(ROLE)
        self.build_to_exam(sid)
        tests = evals.tests(sid)
        self.scores = {tests[0]["id"]: 0.2, tests[1]["id"]: 0.3}  # topic-0 and topic-1 answers
        self.grade(0.75)
        specialists.tick()
        meta = specialists.get(sid)
        self.assertEqual(meta["status"], "studying")
        queued = {tid for tid, t in meta["topics"].items() if t["status"] in ("queued", "studying")}
        self.assertEqual(queued, {"topic-0", "topic-1"})
        study = self.runs.open("specialist-researcher")[0]
        self.assertIn("What to fix", study["task"])
        self.assertIn("notes on", study["task"])  # what the grader saw

        for s in self.runs.open("specialist-researcher"):
            Path(s["inputs"]["output"]).write_text(NOTE)
            self.runs.finish(s)
        specialists.tick()
        self.assertEqual(specialists.get(sid)["status"], "examining")
        self.assertEqual(len(self.exams), 2)
        self.grade(0.5)
        specialists.tick()
        meta = specialists.get(sid)
        self.assertEqual(meta["status"], "failed")
        self.assertIn(sid, swarm.disabled(config_store.load()))

    def test_questions_that_couldnt_run_dont_count_and_too_many_stop_the_exam(self):
        sid = specialists.create(ROLE)
        self.build_to_exam(sid)
        ids = self.exams[-1]["ids"]
        self.grade(0.9)
        hub.update_test_run(ids[0], status="running", score=None)
        self.assertIn("5 of 6 questions graded, 1 being answered", specialists.details(sid)["message"])
        self.assertEqual(specialists.details(sid)["exam"][0]["state"], "running")
        hub.update_test_run(ids[0], status="error", score=0, notes="Grading failed: session limit")
        specialists.tick()
        meta = specialists.get(sid)
        self.assertEqual(meta["status"], "live")  # five of six answered well: the failed one isn't a 0
        self.assertEqual(len(meta["exam"]["results"]), 5)

        sid = specialists.create(["IT & Software", "Data", "Data engineer"])
        self.build_to_exam(sid)
        for run_id in self.exams[-1]["ids"]:
            hub.update_test_run(run_id, status="error", score=0, notes="Grading failed: session limit")
        specialists.tick()
        meta = specialists.get(sid)
        self.assertEqual(meta["status"], "failed")
        self.assertIn("session limit", meta["message"])
        specialists.retry(sid)  # sits the same exam again, without rebuilding it
        self.assertEqual(specialists.get(sid)["status"], "examining")
        self.assertEqual(len(self.exams[-1]["ids"]), 6)
        self.assertFalse(self.runs.open("specialist-builder"))

    def test_pass_rule(self):
        good = {f"t{i}": {"score": 0.9} for i in range(6)}
        self.assertTrue(specialists.passed(good)[0])
        one_weak = {**good, "t0": {"score": 0.6}}
        self.assertTrue(specialists.passed(one_weak)[0])  # one in six may miss
        two_weak = {**one_weak, "t1": {"score": 0.6}}
        self.assertFalse(specialists.passed(two_weak)[0])
        self.assertFalse(specialists.passed({f"t{i}": {"score": 0.75} for i in range(6)})[0])  # mean under 80
        self.assertFalse(specialists.passed({})[0])

    def test_the_tutor_keeps_its_conversation_and_reads_its_notes(self):
        sid = specialists.create(ROLE)
        self.build_to_exam(sid)
        self.grade(0.95)
        specialists.tick()
        task_id = specialists.ask(sid, "When do I use Service Bus topics?")
        tutor = specialists.tutor_id(sid)
        agent = config_store.load()["agents"][tutor]
        self.assertTrue(agent["tutor"] and agent["direct"])
        self.assertEqual(hub.get_task(task_id)["to_agent"], tutor)
        definition = team._definition(agent, support.TMP)
        knowledge = str((specialists.DIR / sid / "knowledge").resolve())
        self.assertEqual(definition["knowledge"], knowledge)
        prompt = team._system_prompt(config_store.load(), tutor, definition)
        self.assertIn("your own conversation", prompt)
        self.assertIn(knowledge, prompt)
        self.assertIn(f"Read(/{knowledge}/**)", team._settings(definition)["permissions"]["allow"])
        self.assertEqual([q["question"] for q in specialists.conversation(sid)], ["When do I use Service Bus topics?"])

    def test_a_case_study_keeps_its_files_and_asks_for_a_walkthrough(self):
        import attachments
        import base64
        sid = specialists.create(ROLE)
        self.build_to_exam(sid)
        self.grade(0.95)
        specialists.tick()
        case = "# Case\n\nContoso moves 40 VMs to Azure.\n".encode()
        upload = {"data": "data:text/markdown;base64," + base64.b64encode(case).decode(), "name": "Contoso case.md"}
        attachments.FOLDER.mkdir(parents=True, exist_ok=True)
        sent_to_lago = attachments.FOLDER / "20261027-101010-000001-1-brief.pdf"
        sent_to_lago.write_bytes(b"%PDF-1.4 brief")
        task_id = specialists.ask(sid, "", files=[upload], paths=[str(sent_to_lago)], case=True)
        kept = specialists.files_folder(sid)
        self.assertEqual(sorted(p.name for p in kept.iterdir()), ["20261027-101010-000001-1-brief.pdf", "Contoso-case.md"])
        task = hub.get_task(task_id)
        self.assertTrue(task["title"].startswith("Case study: Contoso-case.md"))
        self.assertIn(f"[File: {kept / 'Contoso-case.md'}]", task["body"])
        self.assertIn(str(specialists.cases_folder(sid) / "contoso-case.md"), task["body"])
        self.assertIn("Say it like this", task["body"])
        [shown] = specialists.conversation(sid)
        self.assertTrue(shown["case"])
        self.assertEqual(shown["question"], "Walk me through this case: solve it and teach me every step.")
        self.assertEqual([f["name"] for f in shown["files"]], ["Contoso-case.md", "20261027-101010-000001-1-brief.pdf"])
        # The same file again is kept once; a different one with the same name is kept beside it.
        specialists.ask(sid, "And step 2?", files=[upload])
        specialists.ask(sid, "Updated case", files=[{**upload, "data": "data:text/markdown;base64," + base64.b64encode(b"v2").decode()}])
        self.assertEqual(len(list(kept.glob("Contoso-case*.md"))), 2)
        self.assertFalse(specialists.conversation(sid)[1]["case"])
        specialists.cases_folder(sid).mkdir(parents=True)
        (specialists.cases_folder(sid) / "contoso-case.md").write_text("# Walkthrough")
        listed = specialists.work_files(sid)
        self.assertEqual(listed[0]["kind"], "case")
        self.assertEqual(specialists.read_work_file(sid, listed[0]["path"]), "# Walkthrough")
        with self.assertRaises(team.TeamError):
            specialists.read_work_file(sid, str(specialists.DIR / sid / "specialist.json"))
        with self.assertRaises(team.TeamError):
            specialists.ask(sid, "Read this", paths=["/etc/hostname"])
        with self.assertRaises(team.TeamError):  # a program isn't a file it reads
            specialists.ask(sid, "Read this", files=[{"data": "data:;base64," + base64.b64encode(b"\x7fELF\x00\x01").decode(), "name": "x"}])

    def test_only_folders_under_specialists_are_knowledge(self):
        self.assertEqual(agentdefs.knowledge_dir({"knowledge": "../agents"}), "")
        self.assertEqual(agentdefs.knowledge_dir({"knowledge": ""}), "")
        self.assertTrue(agentdefs.knowledge_dir({"knowledge": "x/knowledge"}).endswith("specialists/x/knowledge"))
        with self.assertRaises(team.TeamError):
            specialists.read_note("nobody", "../specialist.json")

    def test_perplexity_needs_a_key_for_every_agent(self):
        self.assertIsNone(team.mcp_server("perplexity"))
        vault.save("perplexity", "PERPLEXITY_API_KEY", "pplx-123", project="some-project")
        self.assertIsNone(team.mcp_server("perplexity"))  # a project's key isn't for the builders
        vault.save("perplexity-all", "PERPLEXITY_API_KEY", "pplx-123")
        self.assertEqual(team.mcp_server("perplexity")[0](support.TMP)["args"][-1], team.PERPLEXITY_MCP)

    def test_a_refresh_replaces_the_notes_only_after_passing_the_exam(self):
        sid = specialists.create(ROLE)
        self.build_to_exam(sid)
        self.grade(0.9)
        specialists.tick()
        specialists.check_for_updates(sid)
        call = self.runs.open("specialist-builder")[0]
        self.assertIn("Mode: refresh", call["task"])
        (call["folder"] / "refresh.json").write_text(json.dumps({"summary": ".NET 10 is out.", "changes": [
            {"topic": "topic-0", "what": ".NET 10 LTS", "why": "new default", "sources": ["https://x"]},
            {"topic": "new:Azure Container Apps", "what": "now common", "why": "postings ask"}]}))
        self.runs.finish(call)
        specialists.tick()
        self.assertEqual(specialists.get(sid)["refresh"]["status"], "ready")

        specialists.decide_refresh(sid, True)
        studies = self.runs.open("specialist-researcher")
        self.assertEqual(sorted(Path(s["inputs"]["output"]).name for s in studies),
                         ["azure-container-apps.md", "topic-0.md"])
        self.assertTrue(all("knowledge-next" in s["inputs"]["output"] for s in studies))
        for s in studies:
            Path(s["inputs"]["output"]).write_text(NOTE + " refreshed")
            self.runs.finish(s)
        specialists.tick()
        self.assertEqual(specialists.get(sid)["refresh"]["status"], "examining")
        self.assertIn("knowledge-next", Path(self.exams[-1]["variant"]).read_text())
        self.grade(0.95)
        specialists.tick()
        folder = specialists.DIR / sid
        self.assertIn("refreshed", (folder / "knowledge" / "topic-0.md").read_text())
        self.assertIn("azure-container-apps.md", (folder / "knowledge" / "index.md").read_text())
        self.assertEqual(specialists.get(sid)["refresh"], {})

    def test_a_refresh_that_fails_its_exam_keeps_the_old_notes(self):
        sid = specialists.create(ROLE)
        self.build_to_exam(sid)
        self.grade(0.9)
        specialists.tick()
        specialists.check_for_updates(sid)
        call = self.runs.open("specialist-builder")[0]
        (call["folder"] / "refresh.json").write_text(json.dumps({"summary": "s", "changes": [
            {"topic": "topic-0", "what": "w", "why": "y"}]}))
        self.runs.finish(call)
        specialists.tick()
        specialists.decide_refresh(sid, True)
        for s in self.runs.open("specialist-researcher"):
            Path(s["inputs"]["output"]).write_text("wrong " * 200)
            self.runs.finish(s)
        specialists.tick()
        self.grade(0.3)
        specialists.tick()
        folder = specialists.DIR / sid
        self.assertNotIn("wrong", (folder / "knowledge" / "topic-0.md").read_text())
        self.assertFalse((folder / "knowledge-next").exists())

    def test_lead_request_names_the_specialist_as_lead_and_reviewer(self):
        sid = specialists.create(ROLE)
        with self.assertRaisesRegex(team.TeamError, "isn't ready"):
            specialists.lead_request(sid, "Build the pipeline")
        self.build_to_exam(sid)
        self.grade(0.9)
        specialists.tick()
        text = specialists.lead_request(sid, "Build the pipeline")
        self.assertTrue(text.startswith("Build the pipeline"))
        self.assertIn(f"`{sid}` among the reviewers", text)

    def test_remove_deletes_everything(self):
        sid = specialists.create(ROLE)
        with self.assertRaisesRegex(team.TeamError, "still being built"):
            specialists.remove(sid)
        self.build_to_exam(sid)
        self.grade(0.9)
        specialists.tick()
        specialists.ask(sid, "Hi")
        specialists.remove(sid)
        self.assertFalse((specialists.DIR / sid).exists())
        self.assertNotIn(sid, agentdefs.catalog())
        self.assertNotIn(specialists.tutor_id(sid), config_store.load()["agents"])
        self.assertNotIn(sid, swarm.disabled(config_store.load()))


if __name__ == "__main__":
    unittest.main()
