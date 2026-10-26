import unittest
from unittest import mock

import support

import calls
import config_store
import personas
import voice


def item(key="agent:w1-dev:needs_input:100", kind="input", at=1000.0):
    return {"key": key, "kind": kind, "title": "Builder is asking something", "context": "Webstore",
            "text": "Which payment provider?", "href": "#work/1", "at": at, "urgent": True}


class CallTests(unittest.TestCase):
    """Phone calls with Lago: who answers, and when he may ring you."""

    def setUp(self):
        support.reset()
        calls.CALL_PATH.unlink(missing_ok=True)
        calls.LOG_PATH.unlink(missing_ok=True)
        calls.SPOKE_PATH.unlink(missing_ok=True)
        told = mock.patch("notify.tell_orchestrator")
        self.told = told.start()
        self.addCleanup(told.stop)

    def config(self, **changes):
        config = config_store.without_swarm(config_store.load())
        config.update(changes)
        config_store.save(config)

    # --- who you talk to

    def test_asking_for_another_persona(self):
        config = config_store.load()
        for text, wanted in [("Let me talk to Chip", "chip"), ("can I speak with atlas please", "atlas"),
                             ("put Lago on", "lago"), ("switch me to Chip", "chip"),
                             ("Hey Atlas, how's the deploy?", "atlas"), ("is Chip there?", "chip")]:
            self.assertEqual(calls.switch_to(text, config), wanted, text)
        for text in ("chip in with the tests", "the atlas page is broken", "what's up"):
            self.assertIsNone(calls.switch_to(text, config), text)

    def test_saying_let_me_talk_to_chip_hands_the_call_over(self):
        call = calls.start()
        calls.say(call["id"], "Let me talk to Chip")
        config = config_store.load()
        self.assertEqual(personas.current(config), "chip")
        self.assertEqual(voice.settings(config)["voice"], "kokoro:am_michael")  # his own Kokoro voice
        self.assertEqual(calls.current()["persona"]["name"], "Chip")
        message = self.told.call_args[0][0]
        self.assertIn("you're Chip", message)
        self.assertIn("(on the phone) Let me talk to Chip", message)

    def test_what_you_say_reaches_lago_marked_as_the_phone(self):
        call = calls.start()
        self.assertIn("calling you from their phone", self.told.call_args[0][0])
        calls.say(call["id"], "  how is   the webstore doing ")
        self.told.assert_called_with("(on the phone) how is the webstore doing")
        self.assertEqual(personas.current(config_store.load()), "lago")

    def test_nothing_reaches_lago_after_hanging_up(self):
        call = calls.start()
        self.assertTrue(calls.end(call["id"]))
        self.assertIn("call ended", self.told.call_args[0][0])
        self.told.reset_mock()
        self.assertIsNone(calls.say(call["id"], "hello?"))
        self.told.assert_not_called()

    # --- his voice during a call

    def test_on_a_call_he_always_answers_aloud_on_the_phone(self):
        self.config(voice={**voice.settings(config_store.load()), "talk": False, "quiet_from": "00:00", "quiet_to": "23:59"})
        with mock.patch("voice.say", return_value=True) as say:
            self.assertEqual(voice.speak_reply(config_store.load(), "Done.\n🔊 It's done."), "")
            calls.start()
            self.assertEqual(voice.speak_reply(config_store.load(), "Done.\n🔊 It's done."), "It's done.")
        say.assert_called_once()
        self.assertTrue(voice.to_phone())

    def test_a_call_the_phone_stopped_checking_on_is_over(self):
        call = calls.start()
        self.assertTrue(calls.active(call["at"] + 30))
        self.assertFalse(calls.active(call["at"] + calls.STALE_SECONDS + 1))

    def test_when_claude_is_overloaded_he_says_so_once(self):
        call = calls.start()
        screen = lambda: "✻ 529 Overloaded · Retrying in 22s · attempt 7/7"  # noqa: E731
        asked = calls.current()["asked"]
        self.assertIsNone(calls.stalled(calls.current(), screen, asked + 5))  # he may just be thinking
        note, line = calls.stalled(calls.current(), screen, asked + calls.STALL_SECONDS + 1)
        self.assertIn("overloaded", note)
        self.assertIn("can't answer right now", line)
        self.assertEqual(calls.stalled(calls.current(), screen, asked + 40), (note, None))  # said once
        calls.say(call["id"], "are you there?")
        later = calls.current()["asked"] + calls.STALL_SECONDS + 1
        self.assertIsNotNone(calls.stalled(calls.current(), screen, later)[1])  # once per thing you say

    def test_an_error_from_before_your_message_is_over(self):
        calls.start()
        screen = lambda: "● API Error: 529 Overloaded.\n❯ (on the phone) how is it going\n✻ Thinking…\n❯ "  # noqa: E731
        asked = calls.current()["asked"]
        self.assertIsNone(calls.stalled(calls.current(), screen, asked + 30))

    def test_quiet_after_he_answered_is_fine(self):
        calls.start()
        calls.spoke()
        asked = calls.current()["asked"]
        self.assertIsNone(calls.stalled(calls.current(), lambda: "Overloaded", asked + 100))

    def test_a_long_think_gets_a_still_working(self):
        calls.start()
        asked = calls.current()["asked"]
        self.assertIsNone(calls.stalled(calls.current(), lambda: "✻ Reading files…", asked + 30))
        self.assertEqual(calls.stalled(calls.current(), lambda: "", asked + calls.SLOW_SECONDS + 1)[0], "Still working on it")

    # --- when he rings you

    def test_rings_once_something_has_waited_long_enough(self):
        now = 1000.0 + 10 * 60
        self.assertIsNone(calls.due([item(at=now - 60)], config_store.load(), now))  # a minute: you may still get to it
        self.assertEqual(calls.due([item(at=now - 600)], config_store.load(), now)["kind"], "input")

    def test_rings_only_for_the_kinds_you_chose(self):
        now = 5000.0
        self.assertIsNone(calls.due([item(kind="done", at=0)], config_store.load(), now))
        self.config(calls={"ring": True, "wait_minutes": 10, "kinds": ["finished"]})
        self.assertIsNotNone(calls.due([item(kind="done", at=0)], config_store.load(), now))
        self.assertIsNone(calls.due([item(kind="input", at=0)], config_store.load(), now))

    def test_never_when_switched_off_or_in_quiet_hours(self):
        now = 5000.0
        self.config(calls={"ring": False})
        self.assertIsNone(calls.due([item(at=0)], config_store.load(), now))
        self.config(calls={"ring": True}, voice={"quiet_from": "00:00", "quiet_to": "23:59"})
        self.assertIsNone(calls.due([item(at=0)], config_store.load(), now))

    def test_once_per_item_and_a_few_an_hour(self):
        now = 5000.0
        call = calls.consider([item(at=0)], now)
        self.assertEqual(call["state"], "ringing")
        calls.end(call["id"], "declined", now)
        later = now + calls.AFTER_CALL_SECONDS + 1
        self.assertIsNone(calls.due([item(at=0)], config_store.load(), later))  # that one rang already
        for n in range(calls.MAX_PER_HOUR - 1):
            calls.end(calls.consider([item(key=f"k{n}", at=0)], later)["id"], "declined", later)
            later += calls.AFTER_CALL_SECONDS + 1
        self.assertIsNone(calls.consider([item(key="one-more", at=0)], later))

    def test_stops_ringing_when_it_was_dealt_with(self):
        now = 5000.0
        call = calls.consider([item(at=0)], now)
        self.assertIsNone(calls.consider([], now + 5))
        self.assertIsNone(calls.current(now + 5))
        self.assertIsNone(calls.answer(call["id"]))

    def test_answering_tells_him_why_he_rang(self):
        call = calls.consider([item(at=0)], 5000.0)
        with mock.patch("time.time", return_value=5010.0):
            self.assertEqual(calls.answer(call["id"])["state"], "active")
        message = self.told.call_args[0][0]
        self.assertIn("you rang", message)
        self.assertIn("Builder is asking something", message)

    def test_unanswered_it_rings_out(self):
        call = calls.consider([item(at=0)], 5000.0)
        self.assertIsNone(calls.current(5000.0 + calls.RING_SECONDS + 1))
        self.assertEqual(call["state"], "ringing")


if __name__ == "__main__":
    unittest.main()
