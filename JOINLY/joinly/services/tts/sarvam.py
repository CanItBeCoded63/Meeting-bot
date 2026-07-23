import asyncio
import logging
import os
import base64
import io
import wave
import aiohttp
from collections.abc import AsyncIterator
from typing import Self

from joinly.core import TTS
from joinly.types import AudioFormat
from joinly.utils.usage import add_usage

logger = logging.getLogger(__name__)


class SarvamTTS(TTS):
    """Text-to-Speech (TTS) service using Sarvam AI API."""

    def __init__(
        self,
        *,
        model_name: str = "bulbul:v3",
        speaker: str = "shubh",
        sample_rate: int = 24000,
        chunk_size_bytes: int = 4096,
    ) -> None:
        """Initialize the Sarvam TTS service.

        Args:
            model_name: The Sarvam TTS model to use (default is "bulbul:v3").
            speaker: The prebuilt speaker name to use (default is "shubh").
            sample_rate: The sample rate of the audio (default is 24000).
            chunk_size_bytes: The size of audio chunks to yield in bytes.
        """
        self.api_key = os.getenv("SARVAM_API_KEY")
        if not self.api_key:
            msg = "SARVAM_API_KEY must be set in the environment."
            raise ValueError(msg)

        self._model = model_name
        self._speaker = speaker
        self._chunk_size_bytes = chunk_size_bytes
        self._lock = asyncio.Lock()

        # Sarvam TTS outputs PCM audio (16-bit)
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    async def __aenter__(self) -> Self:
        """Initialize resources."""
        logger.info(
            "Initialized Sarvam TTS with model: %s and voice/speaker: %s",
            self._model,
            self._speaker,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Clean up resources."""
        pass

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Convert text to speech and stream the audio data.

        Args:
            text: The text to convert to speech.

        Yields:
            bytes: The audio data chunks (raw PCM, 16-bit).
        """
        async with self._lock:
            logger.debug("Generating audio for text: '%s'", text)

            try:
                lang = os.getenv("JOINLY_LANGUAGE") or "en-IN"
                if lang == "en":
                    lang = "en-IN"
                elif lang == "hi":
                    lang = "hi-IN"

                payload = {
                    "text": text,
                    "model": self._model,
                    "speaker": self._speaker,
                    "target_language_code": lang,
                    "speech_sample_rate": self.audio_format.sample_rate,
                }

                headers = {
                    "api-subscription-key": self.api_key,
                    "Content-Type": "application/json",
                }

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://api.sarvam.ai/text-to-speech",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status != 200:
                            err_text = await response.text()
                            logger.error(
                                "Sarvam TTS failed with status %d: %s",
                                response.status,
                                err_text,
                            )
                            raise RuntimeError(f"Sarvam TTS failed: {err_text}")

                        resp_json = await response.json()
                        logger.debug(
                            "Sarvam TTS response keys: %s", list(resp_json.keys())
                        )

                        # Sarvam v3 returns "audios" (list); older versions use "audio_base64"
                        audio_base64_raw = resp_json.get("audios")
                        if audio_base64_raw and isinstance(audio_base64_raw, list):
                            audio_base64 = audio_base64_raw[0]
                        else:
                            audio_base64 = resp_json.get("audio_base64")

                        if not audio_base64:
                            logger.warning(
                                "Sarvam TTS returned no audio data. Full response: %s",
                                resp_json,
                            )
                            return

                        raw_audio = base64.b64decode(audio_base64)

                        # Sarvam returns WAV — extract raw PCM by stripping the header
                        try:
                            with wave.open(io.BytesIO(raw_audio), "rb") as wf:
                                actual_rate = wf.getframerate()
                                if actual_rate != self.audio_format.sample_rate:
                                    logger.info(
                                        "Sarvam TTS actual sample rate: %d", actual_rate
                                    )
                                    self.audio_format = AudioFormat(
                                        sample_rate=actual_rate, byte_depth=2
                                    )
                                audio_data = wf.readframes(wf.getnframes())
                        except Exception:
                            logger.debug("Response is not valid WAV, using raw bytes")
                            audio_data = raw_audio

                        # Track usage
                        add_usage(
                            service="sarvam_tts",
                            usage={"characters": len(text)},
                            meta={"model": self._model, "speaker": self._speaker},
                        )

                        logger.debug("Generated %d bytes of audio data.", len(audio_data))

                        # Chunk the audio data for streaming
                        for i in range(0, len(audio_data), self._chunk_size_bytes):
                            yield audio_data[i : i + self._chunk_size_bytes]

            except Exception as e:
                logger.exception("Error during Sarvam TTS generation")
                msg = f"Failed to generate audio from Sarvam TTS: {e}"
                raise RuntimeError(msg) from e
