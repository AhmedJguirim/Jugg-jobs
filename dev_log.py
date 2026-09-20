"""Development file logger.

Usage anywhere in the project:

    from dev_log import log
    log.info("checkpoint: %s", value)

Everything lands in logs/dev.log so you can follow one clean stream instead of
reading the uvicorn/DBOS terminal output.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "dev.log")

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(filename)s:%(lineno)d | %(message)s"
_DATEFMT = "%H:%M:%S"


def _build_handler() -> RotatingFileHandler:
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.setLevel(logging.DEBUG)
    return handler


_handler = _build_handler()

# The logger to import in your own code.
log = logging.getLogger("dev")
log.setLevel(logging.DEBUG)
log.addHandler(_handler)
log.propagate = False  # keep it out of the terminal


def attach(*logger_names: str, level: int = logging.DEBUG) -> None:
    """Also write another library's logs to logs/dev.log.

    The file gets everything down to `level`; the terminal keeps the verbosity
    it had before, so turning up file detail never makes the console noisier.
    """
    for name in logger_names:
        target = logging.getLogger(name)
        if target.level != logging.NOTSET and target.level < level:
            continue  # already more verbose than we need
        previous = target.getEffectiveLevel()
        for existing in target.handlers:
            if existing is not _handler and existing.level < previous:
                existing.setLevel(previous)
        if _handler not in target.handlers:
            target.addHandler(_handler)
        target.setLevel(level)


def section(title: str) -> None:
    """Visual separator, handy when re-running a function you are working on."""
    log.info("%s %s %s", "=" * 20, title, "=" * 20)
