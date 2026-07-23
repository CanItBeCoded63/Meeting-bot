import logging

LOGGING_TRACE = 5


class HealthCheckFilter(logging.Filter):
    """Logging filter to skip successful health check logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter out health check logs."""
        return not (
            "GET /health" in record.getMessage() and "200" in record.getMessage()
        )


class DeepgramShutdownCancellationFilter(logging.Filter):
    """Filter noisy Deepgram SDK task-cancellation logs during clean shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter out Deepgram websocket task cancellation cleanup logs."""
        exc_info = record.exc_info
        has_exception = bool(exc_info and any(exc_info))
        return not (
            record.name == "deepgram.clients.common.v1.abstract_async_websocket"
            and "tasks cancelled error" in record.getMessage()
            and not has_exception
        )


_DEEPGRAM_SHUTDOWN_CANCELLATION_FILTER = DeepgramShutdownCancellationFilter()


def install_deepgram_shutdown_filter() -> None:
    """Install the Deepgram clean-shutdown filter on loggers and handlers."""
    target_logger = logging.getLogger(
        "deepgram.clients.common.v1.abstract_async_websocket"
    )
    target_logger.addFilter(_DEEPGRAM_SHUTDOWN_CANCELLATION_FILTER)
    logging.getLogger().addFilter(_DEEPGRAM_SHUTDOWN_CANCELLATION_FILTER)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_DEEPGRAM_SHUTDOWN_CANCELLATION_FILTER)


def _configure_third_party_logging() -> None:
    """Configure third-party loggers that are noisy during normal operation."""
    install_deepgram_shutdown_filter()


def configure_logging(verbose: int, *, quiet: bool, plain: bool) -> None:
    """Configure logging based on verbosity level."""
    log_level = logging.WARNING

    if quiet:
        log_level = logging.ERROR
    elif verbose == 1:
        log_level = logging.INFO
    elif verbose == 2:  # noqa: PLR2004
        log_level = logging.DEBUG
    elif verbose > 2:  # noqa: PLR2004
        log_level = LOGGING_TRACE

    logging.addLevelName(LOGGING_TRACE, "TRACE")

    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())
    _configure_third_party_logging()

    if not plain:
        try:
            from rich.logging import RichHandler

            logging.basicConfig(
                level=logging.WARNING if not quiet else logging.ERROR,
                format="%(message)s",
                datefmt="[%X]",
                handlers=[RichHandler(rich_tracebacks=True)],
            )
            logging.getLogger("joinly").setLevel(log_level)
            logging.getLogger("joinly_client").setLevel(log_level)
            _configure_third_party_logging()
        except ImportError:
            pass
        else:
            return

    logging.basicConfig(
        level=logging.WARNING if not quiet else logging.ERROR,
        format="[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _configure_third_party_logging()
    logging.getLogger("joinly").setLevel(log_level)
    logging.getLogger("joinly_client").setLevel(log_level)
