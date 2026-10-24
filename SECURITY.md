# Security

## Reporting a problem

Please report security problems privately, through GitHub's **Report a vulnerability** button
on this repository's **Security** tab. Don't open a public issue for them.

Say what you found, how to reproduce it, and what someone could do with it. You'll get an
answer within a week. Once it's fixed, the fix and its release notes credit you, unless you'd
rather not be named.

Only the latest version on `main` gets fixes.

## What Lago Agents protects, and what it doesn't

Lago Agents runs AI agents on your computer, as your user. Read this before you give it work
that matters.

**Agents can do what you can do.** Agents are Claude Code or Codex sessions running as your
Linux user, in your projects' folders. They ask before shell commands and other tools unless
a standing rule (Permissions) or **Skip permissions** says otherwise. Those prompts and rules
are guard rails, not a sandbox: an agent you allow to run commands can read anything your
user can. Turn on Skip permissions only for work you would let it do unattended.

**Secrets.** Secrets you give agents live in your desktop keyring (libsecret), never in Lago's
files. They reach an agent's process when it starts, and each hand-out is logged. Before
anything an agent prints is shown in the app, on the phone or in a browser, known secrets and
common key shapes are replaced with `[hidden]` (`lago/redact.py`). This catches the usual
leaks; an agent that deliberately rewrites a secret can still get it past.

**Budgets.** Spending limits (Accounts & Budgets) stop agents between tool calls. A single
call that's already running finishes first.

**Agents built from GitHub repos.** A drafted agent installs nothing until you approve it.
Its files are fingerprinted when it's submitted, and a draft changed after that is refused.
Its install script runs in a bubblewrap sandbox (`desktop.sandbox_argv`): it sees the
system's programs, the network and its own folder under `~/lago-work/tools`, and none of
your files, keys or environment. Without bubblewrap installed, the script runs unsandboxed
(with none of your environment) and its log says so. Install `bubblewrap` to close that
gap. What the installed agent does afterwards is covered by the permission prompts, like
any other agent.

**The phone app** connects over Tailscale only, with TLS pinned to this computer's
certificate at pairing, a one-time pairing code, a device key in the Android Keystore and
your phone password. The README's Phone section has the details.

**Browser access for a company** (People) is off by default.
- Over your tailnet, Tailscale tells Lago who each visitor is, and each person's role is
  checked on every call against the project the call touches.
- Behind a separate company tailnet, Lago listens on a socket in its state folder that only
  your user can open, so other people's programs on a shared computer can't reach it or pose
  as a visitor.
- In both cases, anyone who can act as your Linux user counts as the owner.

**Your data** lives in `~/.config/lago-agents` (config, agents, skills) and
`~/.local/state/lago-agents` (the hub database, logs, the phone's certificate). Both folders
are private to your user. Lago keeps a record of the agents' tool calls for three days, for
the Live Map.
