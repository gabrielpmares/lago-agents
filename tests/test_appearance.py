import unittest
from unittest import mock

import support

import api
import config_store


class AppearanceTests(unittest.TestCase):
    """The switch under the sidebar picks light, dark, or the desktop's look."""

    def setUp(self):
        support.reset()
        self.window = mock.Mock()
        with mock.patch("background.start_loops"):
            self.api = api.Api(self.window)

    def test_a_choice_overrides_the_desktop(self):
        with mock.patch("subprocess.run") as run:
            self.assertEqual(self.api.set_appearance("light"), {"ok": True, "theme": "light"})
            run.assert_not_called()
        self.assertEqual(config_store.load()["appearance"], "light")
        self.window.set_terminal_theme.assert_called_with("light")

    def test_auto_follows_the_desktop(self):
        desktop = mock.Mock(stdout="'prefer-dark'\n")
        with mock.patch("subprocess.run", return_value=desktop):
            self.assertEqual(self.api.set_appearance("auto")["theme"], "dark")

    def test_saving_teams_keeps_the_choice(self):
        self.api.set_appearance("dark")
        self.api.save_config(config_store.load())
        self.assertEqual(config_store.load()["appearance"], "dark")

    def test_rejects_unknown_values(self):
        with self.assertRaises(api.ApiError):
            self.api.set_appearance("sepia")


if __name__ == "__main__":
    unittest.main()
