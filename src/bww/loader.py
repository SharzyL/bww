"""Top-level KDL loader: file → Config → Profile.

Reads `config.kdl`, walks profile/default nodes, and produces normalized
Profile objects. Per-option value reading is delegated to
`dispatch.read_kdl_option`.
"""

from __future__ import annotations

from typing import Any

import kdl

from .dispatch import read_kdl_option
from .expand import get_config_path
from .models import Config, ConfigError, MountEntry, Profile
from .options import OPTIONS
from .utils import logger

__all__ = ['load_config']


def load_config(config_override: str | None = None) -> Config:
    """Load and parse the bww configuration file.

    Reads KDL from `$XDG_CONFIG_HOME/bww/config.kdl` (or
    `~/.config/bww/config.kdl`). Returns an empty Config if the file
    doesn't exist. Raises ConfigError on invalid KDL.
    """
    config_path = get_config_path(config_override)

    if not config_path.exists():
        return Config()

    try:
        text = config_path.read_text(encoding='utf-8')
        doc = kdl.parse(text)
    except kdl.ParseError as e:
        raise ConfigError(f'Invalid KDL in {config_path}: {e}') from e
    except Exception as e:
        raise ConfigError(f'Cannot read config file {config_path}: {e}') from e

    profiles: dict[str, Profile] = {}
    defaults: dict[str, Profile] = {}

    # Support both:
    #   nested form: profiles { name { ... } } / defaults { name { ... } }
    #   flat form:   profiles.name { ... }     / defaults.name { ... }
    for node in doc.nodes:
        if node.name == 'profiles':
            for child in node.nodes:
                profiles[child.name] = _parse_profile(child.name, _kdl_profile_to_dict(child))
        elif node.name.startswith('profiles.'):
            name = node.name.split('.', 1)[1]
            profiles[name] = _parse_profile(name, _kdl_profile_to_dict(node))
        elif node.name == 'defaults':
            for child in node.nodes:
                defaults[child.name] = _parse_profile(child.name, _kdl_profile_to_dict(child))
        elif node.name.startswith('defaults.'):
            name = node.name.split('.', 1)[1]
            defaults[name] = _parse_profile(name, _kdl_profile_to_dict(node))
        else:
            logger.warning(f"{config_path.name}: unknown top-level key '{node.name}' (ignored)")

    return Config(profiles=profiles, defaults=defaults)


def _warn_unknown_keys(node: kdl.Node, profile_name: str) -> None:
    """Emit a warning for any child node name that isn't in the registry."""
    known = {'inherit'} | {s.key for s in OPTIONS}
    for child in node.nodes:
        if child.name not in known:
            logger.warning(f"profile [{profile_name}]: unknown key '{child.name}' (ignored)")


def _kdl_profile_to_dict(node: kdl.Node) -> dict[str, Any]:
    """Convert a KDL profile/default node into a dict for `_parse_profile()`.

    Output dict keys match `_parse_profile` lookups:
      - 'inherit'                          (string list)
      - spec.key for mount / string-list / bool kinds  ('rw', 'share-net', ...)
      - spec.dest for kv-list / pattern-list           ('set_env', 'unset_env')
    Unknown keys emit a warning and are otherwise ignored.
    """
    data: dict[str, Any] = {}

    # `inherit` is special-cased — not in OPTIONS, but a recognized KDL key.
    inherit = read_kdl_option_inherit(node)
    if inherit:
        data['inherit'] = inherit

    for spec in OPTIONS:
        val = read_kdl_option(node, spec)
        if spec.kind == 'bool':
            data[spec.key] = val
        elif spec.kind in ('kv-list', 'pattern-list'):
            # Stored under dest (underscore form) to match _parse_profile contract.
            if val:
                data[spec.dest] = val
        else:  # mount, string-list — spec.key == spec.dest in practice
            if val:
                data[spec.key] = val

    _warn_unknown_keys(node, node.name)
    return data


def read_kdl_option_inherit(node: kdl.Node) -> list[str]:
    """Read the special `inherit` KDL key (string list, not in OPTIONS)."""
    out: list[str] = []
    for n in list(node.getAll('inherit')):
        for arg in n.args:
            if not isinstance(arg, str):
                raise ConfigError(f'Profile [{node.name}] inherit values must be strings')
            out.append(arg)
    return out


def _parse_profile(name: str, data: dict[str, Any]) -> Profile:
    """Convert a KDL-derived dict into a normalized Profile.

    Raises ConfigError on type mismatches.
    """
    # `inherit` may be string or list — normalize to list[str].
    inherit_raw = data.get('inherit', [])
    if isinstance(inherit_raw, str):
        inherit = [inherit_raw]
    elif isinstance(inherit_raw, list):
        inherit = inherit_raw
    else:
        raise ConfigError(f'Profile [{name}] inherit must be string or list')

    # Mount fields accept list[MountEntry] (KDL path) or list[str] (CLI path
    # or legacy callers — interpreted as in-place 1-arg form).
    rw = _normalize_mount_entries(data.get('rw', []), 'rw', f'Profile [{name}] rw')
    ro = _normalize_mount_entries(data.get('ro', []), 'ro', f'Profile [{name}] ro')
    tmpfs = _normalize_mount_entries(data.get('tmpfs', []), 'tmpfs', f'Profile [{name}] tmpfs')
    bwargs = _normalize_path_list(data.get('bwargs', []), f'Profile [{name}] bwargs')
    set_env = _normalize_kv_pairs(data.get('set_env', []), f'Profile [{name}] set-env')
    unset_env = _normalize_path_list(data.get('unset_env', []), f'Profile [{name}] unset-env')

    bool_kwargs: dict[str, bool] = {}
    for spec in OPTIONS:
        if spec.kind != 'bool':
            continue
        bool_kwargs[spec.dest] = _get_bool(data, spec.key, f'Profile [{name}]')

    return Profile(
        name=name,
        inherit=inherit,
        rw=rw,
        ro=ro,
        tmpfs=tmpfs,
        bwargs=bwargs,
        set_env=set_env,
        unset_env=unset_env,
        **bool_kwargs,
    )


def _normalize_mount_entries(value: Any, mode: str, context: str) -> list[MountEntry]:
    """Normalize a mount field to list[MountEntry].

    Accepts either list[MountEntry] (KDL path) or list[str] (legacy/CLI —
    treated as in-place 1-arg form). For mode='tmpfs' a string is converted
    to MountEntry(src=None, dest=path).
    """
    if not isinstance(value, list):
        raise ConfigError(f'{context} must be a list')
    out: list[MountEntry] = []
    for v in value:
        if isinstance(v, MountEntry):
            out.append(v)
        elif isinstance(v, str):
            out.append(MountEntry(src=None, dest=v) if mode == 'tmpfs' else MountEntry(src=v, dest=v))
        else:
            raise ConfigError(f'{context} entries must be MountEntry or string')
    return out


def _normalize_path_list(value: Any, context: str) -> list[str]:
    """Normalize a list-of-strings field. Accepts string, list, or dict-of-keys."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise ConfigError(f'{context} must contain only strings')
        return value
    if isinstance(value, dict):
        keys: list[str] = []
        for k in value:
            if not isinstance(k, str):
                raise ConfigError(f'{context} dict keys must be strings')
            keys.append(k)
        return keys
    raise ConfigError(f'{context} must be string, list, or dict')


def _normalize_kv_pairs(value: Any, context: str) -> list[tuple[str, str]]:
    """Normalize a sequence of (key, value) pairs (list of 2-tuples of strings)."""
    if not isinstance(value, list):
        raise ConfigError(f'{context} must be a list of (KEY, VALUE) pairs')
    out: list[tuple[str, str]] = []
    for v in value:
        if isinstance(v, (tuple, list)) and len(v) == 2 and all(isinstance(x, str) for x in v):
            out.append((v[0], v[1]))
        else:
            raise ConfigError(f'{context} entries must be (KEY, VALUE) string pairs')
    return out


def _get_bool(data: dict[str, Any], key: str, context: str, default: bool = False) -> bool:
    """Read a boolean key from a config dict with validation."""
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f'{context} {key} must be a boolean')
    return value
