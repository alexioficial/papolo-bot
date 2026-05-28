FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PAPOLO_DB_PATH=/data/papolo.sqlite \
    PAPOLO_WORKSPACE_ROOT=/data/workspaces

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY vendor/papolo /app/vendor/papolo
RUN pip install -e /app/vendor/papolo

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY . /app

RUN mkdir -p /data

CMD ["python", "bot.py"]
