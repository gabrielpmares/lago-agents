#!/usr/bin/env python3
"""Synthesize one announcement, give it a robot effect, and play it after a chime.

voice.py runs this with the Python of the TTS venv (~/.local/share/lago-agents/tts), which
has edge-tts (Microsoft's online neural voices, free, no key) and kokoro-onnx (an offline
model in ~/.local/share/lago-agents/kokoro). When the voice fails (say, Edge while offline)
the fallback voice speaks instead. Effects are ffmpeg audio filters, and ffmpeg then brings
the speech up to a steady loudness (the voices come out quiet, and the effects make them
quieter still); without ffmpeg, or if the filter fails, the plain voice plays.

    speak.py --voice kokoro:bm_george --fallback kokoro:bm_lewis [--effect retro-computer]
             [--rate 10] [--volume 100] [--chime [SOUND]] [--out PATH] -- TEXT

--playing PATH holds this process's id from the moment the speech (not the chime) starts
playing until it ends, so the app opens his mouth only while he's actually talking.

--chime plays SOUND (a file: voice.py picks one per kind of news) before the speech, or the
desktop's message chime when no file is given.

Long text is made a sentence or two at a time, and each piece plays while the next is made,
so he starts talking after the first sentence rather than the whole reply.

With --out, nothing plays here: the finished speech (after the chime, when asked) is written
to PATH.m4a (PATH.wav without ffmpeg), for your phone to play (see voice.py's PHONE_QUEUE).
"""

import argparse
import asyncio
import functools
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

KOKORO_DIR = Path.home() / ".local" / "share" / "lago-agents" / "kokoro"
CHIME = "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"
EDGE_TIMEOUT_SECONDS = 8
# Every effect starts from 24 kHz, the rate Kokoro and Edge both produce.
EFFECTS = {
    "ai-assistant": "chorus=0.6:0.9:20|32:0.4|0.3:0.25|0.4:2|2.3,aecho=0.8:0.6:12:0.3",
    "retro-computer": "acrusher=bits=5:samples=4:mix=0.8,lowpass=f=3500",
    "deep-robot": "asetrate=24000*0.8,aresample=24000,atempo=1.25,"
                  "aecho=0.8:0.9:4|7:0.5|0.4",
    "android-buzz": "tremolo=f=45:d=0.6,flanger=delay=1:depth=1:speed=0.3",
    "hologram": "highpass=f=150,flanger=delay=3:depth=4:regen=20:speed=0.4,aecho=0.8:0.6:35:0.3",
    "walkie-talkie": "highpass=f=350,lowpass=f=3200,volume=8dB,asoftclip=type=tanh",
    "space-station": "aecho=0.8:0.8:90|180|270:0.4|0.25|0.12",
    # Pitch up or down at the same speed: a smaller or bigger creature, not a robot.
    "small-creature": "asetrate=24000*1.3,aresample=24000,atempo=0.77",
    "big-creature": "asetrate=24000*0.82,aresample=24000,atempo=1.22",
    "fairy": "asetrate=24000*1.45,aresample=24000,atempo=0.69,"
             "chorus=0.6:0.9:25|40:0.4|0.3:0.3|0.5:1.5|2,aecho=0.8:0.5:80:0.25",
    "monster": "asetrate=24000*0.68,aresample=24000,atempo=1.47,volume=6dB,asoftclip=type=tanh",
    "alien": "asetrate=24000*1.15,aresample=24000,atempo=0.87,tremolo=f=110:d=0.5,"
             "aphaser=type=t:speed=2:decay=0.5",
    "ghost": "asetrate=24000*0.94,aresample=24000,atempo=1.064,flanger=delay=5:depth=6:speed=0.15,"
             "aecho=0.8:0.7:200|400:0.45|0.3",
    # Phase thrown away (one pitch, a monotone robot) or randomized (breath, a whisper).
    "monotone-robot": "afftfilt=real='hypot(re,im)*sin(0)':imag='hypot(re,im)*cos(0)':win_size=512:overlap=0.75",
    "whisper": "afftfilt=real='hypot(re,im)*cos((random(0)*2-1)*2*3.14)':"
               "imag='hypot(re,im)*sin((random(1)*2-1)*2*3.14)':win_size=128:overlap=0.8",
    "arcade": "asetrate=24000*1.12,aresample=24000,atempo=0.893,acrusher=bits=4:samples=8:mix=1",
    "underwater": "lowpass=f=900,vibrato=f=1.5:d=0.25,aecho=0.6:0.5:60:0.3",
    "gramophone": "highpass=f=500,lowpass=f=2600,vibrato=f=0.8:d=0.04,acrusher=bits=8:mix=0.25",
    "megaphone": "highpass=f=700,lowpass=f=4200,volume=16dB,asoftclip=type=hard",
    "dream": "aphaser=type=t:speed=0.4:decay=0.6,chorus=0.5:0.9:50|60:0.4|0.32:0.25|0.4:2|1.3,"
             "aecho=0.8:0.6:120:0.3",
}
# Volume 100% is LOUDNESS (LUFS), about as loud as music or a video; doubling adds 6 dB.
LOUDNESS = -14
MIN_VOLUME, MAX_VOLUME = 25, 200
PIECE_MIN = 40  # characters: shorter sentences go with the next, so the joins stay few
FIRST_MIN = 20  # the first piece may be shorter: it's what he waits on before talking


def edge(voice, text, rate, out):
    import edge_tts

    async def run():
        await edge_tts.Communicate(text, voice, rate=f"{rate:+d}%").save(str(out))
    asyncio.run(asyncio.wait_for(run(), EDGE_TIMEOUT_SECONDS))


def kokoro(voice, text, rate, out):
    import soundfile

    audio, sample_rate = _kokoro_model().create(text, voice=voice, speed=max(0.5, min(2.0, 1 + rate / 100)),
                                      lang="en-gb" if voice.startswith("b") else "en-us")
    soundfile.write(str(out), audio, sample_rate)


@functools.cache
def _kokoro_model():
    """Loaded once (~0.6 s) for all the pieces of an announcement."""
    from kokoro_onnx import Kokoro
    return Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))


ENGINES = {"edge": (edge, ".mp3"), "kokoro": (kokoro, ".wav")}

# Lago plays as its own app with its own role, so PipeWire keeps a volume for it apart from
# everything else's (pw-play's default role, Music, can have been turned down for another
# player: it once left Lago at 4%), and the sound settings list it as "Lago Agents".
PLAYER_NAME = "Lago Agents"
PLAYER_ROLE = "Assistant"


def play_command(path):
    """The command that plays `path` as Lago, or None when there's no player."""
    if shutil.which("afplay"):  # macOS
        return ["afplay", str(path)]
    if shutil.which("pw-play"):
        return ["pw-play", f"--media-role={PLAYER_ROLE}", "-P",
                f'{{ application.name = "{PLAYER_NAME}" application.id = "lago-agents" }}', str(path)]
    if shutil.which("paplay"):
        return ["paplay", f"--property=media.role={PLAYER_ROLE}",
                f"--property=application.name={PLAYER_NAME}", str(path)]
    return None


def synthesize(choice, text, rate, folder, index=0):
    engine, _, name = choice.partition(":")
    make, suffix = ENGINES[engine]
    out = Path(folder) / f"speech{index}{suffix}"
    make(name, text, rate, out)
    return out


def pieces(text):
    """The text in pieces of a sentence or more (at least PIECE_MIN characters, FIRST_MIN for
    the first, bar the last)."""
    out, current = [], ""
    for sentence in re.split(r"(?<=[.!?;:])\s+", text.strip()):
        current = f"{current} {sentence}".strip()
        if len(current) >= (PIECE_MIN if out else FIRST_MIN):
            out.append(current)
            current = ""
    if current and out and len(current) < PIECE_MIN // 3:
        out[-1] += " " + current  # a short tail goes with the piece before
    elif current:
        out.append(current)
    return out or [text]


def make_all(texts, choices, rate, effect, volume, folder, ready):
    """Synthesize and finish each piece in turn, putting its file on `ready` (None when done
    or nothing more can be said). A voice that fails isn't tried again on later pieces."""
    choices = list(dict.fromkeys(filter(None, choices)))
    try:
        for index, text in enumerate(texts):
            speech = None
            for choice in list(choices):
                try:
                    speech = synthesize(choice, text, rate, folder, index)
                    break
                except Exception as error:  # try the fallback voice
                    print(f"speak.py: {choice} failed: {error!r}", file=sys.stderr)
                    choices.remove(choice)
            if speech is None:
                return
            ready.put(finish(speech, effect, volume))
    finally:
        ready.put(None)


def finish(speech, effect, volume):
    """The speech with the effect applied and brought to the volume's loudness, or unchanged
    if ffmpeg can't. Up to 100% it's normalized to that loudness; above, it's pushed into a
    limiter, which makes it louder by flattening the peaks."""
    if not shutil.which("ffmpeg"):
        return speech
    out = speech.with_name(f"{speech.stem}-final.wav")
    gain = 20 * math.log10(max(MIN_VOLUME, min(MAX_VOLUME, volume)) / 100)  # dB from 100%
    chain = ["aresample=24000"] + ([EFFECTS[effect]] if effect in EFFECTS else []) + [
        "acompressor=threshold=-30dB:ratio=3:attack=3:release=120",
        f"loudnorm=I={LOUDNESS + min(0, gain):.1f}:TP=-1:LRA=5", "aresample=24000"]
    if gain > 0:
        chain += [f"volume={gain:.1f}dB", "alimiter=limit=0.8:attack=3:release=60:level=false"]
    proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(speech),
                           "-af", ",".join(chain), str(out)], capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"speak.py: finishing ({effect or 'no effect'}) failed: {proc.stderr.strip()}",
              file=sys.stderr)
        return speech
    return out


def save(speech, out, chime):
    """Write the speech (with the `chime` file first) where the phone picks it up: AAC in .m4a,
    small enough to send at once. Written to a .part file and renamed, so it's never seen
    half-done."""
    part = Path(f"{out}.part")
    if shutil.which("ffmpeg"):
        inputs = (["-i", chime] if chime and Path(chime).exists() else []) + ["-i", str(speech)]
        count = len(inputs) // 2
        mono = "".join(f"[{i}:a]aresample=24000,aformat=channel_layouts=mono[a{i}];" for i in range(count))
        joined = "".join(f"[a{i}]" for i in range(count)) + f"concat=n={count}:v=0:a=1[out]"
        proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", mono + joined,
                               "-map", "[out]", "-c:a", "aac", "-b:a", "64k", "-f", "mp4", str(part)],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            part.rename(f"{out}.m4a")
            return 0
        print(f"speak.py: encoding for the phone failed: {proc.stderr.strip()}", file=sys.stderr)
    shutil.copyfile(speech, part)
    part.rename(f"{out}{speech.suffix}")
    return 0


def _unmark(path):
    """Remove the playing marker, unless another announcement has written its own since."""
    try:
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", required=True)
    parser.add_argument("--fallback", default="")
    parser.add_argument("--effect", default="")
    parser.add_argument("--rate", type=int, default=0)
    parser.add_argument("--volume", type=int, default=100)
    parser.add_argument("--chime", nargs="?", const=CHIME, default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--playing", default="")
    parser.add_argument("text")
    args = parser.parse_args()
    if not play_command("") and not args.out:
        return 1
    with tempfile.TemporaryDirectory(prefix="lago-voice-") as folder:
        ready = queue.Queue()
        # The phone gets one file, so it's made whole; here each piece plays as it's ready.
        texts = [args.text] if args.out else pieces(args.text)
        threading.Thread(target=make_all, daemon=True, args=(
            texts, (args.voice, args.fallback), args.rate, args.effect, args.volume,
            folder, ready)).start()
        if args.out:
            speech = ready.get()
            return save(speech, args.out, args.chime) if speech else 1
        if args.chime and Path(args.chime).exists():  # while the first piece is made
            subprocess.run(play_command(args.chime), stderr=subprocess.DEVNULL)
        played = False
        try:
            while (speech := ready.get()) is not None:
                if not played and args.playing:
                    Path(args.playing).write_text(str(os.getpid()))
                subprocess.run(play_command(speech), stderr=subprocess.DEVNULL)
                played = True
        finally:
            if played and args.playing:
                _unmark(Path(args.playing))
    return 0 if played else 1  # 1: voice.py's last resort says it instead


if __name__ == "__main__":
    sys.exit(main())
