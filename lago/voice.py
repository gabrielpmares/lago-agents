"""Saying things out loud: "Sam, the task on Webstore is done."

speak.py does the talking, with the Python of the TTS venv in ~/.local/share/lago-agents/tts:
offline Kokoro voices (George by default), or Microsoft Edge's online neural voices (free,
no key) with a Kokoro voice as the fallback. Without that venv, speech-dispatcher
(`spd-say`) with espeak-ng speaks instead. Each announcement runs detached under a lock
file, so announcements from different agents queue up instead of talking over each other.
Settings live in config.json under "voice" and are edited in the Orchestrator page.
"""

import math
import os
import re
import struct
import wave
import zlib
import shlex
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path

import config_store
import features
from config_store import APP_DIR, STATE_DIR

LOCK_FILE = STATE_DIR / "voice.lock"
# When you're on your phone, Lago talks there instead (remote.py decides and keeps
# ROUTE_FILE fresh): speech is made here as usual but written to PHONE_QUEUE, and the phone
# fetches and plays each file in order. hush() touches HUSH_STAMP so the phone stops too.
ROUTE_FILE = STATE_DIR / "remote" / "voice-route.json"
PHONE_QUEUE = STATE_DIR / "voice-out"
HUSH_STAMP = STATE_DIR / "voice-hush"
PIDS_FILE = STATE_DIR / "voice.pids"  # process groups of announcements, for hush()
PLAYING_FILE = STATE_DIR / "voice.playing"  # the process whose speech is coming out now
SPEAK = APP_DIR / "speak.py"
TTS_DIR = Path.home() / ".local" / "share" / "lago-agents"
TTS_PYTHON = TTS_DIR / "tts" / "bin" / "python"
KOKORO_MODEL = TTS_DIR / "kokoro" / "kokoro-v1.0.onnx"

EDGE_VOICES = {"edge:en-GB-RyanNeural": "Ryan (British, online)",
               "edge:en-US-AndrewMultilingualNeural": "Andrew (American, online)",
               "edge:en-US-AvaMultilingualNeural": "Ava (American, online)"}
# Every English voice in voices-v1.0.bin, British then American, men then women.
KOKORO_VOICES = {"kokoro:bm_george": "George (British, offline)",
                 "kokoro:bm_lewis": "Lewis (British, offline)",
                 "kokoro:bm_daniel": "Daniel (British, offline)",
                 "kokoro:bm_fable": "Fable (British, offline)",
                 "kokoro:bf_emma": "Emma (British, offline)",
                 "kokoro:bf_isabella": "Isabella (British, offline)",
                 "kokoro:bf_alice": "Alice (British, offline)",
                 "kokoro:bf_lily": "Lily (British, offline)",
                 "kokoro:am_michael": "Michael (American, offline)",
                 "kokoro:am_fenrir": "Fenrir (American, offline)",
                 "kokoro:am_puck": "Puck (American, offline)",
                 "kokoro:am_adam": "Adam (American, offline)",
                 "kokoro:am_echo": "Echo (American, offline)",
                 "kokoro:am_eric": "Eric (American, offline)",
                 "kokoro:am_liam": "Liam (American, offline)",
                 "kokoro:am_onyx": "Onyx (American, offline)",
                 "kokoro:am_santa": "Santa (American, offline)",
                 "kokoro:af_heart": "Heart (American, offline)",
                 "kokoro:af_bella": "Bella (American, offline)",
                 "kokoro:af_nicole": "Nicole (American, offline)",
                 "kokoro:af_aoede": "Aoede (American, offline)",
                 "kokoro:af_kore": "Kore (American, offline)",
                 "kokoro:af_sarah": "Sarah (American, offline)",
                 "kokoro:af_alloy": "Alloy (American, offline)",
                 "kokoro:af_nova": "Nova (American, offline)",
                 "kokoro:af_sky": "Sky (American, offline)",
                 "kokoro:af_jessica": "Jessica (American, offline)",
                 "kokoro:af_river": "River (American, offline)"}
ESPEAK_VOICES = {"English (America)": "eSpeak (synthetic, offline)"}
# Effects speak.py can put on Kokoro and Edge voices (its EFFECTS has the filters): robots,
# places and devices the voice comes through, creatures, and moods.
EFFECTS = {"": "None", "ai-assistant": "AI assistant", "retro-computer": "Retro computer",
           "deep-robot": "Deep robot", "android-buzz": "Android buzz", "hologram": "Hologram",
           "walkie-talkie": "Walkie-talkie", "space-station": "Space station",
           "small-creature": "Small creature", "big-creature": "Big creature", "fairy": "Fairy",
           "monster": "Monster", "alien": "Alien", "ghost": "Ghost", "monotone-robot": "Monotone robot",
           "whisper": "Whisper", "arcade": "Arcade 8-bit", "underwater": "Underwater",
           "gramophone": "Old gramophone", "megaphone": "Megaphone", "dream": "Dream"}
DEFAULTS = {"enabled": True, "voice": "kokoro:bm_george", "fallback": "kokoro:bm_lewis",
            "effect": "retro-computer", "rate": 0, "volume": 100, "greet": True, "talk": True, "chime": True, "quiet_from": "", "quiet_to": "",
            "wake": False, "wake_phrase": "Hey Lago", "wake_personas": True, "wake_sound": "soft",
            "sent_sound": "blip",
            # The chime before each announcement, by what kind of news it is (CATEGORIES).
            "chimes": {"needs_you": "attention", "done": "success", "progress": "nudge",
                       "problem": "problem"},
            "kinds": ["solved", "done", "failed", "question", "gate", "approval", "blocked",
                      "permission", "waiting"]}
# What kind of news an announcement is: its chime says it before the words do.
CATEGORIES = {"needs_you": "Needs you", "done": "Done", "progress": "Working on it",
              "problem": "Problem"}
KIND_CATEGORY = {"question": "needs_you", "gate": "needs_you", "approval": "needs_you",
                 "blocked": "needs_you", "permission": "needs_you", "waiting": "needs_you",
                 "solved": "done", "done": "progress", "update": "progress", "failed": "problem"}
# How Lago tags the 🔊 line of a reply (prompts/orchestrator.md), for its chime.
REPLY_TAGS = {"needs you": "needs_you", "done": "done", "working": "progress", "problem": "problem"}

# The kinds there were before `kinds_known` was saved with the settings: any kind added since
# speaks until you switch it off.
OLD_KINDS = ("done", "failed", "question", "gate", "approval", "blocked", "permission", "waiting")

VOLUME_MIN, VOLUME_MAX = 25, 200  # percent; 100 is as loud as music or a video

PHRASES = {
    "solved": "{name}, the task on {project} is fully done. You can close its session.",
    "done": "{name}, a stage of the work on {project} is done.",
    "failed": "{name}, the task on {project} failed.",
    "question": "{name}, {project} has a question for you.",
    "gate": "{name}, {project} has a result for you to check.",
    "approval": "{name}, {project} has a plan waiting for your approval.",
    "blocked": "{name}, the work on {project} is blocked.",
    "permission": "{name}, {project} needs your permission.",
    "waiting": "{name}, {project} is waiting for you.",
}

DEFAULTS["kinds_known"] = list(PHRASES)

def voices():
    """The voices that can speak here. `offline` ones can be the fallback."""
    found = []
    if TTS_PYTHON.exists():
        found += [{"id": vid, "label": label, "offline": False} for vid, label in EDGE_VOICES.items()]
        if KOKORO_MODEL.exists():
            found += [{"id": vid, "label": label, "offline": True}
                      for vid, label in KOKORO_VOICES.items()]
    if shutil.which("spd-say"):
        found += [{"id": vid, "label": label, "offline": True} for vid, label in ESPEAK_VOICES.items()]
    return found


# The sounds "Hey Lago" plays: while it waits for you to speak (wake_sound) and once it has
# heard you (sent_sound). A tone made here (notes in Hz, each with its length in seconds),
# a system sound, or nothing.
# The same sounds (and three more tones) are what the announcement chimes pick from. A note
# at 0 Hz is a short rest.
WAKE_TONES = {"soft": [(587.33, 0.09), (880.0, 0.16)], "blip": [(740.0, 0.07)],
              "low": [(392.0, 0.08), (523.25, 0.14)],
              "attention": [(880.0, 0.08), (0, 0.05), (880.0, 0.08), (1318.51, 0.22)],
              "success": [(523.25, 0.07), (659.25, 0.07), (783.99, 0.07), (1046.5, 0.26)],
              "problem": [(659.25, 0.12), (523.25, 0.12), (392.0, 0.28)],
              "nudge": [(740.0, 0.07)]}
# How loud each tone is (a sine's peak, 0 to 1). The wake sounds stay soft; the announcement
# chimes have to be heard across the room.
TONE_LEVEL = {"attention": 0.9, "success": 0.9, "problem": 0.9, "nudge": 0.9}  # about as loud as his voice
QUIET_TONE = 0.22
WAKE_SOUNDS = {"soft": "Soft tone (two notes)", "blip": "Short blip", "low": "Low tone",
               "attention": "Attention (two quick notes, then higher)",
               "success": "Success (four rising notes)", "problem": "Problem (three falling notes)",
               "nudge": "Nudge (a short, louder blip)",
               "/usr/share/sounds/Yaru/stereo/message.oga": "Ubuntu message",
               "/usr/share/sounds/freedesktop/stereo/bell.oga": "Bell",
               "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga": "Original chime",
               "/usr/share/sounds/freedesktop/stereo/complete.oga": "Complete chime",
               "": "None"}


def _tone(name):
    """A gentle sine tone (soft attack, fading tail) as a WAV in the state folder. The file
    name carries its notes and level, so changing either makes a new file."""
    level = TONE_LEVEL.get(name, QUIET_TONE)
    stamp = zlib.crc32(repr((WAKE_TONES[name], level)).encode())
    path = STATE_DIR / f"tone-{name}-{stamp:08x}.wav"
    if path.exists():
        return path
    rate, samples = 24000, []
    for freq, seconds in WAKE_TONES[name]:
        count = int(rate * seconds)
        for i in range(count):
            t = i / rate
            envelope = min(1.0, t / 0.008) * math.exp(-4.5 * t / seconds)
            samples.append(level * envelope * (math.sin(2 * math.pi * freq * t)
                                              + 0.15 * math.sin(4 * math.pi * freq * t)))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x)) * 32767))
                                 for x in samples))
    return path


def wake_sound(choice):
    """The file to play for a wake-sound choice, or None for silence."""
    if choice in WAKE_TONES:
        return _tone(choice)
    return Path(choice) if choice and Path(choice).is_file() else None


def play_file(path):
    from speak import play_command  # the same player identity as the speech
    command = play_command(path) if path else None
    if command:
        _reap(subprocess.Popen(command, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True))


def _reap(proc):
    """Collect the process when it ends, in the background. Otherwise it lingers as a zombie
    while the process that started it (the app, Lago's tools) runs, and _alive() would
    count it as still speaking."""
    threading.Thread(target=proc.wait, name="voice-reap", daemon=True).start()


def _alive(pid):
    """The process runs (a zombie, ended but not yet collected, doesn't count)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def settings(config):
    saved = config.get("voice") or {}
    out = {**DEFAULTS, **saved}
    known = saved.get("kinds_known", OLD_KINDS)
    out["kinds"] = list(out["kinds"]) + [k for k in DEFAULTS["kinds"]
                                         if k not in known and k not in out["kinds"]]
    out["kinds_known"] = list(PHRASES)
    out["chimes"] = {**DEFAULTS["chimes"], **(saved.get("chimes") or {})}
    if not features.on(config, "voice"):  # the Voice extra is off: nothing speaks or listens
        out.update(enabled=False, talk=False, greet=False, wake=False)
    return out


def chime(voice, category):
    """The sound to play before an announcement of this category, or None."""
    if not voice.get("chime"):
        return None
    chimes = {**DEFAULTS["chimes"], **(voice.get("chimes") or {})}
    return wake_sound(chimes.get(category) or chimes["progress"])


def _minutes(hhmm):
    hours, _, minutes = str(hhmm).partition(":")
    return int(hours) * 60 + int(minutes or 0)


def quiet_now(voice, now=None):
    """Inside the quiet hours (which may wrap past midnight)?"""
    if not voice.get("quiet_from") or not voice.get("quiet_to"):
        return False
    try:
        start, end = _minutes(voice["quiet_from"]), _minutes(voice["quiet_to"])
    except ValueError:
        return False
    local = time.localtime(now)
    current = local.tm_hour * 60 + local.tm_min
    return start <= current < end if start <= end else current >= start or current < end


def phrase(config, kind, project_name):
    template = PHRASES.get(kind)
    if not template:
        return ""
    return template.format(name=config.get("user_name") or "Hey", project=project_name)


def greeting(config, now=None):
    hour = time.localtime(now).tm_hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
    name = config.get("user_name") or "there"
    return f"Good {part}, {name}. What will we work on today?"


# The Orchestrator ends each reply with a line like "🔊 Both tasks are running." for Lago to
# say aloud (see prompts/orchestrator.md).
SPOKEN_LINE = re.compile(r"^[^\w🔊]*🔊[\s*_:]*(?:spoken\s*:)?\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
SPOKEN_LIMIT = 300


REPLY_TAG = re.compile(r"^\[\s*(" + "|".join(REPLY_TAGS) + r")\s*\]\s*", re.IGNORECASE)


def spoken_line(reply):
    """The line of a reply meant to be said aloud, without markdown or its tag, or ""."""
    found = SPOKEN_LINE.findall(reply or "")
    if not found:
        return ""
    text = REPLY_TAG.sub("", re.sub(r"[*_`#>]", "", found[-1]).strip())
    return text[:SPOKEN_LIMIT].rsplit(" ", 1)[0] if len(text) > SPOKEN_LIMIT else text


def reply_category(reply):
    """The category Lago tagged his spoken line with ("[needs you] ..."); "progress" if none."""
    found = SPOKEN_LINE.findall(reply or "")
    tag = REPLY_TAG.match(re.sub(r"[*_`#>]", "", found[-1]).strip()) if found else None
    return REPLY_TAGS[tag.group(1).lower()] if tag else "progress"


def speak_reply(config, reply):
    """Say the spoken line of the Orchestrator's reply, if Lago talks. Returns what was said."""
    voice = settings(config)
    text = spoken_line(reply)
    if not text or not voice["talk"] or quiet_now(voice):
        return ""
    return text if say(text, voice, reply_category(reply)) else ""


def announce(config, kind, project_name):
    """Speak the phrase for this kind of event, if the settings allow. Returns what was said."""
    voice = settings(config)
    if not voice["enabled"] or kind not in voice["kinds"] or quiet_now(voice):
        return ""
    text = phrase(config, kind, project_name)
    return text if text and say(text, voice, KIND_CATEGORY.get(kind, "progress")) else ""


def to_phone():
    """Whether Lago should talk on your phone now (see remote.py's VoiceRoute)."""
    try:
        import json
        route = json.loads(ROUTE_FILE.read_text())
    except (OSError, ValueError):
        return False
    return bool(route.get("phone")) and route.get("until", 0) > time.time()


def hush():
    """Stop whatever is being said and drop what's queued. Each announcement runs in its own
    process group (flock, the synthesizer and the player), so that's what gets stopped."""
    try:
        groups = {int(line) for line in PIDS_FILE.read_text().split()}
    except (OSError, ValueError):
        groups = set()
    PIDS_FILE.unlink(missing_ok=True)
    for queued in PHONE_QUEUE.glob("*") if PHONE_QUEUE.is_dir() else ():
        queued.unlink(missing_ok=True)
    HUSH_STAMP.touch()
    for group in groups:
        try:
            if str(LOCK_FILE).encode() in Path(f"/proc/{group}/cmdline").read_bytes():
                os.killpg(group, signal.SIGTERM)
        except OSError:
            pass  # already finished


def playing():
    """Whether his voice is coming out of the speakers right now (not just being made, or
    waiting its turn): what his mouth follows."""
    try:
        return _alive(int(PLAYING_FILE.read_text()))
    except (OSError, ValueError):
        return False


def speaking():
    """Whether something is being said (or queued) right now."""
    try:
        groups = [int(line) for line in PIDS_FILE.read_text().split()]
    except (OSError, ValueError):
        return False
    return any(_alive(g) for g in groups)


def _espeak(text, rate, volume=100, name="English (America)"):
    loud = max(-100, min(100, volume - 100))  # spd-say's 0 is its normal volume, 100 its loudest
    return shlex.join(["spd-say", "--wait", "-o", "espeak-ng", "-r", str(rate), "-i", str(loud),
                       "-y", name, "--", text])


def say(text, voice=None, category="progress"):
    """Queue `text` to be spoken, after the chime for its `category` (CATEGORIES). Returns
    False when there's no speech engine, or the Voice extra is off."""
    if not features.on(config_store.load(), "voice"):
        return False
    voice = {**DEFAULTS, **(voice or {})}
    available = [v["id"] for v in voices()]
    if not available or not shutil.which("flock"):
        return False
    chosen = voice["voice"] if voice["voice"] in available else available[0]
    rate = max(-100, min(100, int(voice.get("rate") or 0)))
    volume = max(VOLUME_MIN, min(VOLUME_MAX, int(voice.get("volume") or 100)))
    out = None
    if to_phone():
        PHONE_QUEUE.mkdir(mode=0o700, exist_ok=True)
        out = PHONE_QUEUE / f"{time.time_ns()}-{os.getpid()}"  # speak.py adds the extension
    if chosen in ESPEAK_VOICES:
        command = _espeak(text, rate, volume, chosen)
        if not out:  # spd-say starts at once: mark it playing for its whole run
            marker = shlex.quote(str(PLAYING_FILE))
            command = f"echo $$ > {marker}; {command}; rm -f {marker}"
        if out:  # espeak-ng writes a file; spd-say can't
            command = shlex.join(["espeak-ng", "-v", "en-us", "-s", str(175 + rate), "-w", f"{out}.part", "--", text]) \
                + f" && mv {shlex.quote(str(out))}.part {shlex.quote(str(out))}.wav"
    else:
        fallback = voice.get("fallback") if voice.get("fallback") in available else ""
        speak = [str(TTS_PYTHON), str(SPEAK), "--voice", chosen, "--rate", str(rate),
                 "--volume", str(volume)]
        if fallback and fallback not in ESPEAK_VOICES and fallback != chosen:
            speak += ["--fallback", fallback]
        if voice.get("effect") in EFFECTS and voice["effect"]:
            speak += ["--effect", voice["effect"]]
        sound = chime(voice, category)
        if sound:
            speak += ["--chime", str(sound)]
        if out:
            speak += ["--out", str(out)]
        else:
            speak += ["--playing", str(PLAYING_FILE)]
        command = shlex.join(speak + ["--", text])
        if shutil.which("spd-say") and not out:
            command += " || " + _espeak(text, rate, volume)  # last resort: say it somehow
    try:
        proc = subprocess.Popen(["flock", str(LOCK_FILE), "sh", "-c", command],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    _reap(proc)
    _remember(proc.pid)
    return True


def _remember(group):
    """Note an announcement's process group, forgetting the ones that have finished."""
    try:
        groups = [int(line) for line in PIDS_FILE.read_text().split()]
    except (OSError, ValueError):
        groups = []
    alive = [g for g in groups if _alive(g)]
    PIDS_FILE.write_text("\n".join(str(g) for g in alive + [group]) + "\n")
