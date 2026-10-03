"""Route the package's ``logging`` calls into treaty's ``ctx.log`` for one run.

Helpers deep in the command code log with the standard ``logging`` module; a handler wraps
its body in ``logging_to(ctx)`` so those lines reach stderr the way treaty writes logs: a
JSON object in JSON mode, ``message key=value`` otherwise, with secrets redacted.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from treaty import Ctx

_PACKAGE = "telegram_cli"


class _CtxHandler(logging.Handler):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__(level=logging.INFO)
        self._ctx = ctx

    def emit(self, record: logging.LogRecord) -> None:
        # Progress lines read plainly; only a warning or worse says so
        if record.levelno >= logging.WARNING:
            self._ctx.log(record.getMessage(), severity=record.levelname.lower())
        else:
            self._ctx.log(record.getMessage())


@contextmanager
def logging_to(ctx: Ctx) -> Iterator[None]:
    """Send INFO and above from the package's loggers to ``ctx.log`` while the body runs"""
    logger = logging.getLogger(_PACKAGE)
    handler = _CtxHandler(ctx)
    saved = (logger.level, logger.propagate)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # or Python's last-resort handler prints warnings twice
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]
