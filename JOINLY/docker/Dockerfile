# Stage 1: Build environment
FROM python:3.12-slim AS builder

ENV UV_LINK_MODE="copy" \
    UV_COMPILE_BYTECODE=1 \
    UV_HTTP_TIMEOUT=300

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy lockfiles and install dependencies (no BuildKit cache mounts needed)
COPY uv.lock pyproject.toml ./
COPY client/pyproject.toml ./client/
COPY common/pyproject.toml ./common/
RUN uv sync --locked --no-install-project --no-install-workspace --no-dev --no-editable

# Copy scripts and download ML assets (playwright + silero VAD)
# Whisper and Kokoro skipped — Deepgram handles STT and TTS
COPY scripts ./scripts
RUN PATH="/app/.venv/bin:${PATH}" \
    python scripts/download_assets.py --assets playwright silero

# Copy full source and install application
COPY . /app

# Install application into .venv (non-editable mode)
RUN uv sync --locked --no-dev --no-editable

# Stage 2: Runtime image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    JOINLY_SERVER_HOST="0.0.0.0" \
    JOINLY_SERVER_PORT=8000 \
    JOINLY_LOGGING_PLAIN=1

EXPOSE 8000

# Install system dependencies (audio, video, browser)
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

# Copy virtual environment and cached assets from builder
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /root/.cache /home/app/.cache

# Copy startup script from builder (scripts/ is not in .dockerignore)
COPY --from=builder --chown=app:app /app/scripts/copy-profile.sh /app/scripts/copy-profile.sh
RUN sed -i 's/\r$//' /app/scripts/copy-profile.sh && chmod +x /app/scripts/copy-profile.sh

# Set user and working directory
USER app
WORKDIR /app

ENTRYPOINT ["/app/.venv/bin/joinly"]
