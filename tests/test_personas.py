import unittest
from unittest import mock

import support

import api
import config_store
import personas
import team
import voice


class PersonaTests(unittest.TestCase):
    """Lago, Chip or Atlas: the same assistant with another name, face and voice."""

    def setUp(self):
        support.reset()
        with mock.patch("background.start_loops"):
            self.api = api.Api(None)

    def test_lago_by_default(self):
        config = config_store.load()
        self.assertEqual(personas.current(config), "lago")
        self.assertEqual(personas.name(config), "Lago")

    def test_switching_brings_his_voice_and_effect(self):
        self.api.set_persona("chip")
        config = config_store.load()
        self.assertEqual(personas.current(config), "chip")
        settings = voice.settings(config)
        self.assertEqual(settings["voice"], "kokoro:am_michael")
        self.assertEqual(settings["effect"], "retro-computer")

    def test_the_wake_phrase_follows_the_name(self):
        self.api.set_persona("atlas")
        self.assertEqual(voice.settings(config_store.load())["wake_phrase"], "Hey Atlas")

    def test_a_wake_phrase_of_your_own_stays(self):
        config = config_store.without_swarm(config_store.load())
        config["voice"] = {"wake_phrase": "Computer"}
        config_store.save(config)
        self.api.set_persona("chip")
        self.assertEqual(voice.settings(config_store.load())["wake_phrase"], "Computer")

    def test_a_running_lago_is_told(self):
        with mock.patch("runner.is_running", return_value=True), \
                mock.patch("notify.tell_orchestrator") as tell:
            self.api.set_persona("atlas")
        self.assertIn("switched you to Atlas", tell.call_args.args[0])

    def test_his_prompt_uses_the_persona(self):
        self.api.set_persona("chip")
        config = config_store.load()
        prompt = team._orchestrator_prompt(config, None)
        self.assertIn("You are Chip", prompt)
        self.assertIn("same notes, memory and work", prompt)

    def test_saving_teams_keeps_the_persona(self):
        self.api.set_persona("atlas")
        self.api.save_config(config_store.load())
        self.assertEqual(personas.current(config_store.load()), "atlas")

    def test_trying_one_out_speaks_in_his_voice(self):
        with mock.patch("voice.say", return_value=True) as say:
            self.api.preview_persona("atlas")
        text, settings = say.call_args.args[0], say.call_args.args[1]
        self.assertIn("I am Atlas", text)
        self.assertEqual(settings["voice"], "kokoro:bm_lewis")
        self.assertFalse(settings["chime"])

    def test_rejects_unknown_personas(self):
        with self.assertRaises(api.ApiError):
            self.api.set_persona("clippy")


if __name__ == "__main__":
    unittest.main()


class CallingByNameTests(unittest.TestCase):
    """"Hey Chip, ..." wakes the listener as Chip, and Chip answers (listen.py)."""

    def setUp(self):
        support.reset()
        with mock.patch("background.start_loops"):
            self.api = api.Api(None)

    def test_every_persona_answers_to_his_name(self):
        phrases = personas.wake_phrases(config_store.load())
        self.assertEqual(phrases, [("Hey Lago", "lago"), ("Hey Chip", "chip"), ("Hey Atlas", "atlas")])
        config = config_store.without_swarm(config_store.load())
        config["voice"] = {"wake_phrase": "Computer", "wake_personas": False}
        self.assertEqual(personas.wake_phrases(config), [("Computer", "lago")])

    def test_the_phrase_said_picks_the_persona(self):
        import listen
        phrases = personas.wake_phrases(config_store.load())
        self.assertEqual(listen.match("Hey, Chip. What's running?", phrases), ("Hey Chip", "chip"))
        self.assertEqual(listen.match("hey atlas check the servers", phrases), ("Hey Atlas", "atlas"))
        self.assertIsNone(listen.match("I said hello to my friends", phrases))
        self.assertEqual(listen.after_phrase("Hey Atlas, check the servers.", "Hey Atlas"), "check the servers.")

    def test_calling_another_switches_him_and_he_answers(self):
        import listen
        with mock.patch("notify.tell_orchestrator") as tell:
            listen.send("what's running?", "chip")
        config = config_store.load()
        self.assertEqual(personas.current(config), "chip")
        self.assertEqual(voice.settings(config)["voice"], "kokoro:am_michael")
        text = tell.call_args.args[0]
        self.assertTrue(text.startswith("[Lago] Persona:"))
        self.assertTrue(text.endswith("\n\n(spoken) what's running?"))
        with mock.patch("notify.tell_orchestrator") as tell:
            listen.send("and now?", "chip")  # already Chip: just the message
        self.assertEqual(tell.call_args.args[0], "(spoken) and now?")

    def test_the_chat_shows_who_you_called_and_what_you_said(self):
        events = [{"sub": False, "kind": "prompt", "at": 1,
                   "text": personas.called_note("atlas") + "\n\n(spoken) check the servers"}]
        with mock.patch("transcripts.find", return_value="x"), \
                mock.patch("transcripts.read", return_value={"events": events}):
            rows = api._conversation("s")
        self.assertEqual([r["who"] for r in rows], ["note", "you"])
        self.assertEqual((rows[1]["text"], rows[1]["voice"]), ("check the servers", True))


class ListeningIsOptInTests(unittest.TestCase):
    """Nothing that can hear runs unless you switch it on."""

    def setUp(self):
        support.reset()
        with mock.patch("background.start_loops"):
            self.api = api.Api(None)

    def save(self, **changes):
        return self.api.save_voice({**voice.settings(config_store.load()), **changes})

    def test_switching_on_installs_and_off_removes_it(self):
        import listen
        with mock.patch.object(listen, "set_service") as service, \
                mock.patch.object(listen, "service_running", return_value=False):
            self.save(wake=True)
            service.assert_called_once_with(True)
        with mock.patch.object(listen, "set_service") as service, \
                mock.patch.object(listen, "service_running", return_value=True):
            self.save(wake=False)
            service.assert_called_once_with(False)
            service.reset_mock()
            self.save(volume=80)  # off and staying off: nothing to do
            service.assert_not_called()

    def test_without_the_speech_add_on_it_stays_off(self):
        import listen
        with mock.patch.object(listen, "set_service", side_effect=listen.ListenError("needs the speech add-on")):
            with self.assertRaisesRegex(api.ApiError, "speech add-on"):
                self.save(wake=True)
        self.assertFalse(voice.settings(config_store.load())["wake"])
