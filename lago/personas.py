"""Who your assistant is to you: Lago the otter, Chip the retro computer or Atlas the diving
robot, or Marina, Ada and Iris, the same three in their own colours with women's voices. Each
persona is the same assistant, with the same memory, conversation and work; only the name,
face (ui/app.js PERSONA_FACES and PERSONA_RECOLOURS), voice and manner change. You pick one in the welcome
wizard and can switch on his page, or call him by name ("Hey Chip", listen.py). The choice
lives in config.json under "orchestrator".
"""

import features
import languages

PERSONAS = {
    "lago": {
        "name": "Lago", "figure": "Otter",
        "about": "The original. Cheerful and quick, and always the first to get the crew moving.",
        "manner": "You're cheerful and quick, warm without gushing.",
        "voice": "kokoro:am_santa", "effect": "small-creature",
        "intro": "Hi{user}, I'm Lago! Tell me what you need, and I'll get the crew on it.",
    },
    "chip": {
        "name": "Chip", "figure": "Retro computer",
        "about": "Keen and a little nerdy. Nothing makes him happier than a passing test.",
        "manner": "You're keen and a little nerdy: you enjoy a passing test or a clean result and "
                  "say so briefly, and you like precise numbers.",
        "voice": "kokoro:am_michael", "effect": "retro-computer",
        "abroad": {"pt-pt": "piper:pt_BR-cadu-medium", "pt-br": "piper:pt_BR-cadu-medium"},
        "intro": "Hi{user}! I'm Chip. I ran every test, and all of them passed. Want me to start the next task?",
    },
    "atlas": {
        "name": "Atlas", "figure": "Diving robot",
        "about": "Deep, steady and serious. No face, just a lit window when he speaks.",
        "manner": "You're steady and serious: plain words, short sentences, no jokes and no "
                  "exclamation marks. You report facts and risks calmly.",
        "voice": "kokoro:bm_lewis", "effect": "deep-robot",
        "abroad": {"pt-pt": "piper:pt_BR-cadu-medium", "pt-br": "piper:pt_BR-cadu-medium"},
        "intro": "{user_lead}I am Atlas. I have checked the deep systems. Nothing is broken. I will keep watch.",
    },
    "marina": {
        "name": "Marina", "figure": "Otter", "woman": True,
        "about": "Lago's sister. Warm and calm, she keeps the crew moving without any fuss.",
        "manner": "You're warm and calm: kind, clear and unhurried, never gushing.",
        "voice": "kokoro:bf_isabella", "effect": "small-creature",
        "intro": "Hi{user}, I'm Marina! Tell me what you need, and I'll get the crew on it.",
    },
    "ada": {
        "name": "Ada", "figure": "Retro computer", "woman": True,
        "about": "Sharp and tidy, named after Ada Lovelace. A clean result makes her day.",
        "manner": "You're sharp and tidy: you like a clean result and precise numbers, and you "
                  "say what's done and what's next in as few words as it takes.",
        "voice": "kokoro:bf_isabella", "effect": "ai-assistant",
        "intro": "Hi{user}! I'm Ada. Every test passed, and the next task is lined up. Shall I start it?",
    },
    "iris": {
        "name": "Iris", "figure": "Diving robot", "woman": True,
        "about": "Calm and watchful. Her window glows when she speaks.",
        "manner": "You're calm and watchful: plain words, short sentences, no jokes and no "
                  "exclamation marks. You report facts and risks steadily.",
        "voice": "kokoro:bf_isabella", "effect": "hologram",
        "intro": "{user_lead}I am Iris. I have looked over everything. All is calm. I will keep watch.",
    },
}
DEFAULT = "lago"
# For the women: where a language marks it, they speak of themselves as women.
WOMAN = ("You're a woman: where a language marks it, speak of yourself as one (\"I'm sure\" is "
         "\"estou certa\", \"obrigada\" in Portuguese).")
MAN = "You're a man: where a language marks it, speak of yourself as one (\"obrigado\")."


def current(config):
    if not features.on(config, "personas"):
        return DEFAULT
    chosen = (config.get("orchestrator") or {}).get("persona")
    return chosen if chosen in PERSONAS else DEFAULT


def voice_abroad(persona_id, language):
    """The voice the persona speaks `language` in when he has one of his own ("abroad": Chip's
    and Atlas's Portuguese is Piper's Cadu), else None: languages.voice_for picks one."""
    return (PERSONAS.get(persona_id, {}).get("abroad") or {}).get(language)


def name(config):
    return PERSONAS[current(config)]["name"]


def listing():
    """The personas for the app: everything but the prompt text."""
    return [{"id": pid, **{k: p[k] for k in ("name", "figure", "about", "voice", "effect")}}
            for pid, p in PERSONAS.items()]


def intro(persona_id, user_name="", language=languages.ENGLISH):
    """What the persona says when you try him out, in the language his voice speaks."""
    user = (user_name or "").strip()
    if language:
        return languages.text(language, "intro", user=f" {user}" if user else "",
                              name=PERSONAS[persona_id]["name"], woman=PERSONAS[persona_id].get("woman"))
    return PERSONAS[persona_id]["intro"].format(user=f" {user}" if user else "",
                                                user_lead=f"{user}. " if user else "")


def prompt_section(config):
    """His persona, for the end of his system prompt."""
    p = PERSONAS[current(config)]
    spoken = languages.prompt_section(config.get("voice"))
    return ("## Your persona\n"
            f"The human chose to talk to you as {p['name']} ({p['figure'].lower()}). Introduce "
            f"yourself as {p['name']} and answer to that name; your instructions say Lago because "
            f"that's the app's name for its assistant. {p['manner']} You're the same assistant "
            "as always, with the same notes, memory and work: earlier conversations under "
            "another name were you too. \"[Lago] ...\" messages still come from the app."
            + (f" {WOMAN}" if p.get("woman") else "") + (f"\n\n{spoken}" if spoken else ""))


def switch_note(persona_id):
    """Told to a running Lago when you switch persona."""
    p = PERSONAS[persona_id]
    return (f"[Lago] Persona: the human switched you to {p['name']} ({p['figure'].lower()}). From "
            f"now on, introduce yourself as {p['name']} and answer to that name. {p['manner']} "
            "You're the same assistant, with the same memory and work; only your name, face and "
            f"voice changed. {WOMAN if p.get('woman') else MAN} Acknowledge it in one short line, "
            "in character.")


def use(config, persona_id):
    """Switch `config` (saved by the caller) to a persona: his voice and effect too, and "Hey
    <name>" as the wake phrase when it was the old name's (a phrase of your own stays)."""
    old_name = name(config)
    chosen = PERSONAS[persona_id]
    config["orchestrator"] = {**(config.get("orchestrator") or {}), "persona": persona_id}
    settings = {**(config.get("voice") or {}), "voice": chosen["voice"], "effect": chosen["effect"]}
    if " ".join(str(settings.get("wake_phrase") or "Hey Lago").lower().split()) == f"hey {old_name}".lower():
        settings["wake_phrase"] = f"Hey {chosen['name']}"
    config["voice"] = settings
    return config


def wake_phrases(config):
    """[(phrase, persona id)] the listener answers to: your wake phrase for the current persona
    and, with "wake_personas" on, "Hey <name>" for every persona, which also switches to him."""
    settings = config.get("voice") or {}
    phrases = [(settings.get("wake_phrase") or f"Hey {name(config)}", current(config))]
    if settings.get("wake_personas", True) and features.on(config, "personas"):
        phrases += [(f"Hey {p['name']}", pid) for pid, p in PERSONAS.items()]
    seen, out = set(), []
    for phrase, pid in phrases:
        key = " ".join(phrase.lower().split())
        if key not in seen:
            seen.add(key)
            out.append((phrase, pid))
    return out


def called_note(persona_id):
    """Put before a spoken message when you called another persona by name: they answer it."""
    p = PERSONAS[persona_id]
    return (f"[Lago] Persona: the human called you by name, so from now on you're {p['name']} "
            f"({p['figure'].lower()}): introduce yourself as {p['name']} and answer to that name. "
            f"{p['manner']} {WOMAN if p.get('woman') else MAN} Same assistant, memory and work. "
            f"Answer their message below as {p['name']}.")
