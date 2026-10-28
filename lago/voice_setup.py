#!/usr/bin/env python3
"""Installing Lago's voices from the app: the speech venv (voice.TTS_PYTHON, with kokoro-onnx,
piper-tts, edge-tts and faster-whisper), Kokoro's model files (about 350 MB) and Piper's voices
(about 60 MB each), so his voices and the personas' sound the same on every computer, in every
language, and talking to him works.

start() runs this file detached; it writes how far it got to STATUS_FILE, which status() reads
for the app. Files are downloaded to a .part name and renamed when complete, so an interrupted
install starts over cleanly.
"""

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import voice
from config_store import STATE_DIR

STATUS_FILE = STATE_DIR / "voice-setup.json"
LOG_FILE = STATE_DIR / "voice-setup.log"
PACKAGES = ["kokoro-onnx", "soundfile", "edge-tts", "faster-whisper", "piper-tts"]
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_FILES = ["kokoro-v1.0.onnx", "voices-v1.0.bin"]
# Each Piper voice is a model and its settings, from Rhasspy's collection.
PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
PIPER_FILES = {"pt_BR-cadu-medium": "pt/pt_BR/cadu/medium/"}
STALE_SECONDS = 30 * 60  # a "running" status this old belongs to an install that died


def _packages_file():
    """The PACKAGES the venv's last install had, so a venv from before one was added installs again."""
    return voice.TTS_PYTHON.parent.parent / "lago-packages.json"


def _has_packages():
    try:
        return set(PACKAGES) <= set(json.loads(_packages_file().read_text()))
    except (OSError, ValueError, TypeError):
        return False


def installed():
    return voice.TTS_PYTHON.exists() and _has_packages() and voice.KOKORO_MODEL.exists() \
        and (voice.KOKORO_MODEL.parent / "voices-v1.0.bin").exists() \
        and all((voice.PIPER_DIR / f"{name}.onnx").exists() for name in PIPER_FILES)


def status():
    """{"installed", "running", "step", "error", "effects", "ffmpeg_install"} for the app's voice settings."""
    try:
        state = json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    running = state.get("state") == "running" and time.time() - state.get("at", 0) < STALE_SECONDS
    return {"installed": installed(), "running": running, "step": state.get("step", "") if running else "",
            "error": state.get("error", "") if state.get("state") == "failed" else "",
            "effects": bool(shutil.which("ffmpeg")),  # the voice effects are ffmpeg filters
            "ffmpeg_install": "brew install ffmpeg" if sys.platform == "darwin" else "install ffmpeg with your package manager"}


def start():
    """Install in the background; the app polls status()."""
    if status()["running"]:
        return
    _write("running", "Starting")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "w") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def _write(state, step="", error=""):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"state": state, "step": step, "error": error, "at": time.time()}))
    tmp.replace(STATUS_FILE)


def _venv():
    venv = voice.TTS_PYTHON.parent.parent
    if not voice.TTS_PYTHON.exists():
        _write("running", "Making the speech environment")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    _write("running", "Installing the speech engines (a few minutes)")
    subprocess.run([str(voice.TTS_PYTHON), "-m", "pip", "install", "--quiet", "--upgrade", *PACKAGES], check=True)
    _packages_file().write_text(json.dumps(PACKAGES))


def _download(name, folder, url=MODEL_URL):
    target = folder / name
    if target.exists():
        return
    part = target.with_name(name + ".part")
    with urllib.request.urlopen(url + name, timeout=60) as response, open(part, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                _write("running", f"Downloading the voices ({done * 100 // total}%)")
    part.replace(target)


def main():
    try:
        _venv()
        folder = voice.KOKORO_MODEL.parent
        folder.mkdir(parents=True, exist_ok=True)
        for name in MODEL_FILES:
            _download(name, folder)
        voice.PIPER_DIR.mkdir(parents=True, exist_ok=True)
        for name, path in PIPER_FILES.items():
            _download(f"{name}.onnx.json", voice.PIPER_DIR, PIPER_URL + path)  # small: first
            _download(f"{name}.onnx", voice.PIPER_DIR, PIPER_URL + path)
        _write("done")
    except (OSError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        _write("failed", error=f"Couldn't install the voices ({error}). The log is in {LOG_FILE}.")
        sys.exit(1)


if __name__ == "__main__":
    main()
