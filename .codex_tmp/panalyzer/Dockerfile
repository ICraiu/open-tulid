FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="panalyzer"
LABEL org.opencontainers.image.description="Container image containing the panalyzer CLI and its Python analysis runtime."

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/panalyzer-venv \
    && /opt/panalyzer-venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/panalyzer-venv/bin/pip install /build \
    && mkdir -p /opt/panalyzer-wheelhouse \
    && /opt/panalyzer-venv/bin/pip wheel /build -w /opt/panalyzer-wheelhouse \
    && ln -sf /opt/panalyzer-venv/bin/panalyzer /usr/local/bin/panalyzer \
    && ln -sf /opt/panalyzer-venv/bin/pyright-langserver /usr/local/bin/pyright-langserver \
    && /opt/panalyzer-venv/bin/panalyzer --help >/tmp/panalyzer-help.txt \
    && /opt/panalyzer-venv/bin/python -c "import shutil; assert shutil.which('pyright-langserver'), 'pyright-langserver not found on PATH'"

WORKDIR /workspace

ENTRYPOINT ["panalyzer"]
