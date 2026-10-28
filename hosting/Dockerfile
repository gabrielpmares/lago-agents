# One customer's Lago Agents, hosted (see README.md here). Build from the repository's root:
#   docker build -f hosting/Dockerfile -t lago-agents .
# Everything of the customer's is in /data, their volume: the app in /app is the same for all.
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-cryptography tmux git gh nodejs npm ca-certificates curl procps less \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

RUN useradd --uid 1000 --home-dir /data/home --no-create-home --shell /bin/bash lago \
    && mkdir -p /data && chown lago:lago /data
COPY --chown=root:root lago /app/lago
COPY --chown=root:root lago-agents /app/lago-agents
COPY --chown=root:root hosting/entrypoint.sh /app/hosting/entrypoint.sh

ENV LAGO_HOSTED=1 \
    LAGO_PORT=8080 \
    HOME=/data/home \
    LAGO_AGENTS_HOME=/data/config \
    XDG_CONFIG_HOME=/data/home/.config \
    XDG_STATE_HOME=/data/state \
    LANG=C.UTF-8
USER lago
WORKDIR /data
VOLUME /data
EXPOSE 8080
ENTRYPOINT ["/app/hosting/entrypoint.sh"]
