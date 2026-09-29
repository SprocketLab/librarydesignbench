"""Logging configuration helpers."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

import structlog
from rich.console import Console
from rich.logging import RichHandler
from structlog.typing import EventDict
from structlog.typing import Processor

_LOCAL_LOGGERS = ("lib_design_bench", "__main__")
_TRANSPORT_LOGGERS = ("grpclib", "h2", "hpack", "httpcore", "httpx")
_TRIAL_LOGGER_PREFIX = "harbor.utils.logger.harbor.trial.trial."
"""Harbor's per-trial loggers (`Trial._init_logger`), each also writing the
trial's own `trial.log`."""
_CHATTY_LOGGERS = ("LiteLLM", "asyncio")
_console: Console | None = None


class TransportDebugFilter(logging.Filter):
    """Drop DEBUG records from dependencies that may log request credentials."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Allow non-debug records and non-transport diagnostic records."""
        return record.levelno > logging.DEBUG or not record.name.startswith(
            _TRANSPORT_LOGGERS
        )


class LocalOrErrorFilter(logging.Filter):
    """Allow local logs and errors from every logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Accept a local log record or an error."""
        return record.levelno >= logging.ERROR or any(
            record.name == logger_name or record.name.startswith(f"{logger_name}.")
            for logger_name in _LOCAL_LOGGERS
        )


class RunLogFilter(logging.Filter):
    """Keep `run.log` to the orchestration no trial file records.

    Below WARNING, it drops Harbor's per-trial records, which the trial's own
    `trial.log` already holds, and the chatter of libraries LDB never calls.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Allow warnings, and debug or info from neither source."""
        return record.levelno >= logging.WARNING or not (
            record.name.startswith(_TRIAL_LOGGER_PREFIX)
            or any(
                record.name == name or record.name.startswith(f"{name}.")
                for name in _CHATTY_LOGGERS
            )
        )


def configure_logging(*, console_level: int) -> None:
    """Configure console logging for the benchmark's own loggers.

    Non-error records from libraries outside `_LOCAL_LOGGERS` are hidden so
    dependency progress/warnings do not drown out benchmark logs.
    """
    global _console
    _console = Console(file=sys.stdout)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    pre_chain: list[Processor] = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
    ]
    handler = _make_console_handler(pre_chain, _console, console_level)
    handler.addFilter(LocalOrErrorFilter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


@contextmanager
def run_logging(run_dir: Path):
    """Append run diagnostics without replacing the process console handlers."""
    handler = _make_jsonl_file_handler(
        run_dir / "run.log",
        [
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
        ],
    )
    handler.addFilter(TransportDebugFilter())
    handler.addFilter(RunLogFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()


def route_console_to_stderr() -> None:
    """Reserve stdout for a machine-readable CLI result, including during trials."""
    global _console
    _console = Console(file=sys.stderr)
    for handler in logging.getLogger().handlers:
        if isinstance(handler, RichHandler):
            handler.console = _console


def get_rich_console() -> Console:
    """Return the console used by `RichHandler` for coordinated live output."""
    global _console
    if _console is None or getattr(_console.file, "closed", False):
        _console = Console(file=sys.stdout)
    return _console


def _make_console_handler(
    pre_chain: Sequence[Processor], console: Console, level: int
) -> logging.Handler:
    handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        show_path=False,
    )
    handler.setLevel(level)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                _render_rich_log,
            ],
            foreign_pre_chain=pre_chain,
        )
    )
    return handler


def _render_rich_log(_, __, event_dict: EventDict) -> str:
    rendered_event = dict(event_dict)
    output = str(rendered_event.pop("event"))
    rendered_event.pop("level", None)
    rendered_event.pop("timestamp", None)
    rendered_event.pop("logger", None)
    rendered_event.pop("logger_name", None)

    if rendered_event:
        fields = " ".join(
            f"{key}={_console_value_repr(value)}"
            for key, value in sorted(rendered_event.items())
        )
        output = f"{output} {fields}"
    return output


def _console_value_repr(value: object) -> str:
    if isinstance(value, str) and not set(value) & {
        " ",
        "\t",
        "=",
        "\r",
        "\n",
        '"',
        "'",
    }:
        return value
    return repr(value)


def _make_jsonl_file_handler(
    log_file: Path, pre_chain: Sequence[Processor]
) -> logging.Handler:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=pre_chain,
        )
    )
    return handler
