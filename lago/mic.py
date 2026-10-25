"""Talking to Lago: record your voice with PipeWire and turn it into text.

The app starts a recording when you press the microphone button (or Ctrl+Space) and stops
it when you press again; transcribe.py turns it into text locally with Whisper. Only one
recording runs at a time, and a forgotten one stops by itself after MAX_SECONDS.

Talking is optional: someone who doesn't let Lago use the microphone still types, and his
voices still speak. macOS records only silence for an app it was denied the microphone, so a
recording that is all zeros says how to allow it instead of going to Whisper, which would make
up words for it ("Thank you.") and send them.
"""

import array
import signal
import subprocess
import sys
import threading
import time
import wave

import config_store
import desktop
from config_store import APP_DIR, STATE_DIR
from voice import TTS_PYTHON

RECORDING = STATE_DIR / "mic.wav"
TRANSCRIBE = APP_DIR / "transcribe.py"
MAX_SECONDS = 120
TRANSCRIBE_TIMEOUT = 120
SILENT_PEAK = 4  # a working microphone's own noise is louder than this, even in a quiet room


class MicError(Exception):
    """A problem worth showing as-is."""


_lock = threading.Lock()
_recorder = None
_started = 0.0


def no_microphone():
    """What to say when the microphone gives nothing: blocked, muted or not there."""
    if sys.platform == "darwin":
        return ("Lago can't hear the microphone. To talk to him, allow Lago Agents in System Settings, "
                "Privacy & Security, Microphone; typing works without it.")
    return "Lago can't hear the microphone: check that it's connected and not muted. Typing works without it."


def silent(path):
    """True for a WAV file with no sound at all (or none that can be read)."""
    try:
        with wave.open(str(path)) as recording:
            samples = array.array("h", recording.readframes(recording.getnframes()))
    except (OSError, EOFError, wave.Error):
        return True
    return max(map(abs, samples), default=0) < SILENT_PEAK


def available():
    return bool(desktop.recorder_argv(16000, str(RECORDING))) and TTS_PYTHON.exists()


def recording():
    return _recorder is not None and _recorder.poll() is None


def start():
    global _recorder, _started
    if not available():
        raise MicError("Voice input needs a recorder (pw-record, parecord or arecord; ffmpeg on a Mac) and the speech venv in "
                       "~/.local/share/lago-agents/tts with faster-whisper.")
    with _lock:
        if recording():
            return
        RECORDING.unlink(missing_ok=True)
        _recorder = subprocess.Popen(desktop.recorder_argv(16000, str(RECORDING)),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        _started = time.time()
        recorder = _recorder
    guard = threading.Timer(MAX_SECONDS, lambda: _stop_if(recorder))
    guard.daemon = True
    guard.start()


def _stop_if(recorder):
    with _lock:
        if recorder is _recorder:
            _stop()


def _stop():
    """Stop the recorder (SIGINT lets it finish the WAV file). Returns seconds recorded."""
    global _recorder
    recorder, _recorder = _recorder, None
    if recorder is None:
        return 0.0
    if recorder.poll() is None:
        recorder.send_signal(signal.SIGINT)
        try:
            recorder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            recorder.kill()
    return time.time() - _started


def cancel():
    with _lock:
        _stop()
    RECORDING.unlink(missing_ok=True)


def _hint():
    """Names Whisper should spell right: you, Lago, and your projects."""
    config = config_store.load()
    names = [config.get("user_name") or "", "Lago", "Conductor", "Orchestrator"]
    names += [p.get("name", pid) for pid, p in config["projects"].items()]
    return ", ".join(n for n in names if n)


def stop_and_transcribe():
    """Stop recording and return what you said ("" when nothing was heard)."""
    with _lock:
        seconds = _stop()
    if seconds < 0.4:
        RECORDING.unlink(missing_ok=True)
        return ""
    if not RECORDING.exists() or silent(RECORDING):  # the recorder couldn't start, or only got silence
        RECORDING.unlink(missing_ok=True)
        raise MicError(no_microphone())
    try:
        proc = subprocess.run([str(TTS_PYTHON), str(TRANSCRIBE), "--hint", _hint(), str(RECORDING)],
                              capture_output=True, text=True, timeout=TRANSCRIBE_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise MicError("Transcribing took too long.") from error
    finally:
        RECORDING.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise MicError("Couldn't transcribe: " + (proc.stderr.strip().splitlines() or ["unknown error"])[-1])
    return proc.stdout.strip()
