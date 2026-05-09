"""Registry-driven option dispatchers.

Each pipeline phase (KDL parse, CLI argparse, CLI value, merge, mount build,
env resolve, bwrap emit) has one switch on `OptionSpec.kind`. Per-option
behavior that differs between options of the same kind (e.g. bwrap emit)
lives in `executor._emit_option`, dispatched by `spec.key` instead.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path
from typing import Any

import kdl

from .expand import expand_env_vars, expand_glob_pattern, expand_path, parse_bwargs
from .models import ConfigError, Mount, MountEntry, Profile
from .options import OPTIONS, OptionSpec
from .utils import logger

__all__ = [
    'add_cli_arg',
    'elide_redundant_inplace',
    'merge_profile',
    'read_kdl_option',
    'resolve_env_directives',
    'resolve_symlink_srcs',
]


# ---------------------------------------------------------------------------
# KDL value readers (used by `loader._kdl_profile_to_dict`)
# ---------------------------------------------------------------------------


def _kdl_strings(node: kdl.Node, key: str) -> list[str]:
    """Collect all string args from `key` nodes inside `node`."""
    out: list[str] = []
    for n in list(node.getAll(key)):
        for arg in n.args:
            if not isinstance(arg, str):
                raise ConfigError(f'Profile [{node.name}] {key} values must be strings')
            out.append(arg)
    return out


def _kdl_bool(node: kdl.Node, key: str) -> bool:
    """Read a single-bool option (multiple occurrences allowed; last wins)."""
    val = False
    for n in list(node.getAll(key)):
        if len(n.args) != 1 or not isinstance(n.args[0], bool):
            raise ConfigError(f'Profile [{node.name}] {key} must be a boolean')
        val = n.args[0]
    return val


def _kdl_kv_pairs(node: kdl.Node, key: str) -> list[tuple[str, str]]:
    """Collect (key, value) pairs from `key` nodes (must take exactly 2 string args)."""
    out: list[tuple[str, str]] = []
    for n in list(node.getAll(key)):
        if len(n.args) != 2 or not all(isinstance(a, str) for a in n.args):
            raise ConfigError(f'Profile [{node.name}] {key} must be: {key} "KEY" "VALUE"')
        out.append((n.args[0], n.args[1]))
    return out


def _kdl_mount_entries(node: kdl.Node, spec: OptionSpec) -> list[MountEntry]:
    """Parse mount-kind KDL nodes into MountEntry list.

    Accepted forms:
      `rw "/home"`            → MountEntry(src='/home', dest='/home')   (in-place)
      `rw "/host" "/sandbox"` → MountEntry(src='/host', dest='/sandbox') (non-in-place)
      `tmpfs "/work"`         → MountEntry(src=None, dest='/work')
    `tmpfs` rejects the 2-arg form (no host source). Glob in src with an
    explicit dest is rejected later, in `_build_mounts`.
    """
    out: list[MountEntry] = []
    for n in list(node.getAll(spec.key)):
        args = list(n.args)
        if not all(isinstance(a, str) for a in args):
            raise ConfigError(f'Profile [{node.name}] {spec.key} args must be strings')
        if spec.mount_mode == 'tmpfs':
            if len(args) != 1:
                raise ConfigError(f'Profile [{node.name}] tmpfs takes exactly 1 string arg')
            out.append(MountEntry(src=None, dest=args[0]))
            continue
        if len(args) == 1:
            out.append(MountEntry(src=args[0], dest=args[0]))  # in-place
        elif len(args) == 2:
            out.append(MountEntry(src=args[0], dest=args[1]))  # non-in-place
        else:
            raise ConfigError(f'Profile [{node.name}] {spec.key} takes 1 or 2 string args, got {len(args)}')
    return out


def read_kdl_option(node: kdl.Node, spec: OptionSpec) -> Any:
    """Dispatch KDL value-reading by option kind."""
    match spec.kind:
        case 'bool':
            return _kdl_bool(node, spec.key)
        case 'mount':
            return _kdl_mount_entries(node, spec)
        case 'string-list' | 'pattern-list':
            return _kdl_strings(node, spec.key)
        case 'kv-list':
            return _kdl_kv_pairs(node, spec.key)
        case _:
            raise ValueError(f'read_kdl_option: unknown kind {spec.kind!r}')


# ---------------------------------------------------------------------------
# CLI argparse + value extraction
# ---------------------------------------------------------------------------


def add_cli_arg(parser: argparse.ArgumentParser, spec: OptionSpec) -> None:
    """Register the argparse argument for one OptionSpec.

    Bool options are `store_true`; mount / pattern-list / kv-list use
    `action='append'`; string-list (bwargs) takes a single value.
    """
    match spec.kind:
        case 'bool':
            parser.add_argument(spec.cli_flag, action='store_true', dest=spec.dest, help=spec.help)
        case 'mount' | 'pattern-list' | 'kv-list':
            parser.add_argument(
                spec.cli_flag,
                action='append',
                default=[],
                metavar=spec.cli_metavar or 'VALUE',
                dest=spec.dest,
                help=spec.help,
            )
        case 'string-list':
            # bwargs: a single space-separated string (parsed via shlex later).
            parser.add_argument(spec.cli_flag, metavar='ARGS', dest=spec.dest, help=spec.help)
        case _:
            raise ValueError(f'add_cli_arg: unknown kind {spec.kind!r}')


def from_cli(args: argparse.Namespace, spec: OptionSpec) -> Any:
    """Extract one option's value from the argparse Namespace.

    Returns a value of the same shape as `Profile.<spec.dest>` would hold:
      - bool                     for kind='bool'
      - list[MountEntry]         for kind='mount'
      - list[str]                for kind='string-list' / 'pattern-list'
      - list[tuple[str, str]]    for kind='kv-list' (parsed from KEY=VALUE)
    """
    raw = getattr(args, spec.dest, None)
    match spec.kind:
        case 'bool':
            return bool(raw)
        case 'mount':
            paths = list(raw or [])
            if spec.mount_mode == 'tmpfs':
                return [MountEntry(src=None, dest=p) for p in paths]
            return [MountEntry(src=p, dest=p) for p in paths]
        case 'string-list':
            return parse_bwargs(raw) if raw else []
        case 'pattern-list':
            return list(raw or [])
        case 'kv-list':
            out: list[tuple[str, str]] = []
            for item in raw or []:
                if '=' not in item:
                    raise ConfigError(f'{spec.cli_flag} requires KEY=VALUE format, got: {item!r}')
                key, value = item.split('=', 1)
                if not key:
                    raise ConfigError(f'{spec.cli_flag} KEY cannot be empty: {item!r}')
                out.append((key, value))
            return out
        case _:
            raise ValueError(f'from_cli: unknown kind {spec.kind!r}')


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _merge_option(a: Any, b: Any, spec: OptionSpec) -> Any:
    """Merge two values for one option according to its kind.

    Bools merge with OR (any True wins). All list-shaped kinds extend
    (concat); semantic dedup (set-env last-wins-per-key, unset-env pattern
    matching, mount dest dedup) happens later in resolve_env_directives /
    _build_mounts.
    """
    match spec.kind:
        case 'bool':
            return bool(a) or bool(b)
        case 'mount' | 'string-list' | 'kv-list' | 'pattern-list':
            return [*(a or []), *(b or [])]
        case _:
            raise ValueError(f'_merge_option: unknown kind {spec.kind!r}')


def merge_profile(dst: Profile, src: Profile) -> None:
    """Merge `src` into `dst` in place, dispatching by option kind."""
    for spec in OPTIONS:
        merged = _merge_option(getattr(dst, spec.dest), getattr(src, spec.dest), spec)
        setattr(dst, spec.dest, merged)


# ---------------------------------------------------------------------------
# Mount builder + env-directive resolver (post-merge resolution stages)
# ---------------------------------------------------------------------------


def build_mounts(merged: Profile, config_dir: Path) -> dict[str, Mount]:
    """Expand all mount-kind specs into Mount objects, deduplicated by dest.

    Iteration order is OPTIONS order × per-spec list order, so the dest-keyed
    overwrite is deterministic: later entries (further down the merge chain
    or later in the same list) win.

    Glob expansions log a debug entry — they're bwrap args bww derived rather
    than receiving verbatim from the user.
    """
    expanded: list[Mount] = []
    for spec in OPTIONS:
        if spec.kind != 'mount':
            continue
        mode = spec.mount_mode
        if mode is None:
            raise ValueError(f'mount spec {spec.key!r} missing mount_mode')
        for entry in getattr(merged, spec.dest):
            if entry.src is None:
                # tmpfs: no src, no glob; just expand specifiers in dest.
                expanded.append(Mount(src=None, dest=expand_path(entry.dest, config_dir), mode=mode))
                continue
            if entry.src == entry.dest:
                # In-place: glob-expand src; dest matches each expansion.
                matches = expand_glob_pattern(entry.src, config_dir)
                if any(c in entry.src for c in '*?[]'):
                    logger.debug(f'glob {entry.src!r} → {matches}')
                for src in matches:
                    expanded.append(Mount(src=src, dest=src, mode=mode))
            else:
                # Non-in-place: src must be concrete (no glob).
                if any(c in entry.src for c in '*?[]'):
                    raise ConfigError(
                        f'mount {spec.key} {entry.src!r} -> {entry.dest!r}: '
                        f'glob is not allowed when an explicit dest is given'
                    )
                expanded.append(
                    Mount(src=expand_path(entry.src, config_dir), dest=expand_path(entry.dest, config_dir), mode=mode)
                )

    by_dest: dict[str, Mount] = {}
    for m in expanded:
        by_dest[m.dest] = m
    return by_dest


def _walk_symlink_chain(start: Path) -> tuple[Path, list[tuple[Path, Path]]]:
    """Walk a host symlink chain one hop at a time.

    Returns (final_resolved_path, [(link, hop_target), ...]) where each
    `(link, hop_target)` mirrors one host symlink hop. `hop_target` is the
    readlink content made absolute against `link.parent` if relative, with
    `..`/`.` collapsed but no further symlink resolution applied — that way
    the chain in the sandbox preserves the host's intermediate structure.
    Raises ConfigError on a symlink loop.
    """
    chain: list[tuple[Path, Path]] = []
    cur = start
    seen: set[str] = set()
    while cur.is_symlink():
        key = str(cur)
        if key in seen:
            raise ConfigError(f'symlink loop while resolving {start}: revisited {key}')
        seen.add(key)
        raw = os.readlink(cur)
        nxt_raw = Path(raw) if raw.startswith('/') else cur.parent / raw
        nxt = Path(os.path.normpath(nxt_raw))
        chain.append((cur, nxt))
        cur = nxt
    return cur, chain


def resolve_symlink_srcs(by_dest: dict[str, Mount]) -> tuple[dict[str, Mount], list[tuple[str, str]]]:
    """Rewrite in-place mounts whose src is a symlink: ro/rw-bind the final
    resolved target, and emit one pending `--symlink hop_target link` per
    host symlink hop so the sandbox preserves the entire chain — not just
    the final destination.

    Non-in-place, tmpfs, and non-symlink mounts pass through unchanged.
    Returns (rewritten by_dest, list of pending (target, link) symlinks).
    """
    out: dict[str, Mount] = {}
    pending: list[tuple[str, str]] = []
    for dest, m in by_dest.items():
        if m.src is None or m.src != m.dest or not Path(m.src).is_symlink():
            out[dest] = m
            continue
        final, chain = _walk_symlink_chain(Path(m.src))
        out[str(final)] = Mount(src=str(final), dest=str(final), mode=m.mode)
        for link, hop_target in chain:
            pending.append((str(hop_target), str(link)))
        chain_str = ' -> '.join([m.src, *(str(t) for _, t in chain)])
        logger.debug(f'resolve symlink {chain_str} (bind {final}, defer {len(chain)} --symlink)')
    return out, pending


def _inplace_covering_ancestor(m: Mount, by_dest: dict[str, Mount]) -> Mount | None:
    """Return the nearest ancestor in `by_dest` that makes `m` redundant.

    Redundant ⇔ both `m` and the nearest ancestor are in-place binds of the
    same mode. Non-in-place children, tmpfs children, and children whose
    nearest ancestor differs in mode (deliberate override) or geometry
    (non-in-place ancestor) are kept. Walks only to the nearest ancestor;
    further-up ancestors are shadowed locally and don't establish coverage.
    """
    if m.src is None or m.src != m.dest:
        return None
    p = Path(m.dest).parent
    while True:
        anc = by_dest.get(str(p))
        if anc is not None:
            if anc.src is not None and anc.src == anc.dest and anc.mode == m.mode:
                return anc
            return None
        if p == p.parent:
            return None
        p = p.parent


def elide_redundant_inplace(by_dest: dict[str, Mount]) -> dict[str, Mount]:
    """Drop in-place children whose nearest ancestor is a same-mode in-place
    bind — the child mount would be a no-op vs. the ancestor's coverage.

    Iterates dest-sorted so each decision sees its parent already committed
    to `out` (parents come before children in lex order on normalized paths).
    """
    out: dict[str, Mount] = {}
    for dest in sorted(by_dest):
        m = by_dest[dest]
        anc = _inplace_covering_ancestor(m, out)
        if anc is not None:
            logger.debug(f'elide {m.mode} {m.dest} (covered by {anc.dest})')
            continue
        out[dest] = m
    return out


def resolve_env_directives(
    set_env_pairs: list[tuple[str, str]],
    unset_env_patterns: list[str],
    environ: dict[str, str] | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Resolve set-env values and unset-env patterns.

    For set-env: expands `${ENV_NAME}` in values against `environ`. If the
    value's expansion changed it, a debug log records the substitution.
    For unset-env: expands `${ENV_NAME}` in patterns, then matches each pattern
    against `environ` keys using fnmatch (case-sensitive). Vars also being set
    are excluded from the unset list (set-env wins). Wildcard or
    `${VAR}`-substituted patterns log their concrete matches at debug level —
    these are bwrap args bww derived rather than getting verbatim from the user.
    """
    if environ is None:
        environ = dict(os.environ)

    resolved_set: list[tuple[str, str]] = []
    for k, v in set_env_pairs:
        expanded = expand_env_vars(v)
        if expanded != v:
            logger.debug(f'set-env {k}: {v!r} → {expanded!r}')
        resolved_set.append((k, expanded))

    set_keys = {k for k, _ in resolved_set}
    matched: set[str] = set()
    for pat in unset_env_patterns:
        expanded_pat = expand_env_vars(pat)
        per_pattern: list[str] = []
        for var in environ:
            if var in set_keys:
                continue
            if fnmatch.fnmatchcase(var, expanded_pat):
                if var not in matched:
                    per_pattern.append(var)
                matched.add(var)
        if any(c in expanded_pat for c in '*?[]') or expanded_pat != pat:
            logger.debug(f'unset-env {pat!r} → {sorted(per_pattern)}')

    return resolved_set, sorted(matched)
