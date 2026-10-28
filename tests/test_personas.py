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

    def test_the_women_answer_to_their_names_in_their_own_voices(self):
        import calls
        config = config_store.load()
        self.assertEqual(calls.switch_to("let me talk to Ada", config), "ada")
        self.api.set_persona("marina")
        config = config_store.load()
        self.assertEqual((voice.settings(config)["voice"], voice.settings(config)["effect"]),
                         ("kokoro:bf_isabella", "small-creature"))
        self.assertEqual(voice.settings(config)["wake_phrase"], "Hey Marina")
        self.assertIn("You're a woman", team._orchestrator_prompt(config, None))
        self.assertIn("You're a man", personas.switch_note("chip"))

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
        self.assertEqual(phrases, [("Hey Lago", "lago"), ("Hey Chip", "chip"), ("Hey Atlas", "atlas"),
                                   ("Hey Marina", "marina"), ("Hey Ada", "ada"), ("Hey Iris", "iris")])
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


class LanguageTests(unittest.TestCase):
    """"Talk to me in Portuguese": every persona keeps his effect and speaks it (languages.py)."""

    def setUp(self):
        support.reset()
        with mock.patch("background.start_loops"):
            self.api = api.Api(None)

    def speech_command(self, **settings):
        """What say() runs, with the offline voices installed."""
        config = config_store.without_swarm(config_store.load())
        with mock.patch("voice.voices", return_value=[{"id": "kokoro:am_santa", "offline": True}]), \
                mock.patch.object(voice.Path, "exists", return_value=True), \
                mock.patch("voice.to_phone", return_value=False), \
                mock.patch("features.on", return_value=True), \
                mock.patch("voice._remember"), mock.patch("subprocess.Popen") as popen:
            self.assertTrue(support.REAL_SAY("Olá", {**voice.settings(config), **settings}))
        return popen.call_args.args[0][-1]

    def test_lago_asks_for_a_language(self):
        import lago_mcp
        reply = lago_mcp.tool_speak_language("Portuguese")
        self.assertIn("now speaks Portuguese (Portugal)", reply)
        self.assertEqual(voice.settings(config_store.load())["language"], "pt-pt")
        self.assertIn("## Language", personas.prompt_section(config_store.load()))
        lago_mcp.tool_speak_language("Brazilian Portuguese")
        self.assertEqual(voice.settings(config_store.load())["language"], "pt-br")
        lago_mcp.tool_speak_language("English")
        self.assertEqual(voice.settings(config_store.load())["language"], "")
        self.assertNotIn("## Language", personas.prompt_section(config_store.load()))
        with self.assertRaises(team.TeamError):
            lago_mcp.tool_speak_language("Klingon")

    def test_each_persona_keeps_his_effect_in_a_voice_for_the_language(self):
        command = self.speech_command(voice="kokoro:am_santa", effect="small-creature", language="pt-pt")
        self.assertIn("--voice kokoro:pm_santa@pt", command)
        self.assertIn("--effect small-creature", command)
        self.assertNotIn("--fallback", command)
        self.assertIn("--voice kokoro:am_santa ", self.speech_command(voice="kokoro:am_santa", language=""))

    def mac_command(self, **settings):
        """What say() runs on a Mac without Kokoro: only macOS's own voices."""
        config = config_store.without_swarm(config_store.load())
        listing = "\n".join(["Samantha            en_US    # Hello", "Daniel              en_GB    # Hello",
                             "Joana               pt_PT    # Olá", "Luciana             pt_BR    # Olá",
                             "Eddy (Portuguese (Brazil)) pt_BR    # Olá"])
        mac_voices = [{"id": vid, "offline": True} for vid in voice.SAY_VOICES]
        with mock.patch("voice.voices", return_value=mac_voices), \
                mock.patch.object(voice.Path, "exists", return_value=False), \
                mock.patch("voice.to_phone", return_value=False), \
                mock.patch("features.on", return_value=True), \
                mock.patch("voice.subprocess.run", return_value=mock.Mock(stdout=listing)), \
                mock.patch("voice._remember"), mock.patch("subprocess.Popen") as popen:
            self.assertTrue(support.REAL_SAY("Olá", {**voice.settings(config), **settings}))
        return popen.call_args.args[0][-1]

    def test_on_a_mac_without_kokoro_each_keeps_a_voice_of_their_sex_and_language(self):
        self.assertIn("say -v Daniel", self.mac_command(voice="kokoro:am_michael", language=""))
        self.assertIn("say -v Samantha", self.mac_command(voice="kokoro:bf_isabella", language=""))
        self.assertIn("say -v Joana", self.mac_command(voice="kokoro:bf_isabella", language="pt-pt"))
        self.assertIn("say -v 'Eddy (Portuguese (Brazil))'", self.mac_command(voice="kokoro:am_michael", language="pt-br"))
        self.assertIn("say -v Luciana", self.mac_command(voice="kokoro:bf_isabella", language="pt-br"))

    def test_chip_and_atlas_dont_take_lagos_voice(self):
        import languages
        self.assertEqual(languages.voice_for("kokoro:am_santa", "es"), "kokoro:em_santa@es")
        self.assertEqual(languages.voice_for("kokoro:am_michael", "pt-pt"), "kokoro:pm_alex@pt")
        self.assertEqual(languages.voice_for("kokoro:bf_emma", "pt-br"), "kokoro:pf_dora@pt-br")
        self.assertEqual(languages.voice_for("kokoro:am_michael", "fr"), "kokoro:ff_siwis@fr-fr")  # the only one

    def test_the_apps_own_phrases_are_translated(self):
        config = config_store.without_swarm(config_store.load())
        config["user_name"] = "Ana"
        config["voice"] = {"language": "pt-pt"}
        self.assertEqual(voice.phrase(config, "question", "Loja"), "Ana, Loja tem uma pergunta para ti.")
        self.assertTrue(voice.greeting(config).endswith("Ana. Em que vamos trabalhar hoje?"))
        config_store.save(config)
        with mock.patch("voice.say", return_value=True) as say:
            self.api.preview_persona("chip")
        self.assertIn("eu sou o Chip", say.call_args.args[0])
        with mock.patch("voice.say", return_value=True) as say:
            self.api.preview_persona("iris")
        self.assertIn("eu sou a Iris", say.call_args.args[0])

    def test_changing_it_in_settings_tells_him(self):
        with mock.patch("runner.is_running", return_value=True), \
                mock.patch("notify.tell_orchestrator") as tell:
            self.api.save_voice({**voice.settings(config_store.load()), "language": "es"})
        self.assertIn("now speaks Spanish", tell.call_args.args[0])
        with self.assertRaises(api.ApiError):
            self.api.save_voice({**voice.settings(config_store.load()), "language": "tlh"})


class SwitchingByVoiceTests(unittest.TestCase):
    """"I want to talk to Marina in Portuguese", on a call, by the wake phrase or typed."""

    def setUp(self):
        support.reset()
        config = config_store.without_swarm(config_store.load())
        config.setdefault("features", {})["personas"] = True
        config_store.save(config)

    def test_portuguese_phrases_switch_too(self):
        import calls
        config = config_store.load()
        for text, wanted in (("quero falar com a Marina", "marina"), ("passa-me o Chip", "chip"),
                             ("Olá Ada, tudo bem?", "ada"), ("I want to talk to Iris in Portuguese", "iris"),
                             ("quiero hablar con Atlas", "atlas")):
            self.assertEqual(calls.switch_to(text, config), wanted, text)
        self.assertIsNone(calls.switch_to("quero falar contigo em português", config))

    def test_lago_switches_persona_himself(self):
        import lago_mcp
        self.assertIn("switched you to Ada", lago_mcp.tool_use_persona("ada"))
        self.assertEqual(personas.current(config_store.load()), "ada")
        with self.assertRaises(team.TeamError):
            lago_mcp.tool_use_persona("Clippy")

    def test_the_phone_listens_in_his_language_and_shows_who_he_is(self):
        import calls, lago_mcp
        call = {"id": "c1", "state": "active", "persona": calls._persona(config_store.load())}
        self.assertEqual(calls.public(call)["language"], "")
        lago_mcp.tool_use_persona("marina")
        lago_mcp.tool_speak_language("Portuguese")
        shown = calls.public(call)
        self.assertEqual((shown["language"], shown["persona"]["id"]), ("pt-PT", "marina"))

    def test_chip_speaks_portuguese_in_his_own_voice_when_it_is_installed(self):
        settings = {**voice.settings(config_store.load()), "voice": "kokoro:am_michael", "language": "pt-pt",
                    "persona": "chip", "effect": "retro-computer"}
        def run(available):
            with mock.patch("voice.voices", return_value=[{"id": v, "offline": True} for v in available]), \
                    mock.patch.object(voice.Path, "exists", return_value=True), \
                    mock.patch("voice.to_phone", return_value=False), mock.patch("features.on", return_value=True), \
                    mock.patch("voice._remember"), mock.patch("subprocess.Popen") as popen:
                self.assertTrue(support.REAL_SAY("Olá", settings))
            return popen.call_args.args[0][-1]
        command = run(["kokoro:am_michael", "piper:pt_BR-cadu-medium"])
        self.assertIn("--voice piper:pt_BR-cadu-medium --rate", command)
        self.assertIn("--fallback kokoro:pm_alex@pt", command)
        self.assertIn("--voice kokoro:pm_alex@pt", run(["kokoro:am_michael"]))  # Piper not installed
