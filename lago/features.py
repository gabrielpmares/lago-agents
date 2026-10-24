"""The extras: parts of Lago Agents beyond the basics, each switched on in Settings, Features.

A fresh install starts with the basics only (Lago, tasks, projects, agents, connectors,
history, permissions) and every extra off. A config.json without "features" (one from
before they existed) has every extra on, so an existing setup doesn't lose anything.

Off means gone, not just hidden: its pages and settings leave the app, and what it would
do by itself stops (no speaking, no listening, no phone or browser access, no routines
firing, no budget stops).
"""

import config_store

FEATURES = {
    "voice": ("Voice", "Lago says his replies and task reports aloud, greets you, and can "
                       "listen for \"Hey Lago\"."),
    "personas": ("Personas", "Talk to Chip or Atlas instead of Lago: the same assistant with "
                             "another name, face and voice."),
    "routines": ("Routines", "Recurring jobs, on a schedule or a webhook."),
    "budgets": ("Accounts & Budgets", "Several Claude accounts, and spending limits that stop "
                                      "work when reached."),
    "phone": ("Phone", "The Android app, over Tailscale."),
    "people": ("People", "Share Lago Agents with others on your tailnet, each with a role."),
    "map": ("Live Map", "A live picture of every task and agent at work."),
}


def on(config, feature):
    chosen = config.get("features")
    return True if chosen is None else bool(chosen.get(feature))


def state(config):
    return {fid: on(config, fid) for fid in FEATURES}


def listing(config):
    """For the Features settings: [{"id", "name", "about", "on"}]."""
    return [{"id": fid, "name": name, "about": about, "on": on(config, fid)}
            for fid, (name, about) in FEATURES.items()]


def switch(feature, value):
    """Switch an extra on or off (the caller stops what it runs). Returns the saved config."""
    if feature not in FEATURES:
        raise ValueError(f"There's no feature {feature!r}")
    config = config_store.without_swarm(config_store.load())
    config["features"] = {**state(config), feature: bool(value)}
    config_store.save(config, source="features")
    return config
