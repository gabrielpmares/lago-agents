import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

import config_store
import hub
import lago_hook

FLAG = ("API Error: Opus 5.5's safeguards flagged this session (https://www.anthropic.com/legal/aup). "
        "Double press esc to edit your last message, or try a different model with /model.")


def log(*texts):
    path = Path(tempfile.mkdtemp()) / "session.jsonl"
    path.write_text("".join(json.dumps(
        {"type": "assistant", "isApiErrorMessage": True,
         "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n"
        for text in texts))
    return str(path)


class FlaggedConversationTests(unittest.TestCase):
    """A flagged conversation fails every turn: Lago moves to a fresh one, but only once."""

    def setUp(self):
        support.reset()
        config_store.ORCHESTRATOR_FLAG_RESTART.unlink(missing_ok=True)
        self.rotate = mock.patch("memory.rotate_later").start()
        self.addCleanup(mock.patch.stopall)
        self.event = {"hook_event_name": "StopFailure", "error": "invalid_request",
                      "transcript_path": log(FLAG)}

    def test_reads_the_reason_from_the_log(self):
        self.assertTrue(lago_hook.flagged(self.event["transcript_path"]))
        self.assertFalse(lago_hook.flagged(log("API Error: 500 Internal server error")))
        self.assertFalse(lago_hook.flagged(None))

    def test_lago_starts_a_fresh_conversation(self):
        lago_hook.on_event(config_store.ORCHESTRATOR_ID, self.event)
        self.rotate.assert_called_once_with(clear=True)

    def test_flagged_again_soon_waits_for_you(self):
        lago_hook.on_event(config_store.ORCHESTRATOR_ID, self.event)
        lago_hook.on_event(config_store.ORCHESTRATOR_ID, self.event)
        self.rotate.assert_called_once()
        self.assertIn("flagged too", hub.events()[0]["text"])

    def test_other_agents_are_left_alone(self):
        config = config_store.load()
        config["agents"]["r1-builder"] = {"name": "Builder · run 1", "direct": True}
        config_store.save(config)
        with mock.patch.object(lago_hook.transcripts, "find", return_value=None):
            lago_hook.on_event("r1-builder", self.event)
        self.rotate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
