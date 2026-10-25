"""Talking to Lago (mic.py) when the microphone gives nothing: macOS records only silence
for an app it was denied, and that mustn't go to Whisper. Recordings are written by the
test; nothing records."""

import array
import random
import subprocess
import unittest
import wave
from unittest import mock

import support

import mic


def write_wav(samples):
    mic.RECORDING.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(mic.RECORDING), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(array.array("h", samples).tobytes())


class MicTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        mic.RECORDING.unlink(missing_ok=True)
        self.runs = []
        stop = mock.patch.object(mic, "_stop", lambda: 3.0)
        run = mock.patch.object(mic.subprocess, "run", self.fake_run)
        for patch in (stop, run):
            patch.start()
            self.addCleanup(patch.stop)

    def fake_run(self, args, **kwargs):
        self.runs.append(args)
        return subprocess.CompletedProcess(args, 0, "hello Lago\n", "")

    def test_a_blocked_microphone_says_how_to_allow_it_and_whisper_never_hears_the_silence(self):
        write_wav([0] * 48000)
        with mock.patch.object(mic.sys, "platform", "darwin"), self.assertRaises(mic.MicError) as raised:
            mic.stop_and_transcribe()
        self.assertIn("Privacy & Security, Microphone", str(raised.exception))
        self.assertIn("typing works without it", str(raised.exception))
        self.assertEqual(self.runs, [])
        self.assertFalse(mic.RECORDING.exists())

    def test_a_recorder_that_never_wrote_a_file_says_so_too(self):
        with self.assertRaises(mic.MicError) as raised:
            mic.stop_and_transcribe()
        self.assertIn("can't hear the microphone", str(raised.exception))
        self.assertEqual(self.runs, [])

    def test_a_quiet_room_is_still_transcribed(self):
        noise = random.Random(1)
        write_wav([noise.randint(-30, 30) for _ in range(48000)])
        self.assertEqual(mic.stop_and_transcribe(), "hello Lago")
        self.assertEqual(len(self.runs), 1)


if __name__ == "__main__":
    unittest.main()
