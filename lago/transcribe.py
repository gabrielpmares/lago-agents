#!/usr/bin/env python3
"""Turn a recording of your voice into text, locally, and print it.

mic.py runs this with the Python of the TTS venv (~/.local/share/lago-agents/tts), which has
faster-whisper; the model lives in ~/.local/share/lago-agents/whisper (downloaded on first
use). The audio must be a 16 kHz mono WAV, which is what mic.py records.

    transcribe.py [--hint "Sam, Bookshelf, Webstore"] FILE.wav
"""

import argparse
import sys
from pathlib import Path

MODEL = "small"  # multilingual: you can talk in any language Whisper knows
MODEL_DIR = Path.home() / ".local" / "share" / "lago-agents" / "whisper"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hint", default="", help="Names to spell right (people, projects).")
    parser.add_argument("audio")
    args = parser.parse_args()

    import soundfile
    from faster_whisper import WhisperModel

    audio, _ = soundfile.read(args.audio, dtype="float32")
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", download_root=str(MODEL_DIR))
    segments, _ = model.transcribe(audio, beam_size=1, vad_filter=True,
                                   initial_prompt=args.hint or None)
    print(" ".join(segment.text.strip() for segment in segments).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
