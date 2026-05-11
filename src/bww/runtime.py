"""Profile inheritance, validation, and final RuntimeConfig assembly.

Top-level entry points:
  - `validate_config` — sanity-check a parsed Config (no circular inherits, etc.)
  - `build_runtime_config` — merge defaults + profile + CLI into a RuntimeConfig

Inheritance helpers:
  - `resolve_profile` / `resolve_profile_chain` — recursive parent merging
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .dispatch import (
    build_mounts,
    elide_redundant_inplace,
    from_cli,
    merge_profile,
    resolve_env_directives,
    resolve_symlink_srcs,
)
from .expand import expand_path, get_config_path
from .models import Config, ConfigError, Mount, Profile, RuntimeConfig
from .options import OPTIONS
from .utils import logger

__all__ = [
    'build_runtime_config',
    'resolve_profile',
    'resolve_profile_chain',
    'validate_config',
]


# ---------------------------------------------------------------------------
# Inheritance resolution
# ---------------------------------------------------------------------------


def resolve_profile(profile: Profile, all_profiles: dict[str, Profile], visited: set[str] | None = None) -> Profile:
    """Recursively resolve profile inheritance.

    For list fields, parents are concatenated first, then the child. Bools
    OR together. Detects cycles and unknown parent profiles.
    """
    if visited is None:
        visited = set()

    if profile.name in visited:
        cycle = ' -> '.join(list(visited) + [profile.name])
        raise ConfigError(f'Circular inheritance detected: {cycle}')

    visited.add(profile.name)

    if not profile.inherit:
        visited.discard(profile.name)
        return profile

    resolved = Profile(name=profile.name)

    for parent_name in profile.inherit:
        if parent_name not in all_profiles:
            raise ConfigError(f'Unknown parent profile: {parent_name}')

        parent = resolve_profile(all_profiles[parent_name], all_profiles, visited.copy())
        merge_profile(resolved, parent)

    merge_profile(resolved, profile)
    return resolved


def resolve_profile_chain(profile: Profile, all_profiles: dict[str, Profile]) -> tuple[Profile, list[str]]:
    """Resolve a profile and return the inheritance chain (parents first)."""

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
            merge_profile(resolved, parent_resolved)

        merge_profile(resolved, p)
        chain.append(p.name)
        return resolved, chain

    return _walk(profile, set())


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_config(config: Config) -> None:
    """Validate a parsed Config. Currently catches circular inheritance."""
    # Profiles may inherit defaults and vice versa, so the lookup is unified.
    all_profiles = {**config.profiles, **config.defaults}

    for profile in config.profiles.values():
        resolve_profile(profile, all_profiles)

    for profile in config.defaults.values():
        resolve_profile(profile, all_profiles)


# ---------------------------------------------------------------------------
# Runtime config assembly
# ---------------------------------------------------------------------------


def build_runtime_config(config: Config, args: argparse.Namespace, command: list[str]) -> RuntimeConfig:
    """Build the final RuntimeConfig by merging defaults → profile → CLI args.

    Later sources override earlier ones. After merging:
      - mount-kind specs are aggregated into a single `mounts` set (dest-keyed).
      - set-env / unset-env go through `resolve_env_directives`.
      - The exe is auto-mounted ro at its own path.
      - When share-net is true, common /etc resolver files are auto-mounted
        if not already covered by an explicit dest.
    """
    if not command:
        raise ConfigError('No command specified')

    config_dir = get_config_path(getattr(args, 'config', None)).parent
    raw_exe = command[0]
    # Defaults are keyed by a stable command name (e.g. "chromium"), not by an
    # expanded/resolved path (e.g. "/nix/store/.../chromium").
    defaults_key = Path(raw_exe).name if '/' in raw_exe else raw_exe
    logger.debug(f'Use defaults: {defaults_key}')

    merged_profile = Profile(name='_runtime')
    all_profiles = {**config.profiles, **config.defaults}

    # 1. Apply command defaults.
    if not args.no_default and defaults_key in config.defaults:
        default, chain = resolve_profile_chain(config.defaults[defaults_key], all_profiles)
        logger.debug('profile chain: ' + ' -> '.join(chain))
        merge_profile(merged_profile, default)

    # 2. Apply explicit profile from -p.
    if args.profile:
        profile, chain = _resolve_profile_ref(args.profile, config, all_profiles)
        logger.debug('profile chain: ' + ' -> '.join(chain))
        merge_profile(merged_profile, profile)

    # 3. Apply CLI arguments (highest priority).
    cli_profile = Profile(name='_cli')
    for spec in OPTIONS:
        setattr(cli_profile, spec.dest, from_cli(args, spec))
    merge_profile(merged_profile, cli_profile)

    # 4. Build mounts (dest-keyed deterministic dedup; sees all mount-kind specs).
    by_dest = build_mounts(merged_profile, config_dir)

    # 4b. Rewrite mounts whose src is a host symlink: bind the resolved target
    # (so the actual content is reachable without DEST-side symlink traversal
    # at bwrap setup time), and remember a pending `--symlink target original`
    # so the sandbox preserves the symlink at its original path. Decision to
    # actually emit the --symlink is deferred until after elision (step 7c)
    # because a bind ancestor on the original path makes it redundant.
    by_dest, user_pending_symlinks = resolve_symlink_srcs(by_dest)

    # 5. Resolve the executable.
    expanded_command = list(command)
    exe = expanded_command[0]
    if '/' in exe or exe.startswith(('.', '~')) or '%' in exe:
        expanded_command[0] = expand_path(exe, config_dir)
    else:
        resolved = shutil.which(exe)
        if resolved:
            expanded_command[0] = resolved

    # 6. Auto-mount the exe so it stays runnable inside the sandbox.
    #
    # We always ro-bind the resolved target (following any symlink chain) —
    # that is what bwrap actually has to exec. If the exe is itself a
    # symlink, we register a pending `--symlink target exe` so the
    # user-facing exe path stays valid inside the sandbox; the final emit
    # decision is made in step 7c, which knows about both bind and planned
    # symlink ancestors.
    exe_path = str(Path(expanded_command[0]).absolute())
    target_path = str(Path(expanded_command[0]).resolve())
    by_dest[target_path] = Mount(src=target_path, dest=target_path, mode='ro')
    if exe_path == target_path:
        logger.debug(f'auto ro-bind exe: {target_path} (regular file)')
    else:
        logger.debug(f'auto ro-bind exe target: {target_path} (resolved from symlink {exe_path})')

    exe_pending_symlinks: list[tuple[str, str]] = []
    if exe_path != target_path:
        exe_pending_symlinks.append((target_path, exe_path))

    # 6b. nameserver: synthesize /etc/resolv.conf from user-supplied addrs.
    # Goes before step 7 so the share-net auto-mount sees it's already
    # registered and skips. The NamedTemporaryFile object is held on
    # RuntimeConfig.temp_files; closing it at end-of-run triggers
    # tempfile's built-in `delete=True` auto-unlink.
    temp_files: list[Any] = []
    if merged_profile.nameserver:
        resolv = tempfile.NamedTemporaryFile(  # noqa: SIM115 — kept alive on purpose
            mode='w', prefix='bww-resolv-', suffix='.conf', delete=True
        )
        resolv.write('options single-request-reopen\n')
        for addr in merged_profile.nameserver:
            resolv.write(f'nameserver {addr}\n')
        resolv.flush()
        by_dest['/etc/resolv.conf'] = Mount(src=resolv.name, dest='/etc/resolv.conf', mode='ro')
        temp_files.append(resolv)
        logger.debug(f'nameserver: wrote {resolv.name} with {len(merged_profile.nameserver)} entry/entries')

    # 7. share-net: libc name resolution typically relies on /etc/* files.
    # Mount them read-only if present, without overriding explicit user mounts.
    if merged_profile.share_net:
        for etc_path in ('/etc/hosts', '/etc/resolv.conf', '/etc/resolve.conf', '/etc/nsswitch.conf'):
            if etc_path in by_dest:
                continue
            if Path(etc_path).exists():
                by_dest[etc_path] = Mount(src=etc_path, dest=etc_path, mode='ro')
                logger.debug(f'auto ro-bind for share-net: {etc_path}')

    # 7b. Drop in-place children covered by an in-place same-mode ancestor.
    # The auto exe ro-bind, share-net /etc files, and user mounts are all
    # registered by now — the elision pass sees the full picture.
    by_dest = elide_redundant_inplace(by_dest)

    # 7c. Decide which pending symlinks to actually emit. A symlink at `link`
    # is skipped when an ancestor of `link` is already a bind (rw/ro) or an
    # earlier planned symlink — bwrap would EEXIST against an exposed host
    # symlink or EROFS against a read-only ancestor. Tmpfs ancestors do not
    # block (they're writable; bwrap will create the link + intermediate
    # dirs). Sort by `link` so parents are decided first; that way a parent
    # symlink we keep correctly blocks any descendant.
    exe_pending_set = set(exe_pending_symlinks)
    symlinks: list[tuple[str, str]] = []
    for target, link in sorted(exe_pending_symlinks + user_pending_symlinks, key=lambda s: s[1]):
        origin = 'exe' if (target, link) in exe_pending_set else 'user mount'
        blocker = _symlink_blocker(link, by_dest, symlinks)
        if blocker is not None:
            logger.debug(f'skip symlink for {link} ({origin}, would resolve to {target}): covered by {blocker}')
            continue
        symlinks.append((target, link))
        logger.debug(f'emit symlink for {link} ({origin}) -> {target}')

    # 8. Resolve env directives (set-env wins on overlap with unset-env patterns).
    resolved_set_env, resolved_unset_env = resolve_env_directives(
        merged_profile.set_env,
        merged_profile.unset_env,
    )

    # 9. Build RuntimeConfig kwargs from OPTIONS — most specs copy verbatim
    # from merged_profile to RuntimeConfig.<dest>; mount specs aggregate into
    # `mounts`, env specs go through the resolver above.
    kwargs: dict[str, Any] = {
        'argv0': raw_exe,
        'command': expanded_command,
        'mounts': set(by_dest.values()),
        'symlinks': symlinks,
        'set_env': resolved_set_env,
        'unset_env': resolved_unset_env,
        'temp_files': temp_files,
        'debug': getattr(args, 'debug', False),
        'debug_tmpfs': getattr(args, 'debug_tmpfs', False),
    }
    for spec in OPTIONS:
        if spec.kind == 'mount':
            continue  # contributes to `mounts`, not its own RuntimeConfig field
        if spec.dest in ('set_env', 'unset_env'):
            continue  # already resolved above
        kwargs[spec.dest] = getattr(merged_profile, spec.dest)

    return RuntimeConfig(**kwargs)


def _symlink_blocker(
    link: str,
    by_dest: dict[str, Mount],
    planned_symlinks: list[tuple[str, str]],
) -> str | None:
    """Return a description of the nearest covering ancestor of `link` that
    would prevent emitting `--symlink ... {link}`, or None if it's safe.

    Bind ancestors (rw/ro): the host symlink at `link` is already exposed via
    that bind, so a `--symlink` there would EEXIST or EROFS. Earlier planned
    symlinks at an ancestor: the path walk would cross through that symlink
    into a read-only target. Tmpfs ancestors don't block — bwrap creates
    intermediate dirs inside the writable tmpfs.
    """
    planned_link_paths = {pl for _, pl in planned_symlinks}
    p = Path(link).parent
    while True:
        if str(p) in planned_link_paths:
            return f'planned --symlink at {p}'
        anc = by_dest.get(str(p))
        if anc is not None and anc.src is not None:  # rw or ro bind (tmpfs has src=None)
            return f'{anc.mode} bind {anc.dest!r}'
        if p == p.parent:
            return None
        p = p.parent


def _resolve_profile_ref(
    profile_ref: str, config: Config, all_profiles: dict[str, Profile]
) -> tuple[Profile, list[str]]:
    """Resolve a `-p NAME` / `-p profiles.NAME` / `-p defaults.NAME` reference."""

    def _available() -> str:
        parts: list[str] = []
        if config.profiles:
            parts.append('profiles: ' + ', '.join(sorted(config.profiles.keys())))
        if config.defaults:
            parts.append('defaults: ' + ', '.join(sorted(config.defaults.keys())))
        return '; '.join(parts) if parts else '(none)'

    if profile_ref.startswith('profiles.'):
        key = profile_ref.split('.', 1)[1]
        if key not in config.profiles:
            raise ConfigError(f'Unknown profile: {profile_ref}. Available: {_available()}')
        return resolve_profile_chain(config.profiles[key], all_profiles)

    if profile_ref.startswith('defaults.'):
        key = profile_ref.split('.', 1)[1]
        if key not in config.defaults:
            raise ConfigError(f'Unknown profile: {profile_ref}. Available: {_available()}')
        return resolve_profile_chain(config.defaults[key], all_profiles)

    in_profiles = profile_ref in config.profiles
    in_defaults = profile_ref in config.defaults
    if in_profiles and in_defaults:
        raise ConfigError(
            f'Ambiguous profile name: {profile_ref}. Use profiles.{profile_ref} or defaults.{profile_ref}.'
        )
    if in_profiles:
        return resolve_profile_chain(config.profiles[profile_ref], all_profiles)
    if in_defaults:
        return resolve_profile_chain(config.defaults[profile_ref], all_profiles)
    raise ConfigError(f'Unknown profile: {profile_ref}. Available: {_available()}')
