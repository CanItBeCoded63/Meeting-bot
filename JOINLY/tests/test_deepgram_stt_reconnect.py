from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from joinly.services.stt import deepgram as deepgram_module
from joinly.services.stt.deepgram import DeepgramSTT
from joinly.types import SpeechWindow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest


SECOND_CONNECT_ATTEMPT = 2


class _FakeDeepgramClient:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.connected = False
        self.fail_start = fail_start
        self.finish_calls = 0
        self.handlers = []
        self.sent = []
        self.start_calls = 0

    def on(self, event: object, handler: object) -> None:
        self.handlers.append((event, handler))

    async def is_connected(self) -> bool:
        return self.connected

    async def start(self, *_args: object, **_kwargs: object) -> None:
        self.start_calls += 1
        if self.fail_start:
            msg = "simulated start failure"
            raise RuntimeError(msg)
        self.connected = True

    async def finish(self) -> None:
        self.finish_calls += 1
        self.connected = False

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def finalize(self) -> None:
        return


async def _speech_windows() -> AsyncIterator[SpeechWindow]:
    yield SpeechWindow(data=b"\x01" * 640, time_ns=1_000_000_000, is_speech=True)


async def test_deepgram_stt_stream_reconnects_after_idle_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect before streaming when Deepgram closed an idle socket."""
    clients: list[_FakeDeepgramClient] = []

    class FakeDeepgram:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            client = _FakeDeepgramClient()
            clients.append(client)
            self.listen = SimpleNamespace(
                asyncwebsocket=SimpleNamespace(v=lambda _version: client)
            )

    monkeypatch.setattr(deepgram_module, "DeepgramClient", FakeDeepgram)

    stt = DeepgramSTT(
        connect_retries=1,
        connect_retry_delay=0,
        finalize_min_speech=999,
        padding_silence=0,
        stream_idle_timeout=0.01,
    )

    async with stt:
        assert clients[0].start_calls == 0

        assert [segment async for segment in stt.stream(_speech_windows())] == []
        assert clients[0].start_calls == 1

        clients[0].connected = False
        assert [segment async for segment in stt.stream(_speech_windows())] == []

    assert clients[0].start_calls == SECOND_CONNECT_ATTEMPT


async def test_deepgram_stt_retries_with_fresh_client_after_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create a fresh websocket client when a reconnect attempt fails."""
    clients: list[_FakeDeepgramClient] = []

    class FakeDeepgram:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            client = _FakeDeepgramClient(fail_start=len(clients) == 0)
            clients.append(client)
            self.listen = SimpleNamespace(
                asyncwebsocket=SimpleNamespace(v=lambda _version: client)
            )

    monkeypatch.setattr(deepgram_module, "DeepgramClient", FakeDeepgram)

    stt = DeepgramSTT(
        connect_retries=2,
        connect_retry_delay=0,
        finalize_min_speech=999,
        padding_silence=0,
        stream_idle_timeout=0.01,
    )

    async with stt:
        assert [segment async for segment in stt.stream(_speech_windows())] == []

    assert clients[0].start_calls == 1
    assert clients[0].finish_calls == 1
    assert clients[1].start_calls == 1
