#!/usr/bin/env python3
"""Listen for "Hey Lago" and send what you say next to Lago, like a smart speaker.

Nothing listens unless you choose it: switching "Listen for the wake phrase" on in Lago's
settings installs and starts a user service (lago-agents-listen.service, set_service) that
runs this with the Python of the speech venv, which has faster-whisper; switching it off
stops and removes the service. Everything stays on this machine:
- With it on, pw-record (or parecord, or arecord) streams the microphone. Silero VAD (bundled with faster-whisper)
  marks speech; the audio that isn't speech is dropped as it arrives.
- When someone starts talking, the tiny Whisper model reads the first couple of seconds. If
  they start with the wake phrase, a chime plays and recording carries on until you stop
  talking; the small model then transcribes it and it goes to Lago as a spoken message.
  Anything else is dropped. Nothing is written to disk.
- Each persona answers to his name too ("Hey Chip", "Hey Atlas"; personas.wake_phrases):
  calling another one switches Lago to him, and he answers what you said.
- It doesn't listen while Lago is speaking (his voice would wake it) or while the app's own
  push-to-talk is recording.

    listen.py [--test]   --test prints what it hears instead of sending it
"""

import argparse
import difflib
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_store  # noqa: E402
import desktop  # noqa: E402
import mic  # noqa: E402
import notify  # noqa: E402
import personas  # noqa: E402
import voice  # noqa: E402

SERVICE = "lago-agents-listen.service"

RATE = 16000
FRAME = 512                       # samples per VAD frame (32 ms)
BLOCK = FRAME * 8                 # samples read at a time (256 ms)
SPEECH_PROB = 0.5
PREROLL_BLOCKS = 2                # audio kept from just before speech starts
WAKE_CHECK_SECONDS = 2.5          # how much of an utterance is checked for the phrase
WAKE_SILENCE_SECONDS = 0.5        # a pause this long ends an utterance being checked
COMMAND_SILENCE_SECONDS = 1.3     # a pause this long after speaking ends the command
COMMAND_WAIT_SECONDS = 6          # "Hey Lago" then silence: wait this long for the rest
COMMAND_MAX_SECONDS = 60
SETTING_POLL_SECONDS = 3
WAKE_MATCH = 0.65                 # how close each word must be to the phrase's word
# The tiny model is hinted with the phrase, and on speech it can't make out (another
# language, a TV across the room) it tends to write the hint back: "Hey Lago." Those
# echoes come with a high chance of "no speech" (0.4-0.8 in tests); a phrase you actually
# said scores under 0.1, and only faint or noisy ones go past 0.3.
WAKE_MAX_NO_SPEECH = 0.35
MODEL_DIR = Path.home() / ".local" / "share" / "lago-agents" / "whisper"


def log(text):
    print(time.strftime("%H:%M:%S"), text, flush=True)


class ListenError(Exception):
    pass


# ---------------------------------------------------------------- the service

def service_running():
    import desktop
    return desktop.service_state(SERVICE)["active"]


def set_service(on):
    """On: define the service (it follows this checkout) and start it. Off: stop it and remove it."""
    import desktop
    try:
        if on:
            if not voice.TTS_PYTHON.exists():
                raise ListenError("Listening needs the speech add-on: see Install, \"For the voice\", in "
                                  "the README.")
            desktop.service_write(SERVICE, [str(voice.TTS_PYTHON), str(Path(__file__).resolve())],
                                  "Lago Agents: listen for the wake phrase", after="pipewire.service")
            desktop.service_enable(SERVICE)
        elif desktop.service_path(SERVICE).exists() or service_running():
            desktop.service_disable(SERVICE, remove=True)
    except desktop.ServiceError as error:
        raise ListenError(f"Couldn't {'start' if on else 'stop'} the listener: {error}") from error


# ---------------------------------------------------------------- hearing the phrase


def settings():
    config = config_store.load()
    return voice.settings(config)


def deaf():
    """Lago is talking, or the app's push-to-talk has the microphone."""
    return voice.speaking() or mic.RECORDING.exists()


def words(text):
    return re.sub(r"[^\w\s']", " ", text.lower()).split()


def wake_end(text, phrase):
    """Where the wake phrase ends in `text` (a word index), or None if it isn't there. It
    must be among the first words; Whisper's spelling may differ a little ("Hey, Lagos")."""
    said, wanted = words(text), words(phrase)
    close = lambda a, b: difflib.SequenceMatcher(None, a, b).ratio() >= WAKE_MATCH
    for start in range(min(2, len(said))):
        window = said[start:start + len(wanted)]
        if len(window) == len(wanted) and all(map(close, window, wanted)):
            return start + len(wanted)
    return None


def match(text, phrases):
    """The (phrase, persona id) `text` starts with, or None. The earliest wins, then the longest
    ("Hey Lago Chip" is Lago's)."""
    found = [(wake_end(text, phrase), -len(words(phrase)), phrase, pid) for phrase, pid in phrases]
    found = sorted(f for f in found if f[0] is not None)
    return (found[0][2], found[0][3]) if found else None


def after_phrase(text, phrase):
    """The command: what was said after the wake phrase."""
    end = wake_end(text, phrase)
    if end is None:
        return text.strip()
    tokens = text.split()
    # Map the word index back onto the original text (punctuation kept).
    kept, seen = [], 0
    for token in tokens:
        seen += len(words(token))
        if seen > end:
            kept.append(token)
    return " ".join(kept).lstrip(",.!? ").strip()


def play(sound):
    voice.play_file(sound)


class Listener:
    def __init__(self, test=False):
        self.test = test
        self.wake_model = self.command_model = None  # loaded once listening is switched on

    def load(self):
        import numpy
        from faster_whisper import WhisperModel
        from faster_whisper.vad import get_vad_model

        self.np = numpy
        self.vad = get_vad_model()
        self.WhisperModel = WhisperModel
        self.wake_model = WhisperModel("tiny", device="cpu", compute_type="int8",
                                       download_root=str(MODEL_DIR), cpu_threads=2)

    def transcribe(self, model, audio, hint):
        return self.transcribe_scored(model, audio, hint)[0]

    def transcribe_scored(self, model, audio, hint):
        """(text, the highest chance among its segments that there was no speech at all)."""
        segments, _ = model.transcribe(audio, beam_size=1, initial_prompt=hint,
                                       condition_on_previous_text=False)
        segments = list(segments)
        no_speech = max((s.no_speech_prob for s in segments), default=1.0)
        return " ".join(s.text.strip() for s in segments).strip(), no_speech

    def speech(self, block):
        return float(self.vad(block).max()) >= SPEECH_PROB

    def run(self):
        recorder = None
        try:
            while True:
                current = settings()
                if not current.get("wake") and not self.test:
                    if recorder:
                        recorder.terminate()
                        recorder = None
                        log("stopped listening")
                    time.sleep(SETTING_POLL_SECONDS)
                    continue
                if self.wake_model is None:
                    self.load()
                if recorder is None:
                    recorder = subprocess.Popen(
                        desktop.recorder_argv(RATE, "-"),
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    log("listening for " + ", ".join(f'"{p}"' for p, _ in personas.wake_phrases(config_store.load())))
                self.listen(recorder, personas.wake_phrases(config_store.load()))
                if recorder.poll() is not None:
                    log("the microphone stream ended; restarting it")
                    recorder = None
                    time.sleep(2)
        finally:
            if recorder:
                recorder.terminate()

    def read(self, recorder):
        data = recorder.stdout.read(BLOCK * 2)
        if len(data) < BLOCK * 2:
            return None
        return self.np.frombuffer(data, dtype="<i2").astype("float32") / 32768.0

    def listen(self, recorder, phrases):
        """Listen for SETTING_POLL_SECONDS-ish, handling any utterance that starts."""
        preroll = deque(maxlen=PREROLL_BLOCKS)
        until = time.time() + SETTING_POLL_SECONDS
        while time.time() < until:
            block = self.read(recorder)
            if block is None:
                return
            if deaf():
                preroll.clear()
                continue
            if not self.speech(block):
                preroll.append(block)
                continue
            self.utterance(recorder, list(preroll) + [block], phrases)
            preroll.clear()
            until = time.time() + SETTING_POLL_SECONDS

    def utterance(self, recorder, blocks, phrases):
        """Someone started talking: check the start for a phrase, then take the command."""
        block_seconds = BLOCK / RATE
        silence = 0.0
        while len(blocks) * block_seconds < WAKE_CHECK_SECONDS and silence < WAKE_SILENCE_SECONDS:
            block = self.read(recorder)
            if block is None:
                return
            blocks.append(block)
            silence = 0.0 if self.speech(block) else silence + block_seconds
        hint = " ".join(f"{p}." for p, _ in phrases)
        start, no_speech = self.transcribe_scored(self.wake_model, self.np.concatenate(blocks), hint)
        woke = match(start, phrases)
        if woke is None or no_speech > WAKE_MAX_NO_SPEECH:
            if woke is not None:
                log(f"(not sure it was you, no speech {no_speech:.2f}) {start}")
            elif self.test and start:
                log(f"(ignored) {start}")
            self.skip_rest(recorder, silence)
            return
        log(f"woke on: {start} (no speech {no_speech:.2f})")
        play(voice.wake_sound(settings()["wake_sound"]))
        heard, waited = len(after_phrase(start, woke[0])) > 0, 0.0
        while len(blocks) * block_seconds < COMMAND_MAX_SECONDS:
            block = self.read(recorder)
            if block is None:
                break
            blocks.append(block)
            if self.speech(block):
                heard, silence = True, 0.0
            else:
                silence += block_seconds
                waited += block_seconds
            if heard and silence >= COMMAND_SILENCE_SECONDS:
                break
            if not heard and waited >= COMMAND_WAIT_SECONDS:
                break
        if not heard:
            log("woke, but nothing followed")
            return
        if self.command_model is None:
            self.command_model = self.WhisperModel("small", device="cpu", compute_type="int8",
                                                   download_root=str(MODEL_DIR))
        text = self.transcribe(self.command_model, self.np.concatenate(blocks),
                               f"{hint} {mic._hint()}.")
        woke = match(text, phrases)  # the better model has the last word on who was called
        if woke is None:
            log(f"(ignored on a second listen) {text}")
            return
        command = after_phrase(text, woke[0])
        if not command:
            log(f"nothing to send (heard: {text})")
            return
        play(voice.wake_sound(settings()["sent_sound"]))
        log(f"heard ({woke[1]}): {command}")
        if not self.test:
            send(command, woke[1])

    def skip_rest(self, recorder, silence):
        """Not for Lago: let the rest of the sentence go by without checking it again."""
        block_seconds = BLOCK / RATE
        while silence < WAKE_SILENCE_SECONDS:
            block = self.read(recorder)
            if block is None:
                return
            silence = 0.0 if self.speech(block) else silence + block_seconds


def send(command, persona_id):
    """To Lago as a spoken message; first switching him to the persona you called, if another."""
    config = config_store.without_swarm(config_store.load())
    if persona_id == personas.current(config):
        notify.tell_orchestrator(f"(spoken) {command}")
        return
    config_store.save(personas.use(config, persona_id), source="wake phrase")
    notify.tell_orchestrator(f"{personas.called_note(persona_id)}\n\n(spoken) {command}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="listen even if switched off and print instead of sending")
    args = parser.parse_args()
    Listener(test=args.test).run()


if __name__ == "__main__":
    main()
