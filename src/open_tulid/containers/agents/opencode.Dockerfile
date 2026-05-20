FROM node:24-bookworm-slim

LABEL org.opencontainers.image.title="open-tulid opencode agent"
LABEL org.opencontainers.image.description="Agent image containing the OpenCode CLI."

ENV DEBIAN_FRONTEND=noninteractive
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
ENV OPENCODE_DISABLE_AUTOUPDATE=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        openssh-client \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g opencode-ai \
    && opencode --version

WORKDIR /workspace/project

ENTRYPOINT ["opencode"]
