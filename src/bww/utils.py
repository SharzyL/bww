"""Logging via loguru."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from loguru import Record

__all__ = ['configure_logging', 'logger']


# Short labels for loguru's default level names to keep terminal output terse.
_LEVEL_ALIAS = {'WARNING': 'WARN', 'SUCCESS': 'OK'}

# Levels that go to stdout (pipe-friendly user-facing positive output).
# Everything else (DEBUG, WARNING, ERROR, CRITICAL, TRACE) goes to stderr.
_STDOUT_LEVELS = frozenset({'INFO', 'SUCCESS'})


def _format(record: 'Record') -> str:
    """Format callback: returns a loguru template string per-record.

    Aliases WARNING → WARN, SUCCESS → OK so terminal output stays terse.
    The trailing newline is required when using a callable format.
    """
    level_name = _LEVEL_ALIAS.get(record['level'].name, record['level'].name)
    return f'<level>[{level_name}]</level> <level>{{message}}</level>\n'


def configure_logging(debug: bool = False) -> None:
    """Initialize loguru with two sinks: positive output → stdout, diagnostics → stderr.

    Must be called explicitly (e.g. from main()). Until it runs, loguru's
    default handler is in effect.

    Routing:
      - stdout: INFO, SUCCESS (user-facing positive output)
      - stderr: WARNING and above always; DEBUG when --debug is set

    Also recolors DEBUG to dim/gray to match the historical look.
    Idempotent — replaces any existing handlers, so it's safe to call from
    main() or a test fixture.
    """
    logger.level('DEBUG', color='<dim>')
    logger.remove()
    logger.add(
        sys.stdout,
        format=_format,
        colorize=True,
        backtrace=False,
        diagnose=False,
        filter=lambda r: r['level'].name in _STDOUT_LEVELS,
    )
    logger.add(
        sys.stderr,
        level='DEBUG' if debug else 'WARNING',
        format=_format,
        colorize=True,
        backtrace=False,
        diagnose=False,
        filter=lambda r: r['level'].name not in _STDOUT_LEVELS,
    )
