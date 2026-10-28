# Hosted Lago

One customer's Lago Agents in a container of its own. Everything of theirs lives on the
volume mounted at `/data`; the app in `/app` is the same image for every customer. The plan
this is phase 1 of: "Lago Cloud: hosting plan" (docs).

## Try it on your computer

    docker build -f hosting/Dockerfile -t lago-agents:dev .
    docker run -d --name lago-try -p 127.0.0.1:8080:8080 -v lago-try:/data \
        -e LAGO_VAULT_KEY="$(openssl rand -hex 32)" -e ANTHROPIC_API_KEY=sk-ant-... \
        --memory 2g --cpus 2 --pids-limit 512 --cap-drop ALL --security-opt no-new-privileges \
        lago-agents:dev
    docker logs lago-try        # the link to open, with its one-time secret

Map the container's port 8080 to the same port on your side, or add the address you open
to `LAGO_HOSTS`: Lago answers only the host names it knows.

## What the container is given

| Variable | What it is |
| --- | --- |
| `LAGO_PROXY_SECRET` | Shared with the host's proxy alone. When set, only requests the proxy signed get in (`app_server.py` explains the headers); when not, the one-time link in the log. |
| `LAGO_HOSTS` | The customer's address, as the browser sends it (`alice.example.com`), comma-separated. |
| `LAGO_VAULT_KEY` | Opens the customer's secrets file (`/data/state/lago-agents/vault.enc`). Kept by the host, never on the volume. |
| `ANTHROPIC_API_KEY` | The customer's own key; their agents run on it. |

Inside, `LAGO_HOSTED=1`: no desktop window, no keyring, and no sudo password before agents run
without permission prompts (the customer signed in at the proxy, and the container is the
sandbox).
