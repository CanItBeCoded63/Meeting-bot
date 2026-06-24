# Stage 1: Build environment
FROM python:3.12-slim AS builder

ENV UV_LINK_MODE="copy" \
    UV_COMPILE_BYTECODE=1

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies using uv and lock files
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=client/pyproject.toml,target=client/pyproject.toml \
    --mount=type=bind,source=common/pyproject.toml,target=common/pyproject.toml \
    uv sync --locked --no-install-project --no-install-workspace --no-dev --no-editable

# Copy scripts directory (contains download_assets.py)
COPY scripts /app/scripts

# Run download assets script inside builder stage to download files into /root/.cache
# Note: since scripts/download_assets.py rarely changes, this layer will be heavily cached by Docker.
# Only download playwright (browser) and silero (VAD).
# Whisper and Kokoro are skipped since Deepgram handles STT and TTS.
RUN PATH="/app/.venv/bin:${PATH}" \
    python scripts/download_assets.py \
    --assets playwright silero

# Copy app source code
COPY . /app

# Install application into .venv (non-editable mode)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Stage 2: Runtime image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    JOINLY_SERVER_HOST="0.0.0.0" \
    JOINLY_SERVER_PORT=8000 \
    JOINLY_LOGGING_PLAIN=1

EXPOSE 8000

# Install system dependencies (for audio, video, browser, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxcomposite1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    pulseaudio \
    pulseaudio-utils \
    xvfb \
    x11vnc \
    nodejs \
    npm \
    && npm install -g @dangahagan/weather-mcp tavily-mcp@0.2.2 @notionhq/notion-mcp-server \
    && apt-get purge -y --auto-remove \
    && rm -rf /var/lib/apt/lists/* /tmp/*

# Create non-root user
RUN groupadd --gid 1001 app && \
    useradd --uid 1001 --gid 1001 -m app

# Copy virtual environment from builder and set permissions
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Copy downloaded assets from builder to the runtime user's cache directory
COPY --from=builder --chown=app:app /root/.cache /home/app/.cache

# Set user and working directory
USER app
WORKDIR /app

# Set entrypoint
ENTRYPOINT ["/app/.venv/bin/joinly"]
