import os
import logging
from livekit.agents import tts
import httpx

_session = None

def get_http_session():
    """Get a shared httpx session."""
    global _session
    if _session is None:
        _session = httpx.AsyncClient()
    return _session

class OpenRouterTTS(tts.TTS):
    def __init__(self, api_key: str = "", model: str = "google/gemini-3.1-flash-tts-preview"):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1
        )
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._model = model

    def synthesize(self, text: str, **kwargs) -> "tts.ChunkedStream":
        tts_instance = self
        conn_options = kwargs.get("conn_options")

        # Synthesize expects a ChunkedStream returned which is an async iterable of SynthesizedAudio
        class _Stream(tts.ChunkedStream):
            def __init__(self, text, api_key, model):
                super().__init__(tts=tts_instance, input_text=text, conn_options=conn_options)
                self.text = text
                self.api_key = api_key
                self.model = model

            async def _run(self, *args, **kwargs):
                session = get_http_session()
                try:
                    res = await session.post(
                        "https://openrouter.ai/api/v1/audio/speech",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "input": self.text,
                            "voice": "alloy" 
                        },
                    )
                    if res.status_code != 200:
                        logging.error(f"OpenRouter TTS API error: {res.status_code} - {res.text}")
                    res.raise_for_status()
                    
                    self._event_ch.send_nowait(
                        tts.SynthesizedAudio(
                            text=self.text,
                            data=tts.AudioFrame(
                                data=res.content,
                                sample_rate=24000,
                                num_channels=1,
                                samples_per_channel=len(res.content) // 2 
                            )
                        )
                    )
                except Exception as e:
                    logging.error(f"OpenRouter TTS API error: {e}")
                    raise

        return _Stream(text, self._api_key, self._model)
