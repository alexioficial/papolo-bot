FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PAPOLO_DB_PATH=/data/papolo.sqlite \
    PAPOLO_WORKSPACE_ROOT=/data/workspaces \
    DEBIAN_FRONTEND=noninteractive \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin

WORKDIR /app

# Base apt deps: git for workspace VCS, curl for installs, build tools for native modules,
# openssh-client + ca-certs for git over https/ssh
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git curl ca-certificates openssh-client \
        build-essential pkg-config \
 && rm -rf /var/lib/apt/lists/*

# Node 20 LTS via NodeSource, then pnpm via corepack
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && corepack enable \
 && corepack prepare pnpm@latest --activate \
 && rm -rf /var/lib/apt/lists/*

# Chrome headless libs + agent-browser CLI + Chrome for Testing, pre-bakeados.
# Sin esto, el smoke test visual re-descarga Chrome (177MB) cada corrida y falla por
# falta de libs del sistema (el entorno runtime es efimero y no siempre tiene apt).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxcb1 libxext6 libx11-6 \
        libx11-xcb1 libxcb-shm0 libxcursor1 libxi6 libgtk-3-0 libpango-1.0-0 \
        libpangocairo-1.0-0 libcairo2 libcairo-gobject2 libgdk-pixbuf-2.0-0 \
        libxrender1 libasound2 libfreetype6 libfontconfig1 libdbus-1-3 libdrm2 \
        libatspi2.0-0 libxshmfence1 libgbm1 libglib2.0-0 \
        fonts-liberation fonts-noto-color-emoji \
 && npm install -g agent-browser \
 && agent-browser install \
 && rm -rf /var/lib/apt/lists/*

# uv (Python package/project manager) — installs to /root/.local/bin, then symlink to /usr/local/bin
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv \
 && ln -s /root/.local/bin/uvx /usr/local/bin/uvx

# Rust toolchain (stable) — system-wide install at CARGO_HOME/RUSTUP_HOME
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain stable --profile minimal --no-modify-path \
 && chmod -R a+w /usr/local/cargo /usr/local/rustup

COPY vendor/papolo /app/vendor/papolo
RUN pip install -e /app/vendor/papolo

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY . /app

RUN mkdir -p /data /data/workspaces

CMD ["python", "bot.py"]
