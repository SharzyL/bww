"""Configuration loading, parsing, and management."""

import argparse
import os
import getpass
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import kdl

from .options import BOOL_OPTIONS
from .utils import debug


# ============================================================================
# Data Models
# ============================================================================


@dataclass(frozen=True)
class Mount:
    """Represents a single mount point for bubblewrap."""

    path: str
    mode: Literal['rw', 'ro', 'tmpfs']

    def __hash__(self) -> int:
        """Make Mount hashable for use in sets."""
        return hash((self.path, self.mode))


@dataclass
class Profile:
    """Represents a configuration profile from [profiles.NAME]."""

    name: str
    inherit: list[str] = field(default_factory=list)
    rw: list[str] = field(default_factory=list)
    ro: list[str] = field(default_factory=list)
    tmpfs: list[str] = field(default_factory=list)
    bwargs: list[str] = field(default_factory=list)
    share_net: bool = False
    dev_bind: bool = False
    reuse_session: bool = False
    share_user: bool = False
    share_ipc: bool = False
    share_pid: bool = False
    share_uts: bool = False


@dataclass
class Config:
    """Complete parsed configuration from config.kdl."""

    profiles: dict[str, Profile] = field(default_factory=dict)
    defaults: dict[str, Profile] = field(default_factory=dict)


@dataclass
class RuntimeConfig:
    """Final merged configuration ready for execution."""

    argv0: str
    command: list[str]
    mounts: set[Mount] = field(default_factory=set)
    bwargs: list[str] = field(default_factory=list)
    share_net: bool = False
    dev_bind: bool = False
    reuse_session: bool = False
    share_user: bool = False
    share_ipc: bool = False
    share_pid: bool = False
    share_uts: bool = False
    debug: bool = False
    debug_tmpfs: bool = False


# ============================================================================
# Exceptions
# ============================================================================


class ConfigError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""

    pass


# ============================================================================
# Path Utilities
# ============================================================================


def get_config_path(config_override: str | None = None) -> Path:
    """
    Get the path to the bww config file.

    Follows XDG Base Directory Specification:
    - If config_override is provided, uses that path
    - Uses $XDG_CONFIG_HOME/bww/config.kdl if XDG_CONFIG_HOME is set
    - Falls back to ~/.config/bww/config.kdl

    Args:
        config_override: Optional override path from --config argument

    Returns:
        Path to config file (may not exist)
    """
    if config_override:
        return Path(config_override).expanduser().resolve()

    xdg_config = os.environ.get('XDG_CONFIG_HOME')
    if xdg_config:
        return Path(xdg_config) / 'bww' / 'config.kdl'
    return Path.home() / '.config' / 'bww' / 'config.kdl'


_ENV_VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def _expand_env_vars(s: str) -> str:
    """Expand ${ENV_NAME} references using os.environ."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in os.environ:
            raise ConfigError(f'Environment variable not set: {name}')
        return os.environ[name]

    return _ENV_VAR_RE.sub(repl, s)


def expand_path(path: str, config_dir: Path) -> str:
    """
    Expand specifiers and normalize path to absolute.

    Supports both shell-style and systemd-style expansion:
    - ${ENV_NAME}: environment variable expansion
    - ~: user home directory (shell-style, only at path start)
    - %h: user home directory (systemd specifier, anywhere in path)
    - %E: XDG_CONFIG_HOME directory (configuration root, e.g., ~/.config)
    - %C: XDG_CACHE_HOME directory (cache root, e.g., ~/.cache)
    - %D: XDG_DATA_HOME directory (data home, e.g., ~/.local/share)
    - %S: XDG_STATE_HOME directory (state home, e.g., ~/.local/state)
    - %t: XDG_RUNTIME_DIR (systemd specifier)
    - %u: user name
    - %U: user uid
    - %%: literal percent sign

    Args:
        path: Path string potentially containing specifiers
        config_dir: Directory containing config.kdl

    Returns:
        Normalized absolute path string
    """
    # Be forgiving about accidental whitespace; bwrap is strict about paths.
    expanded = path.strip()

    # Expand ${ENV_NAME} early so specifier expansion can apply to the result too.
    expanded = _expand_env_vars(expanded)

    # Get values for expansion
    home = os.path.expanduser('~')
    xdg_config_home = os.environ.get('XDG_CONFIG_HOME', os.path.join(home, '.config'))
    xdg_cache_home = os.environ.get('XDG_CACHE_HOME', os.path.join(home, '.cache'))
    xdg_data_home = os.environ.get('XDG_DATA_HOME', os.path.join(home, '.local', 'share'))
    xdg_state_home = os.environ.get('XDG_STATE_HOME', os.path.join(home, '.local', 'state'))
    xdg_runtime_dir = os.environ.get('XDG_RUNTIME_DIR', os.path.join(home, '.run'))
    user_name = getpass.getuser()
    user_uid = str(os.getuid())

    # Process %% first (to protect literal % from further processing)
    expanded = expanded.replace('%%', '\x00')

    # Process systemd specifiers
    expanded = expanded.replace('%h', home)
    expanded = expanded.replace('%E', xdg_config_home)
    expanded = expanded.replace('%C', xdg_cache_home)
    expanded = expanded.replace('%D', xdg_data_home)
    expanded = expanded.replace('%S', xdg_state_home)
    expanded = expanded.replace('%t', xdg_runtime_dir)
    expanded = expanded.replace('%u', user_name)
    expanded = expanded.replace('%U', user_uid)

    # Process shell-style tilde expansion (only at start of path)
    if expanded.startswith('~'):
        expanded = home + expanded[1:]

    # Restore literal percent signs
    expanded = expanded.replace('\x00', '%')

    return str(Path(expanded).absolute())


def expand_glob_pattern(pattern: str, config_dir: Path) -> list[str]:
    """
    Expand path pattern with specifiers and (optional) glob support.

    Expands specifiers first (${ENV_NAME}, ~, %h, %E, %C, %D, %S, %t, %u, %U, %%),
    then applies glob matching.
    For non-glob patterns, always returns the expanded path (even if it doesn't exist)
    so bubblewrap can raise a clear error.

    Args:
        pattern: Path pattern like "%E/gtk-*.0" or "%h/.config/*"
        config_dir: Directory containing config.kdl

    Returns:
        List of matching paths (or empty list if none match)
    """
    expanded = expand_path(pattern, config_dir)
    path = Path(expanded)

    # Check if pattern contains glob characters
    if any(c in pattern for c in '*?[]'):
        parent = path.parent
        pattern_str = path.name
        matches = parent.glob(pattern_str)
        return sorted(str(m) for m in matches)

    # Non-glob: return the expanded path as-is.
    return [str(path)]


def parse_bwargs(bwargs_str: str) -> list[str]:
    """
    Parse space-separated bwrap arguments.

    Uses shell-like parsing to handle quoted arguments properly.

    Args:
        bwargs_str: Space-separated string of arguments

    Returns:
        List of parsed argument strings
    """
    return shlex.split(bwargs_str)


# ============================================================================
# Configuration Loading and Parsing
# ============================================================================


def load_config(config_override: str | None = None) -> Config:
    """
    Load and parse the bww configuration file.

    Reads KDL from $XDG_CONFIG_HOME/bww/config.kdl (or ~/.config/bww/config.kdl).
    Creates empty Config if file doesn't exist.

    Args:
        config_override: Optional override path from --config argument

    Returns:
        Parsed Config object

    Raises:
        ConfigError: If KDL is invalid
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
    # - Nested form: profiles { name { ... } } / defaults { name { ... } }
    # - Flat form: profiles.name { ... } / defaults.name { ... }
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

    return Config(profiles=profiles, defaults=defaults)


def _kdl_profile_to_dict(node: kdl.Node) -> dict[str, Any]:
    """Convert a KDL profile/default node into a dict compatible with _parse_profile()."""

    def _strings(key: str) -> list[str]:
        out: list[str] = []
        for n in list(node.getAll(key)):
            for arg in n.args:
                if not isinstance(arg, str):
                    raise ConfigError(f'Profile [{node.name}] {key} values must be strings')
                out.append(arg)
        return out

    def _bool(key: str) -> bool:
        # Allow multiple occurrences; last wins.
        val = False
        for n in list(node.getAll(key)):
            if len(n.args) != 1 or not isinstance(n.args[0], bool):
                raise ConfigError(f'Profile [{node.name}] {key} must be a boolean')
            val = n.args[0]
        return val

    data: dict[str, Any] = {}
    inherit = _strings('inherit')
    if inherit:
        data['inherit'] = inherit

    for key in ('rw', 'ro', 'tmpfs', 'bwargs'):
        vals = _strings(key)
        if vals:
            data[key] = vals

    for opt in BOOL_OPTIONS:
        data[opt.key] = _bool(opt.key)

    return data


def _parse_profile(name: str, data: dict[str, Any]) -> Profile:
    """
    Parse profile data from a config dict.

    Args:
        name: Profile name for error messages
        data: Dictionary produced from KDL (or CLI flags)

    Returns:
        Profile object

    Raises:
        ConfigError: If profile data is invalid
    """
    # Handle inherit as string or list
    inherit_raw = data.get('inherit', [])
    if isinstance(inherit_raw, str):
        inherit = [inherit_raw]
    elif isinstance(inherit_raw, list):
        inherit = inherit_raw
    else:
        raise ConfigError(f'Profile [{name}] inherit must be string or list')

    # Get mount lists (all should be lists or single paths)
    rw = _normalize_path_list(data.get('rw', []), f'Profile [{name}] rw')
    ro = _normalize_path_list(data.get('ro', []), f'Profile [{name}] ro')
    tmpfs = _normalize_path_list(data.get('tmpfs', []), f'Profile [{name}] tmpfs')
    bwargs = _normalize_path_list(data.get('bwargs', []), f'Profile [{name}] bwargs')

    bool_kwargs: dict[str, bool] = {}
    for opt in BOOL_OPTIONS:
        bool_kwargs[opt.dest] = _get_bool(data, opt.key, f'Profile [{name}]')

    return Profile(
        name=name,
        inherit=inherit,
        rw=rw,
        ro=ro,
        tmpfs=tmpfs,
        bwargs=bwargs,
        **bool_kwargs,
    )


def _normalize_path_list(value: Any, context: str) -> list[str]:
    """
    Normalize a path value to a list of strings.

    Accepts single string or list of strings, normalizes to list.

    Args:
        value: Value from config (string, list, or dict)
        context: Context for error messages

    Returns:
        List of string values

    Raises:
        ConfigError: If value type is invalid
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise ConfigError(f'{context} must contain only strings')
        return value
    if isinstance(value, dict):
        # Support dict-style with keys as paths
        return list(value.keys())
    raise ConfigError(f'{context} must be string, list, or dict')


def _get_bool(data: dict[str, Any], key: str, context: str, default: bool = False) -> bool:
    """Read a boolean key from a config dict with validation."""
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f'{context} {key} must be a boolean')
    return value


# ============================================================================
# Profile Resolution
# ============================================================================


def resolve_profile(profile: Profile, all_profiles: dict[str, Profile], visited: set[str] | None = None) -> Profile:
    """
    Recursively resolve profile inheritance.

    Follows the inheritance chain, detecting cycles and merging parent/child configs.
    For list fields, concatenates them (parent first, then child).

    Args:
        profile: Profile to resolve
        all_profiles: All available profiles for lookup
        visited: Set of already-visited profile names (for cycle detection)

    Returns:
        Resolved Profile with all inheritance applied

    Raises:
        ConfigError: If circular inheritance is detected
    """
    if visited is None:
        visited = set()

    if profile.name in visited:
        cycle = ' -> '.join(list(visited) + [profile.name])
        raise ConfigError(f'Circular inheritance detected: {cycle}')

    visited.add(profile.name)

    # If no inheritance, return as-is
    if not profile.inherit:
        visited.discard(profile.name)
        return profile

    # Recursively resolve parents
    resolved = Profile(name=profile.name)

    for parent_name in profile.inherit:
        if parent_name not in all_profiles:
            raise ConfigError(f'Unknown parent profile: {parent_name}')

        parent = resolve_profile(all_profiles[parent_name], all_profiles, visited.copy())

        # Merge parent into resolved (lists concatenate)
        resolved.rw.extend(parent.rw)
        resolved.ro.extend(parent.ro)
        resolved.tmpfs.extend(parent.tmpfs)
        resolved.bwargs.extend(parent.bwargs)
        for opt in BOOL_OPTIONS:
            setattr(resolved, opt.dest, getattr(resolved, opt.dest) or getattr(parent, opt.dest))

    # Merge child's values (child overrides parent)
    resolved.rw.extend(profile.rw)
    resolved.ro.extend(profile.ro)
    resolved.tmpfs.extend(profile.tmpfs)
    resolved.bwargs.extend(profile.bwargs)
    for opt in BOOL_OPTIONS:
        setattr(resolved, opt.dest, getattr(resolved, opt.dest) or getattr(profile, opt.dest))

    return resolved


def resolve_profile_chain(profile: Profile, all_profiles: dict[str, Profile]) -> tuple[Profile, list[str]]:
    """
    Resolve a profile and return the inheritance chain.

    Chain order is parents first, then the requested profile (e.g. ["base", "child"]).
    """

    def _walk(p: Profile, visited: set[str]) -> tuple[Profile, list[str]]:
        if p.name in visited:
            cycle = ' -> '.join(list(visited) + [p.name])
            raise ConfigError(f'Circular inheritance detected: {cycle}')

        visited = set(visited)
        visited.add(p.name)

        if not p.inherit:
            return p, [p.name]

        resolved = Profile(name=p.name)
        chain: list[str] = []

        for parent_name in p.inherit:
            if parent_name not in all_profiles:
                raise ConfigError(f'Unknown parent profile: {parent_name}')
            parent_resolved, parent_chain = _walk(all_profiles[parent_name], visited)
            chain.extend(parent_chain)

            resolved.rw.extend(parent_resolved.rw)
            resolved.ro.extend(parent_resolved.ro)
            resolved.tmpfs.extend(parent_resolved.tmpfs)
            resolved.bwargs.extend(parent_resolved.bwargs)
            for opt in BOOL_OPTIONS:
                setattr(resolved, opt.dest, getattr(resolved, opt.dest) or getattr(parent_resolved, opt.dest))

        resolved.rw.extend(p.rw)
        resolved.ro.extend(p.ro)
        resolved.tmpfs.extend(p.tmpfs)
        resolved.bwargs.extend(p.bwargs)
        for opt in BOOL_OPTIONS:
            setattr(resolved, opt.dest, getattr(resolved, opt.dest) or getattr(p, opt.dest))

        chain.append(p.name)
        return resolved, chain

    return _walk(profile, set())


# ============================================================================
# Validation
# ============================================================================


def validate_config(config: Config) -> None:
    """
    Validate configuration for common issues.

    Checks:
    - No circular inheritance in profiles or defaults
    - All referenced parent profiles exist
    - Validates path patterns

    Args:
        config: Configuration to validate

    Raises:
        ConfigError: If validation fails
    """
    # Combined lookup for inheritance resolution (profiles may inherit defaults and vice versa).
    all_profiles = {**config.profiles, **config.defaults}

    # Validate all profiles can be resolved (catches circular inheritance)
    for profile in config.profiles.values():
        try:
            resolve_profile(profile, all_profiles)
        except ConfigError:
            raise

    # Validate all defaults can be resolved
    for profile in config.defaults.values():
        try:
            resolve_profile(profile, all_profiles)
        except ConfigError:
            raise


# ============================================================================
# Runtime Configuration Building
# ============================================================================


def build_runtime_config(config: Config, args: argparse.Namespace, command: list[str]) -> RuntimeConfig:
    """
    Build final RuntimeConfig by merging all sources.

    Merges configuration in order: defaults -> profile -> CLI args
    Later sources override earlier ones.

    Args:
        config: Parsed configuration
        args: Parsed command-line arguments
        command: Target command and its arguments

    Returns:
        RuntimeConfig ready for execution

    Raises:
        ConfigError: If command is missing or profile resolution fails
    """
    if not command:
        raise ConfigError('No command specified')

    config_dir = get_config_path(getattr(args, 'config', None)).parent
    raw_exe = command[0]
    # Defaults are keyed by a stable "command name" (e.g. "chromium"), not by an
    # expanded/resolved path (e.g. "/nix/store/.../chromium").
    defaults_key = Path(raw_exe).name if '/' in raw_exe else raw_exe
    debug(f'Use defaults: {defaults_key}', enabled=getattr(args, 'debug', False))

    # Start with default profile for the command (unless --no-default)
    merged_profile = Profile(name='_runtime')

    # Combined profile/defaults lookup for inheritance resolution
    all_profiles = {**config.profiles, **config.defaults}

    # 1. Apply command defaults
    if not args.no_default and defaults_key in config.defaults:
        default, chain = resolve_profile_chain(config.defaults[defaults_key], all_profiles)
        debug(
            'Profile chain: ' + ' -> '.join(chain),
            enabled=getattr(args, 'debug', False),
        )
        merged_profile.rw.extend(default.rw)
        merged_profile.ro.extend(default.ro)
        merged_profile.tmpfs.extend(default.tmpfs)
        merged_profile.bwargs.extend(default.bwargs)
        for opt in BOOL_OPTIONS:
            setattr(merged_profile, opt.dest, getattr(merged_profile, opt.dest) or getattr(default, opt.dest))

    # 2. Apply explicit profile from -p
    if args.profile:
        profile_ref = args.profile

        def _available() -> str:
            parts: list[str] = []
            if config.profiles:
                parts.append('profiles: ' + ', '.join(sorted(config.profiles.keys())))
            if config.defaults:
                parts.append('defaults: ' + ', '.join(sorted(config.defaults.keys())))
            return '; '.join(parts) if parts else '(none)'

        # Support explicit namespaces: "profiles.NAME" and "defaults.NAME".
        if profile_ref.startswith('profiles.'):
            key = profile_ref.split('.', 1)[1]
            if key not in config.profiles:
                raise ConfigError(f'Unknown profile: {profile_ref}. Available: {_available()}')
            profile, chain = resolve_profile_chain(config.profiles[key], all_profiles)
            debug('profile chain: ' + ' -> '.join(chain), enabled=getattr(args, 'debug', False))
        elif profile_ref.startswith('defaults.'):
            key = profile_ref.split('.', 1)[1]
            if key not in config.defaults:
                raise ConfigError(f'Unknown profile: {profile_ref}. Available: {_available()}')
            profile, chain = resolve_profile_chain(config.defaults[key], all_profiles)
            debug('profile chain: ' + ' -> '.join(chain), enabled=getattr(args, 'debug', False))
        else:
            in_profiles = profile_ref in config.profiles
            in_defaults = profile_ref in config.defaults
            if in_profiles and in_defaults:
                raise ConfigError(
                    f'Ambiguous profile name: {profile_ref}. Use profiles.{profile_ref} or defaults.{profile_ref}.'
                )
            if in_profiles:
                profile, chain = resolve_profile_chain(config.profiles[profile_ref], all_profiles)
                debug('profile chain: ' + ' -> '.join(chain), enabled=getattr(args, 'debug', False))
            elif in_defaults:
                profile, chain = resolve_profile_chain(config.defaults[profile_ref], all_profiles)
                debug('profile chain: ' + ' -> '.join(chain), enabled=getattr(args, 'debug', False))
            else:
                raise ConfigError(f'Unknown profile: {profile_ref}. Available: {_available()}')

        merged_profile.rw.extend(profile.rw)
        merged_profile.ro.extend(profile.ro)
        merged_profile.tmpfs.extend(profile.tmpfs)
        merged_profile.bwargs.extend(profile.bwargs)
        for opt in BOOL_OPTIONS:
            setattr(merged_profile, opt.dest, getattr(merged_profile, opt.dest) or getattr(profile, opt.dest))

    # 3. Apply CLI arguments (highest priority)
    cli_data: dict[str, Any] = {
        'rw': getattr(args, 'rw', []),
        'ro': getattr(args, 'ro', []),
        'tmpfs': getattr(args, 'tmpfs', []),
        'bwargs': parse_bwargs(args.bwargs) if getattr(args, 'bwargs', None) else [],
    }
    for opt in BOOL_OPTIONS:
        cli_data[opt.key] = bool(getattr(args, opt.dest, False))
    cli_profile = _parse_profile('_cli', cli_data)

    merged_profile.rw.extend(cli_profile.rw)
    merged_profile.ro.extend(cli_profile.ro)
    merged_profile.tmpfs.extend(cli_profile.tmpfs)
    merged_profile.bwargs.extend(cli_profile.bwargs)
    for opt in BOOL_OPTIONS:
        setattr(merged_profile, opt.dest, getattr(merged_profile, opt.dest) or getattr(cli_profile, opt.dest))

    # 5. Expand mount patterns and build Mount objects
    mounts: set[Mount] = set()

    for path in merged_profile.rw:
        expanded_paths = expand_glob_pattern(path, config_dir)
        for expanded in expanded_paths:
            mounts.add(Mount(expanded, 'rw'))

    for path in merged_profile.ro:
        expanded_paths = expand_glob_pattern(path, config_dir)
        for expanded in expanded_paths:
            mounts.add(Mount(expanded, 'ro'))

    for path in merged_profile.tmpfs:
        expanded_paths = expand_glob_pattern(path, config_dir)
        for expanded in expanded_paths:
            mounts.add(Mount(expanded, 'tmpfs'))

    # 6. Deduplicate mounts (last mount for same path wins)
    mounts_list = list(mounts)
    unique_mounts: dict[str, Mount] = {}
    for mount in mounts_list:
        unique_mounts[mount.path] = mount

    expanded_command = list(command)
    if not expanded_command:
        raise ConfigError("empty command")

    exe = expanded_command[0]
    # If the user provided a path-like executable, expand it to an absolute path.
    # Otherwise, resolve via PATH (useful when bwrap's environment/path differs).
    if '/' in exe or exe.startswith(('.', '~')) or '%' in exe:
        expanded_command[0] = expand_path(exe, config_dir)
    else:
        resolved = shutil.which(exe)
        if resolved:
            expanded_command[0] = resolved

    # Ensure the executed binary itself is available inside the sandbox.
    # Mount it read-only at the same path, but only when it isn't already covered
    # by an existing mount (e.g. /nix/store is already mounted).
    def _covered_by_mounts(target: Path) -> bool:
        # Treat a mount as covering its subtree (bwrap bind mounts directories recursively).
        target_str = str(Path(target).absolute())
        for mnt in unique_mounts.values():
            if mnt.mode == 'tmpfs':
                continue
            if target_str == mnt:
                return True
            prefix = mnt.path.rstrip('/') + os.sep
            if target_str.startswith(prefix):
                return True
        return False

    exe_path = Path(expanded_command[0])
    if not _covered_by_mounts(exe_path):
        unique_mounts[str(exe_path)] = Mount(str(exe_path), 'ro')
        debug(
            f'auto ro-bind exe: {exe_path}',
            enabled=getattr(args, 'debug', False),
        )

    # When the network namespace is shared, libc name resolution typically relies on
    # a few /etc/* files. Mount them read-only if present, without overriding
    # any explicit mounts from config/CLI.
    if merged_profile.share_net:
        for etc_path in (
            '/etc/hosts',
            '/etc/resolv.conf',
            '/etc/resolve.conf',  # common typo; include if it exists
            '/etc/nsswitch.conf',
        ):
            if etc_path in unique_mounts:
                continue
            if Path(etc_path).exists():
                unique_mounts[etc_path] = Mount(etc_path, 'ro')

    return RuntimeConfig(
        argv0=raw_exe,
        command=expanded_command,
        mounts=set(unique_mounts.values()),
        bwargs=merged_profile.bwargs,
        **{opt.dest: getattr(merged_profile, opt.dest) for opt in BOOL_OPTIONS},
        debug=getattr(args, 'debug', False),
        debug_tmpfs=getattr(args, 'debug_tmpfs', False),
    )
