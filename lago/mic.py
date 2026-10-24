"""Talking to Lago: record your voice with PipeWire and turn it into text.

The app starts a recording when you press the microphone button (or Ctrl+Space) and stops
it when you press again; transcribe.py turns it into text locally with Whisper. Only one
recording runs at a time, and a forgotten one stops by itself after MAX_SECONDS.
"""

import signal
import subprocess
import threading
import time

import config_store
import desktop
from config_store import APP_DIR, STATE_DIR
from voice import TTS_PYTHON

RECORDING = STATE_DIR / "mic.wav"
TRANSCRIBE = APP_DIR / "transcribe.py"
MAX_SECONDS = 120
TRANSCRIBE_TIMEOUT = 120


class MicError(Exception):
    """A problem worth showing as-is."""


_lock = threading.Lock()
_recorder = None
_started = 0.0


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
    if seconds < 0.4 or not RECORDING.exists():
        RECORDING.unlink(missing_ok=True)
        return ""
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
