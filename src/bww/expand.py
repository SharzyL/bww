"""Path / env-var expansion utilities.

`expand_path` does specifier substitution (`%h`, `%E`, `~`, `${VAR}`, …) and
returns an absolute path. `expand_glob_pattern` is `expand_path` plus glob
expansion. `parse_bwargs` shlex-splits the user's `--bwargs` string.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
from pathlib import Path

from .models import ConfigError

__all__ = [
    'expand_env_vars',
    'expand_glob_pattern',
    'expand_path',
    'get_config_path',
    'parse_bwargs',
]


def get_config_path(config_override: str | None = None) -> Path:
    """Resolve the bww config-file location.

    Follows XDG Base Directory Specification:
      - If `config_override` is provided, uses that path (after ~/expanduser).
      - Else `$XDG_CONFIG_HOME/bww/config.kdl` if XDG_CONFIG_HOME is set.
      - Else `~/.config/bww/config.kdl`.

    Returned path may not exist on disk.
    """
    if config_override:
        return Path(config_override).expanduser().resolve()
    xdg_config = os.environ.get('XDG_CONFIG_HOME')
    if xdg_config:
        return Path(xdg_config) / 'bww' / 'config.kdl'
    return Path.home() / '.config' / 'bww' / 'config.kdl'


_ENV_VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def expand_env_vars(s: str) -> str:
    """Expand `${ENV_NAME}` references using `os.environ`. Raises if missing."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in os.environ:
            raise ConfigError(f'Environment variable not set: {name}')
        return os.environ[name]

    return _ENV_VAR_RE.sub(repl, s)


def expand_path(path: str, config_dir: Path) -> str:
    """Expand specifiers and return an absolute path string.

    Supports both shell-style and systemd-style expansion:
      - `${ENV_NAME}` — environment variable
      - `~` — user home (shell-style; only at path start)
      - `%h` — user home (systemd specifier; anywhere)
      - `%E` — XDG_CONFIG_HOME (config root, e.g. ~/.config)
      - `%C` — XDG_CACHE_HOME (cache root, e.g. ~/.cache)
      - `%D` — XDG_DATA_HOME (data home, e.g. ~/.local/share)
      - `%S` — XDG_STATE_HOME (state home, e.g. ~/.local/state)
      - `%t` — XDG_RUNTIME_DIR
      - `%u` — user name
      - `%U` — user uid
      - `%%` — literal percent sign
    """
    expanded = path.strip()  # bwrap is strict about paths; trim whitespace.
    expanded = expand_env_vars(expanded)

    home = os.path.expanduser('~')
    xdg_config_home = os.environ.get('XDG_CONFIG_HOME', os.path.join(home, '.config'))
    xdg_cache_home = os.environ.get('XDG_CACHE_HOME', os.path.join(home, '.cache'))
    xdg_data_home = os.environ.get('XDG_DATA_HOME', os.path.join(home, '.local', 'share'))
    xdg_state_home = os.environ.get('XDG_STATE_HOME', os.path.join(home, '.local', 'state'))
    xdg_runtime_dir = os.environ.get('XDG_RUNTIME_DIR', os.path.join(home, '.run'))
    user_name = getpass.getuser()
    user_uid = str(os.getuid())

    # Process %% first so a literal percent isn't picked up by other specifiers.
    expanded = expanded.replace('%%', '\x00')

    expanded = expanded.replace('%h', home)
    expanded = expanded.replace('%E', xdg_config_home)
    expanded = expanded.replace('%C', xdg_cache_home)
    expanded = expanded.replace('%D', xdg_data_home)
    expanded = expanded.replace('%S', xdg_state_home)
    expanded = expanded.replace('%t', xdg_runtime_dir)
    expanded = expanded.replace('%u', user_name)
    expanded = expanded.replace('%U', user_uid)

    # Shell-style tilde — only at start of path.
    if expanded.startswith('~'):
        expanded = home + expanded[1:]

    expanded = expanded.replace('\x00', '%')
    return str(Path(expanded).absolute())


def expand_glob_pattern(pattern: str, config_dir: Path) -> list[str]:
    """`expand_path` + glob expansion.

    For non-glob patterns, returns the expanded path even if it doesn't exist
    so bubblewrap can raise a clear error. For glob patterns, returns the
    sorted list of concrete matches (or empty if none).
    """
    expanded = expand_path(pattern, config_dir)
    path = Path(expanded)

    if any(c in pattern for c in '*?[]'):
        return sorted(str(m) for m in path.parent.glob(path.name))

    return [str(path)]


def parse_bwargs(bwargs_str: str) -> list[str]:
    """Parse the `--bwargs` space-separated string via shlex (handles quoting)."""
    return shlex.split(bwargs_str)
