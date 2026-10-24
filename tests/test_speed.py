"""What keeps Lago quick: his reply spoken a piece at a time, runs' tasks in one query."""

import unittest
from unittest import mock

import support

import config_store
import hub
import speak


class SpokenPiecesTests(unittest.TestCase):
    def test_a_short_line_is_one_piece(self):
        self.assertEqual(speak.pieces("Both tasks are running."), ["Both tasks are running."])

    def test_a_long_reply_splits_at_sentences_and_says_everything(self):
        text = ("Morning, Sam. Two sessions finished overnight: the dashboard and the routines "
                "page. One is waiting on you about the phone app. What next? Ok.")
        pieces = speak.pieces(text)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(" ".join(pieces), text)
        self.assertGreaterEqual(len(pieces[0]), speak.FIRST_MIN)
        self.assertTrue(all(len(p) >= speak.PIECE_MIN for p in pieces[1:-1]))
        self.assertTrue(pieces[-1].endswith("What next? Ok."))  # a short tail isn't alone

    def test_numbers_and_unpunctuated_text_stay_whole(self):
        self.assertEqual(speak.pieces("Version 3.5 is out"), ["Version 3.5 is out"])


class LatestTasksTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_newest_task_of_each_agent(self):
        hub.create_task("run-a", "first", "")
        newest_a = hub.create_task("run-a", "second", "")
        newest_b = hub.create_task("run-b", "only", "")
        hub.create_task("someone-else", "not asked", "")
        latest = hub.latest_tasks(["run-a", "run-b", "run-c"])
        self.assertEqual({k: v["id"] for k, v in latest.items()}, {"run-a": newest_a, "run-b": newest_b})
        self.assertEqual(hub.latest_tasks([]), {})


class WelcomeTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        (config_store.STATE_DIR / "last-welcome").unlink(missing_ok=True)

    def test_greeting_is_said_before_the_last_conversation_is_written_down(self):
        import api
        order = []
        app = api.Api.__new__(api.Api)
        app.welcomed = False
        with mock.patch.object(api.onboarding, "needed", return_value=False), \
                mock.patch.object(api.voice, "say", side_effect=lambda *a, **k: order.append("greet")), \
                mock.patch.object(api.voice, "quiet_now", return_value=False), \
                mock.patch.object(api, "_write_down_last_conversation",
                                  side_effect=lambda: order.append("write down")), \
                mock.patch.object(api, "_skip_permissions", return_value=False), \
                mock.patch.object(api.notify, "tell_orchestrator"), \
                mock.patch("lago_mcp.tool_work_sessions", return_value=""), \
                mock.patch.object(api.open_tasks, "brief", return_value=""):
            self.assertEqual(app.welcome(), {"ok": True})
        self.assertEqual(order, ["greet", "write down"])
        self.assertTrue(app.welcomed)


class MouthTests(unittest.TestCase):
    def test_playing_only_while_the_marked_process_runs(self):
        import os
        import voice
        voice.PLAYING_FILE.unlink(missing_ok=True)
        self.assertFalse(voice.playing())
        voice.PLAYING_FILE.write_text(str(os.getpid()))
        self.assertTrue(voice.playing())
        voice.PLAYING_FILE.write_text("999999999")  # killed by hush() before it could clean up
        self.assertFalse(voice.playing())
        voice.PLAYING_FILE.unlink()


if __name__ == "__main__":
    unittest.main()
