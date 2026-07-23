import os
import sys
import json
import logging
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

# Force Windows console to support UTF-8 to prevent internal plugin logging crashes
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

from datetime import datetime
from dotenv import load_dotenv
load_dotenv(".env")

# ── LangWatch Observability (set up before anything else) ─────────────────────
import langwatch
from langwatch.attributes import AttributeKey

langwatch.setup(
    api_key=os.getenv("LANGWATCH_API_KEY"),
    endpoint_url=os.getenv("LANGWATCH_ENDPOINT", "https://app.langwatch.ai"),
    base_attributes={
        AttributeKey.ServiceName: "sarvam-voice-agent",
        AttributeKey.ServiceVersion: "1.0.0",
    },
)

# ── LiveKit Imports ────────────────────────────────────────────────────────────
from livekit import agents
from livekit.agents import Agent, AgentSession, RunContext, WorkerOptions
from livekit.agents.llm import function_tool
from livekit.plugins import openai, sarvam, deepgram, silero
import openai as openai_sdk
import httpx
import aiohttp

# Set up logging
logger = logging.getLogger("sarvam-livekit-agent")
logger.setLevel(logging.INFO)

# ── Project Configuration ─────────────────────────────────────────────────────
CONFIG_FILE = Path(__file__).parent / "projects_config.json"


@dataclass
class ProjectConfig:
    """Resolved STT/TTS/LLM settings for a single project."""
    project_id: str = "default"
    stt_provider: str = "sarvam"
    stt_model: str = "saaras:v3"
    stt_language: str = "hi-IN"
    tts_provider: str = "sarvam"
    tts_model: str = "bulbul:v3"
    tts_language: str = "hi-IN"
    tts_speaker: Optional[str] = "shubh"
    llm_model: str = "openai/gpt-4o-mini"


def load_project_config() -> ProjectConfig:
    """
    Resolve the project config. Priority order:
      1. Explicit env vars (STT_PROVIDER, TTS_PROVIDER, etc.) -- highest
      2. PROJECT_ID lookup in projects_config.json
      3. Hard-coded defaults -- lowest
    """
    pid = os.getenv("PROJECT_ID", "default")
    cfg = {}

    # Try loading from JSON
    if CONFIG_FILE.exists():
        try:
            all_projects = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if pid in all_projects:
                cfg = all_projects[pid]
                logger.info(f"Loaded config for project '{pid}'")
            elif pid != "default":
                logger.warning(f"PROJECT_ID '{pid}' not found in {CONFIG_FILE.name} -- using defaults")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse {CONFIG_FILE.name}: {e}")
    else:
        logger.warning(f"{CONFIG_FILE.name} not found -- using defaults")

    stt = cfg.get("stt", {})
    tts = cfg.get("tts", {})
    llm = cfg.get("llm", {})

    resolved = ProjectConfig(
        project_id=pid,
        # STT
        stt_provider=os.getenv("STT_PROVIDER", stt.get("provider", "sarvam")).lower(),
        stt_model=os.getenv("STT_MODEL", stt.get("model", "saaras:v3")),
        stt_language=os.getenv("STT_LANGUAGE", stt.get("language", "hi-IN")),
        # TTS
        tts_provider=os.getenv("TTS_PROVIDER", tts.get("provider", "sarvam")).lower(),
        tts_model=os.getenv("TTS_MODEL", tts.get("model", "bulbul:v3")),
        tts_language=os.getenv("TTS_LANGUAGE", tts.get("language", "hi-IN")),
        tts_speaker=os.getenv("TTS_SPEAKER", tts.get("speaker", "shubh")),
        # LLM
        llm_model=os.getenv("LLM_CHOICE", llm.get("model", "openai/gpt-4o-mini")),
    )

    # Validate providers
    allowed_stt = {"sarvam", "deepgram"}
    allowed_tts = {"sarvam", "deepgram"}
    if resolved.stt_provider not in allowed_stt:
        logger.warning(f"Unknown STT_PROVIDER '{resolved.stt_provider}' -- falling back to sarvam")
        resolved.stt_provider = "sarvam"
    if resolved.tts_provider not in allowed_tts:
        logger.warning(f"Unknown TTS_PROVIDER '{resolved.tts_provider}' -- falling back to sarvam")
        resolved.tts_provider = "sarvam"

    logger.info(
        f"Project '{resolved.project_id}' config: "
        f"STT={resolved.stt_provider}/{resolved.stt_model}, "
        f"TTS={resolved.tts_provider}/{resolved.tts_model}, "
        f"LLM={resolved.llm_model}"
    )
    return resolved


# ── STT / TTS Builders ────────────────────────────────────────────────────────

def build_stt(cfg: ProjectConfig):
    """Create the STT instance based on project config."""
    if cfg.stt_provider == "deepgram":
        return deepgram.STT(model=cfg.stt_model, language=cfg.stt_language)
    # Default: Sarvam
    return sarvam.STT(language=cfg.stt_language, model=cfg.stt_model)


def build_tts(cfg: ProjectConfig):
    """Create the TTS instance based on project config."""
    if cfg.tts_provider == "deepgram":
        return deepgram.TTS(model=cfg.tts_model)
    # Default: Sarvam (with extended timeout for non-streaming model)
    return sarvam.TTS(
        target_language_code=cfg.tts_language,
        model=cfg.tts_model,
        speaker=cfg.tts_speaker,
        http_session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60.0)),
    )


# ── Agent ─────────────────────────────────────────────────────────────────────

class Assistant(Agent):
    """Voice assistant with configurable STT/TTS providers."""

    def __init__(self, project_cfg: ProjectConfig):
        # Create custom OpenAI client with extended timeout (OpenRouter can be slow)
        custom_client = openai_sdk.AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url="https://openrouter.ai/api/v1",
            timeout=httpx.Timeout(60.0),
        )

        super().__init__(
            instructions="""
                You are a helpful and friendly voice assistant with Airbnb booking capabilities.
                Be friendly, concise, and conversational.
                Speak naturally in Hindi as if you're having a real conversation.
            """,
            stt=build_stt(project_cfg),
            llm=openai.LLM(model=project_cfg.llm_model, client=custom_client),
            tts=build_tts(project_cfg),
            vad=silero.VAD.load(),
        )

        self.project_cfg = project_cfg
        self.bookings = []

    @langwatch.span(type="tool", name="tool.get_current_date_and_time")
    @function_tool
    async def get_current_date_and_time(self, context: RunContext) -> str:
        """Get the current date and time."""
        current_datetime = datetime.now().strftime("%B %d, %Y at %I:%M %p")
        return f"The current date and time is {current_datetime}."

    @langwatch.span(type="tool", name="tool.search_airbnbs")
    @function_tool
    async def search_airbnbs(self, context: RunContext, city: str) -> str:
        """Search for available Airbnbs in a city.

        Args:
            city (str): The city to search for Airbnbs in.
        """
        # Mock implementation
        airbnbs = [
            {"id": "1", "name": "Cozy cabin", "price": 100},
            {"id": "2", "name": "Luxury apartment", "price": 250},
        ]

        result = f"Here are some available Airbnbs in {city}:\n\n"
        for airbnb in airbnbs:
            result += f"- {airbnb['name']} (${airbnb['price']}/night) [ID: {airbnb['id']}]\n"

        return result

    @langwatch.span(type="tool", name="tool.book_airbnb")
    @function_tool
    async def book_airbnb(self, context: RunContext, airbnb_id: str, guest_name: str, check_in_date: str, check_out_date: str) -> str:
        """Book an Airbnb.

        Args:
            airbnb_id (str): The ID of the Airbnb to book.
            guest_name (str): The name of the guest booking the Airbnb.
            check_in_date (str): The check-in date (YYYY-MM-DD).
            check_out_date (str): The check-out date (YYYY-MM-DD).
        """
        booking = {
            "airbnb_id": airbnb_id,
            "guest_name": guest_name,
            "check_in": check_in_date,
            "check_out": check_out_date,
            "total_price": 500,
        }
        self.bookings.append(booking)

        result = f"Successfully booked Airbnb {airbnb_id} for {guest_name}!\n\n"
        result += f"Check-in: {booking['check_in']}\n"
        result += f"Check-out: {booking['check_out']}\n"
        result += f"Total Price: ${booking['total_price']}\n\n"
        result += f"You'll receive a confirmation email shortly. Have a great stay!"

        return result

    async def on_enter(self):
        """Called when user joins - agent starts the conversation."""
        logger.info("Agent entered the room. Starting conversation...")
        self.session.generate_reply()

    def on_user_speech_committed(self, msg):
        logger.info(f"User speech committed: {msg}")

    def on_agent_speech_started(self):
        logger.info("Agent speech started.")

    def on_agent_speech_stopped(self):
        logger.info("Agent speech stopped.")


# ── Entrypoint ────────────────────────────────────────────────────────────────

@langwatch.trace(name="voice-agent-session")
async def entrypoint(ctx: agents.JobContext):
    """Entry point for the agent. Each room session is one LangWatch trace."""
    logger.info(f"User connected to room: {ctx.room.name}")

    # Connect to the room first so we can read participant metadata
    await ctx.connect()

    # Try to read provider config from participant metadata (set by web UI)
    project_cfg = None
    participant = await ctx.wait_for_participant()

    if participant and participant.metadata:
        try:
            meta = json.loads(participant.metadata)
            if meta.get("source") == "web-ui" and meta.get("stt_provider"):
                logger.info(f"Web UI session detected — using metadata config: {meta}")
                project_cfg = ProjectConfig(
                    project_id="web-session",
                    stt_provider=meta.get("stt_provider", "sarvam"),
                    stt_model=meta.get("stt_model", "saaras:v3"),
                    stt_language=meta.get("stt_language", "hi-IN"),
                    tts_provider=meta.get("tts_provider", "sarvam"),
                    tts_model=meta.get("tts_model", "bulbul:v3"),
                    tts_language=meta.get("tts_language", "hi-IN"),
                    tts_speaker=meta.get("tts_speaker", "shubh"),
                    llm_model=os.getenv("LLM_CHOICE", "openai/gpt-4o-mini"),
                )
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(f"Could not parse participant metadata: {e}")

    # Fall back to CLI / PROJECT_ID config if no web metadata
    if project_cfg is None:
        project_cfg = load_project_config()

    # Attach project + provider metadata to LangWatch trace
    langwatch.get_current_trace().update(
        metadata={
            "room_name": ctx.room.name,
            "project_id": project_cfg.project_id,
            "stt_provider": project_cfg.stt_provider,
            "stt_model": project_cfg.stt_model,
            "tts_provider": project_cfg.tts_provider,
            "tts_model": project_cfg.tts_model,
            "llm_model": project_cfg.llm_model,
            "agent_version": "1.0.0",
        }
    )

    # Configure the voice pipeline
    session = AgentSession(
        vad=silero.VAD.load(),
        min_endpointing_delay=0.07,
    )

    # Start the session with the project-configured assistant
    await session.start(
        room=ctx.room,
        agent=Assistant(project_cfg),
    )


if __name__ == "__main__":
    from livekit.agents import cli
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))