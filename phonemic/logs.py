"""Logging set-up shared by the command line and the desktop window."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
NOISY_LOGGERS = ("aiortc", "aioice", "aiohttp.access", "aiohttp.server", "asyncio")


def configure_logging(verbose: bool, log_file: Path | None = None) -> None:
    """Log to stderr (when there is one) and optionally to a rotating file.

    Windowed apps (pythonw, the .exe) have no stderr, so they rely on the file.
    """
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=1, encoding="utf-8"
            )
        )
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    noisy_level = logging.INFO if verbose else logging.WARNING
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)
