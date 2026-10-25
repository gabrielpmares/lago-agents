"""Installing Lago's voices from the app (voice_setup.py): the speech venv and Kokoro's files,
with pip and the downloads stood in for, in the test's own folder."""

import contextlib
import io
import json
import shutil
import subprocess
import time
import unittest
from unittest import mock

import support

import api
import voice
import voice_setup


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __exit__(self, *exc):
        self.close()


class VoiceSetupTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        root = support.TMP / "share"
        shutil.rmtree(root, ignore_errors=True)
        for name, value in (("TTS_PYTHON", root / "tts" / "bin" / "python"),
                            ("KOKORO_MODEL", root / "kokoro" / "kokoro-v1.0.onnx")):
            patch = mock.patch.object(voice, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        voice_setup.STATUS_FILE.unlink(missing_ok=True)
        self.runs, self.fetched = [], []

    def fake_run(self, args, check=False):
        self.runs.append(args)
        if args[1:3] == ["-m", "venv"]:
            voice.TTS_PYTHON.parent.mkdir(parents=True, exist_ok=True)
            voice.TTS_PYTHON.touch()
        return subprocess.CompletedProcess(args, 0)

    def fake_urlopen(self, url, timeout=None):
        self.fetched.append(url)
        return FakeResponse(b"model bytes")

    def install(self):
        with mock.patch.object(voice_setup.subprocess, "run", self.fake_run), \
                mock.patch.object(voice_setup.urllib.request, "urlopen", self.fake_urlopen):
            voice_setup.main()

    def test_it_makes_the_venv_installs_the_engines_and_downloads_the_voices(self):
        self.assertFalse(voice_setup.status()["installed"])
        self.install()
        self.assertEqual(self.runs[0][1:], ["-m", "venv", str(voice.TTS_PYTHON.parent.parent)])
        self.assertEqual(self.runs[1][:4], [str(voice.TTS_PYTHON), "-m", "pip", "install"])
        self.assertIn("kokoro-onnx", self.runs[1])
        self.assertEqual([u.rsplit("/", 1)[1] for u in self.fetched], voice_setup.MODEL_FILES)
        self.assertEqual(voice.KOKORO_MODEL.read_bytes(), b"model bytes")
        self.assertEqual(list(voice.KOKORO_MODEL.parent.glob("*.part")), [])
        status = voice_setup.status()
        self.assertEqual((status["installed"], status["running"], status["error"]), (True, False, ""))
        self.assertIn("kokoro:bm_george", [v["id"] for v in voice.voices()])  # the personas' voices are there now
        self.fetched.clear()
        self.install()  # again: nothing downloaded twice
        self.assertEqual(self.fetched, [])

    def test_a_venv_missing_a_package_added_since_installs_again(self):
        self.install()
        voice_setup._packages_file().write_text(json.dumps(["kokoro-onnx", "edge-tts", "faster-whisper"]))
        self.assertFalse(voice_setup.status()["installed"])  # the app offers Install voices again
        self.runs.clear()
        self.install()
        self.assertEqual([r[2] for r in self.runs], ["pip"])  # no new venv, no downloads; pip adds soundfile
        self.assertTrue(voice_setup.status()["installed"])

    def test_a_failed_download_says_so_and_leaves_no_half_file(self):
        def broken(url, timeout=None):
            raise OSError("network is unreachable")
        with mock.patch.object(voice_setup.subprocess, "run", self.fake_run), \
                mock.patch.object(voice_setup.urllib.request, "urlopen", broken), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            voice_setup.main()
        status = voice_setup.status()
        self.assertFalse(status["installed"])
        self.assertIn("network is unreachable", status["error"])
        self.assertFalse(voice.KOKORO_MODEL.exists())

    def test_the_app_starts_it_once_and_a_dead_install_isnt_running_forever(self):
        started = []
        with mock.patch.object(voice_setup.subprocess, "Popen", lambda args, **k: started.append(args)):
            reply = api.Api(None).install_voices()
            api.Api(None).install_voices()  # a second click while it runs
        self.assertEqual(len(started), 1)
        self.assertTrue(reply["running"])
        self.assertTrue(started[0][1].endswith("voice_setup.py"))
        state = json.loads(voice_setup.STATUS_FILE.read_text())
        voice_setup.STATUS_FILE.write_text(json.dumps({**state, "at": time.time() - voice_setup.STALE_SECONDS - 1}))
        self.assertFalse(voice_setup.status()["running"])


if __name__ == "__main__":
    unittest.main()
