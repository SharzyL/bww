"""Small utilities shared across the project (logging, formatting helpers)."""

from __future__ import annotations

import sys
from enum import Enum


class Color(Enum):
    """ANSI color codes for terminal output."""

    RESET = '\033[0m'
    GRAY = '\033[90m'
    RED = '\033[31m'
    YELLOW = '\033[33m'
    GREEN = '\033[32m'
    CYAN = '\033[36m'
    MAGENTA = '\033[35m'


def error(msg: str) -> None:
    """Print error message in red to stderr."""
    print(f'{Color.RED.value}[ERROR]{Color.RESET.value} {msg}', file=sys.stderr)


def warn(msg: str) -> None:
    """Print warning message in yellow to stderr."""
    print(f'{Color.YELLOW.value}[WARN]{Color.RESET.value} {msg}', file=sys.stderr)


def info(msg: str) -> None:
    """Print info message in blue."""
    print(f'\033[34m[INFO]{Color.RESET.value} {msg}')


def debug(msg: str, enabled: bool = True) -> None:
    """Print debug message in gray (only if enabled)."""
    if enabled:
        print(f'{Color.GRAY.value}[DEBUG]{Color.RESET.value} {Color.GRAY.value}{msg}{Color.RESET.value}')


def success(msg: str) -> None:
    """Print success message in green."""
    print(f'{Color.GREEN.value}[OK]{Color.RESET.value} {msg}')


def command(msg: str) -> None:
    """Print command message in gray."""
    color = Color.GRAY.value
    reset = Color.RESET.value

    if '\n' not in msg:
        print(f'{color}[CMD]{reset} {color}{msg}{reset}')
        return

    lines = msg.splitlines()
    # Keep the prefix and the command on the same line (easy to read/copy).
    print(f'{color}[CMD]{reset} {color}{lines[0]}{reset}')
    for line in lines[1:]:
        print(f'{color}{line}{reset}')
