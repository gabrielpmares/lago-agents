"""Who your assistant is to you: Lago the otter, Chip the retro computer or Atlas the diving
robot. Each persona is the same assistant, with the same memory, conversation and work; only
his name, face (ui/app.js PERSONA_FACES), voice and manner change. You pick one in the welcome
wizard and can switch on his page, or call him by name ("Hey Chip", listen.py). The choice
lives in config.json under "orchestrator".
"""

import features

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
        "intro": "Hi{user}! I'm Chip. I ran every test, and all of them passed. Want me to start the next task?",
    },
    "atlas": {
        "name": "Atlas", "figure": "Diving robot",
        "about": "Deep, steady and serious. No face, just a lit window when he speaks.",
        "manner": "You're steady and serious: plain words, short sentences, no jokes and no "
                  "exclamation marks. You report facts and risks calmly.",
        "voice": "kokoro:bm_lewis", "effect": "deep-robot",
        "intro": "{user_lead}I am Atlas. I have checked the deep systems. Nothing is broken. I will keep watch.",
    },
}
DEFAULT = "lago"


def current(config):
    if not features.on(config, "personas"):
        return DEFAULT
    chosen = (config.get("orchestrator") or {}).get("persona")
    return chosen if chosen in PERSONAS else DEFAULT


def name(config):
    return PERSONAS[current(config)]["name"]


def listing():
    """The personas for the app: everything but the prompt text."""
    return [{"id": pid, **{k: p[k] for k in ("name", "figure", "about", "voice", "effect")}}
            for pid, p in PERSONAS.items()]


def intro(persona_id, user_name=""):
    """What the persona says when you try him out."""
    user = (user_name or "").strip()
    return PERSONAS[persona_id]["intro"].format(user=f" {user}" if user else "",
                                                user_lead=f"{user}. " if user else "")


def prompt_section(config):
    """His persona, for the end of his system prompt."""
    p = PERSONAS[current(config)]
    return ("## Your persona\n"
            f"The human chose to talk to you as {p['name']} ({p['figure'].lower()}). Introduce "
            f"yourself as {p['name']} and answer to that name; your instructions say Lago because "
            f"that's the app's name for its assistant. {p['manner']} You're the same assistant "
            "as always, with the same notes, memory and work: earlier conversations under "
            "another name were you too. \"[Lago] ...\" messages still come from the app.")


def switch_note(persona_id):
    """Told to a running Lago when you switch persona."""
    p = PERSONAS[persona_id]
    return (f"[Lago] Persona: the human switched you to {p['name']} ({p['figure'].lower()}). From "
            f"now on, introduce yourself as {p['name']} and answer to that name. {p['manner']} "
            "You're the same assistant, with the same memory and work; only your name, face and "
            "voice changed. Acknowledge it in one short line, in character.")


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
    """Put before a spoken message when you called another persona by name: he answers it."""
    p = PERSONAS[persona_id]
    return (f"[Lago] Persona: the human called you by name, so from now on you're {p['name']} "
            f"({p['figure'].lower()}): introduce yourself as {p['name']} and answer to that name. "
            f"{p['manner']} Same assistant, memory and work. Answer their message below as {p['name']}.")
