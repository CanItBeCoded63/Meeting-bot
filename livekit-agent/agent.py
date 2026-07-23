"""
LiveKit Voice Agent — Main Entrypoint
======================================
Run this file to start the agent:
    uv run python agent.py dev       # development with hot-reload
    uv run python agent.py console   # terminal-based testing (no mic needed)
    uv run python agent.py start     # production mode
"""

import logging
import os
import sys

# Force Windows console to support UTF-8
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

# Load environment variables BEFORE importing Langfuse so it picks up keys
load_dotenv(".env")

# ── Langfuse v4 Observability ─────────────────────────────────────────────────
from langfuse import observe, get_client

lf = get_client()  # Reads LANGFUSE_SECRET_KEY / PUBLIC_KEY / BASE_URL from env

from datetime import datetime

from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    JobProcess,
    RoomOutputOptions,
    RunContext,
    WorkerOptions,
    cli,
)
from livekit.agents.llm import function_tool
from livekit.plugins import deepgram, openai, silero

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("livekit-agent")


def prewarm(proc: JobProcess):
    """Pre-load the VAD model so the first session starts instantly."""
    proc.userdata["vad"] = silero.VAD.load()


# ── Agent ────────────────────────────────────────────────────────────────────

class Assistant(Agent):
    """General-purpose voice AI assistant."""

    def __init__(self):
        super().__init__(
            instructions="""You are a helpful and friendly voice AI assistant.
            Speak clearly and naturally, as if having a phone conversation.
            Be concise but warm in your responses.
            If you don't know something, be honest about it.""",
        )

    # ── Tools ────────────────────────────────────────────────────────────────

    @observe(name="tool.get_current_date_and_time", as_type="tool")
    @function_tool
    async def get_current_date_and_time(self, context: RunContext) -> str:
        """Return the current date and time."""
        now = datetime.now().strftime("%B %d, %Y at %I:%M %p")
        lf.update_current_span(metadata={"tool": "get_current_date_and_time", "datetime": now})
        return f"The current date and time is {now}."

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_enter(self):
        """Greet the user when the session starts."""
        logger.info("Agent session started")
        await self.session.generate_reply(
            instructions="Greet the user warmly and ask how you can help them today."
        )

    async def on_exit(self):
        """Clean up when the session ends."""
        logger.info("Agent session ended")


# ── Entrypoint ────────────────────────────────────────────────────────────────

@observe(name="agent.session", as_type="agent")
async def entrypoint(ctx: agents.JobContext):
    """
    Main entry point for the LiveKit agent worker.
    Each room connection creates one Langfuse trace so you can see the full
    session (tools called, LLM turns, latencies) in the Langfuse dashboard.
    """
    logger.info(f"Agent started in room: {ctx.room.name}")

    # Enrich the root Langfuse trace with session metadata (v4 API)
    lf.update_current_span(
        metadata={
            "room_name": ctx.room.name,
            "llm_model": os.getenv("LLM_CHOICE", "gpt-4.1-mini"),
            "tags": ["livekit", "voice-agent", "deepgram", "openai"],
        }
    )

    # Flush pending Langfuse events when the room disconnects
    @ctx.room.on("disconnected")
    def on_disconnect(_reason):
        logger.info("Room disconnected — flushing Langfuse traces")
        lf.flush()

    # Build the voice pipeline
    session = AgentSession(
        stt=deepgram.STT(model="nova-2", language="en"),
        llm=openai.LLM(
            model=os.getenv("LLM_CHOICE", "gpt-4.1-mini"),
            temperature=0.7,
        ),
        tts=openai.TTS(voice="echo", speed=1.0),
        vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
    )

    # Start the session
    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_output_options=RoomOutputOptions(transcription_enabled=True),
    )

    # ── Session event hooks ──────────────────────────────────────────────────
    @session.on("agent_state_changed")
    def on_state_changed(ev):
        logger.info(f"State: {ev.old_state} -> {ev.new_state}")

    @session.on("user_started_speaking")
    def on_user_speaking():
        logger.debug("User started speaking")

    @session.on("user_stopped_speaking")
    def on_user_stopped():
        logger.debug("User stopped speaking")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
