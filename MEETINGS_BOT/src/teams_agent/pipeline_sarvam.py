"""Pipecat pipeline: WebsocketServerTransport + Sarvam STT + OpenAI LLM + Sarvam TTS.

Audio routing:
  Browser JS → expose_function → bridge.py → WSS → Pipecat → Sarvam STT
  Sarvam STT → OpenAI LLM → Sarvam TTS → Pipecat → WSS → bridge.py → page.evaluate → Browser JS
"""

import asyncio
import logging
import os
import ssl
import time

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.transports.websocket.server import (
    WebsocketServerParams,
    WebsocketServerTransport,
)
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    LLMRunFrame,
    LLMTextFrame,
    LLMFullResponseEndFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from src.teams_agent.config import Config
from src.teams_agent.serializer import RawPCMSerializer

logger = logging.getLogger("teams_agent.pipeline_sarvam")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _create_ssl_context() -> ssl.SSLContext:
    """Create SSL context with self-signed localhost certificate."""
    certs_dir = os.path.join(_PROJECT_ROOT, "certs")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        os.path.join(certs_dir, "localhost.pem"),
        os.path.join(certs_dir, "localhost-key.pem"),
    )
    return ctx


def build_transport(cfg: Config) -> WebsocketServerTransport:
    """Create TLS WebsocketServerTransport. Connected by bridge.py, not browser."""
    transport = WebsocketServerTransport(
        params=WebsocketServerParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
            audio_in_channels=1,
            audio_out_channels=1,
            serializer=RawPCMSerializer(sample_rate=16000, num_channels=1),
        ),
        host="localhost",
        port=cfg.WS_PORT,
    )

    ssl_ctx = _create_ssl_context()
    input_transport = transport.input()

    async def _tls_server_handler():
        from websockets import serve as websocket_serve

        logger.info("Starting WSS server on localhost:%d", cfg.WS_PORT)
        async with websocket_serve(
            input_transport._client_handler,
            input_transport._host,
            input_transport._port,
            ssl=ssl_ctx,
        ) as server:
            await input_transport._callbacks.on_websocket_ready()
            await input_transport._stop_server_event.wait()

    input_transport._server_task_handler = _tls_server_handler
    return transport


class TranscriptObserver(FrameProcessor):
    """Observes transcription and assistant frames to feed the collector in real time."""

    def __init__(self, collector):
        super().__init__()
        self.collector = collector
        self.assistant_text_chunks = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            if text:
                logger.info(f"User STT (Sarvam): {text}")
                if self.collector:
                    await self.collector.add_entry("user", text)
        elif isinstance(frame, LLMTextFrame):
            self.assistant_text_chunks.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            assistant_response = "".join(self.assistant_text_chunks).strip()
            if assistant_response:
                logger.info(f"Assistant LLM (OpenAI): {assistant_response}")
                if self.collector:
                    await self.collector.add_entry("assistant", assistant_response)
            self.assistant_text_chunks.clear()
        
        await self.push_frame(frame, direction)


async def create_and_run_pipeline(
    shutdown_event: asyncio.Event | None = None,
    ws_ready_event: asyncio.Event | None = None,
    transcript_collector=None,
    dg_processor_ref: list | None = None,
    system_prompt_override: str | None = None,
):
    """Build and run the Sarvam STT + OpenAI LLM + Sarvam TTS pipeline. Blocks until pipeline stops."""
    cfg = Config()

    transport = build_transport(cfg)

    # Initialize Sarvam STT
    stt = SarvamSTTService(
        api_key=cfg.SARVAM_API_KEY,
        settings=SarvamSTTService.Settings(
            model=cfg.SARVAM_STT_MODEL
        )
    )

    # Initialize OpenAI/OpenRouter or Azure OpenAI LLM
    if cfg.USE_AZURE_OPENAI:
        llm = OpenAILLMService(
            api_key=cfg.AZURE_OPENAI_API_KEY,
            base_url=cfg.AZURE_OPENAI_BASE_URL,
            default_headers={"api-key": cfg.AZURE_OPENAI_API_KEY},
            settings=OpenAILLMService.Settings(
                model=cfg.AZURE_OPENAI_DEPLOYMENT_NAME,
                system_instruction=system_prompt_override or cfg.SYSTEM_INSTRUCTION,
            )
        )
        logger.info("Using Azure OpenAI: deployment=%s, endpoint=%s", cfg.AZURE_OPENAI_DEPLOYMENT_NAME, cfg.AZURE_OPENAI_ENDPOINT)
    else:
        llm = OpenAILLMService(
            api_key=cfg.OPENAI_API_KEY,
            base_url=cfg.OPENAI_BASE_URL,
            settings=OpenAILLMService.Settings(
                model=cfg.LLM_CHOICE,
                system_instruction=system_prompt_override or cfg.SYSTEM_INSTRUCTION,
            )
        )
        logger.info("Using OpenAI/OpenRouter: model=%s", cfg.LLM_CHOICE)

    # Initialize Sarvam TTS
    tts = SarvamTTSService(
        api_key=cfg.SARVAM_API_KEY,
        settings=SarvamTTSService.Settings(
            model=cfg.SARVAM_TTS_MODEL,
            voice=cfg.SARVAM_TTS_VOICE,
        )
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    transcript_observer = TranscriptObserver(transcript_collector)

    logger.info(
        "SarvamPipeline: STT=%s, LLM=%s, TTS=%s (%s)",
        cfg.SARVAM_STT_MODEL,
        cfg.LLM_CHOICE,
        cfg.SARVAM_TTS_MODEL,
        cfg.SARVAM_TTS_VOICE,
    )

    if ws_ready_event:
        @transport.event_handler("on_websocket_ready")
        async def on_ready(transport):
            logger.info("WSS server ready on port %d", cfg.WS_PORT)
            ws_ready_event.set()

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, websocket):
        logger.info("Audio bridge connected")
        # Kick off the conversation
        context.add_message(
            {"role": "user", "content": "Please introduce yourself briefly to the meeting."}
        )
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, websocket):
        logger.info("Audio bridge disconnected")

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        transcript_observer,
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
        idle_timeout_secs=None,
    )

    if shutdown_event:
        async def _watch_shutdown():
            await shutdown_event.wait()
            logger.info("Shutdown event received, stopping pipeline...")
            await task.cancel()

        asyncio.create_task(_watch_shutdown())

    runner = PipelineRunner()
    logger.info("Sarvam Voice Pipeline starting...")
    await runner.run(task)
    logger.info("Sarvam Voice Pipeline stopped.")
