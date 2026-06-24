import asyncio
import io
import logging
import os
import wave
import aiohttp
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Self

from joinly.core import STT
from joinly.types import (
    AudioFormat,
    SpeechWindow,
    TranscriptSegment,
)
from joinly.utils.audio import calculate_audio_duration
from joinly.utils.usage import add_usage

logger = logging.getLogger(__name__)


class SarvamSTT(STT):
    """Speech-to-Text (STT) service using Sarvam AI API."""

    def __init__(
        self,
        *,
        model_name: str = "saaras:v3",
        sample_rate: int = 16000,
    ) -> None:
        """Initialize the Sarvam STT service.

        Args:
            model_name: The Sarvam STT model to use (default is "saaras:v3").
            sample_rate: The sample rate of the audio (default is 16000).
        """
        self.api_key = os.getenv("SARVAM_API_KEY")
        if not self.api_key:
            msg = "SARVAM_API_KEY must be set in the environment."
            raise ValueError(msg)

        self._model = model_name
        self._lock = asyncio.Lock()
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    async def __aenter__(self) -> Self:
        """Initialize the client."""
        logger.info("Initialized Sarvam STT with model: %s", self._model)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Clean up resources."""
        pass

    async def stream(
        self, windows: AsyncIterator[SpeechWindow]
    ) -> AsyncIterator[TranscriptSegment]:
        """Transcribe audio stream using Sarvam AI API.

        Args:
            windows: An asynchronous iterator of audio windows to transcribe.

        Yields:
            TranscriptSegment: The transcribed segment(s).
        """
        # Buffer the entire audio stream of the utterance
        start_time: float | None = None
        end_time: float = 0.0
        audio_buffer = bytearray()
        speakers: defaultdict[str, float] = defaultdict(float)

        async for window in windows:
            if start_time is None:
                start_time = window.time_ns / 1e9

            audio_buffer.extend(window.data)

            duration = calculate_audio_duration(len(window.data), self.audio_format)
            end_time = (window.time_ns / 1e9) + duration
            if window.speaker:
                speakers[window.speaker] += duration

        if not audio_buffer:
            logger.warning("Received no audio data to transcribe.")
            return

        # Convert PCM to WAV format
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(self.audio_format.byte_depth)
            wf.setframerate(self.audio_format.sample_rate)
            wf.writeframes(audio_buffer)
        wav_buffer.seek(0)
        audio_bytes = wav_buffer.getvalue()

        # Send to Sarvam API
        async with self._lock:
            audio_duration_secs = calculate_audio_duration(
                len(audio_buffer), self.audio_format
            )
            logger.debug(
                "Sending %.2f seconds of audio to Sarvam for transcription.",
                audio_duration_secs,
            )

            try:
                # We use FormData for multipart/form-data upload
                data = aiohttp.FormData()
                # Use a dummy filename for the memory buffer
                data.add_field("file", audio_bytes, filename="audio.wav", content_type="audio/wav")
                data.add_field("model", self._model)
                # Map BCP-47 style code or language code
                lang = os.getenv("JOINLY_LANGUAGE") or "en-IN"
                if lang == "en":
                    lang = "en-IN"
                elif lang == "hi":
                    lang = "hi-IN"
                data.add_field("language_code", lang)

                headers = {
                    "api-subscription-key": self.api_key
                }

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://api.sarvam.ai/speech-to-text",
                        headers=headers,
                        data=data,
                    ) as response:
                        if response.status != 200:
                            err_text = await response.text()
                            logger.error(
                                "Sarvam STT failed with status %d: %s",
                                response.status,
                                err_text,
                            )
                            raise RuntimeError(f"Sarvam STT failed: {err_text}")

                        resp_json = await response.json()
                        transcribed_text = resp_json.get("transcript", "").strip()

                        # Track usage
                        add_usage(
                            service="sarvam_stt",
                            usage={"seconds": audio_duration_secs},
                            meta={"model": self._model},
                        )

                        if transcribed_text:
                            # Determine the primary speaker
                            speaker = (
                                max(speakers.items(), key=lambda item: item[1])[0]
                                if speakers
                                else None
                            )

                            yield TranscriptSegment(
                                text=transcribed_text,
                                start=start_time or 0.0,
                                end=end_time,
                                speaker=speaker,
                            )
                        else:
                            logger.info("Sarvam STT returned an empty transcription.")

            except Exception as e:
                logger.exception("Error during Sarvam transcription")
                msg = f"Failed to transcribe audio with Sarvam: {e}"
                raise RuntimeError(msg) from e
