FROM node:24-bookworm-slim

LABEL org.opencontainers.image.title="open-tulid codex agent"
LABEL org.opencontainers.image.description="Agent image containing the OpenAI Codex CLI."

ENV DEBIAN_FRONTEND=noninteractive
ENV NPM_CONFIG_UPDATE_NOTIFIER=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        openssh-client \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex \
    && codex --version

WORKDIR /workspace/project

ENTRYPOINT ["codex"]
