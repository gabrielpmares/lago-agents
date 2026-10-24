"""The extras (features.py): off on a fresh install, on for a setup from before them, and off
means nothing of them runs."""

import unittest
from unittest import mock

import support

import api
import budgets
import config_store
import features
import personas
import voice


class FeatureTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        with mock.patch("background.start_loops"):
            self.api = api.Api(None)

    def fresh(self):
        """A first start: no config.json yet."""
        config_store.CONFIG_PATH.unlink()

    def test_a_fresh_install_has_the_basics_only(self):
        self.fresh()
        self.assertEqual(set(features.state(config_store.load()).values()), {False})
        init = self.api.init()
        self.assertFalse(any(init["features"].values()))
        self.assertEqual({f["id"] for f in init["feature_list"]}, set(features.FEATURES))

    def test_a_setup_from_before_features_keeps_everything(self):
        self.assertTrue(all(features.state(config_store.load()).values()))  # BASE_CONFIG has no "features"

    def test_switching_one_keeps_the_others(self):
        self.api.set_feature("phone", False)
        state = features.state(config_store.load())
        self.assertFalse(state["phone"])
        self.assertTrue(state["voice"] and state["routines"])
        with self.assertRaises(api.ApiError):
            self.api.set_feature("teleport", True)

    def test_voice_off_speaks_nothing_and_stops_listening(self):
        import listen
        with mock.patch.object(listen, "set_service") as service, mock.patch.object(voice, "hush"):
            self.api.set_feature("voice", False)
        service.assert_called_once_with(False)
        settings = voice.settings(config_store.load())
        self.assertFalse(settings["talk"] or settings["greet"] or settings["enabled"] or settings["wake"])
        with mock.patch.object(voice, "voices") as engines:
            self.assertFalse(voice.say("hello"))
        engines.assert_not_called()  # stopped before looking for a voice
        with self.assertRaises(api.ApiError):
            self.api.mic_start()

    def test_voice_back_on_restarts_the_listener_if_it_was_on(self):
        import listen
        config = config_store.without_swarm(config_store.load())
        config["voice"] = {"wake": True}
        config["features"] = {"voice": False}
        config_store.save(config)
        with mock.patch.object(listen, "set_service") as service:
            self.api.set_feature("voice", True)
        service.assert_called_once_with(True)

    def test_personas_off_is_just_lago(self):
        self.api.set_persona("chip")
        self.api.set_feature("personas", False)
        config = config_store.load()
        self.assertEqual(personas.current(config), "lago")
        self.assertNotIn("chip", [pid for _, pid in personas.wake_phrases(config)])

    def test_budgets_off_stop_nothing(self):
        self.api.set_feature("budgets", False)
        with mock.patch.object(budgets.hub, "get_spend") as spend:
            self.assertIsNone(budgets.check("dev-qa", "session-1", "/x.jsonl", force=True))
        spend.assert_not_called()
