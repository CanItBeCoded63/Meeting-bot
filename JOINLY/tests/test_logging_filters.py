from __future__ import annotations

import logging

from joinly.utils.logging import DeepgramShutdownCancellationFilter
from joinly.utils.logging import install_deepgram_shutdown_filter


def _record(
    *,
    name: str = "deepgram.clients.common.v1.abstract_async_websocket",
    message: str = "tasks cancelled error: ",
    exc_info: object | None = None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )


def test_deepgram_shutdown_cancellation_filter_suppresses_empty_cleanup_log() -> None:
    """Deepgram emits this after clean websocket shutdown; it is not actionable."""
    assert not DeepgramShutdownCancellationFilter().filter(_record())


def test_deepgram_shutdown_cancellation_filter_suppresses_empty_exc_tuple() -> None:
    """Deepgram can log the cleanup message with an empty exc_info tuple."""
    assert not DeepgramShutdownCancellationFilter().filter(_record(exc_info=()))


def test_install_deepgram_shutdown_filter_adds_handler_filter() -> None:
    """The client configures logging directly, so root handlers need the filter."""
    root_logger = logging.getLogger()
    handler = logging.NullHandler()
    root_logger.addHandler(handler)
    try:
        install_deepgram_shutdown_filter()
        assert any(
            isinstance(item, DeepgramShutdownCancellationFilter)
            for item in handler.filters
        )
    finally:
        root_logger.removeHandler(handler)


def test_deepgram_shutdown_cancellation_filter_keeps_real_deepgram_errors() -> None:
    """Only the exact empty cleanup log should be filtered."""
    log_filter = DeepgramShutdownCancellationFilter()

    assert log_filter.filter(_record(message="Deepgram failed to connect"))
    assert log_filter.filter(_record(name="joinly.services.stt.deepgram"))
    assert log_filter.filter(
        _record(exc_info=(RuntimeError, RuntimeError("boom"), None))
    )
