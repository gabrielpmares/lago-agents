"""Talking to Lago in another language: "fala comigo em português".

Kokoro (the offline voices, voice.py) speaks Portuguese, Spanish, French, Italian, Hindi,
Japanese and Chinese besides English. With a language chosen (config.json "voice" →
"language", set by Lago's speak_language tool or in his settings), every persona keeps his
effect, and his voice becomes the closest one in that language (voice_for): the same
speaker where Kokoro has him (Santa, Alex, Dora), else one of the same sex. Lago writes his
replies in it too (prompt_section), and the app's own phrases are translated here.
Speech recognition (Whisper) already understands any of them.
"""

# code → its BCP 47 tag (the phone's speech recognition listens in it), name, Kokoro's
# phonemizer language (eSpeak's code), and its voices in voices-v1.0.bin, men then women,
# the closest to the English persona voices first.
LANGUAGES = {
    "pt-pt": {"tag": "pt-PT", "name": "Portuguese (Portugal)", "native": "Português (Portugal)", "kokoro": "pt",
              "men": ["pm_santa", "pm_alex"], "women": ["pf_dora"]},
    "pt-br": {"tag": "pt-BR", "name": "Portuguese (Brazil)", "native": "Português (Brasil)", "kokoro": "pt-br",
              "men": ["pm_santa", "pm_alex"], "women": ["pf_dora"]},
    "es": {"tag": "es-ES", "name": "Spanish", "native": "Español", "kokoro": "es",
           "men": ["em_santa", "em_alex"], "women": ["ef_dora"]},
    "fr": {"tag": "fr-FR", "name": "French", "native": "Français", "kokoro": "fr-fr",
           "men": [], "women": ["ff_siwis"]},
    "it": {"tag": "it-IT", "name": "Italian", "native": "Italiano", "kokoro": "it",
           "men": ["im_nicola"], "women": ["if_sara"]},
    "hi": {"tag": "hi-IN", "name": "Hindi", "native": "हिन्दी", "kokoro": "hi",
           "men": ["hm_omega", "hm_psi"], "women": ["hf_alpha", "hf_beta"]},
    "ja": {"tag": "ja-JP", "name": "Japanese", "native": "日本語", "kokoro": "ja",
           "men": ["jm_kumo"], "women": ["jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro"]},
    "zh": {"tag": "zh-CN", "name": "Chinese (Mandarin)", "native": "中文", "kokoro": "cmn",
           "men": ["zm_yunxi", "zm_yunjian", "zm_yunyang", "zm_yunxia"],
           "women": ["zf_xiaoxiao", "zf_xiaobei", "zf_xiaoni", "zf_xiaoyi"]},
}
ENGLISH = ""
# The online and system voices, by sex (Kokoro's ids say it: am_ a man, af_ a woman).
WOMEN = ("edge:en-US-AvaMultilingualNeural", "say:Samantha", "say:Karen")
# Speakers Kokoro has in English and in other languages: they stay with whoever had them.
ENGLISH_SPEAKERS = ("santa",)

# voice.py's phrases, in each language. "hey" stands in for your name when there's none;
# a greeting is "<part of day><sep><your name>" then the rest.
TEXTS = {
    "pt-pt": {
        "hey": "Olá", "sep": ", ", "day": ("Bom dia", "Boa tarde", "Boa noite"),
        "greeting": "{hello}. Em que vamos trabalhar hoje?",
        "intro": "Olá{user}, eu sou {o} {name}! Diz-me do que precisas, e eu ponho a equipa a trabalhar.",
        "solved": "{name}, a tarefa em {project} está concluída. Podes fechar a sessão.",
        "done": "{name}, uma etapa do trabalho em {project} está feita.",
        "failed": "{name}, a tarefa em {project} falhou.",
        "question": "{name}, {project} tem uma pergunta para ti.",
        "gate": "{name}, {project} tem um resultado para verificares.",
        "approval": "{name}, {project} tem um plano à espera da tua aprovação.",
        "blocked": "{name}, o trabalho em {project} está bloqueado.",
        "permission": "{name}, {project} precisa da tua autorização.",
        "waiting": "{name}, {project} está à tua espera.",
    },
    "pt-br": {
        "hey": "Oi", "sep": ", ", "day": ("Bom dia", "Boa tarde", "Boa noite"),
        "greeting": "{hello}. No que vamos trabalhar hoje?",
        "intro": "Oi{user}, eu sou {o} {name}! Me diz do que você precisa, e eu coloco a equipe para trabalhar.",
        "solved": "{name}, a tarefa em {project} está concluída. Você pode fechar a sessão.",
        "done": "{name}, uma etapa do trabalho em {project} está pronta.",
        "failed": "{name}, a tarefa em {project} falhou.",
        "question": "{name}, {project} tem uma pergunta para você.",
        "gate": "{name}, {project} tem um resultado para você conferir.",
        "approval": "{name}, {project} tem um plano esperando a sua aprovação.",
        "blocked": "{name}, o trabalho em {project} está bloqueado.",
        "permission": "{name}, {project} precisa da sua permissão.",
        "waiting": "{name}, {project} está esperando por você.",
    },
    "es": {
        "hey": "Oye", "sep": ", ", "day": ("Buenos días", "Buenas tardes", "Buenas noches"),
        "greeting": "{hello}. ¿En qué trabajamos hoy?",
        "intro": "¡Hola{user}, soy {name}! Dime qué necesitas y pongo al equipo a trabajar.",
        "solved": "{name}, la tarea de {project} está terminada. Puedes cerrar la sesión.",
        "done": "{name}, una etapa del trabajo en {project} está lista.",
        "failed": "{name}, la tarea de {project} ha fallado.",
        "question": "{name}, {project} tiene una pregunta para ti.",
        "gate": "{name}, {project} tiene un resultado para que lo revises.",
        "approval": "{name}, {project} tiene un plan esperando tu aprobación.",
        "blocked": "{name}, el trabajo en {project} está bloqueado.",
        "permission": "{name}, {project} necesita tu permiso.",
        "waiting": "{name}, {project} te está esperando.",
    },
    "fr": {
        "hey": "Dis", "sep": ", ", "day": ("Bonjour", "Bon après-midi", "Bonsoir"),
        "greeting": "{hello}. Sur quoi on travaille aujourd'hui ?",
        "intro": "Salut{user}, je suis {name} ! Dis-moi ce dont tu as besoin, et je mets l'équipe au travail.",
        "solved": "{name}, la tâche sur {project} est terminée. Tu peux fermer la session.",
        "done": "{name}, une étape du travail sur {project} est faite.",
        "failed": "{name}, la tâche sur {project} a échoué.",
        "question": "{name}, {project} a une question pour toi.",
        "gate": "{name}, {project} a un résultat à vérifier.",
        "approval": "{name}, {project} a un plan qui attend ton accord.",
        "blocked": "{name}, le travail sur {project} est bloqué.",
        "permission": "{name}, {project} a besoin de ta permission.",
        "waiting": "{name}, {project} t'attend.",
    },
    "it": {
        "hey": "Ehi", "sep": ", ", "day": ("Buongiorno", "Buon pomeriggio", "Buonasera"),
        "greeting": "{hello}. Su cosa lavoriamo oggi?",
        "intro": "Ciao{user}, sono {name}! Dimmi cosa ti serve e metto la squadra al lavoro.",
        "solved": "{name}, il compito su {project} è finito. Puoi chiudere la sessione.",
        "done": "{name}, una fase del lavoro su {project} è fatta.",
        "failed": "{name}, il compito su {project} non è riuscito.",
        "question": "{name}, {project} ha una domanda per te.",
        "gate": "{name}, {project} ha un risultato da controllare.",
        "approval": "{name}, {project} ha un piano che aspetta la tua approvazione.",
        "blocked": "{name}, il lavoro su {project} è bloccato.",
        "permission": "{name}, {project} ha bisogno del tuo permesso.",
        "waiting": "{name}, {project} ti sta aspettando.",
    },
    "hi": {
        "hey": "सुनिए", "sep": ", ", "day": ("सुप्रभात", "नमस्ते", "शुभ संध्या"),
        "greeting": "{hello}। आज हम किस पर काम करेंगे?",
        "intro": "नमस्ते{user}, मैं {name} हूँ! बताइए आपको क्या चाहिए, और मैं टीम को काम पर लगा {dunga}।",
        "solved": "{name}, {project} का काम पूरा हो गया है। आप सेशन बंद कर सकते हैं।",
        "done": "{name}, {project} के काम का एक चरण पूरा हुआ।",
        "failed": "{name}, {project} का काम विफल हो गया।",
        "question": "{name}, {project} का आपसे एक सवाल है।",
        "gate": "{name}, {project} का एक नतीजा आपके देखने के लिए तैयार है।",
        "approval": "{name}, {project} की एक योजना आपकी मंज़ूरी का इंतज़ार कर रही है।",
        "blocked": "{name}, {project} का काम रुका हुआ है।",
        "permission": "{name}, {project} को आपकी अनुमति चाहिए।",
        "waiting": "{name}, {project} आपका इंतज़ार कर रहा है।",
    },
    "ja": {
        "hey": "ねえ", "sep": "、", "day": ("おはようございます", "こんにちは", "こんばんは"),
        "greeting": "{hello}。今日は何をしましょうか？",
        "intro": "こんにちは{user}、{name}です！何が必要か教えてください。チームを動かします。",
        "solved": "{name}、{project}のタスクが完了しました。セッションを閉じても大丈夫です。",
        "done": "{name}、{project}の作業の一段階が終わりました。",
        "failed": "{name}、{project}のタスクが失敗しました。",
        "question": "{name}、{project}から質問があります。",
        "gate": "{name}、{project}に確認してほしい結果があります。",
        "approval": "{name}、{project}の計画があなたの承認を待っています。",
        "blocked": "{name}、{project}の作業が止まっています。",
        "permission": "{name}、{project}があなたの許可を必要としています。",
        "waiting": "{name}、{project}があなたを待っています。",
    },
    "zh": {
        "hey": "嘿", "sep": "，", "day": ("早上好", "下午好", "晚上好"),
        "greeting": "{hello}。今天我们做什么？",
        "intro": "你好{user}，我是{name}！告诉我你需要什么，我让团队开始干活。",
        "solved": "{name}，{project}的任务已经完成了。你可以关闭这个会话。",
        "done": "{name}，{project}的一个工作阶段完成了。",
        "failed": "{name}，{project}的任务失败了。",
        "question": "{name}，{project}有个问题要问你。",
        "gate": "{name}，{project}有一个结果等你检查。",
        "approval": "{name}，{project}有一个计划等你批准。",
        "blocked": "{name}，{project}的工作被卡住了。",
        "permission": "{name}，{project}需要你的许可。",
        "waiting": "{name}，{project}在等你。",
    },
}


def chosen(settings):
    """The language his voice speaks (a LANGUAGES code), or ENGLISH."""
    code = (settings or {}).get("language") or ENGLISH
    return code if code in LANGUAGES else ENGLISH


def tag(code):
    """The BCP 47 tag the phone listens in, "" for English (the phone's own language)."""
    return LANGUAGES[code]["tag"] if code else ""


def listing():
    """The languages for the app, English first."""
    return [{"id": ENGLISH, "label": "English"}] + [
        {"id": code, "label": f"{lang['native']} ({lang['name']})" if lang["native"] != lang["name"]
         else lang["name"]} for code, lang in LANGUAGES.items()]


def find(text):
    """The language code for a name the human or Lago used ("Portuguese", "português", "pt",
    "Brazilian Portuguese"), ENGLISH for English, or None."""
    said = " ".join((text or "").lower().replace("_", "-").split())
    if said in ("", "en", "english", "inglês", "ingles", "inglés", "anglais", "inglese"):
        return ENGLISH
    if said in LANGUAGES:
        return said
    if said in ("pt", "portuguese", "português", "portugues"):
        return "pt-pt"
    for code, lang in LANGUAGES.items():
        if said in (lang["name"].lower(), lang["native"].lower()):
            return code
    if "brazil" in said or "brasil" in said:
        return "pt-br"
    if "mandarin" in said:
        return "zh"
    for code, lang in LANGUAGES.items():
        first = lang["name"].split(" (")[0].lower()
        if first in said or lang["native"].split(" (")[0].lower() in said:
            return code
    return None


def woman(voice_id):
    """Whether `voice_id` is a woman's voice."""
    if voice_id.startswith("kokoro:"):
        return voice_id[len("kokoro:") + 1:].startswith("f")
    return voice_id in WOMEN


def voice_for(voice_id, code):
    """The Kokoro voice that speaks `code` in place of `voice_id`, as "kokoro:<voice>@<eSpeak
    language>" (speak.py reads the language from after the @): the same speaker if Kokoro
    has him in that language, else the first of the same sex, else the first there is."""
    lang = LANGUAGES[code]
    speaker = voice_id.rpartition("_")[2].lower()
    same_sex, other = ((lang["women"], lang["men"]) if woman(voice_id) else (lang["men"], lang["women"]))
    named = [v for v in same_sex + other if v.endswith(f"_{speaker}")]
    # Santa is Lago's own voice in English too: Chip and Atlas take another, where there is one.
    unclaimed = [v for v in same_sex if not v.endswith(tuple(f"_{s}" for s in ENGLISH_SPEAKERS))]
    return f"kokoro:{(named or unclaimed or same_sex or other)[0]}@{lang['kokoro']}"


def text(code, key, woman=False, **values):
    """One of the app's phrases (TEXTS) in `code`, or None when it isn't translated. A
    `woman` speaking takes the feminine words where the language marks it."""
    template = TEXTS.get(code, {}).get(key)
    endings = {"o": "a" if woman else "o", "dunga": "दूँगी" if woman else "दूँगा"}
    return template.format(**endings, **values) if template else None


def greeting(code, part, user_name):
    """Good morning (part 0), afternoon (1) or evening (2), with your name if there's one."""
    texts = TEXTS[code]
    hello = texts["day"][part] + (f"{texts['sep']}{user_name}" if user_name else "")
    return texts["greeting"].format(hello=hello)


def prompt_section(settings):
    """For his system prompt: the language to talk in, when it isn't English."""
    code = chosen(settings)
    if not code:
        return ""
    return ("## Language\n" + note(code, switched=False))


def note(code, switched=True):
    """What he's told about speaking `code` (ENGLISH: back to English)."""
    if not code:
        return ("Your voice speaks English again. Write your replies and the 🔊 line in English "
                "from now on." if switched else "")
    lang = LANGUAGES[code]
    return (f"{'Your voice now speaks' if switched else 'The human chose to talk with you in'} "
            f"{lang['name']}. Write your replies and the 🔊 line in {lang['name']}"
            f"{', with European spelling and words (tu, equipa, ecrã)' if code == 'pt-pt' else ''}, "
            "whatever language your instructions, reports and the work sessions use; keep project "
            "names, code and paths as they are. Your persona and its voice effect stay the same. "
            "When they ask for another language or English again, call speak_language.")
