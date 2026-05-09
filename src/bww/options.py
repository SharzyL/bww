"""Option definitions shared between CLI parsing and config parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# OptionKind tags the value-shape and parsing/merging strategy of an option.
# Dispatchers in config.py / executor.py / __init__.py switch on this.
OptionKind = Literal['bool', 'mount', 'string-list', 'kv-list', 'pattern-list']


@dataclass(frozen=True)
class OptionSpec:
    """Declarative description of one per-profile option.

    Drives KDL parsing, CLI argparse, value normalization, merging, and bwrap
    emission. Adding a new option of an existing kind is a single registry
    entry plus matching Profile/RuntimeConfig fields.
    """

    kind: OptionKind
    key: str  # KDL node name and Profile-data dict key, e.g. "share-net", "set-env"
    dest: str  # Profile / RuntimeConfig attribute name, e.g. "share_net"
    cli_flag: str  # argparse flag, e.g. "--share-net"
    help: str
    # Kind-specific extras (None for kinds that don't use them):
    mount_mode: Literal['rw', 'ro', 'tmpfs'] | None = None  # mount kind only
    cli_metavar: str | None = None


# OPTIONS order is the bwrap-emit order: bools first (in unshare order),
# then mount specs (consumed by the mount builder, not the emit loop),
# then list-shaped emission groups (unset-env, set-env, bwargs).
OPTIONS: tuple[OptionSpec, ...] = (
    # ----- bools: namespace shares -----
    OptionSpec(
        kind='bool',
        key='share-user',
        dest='share_user',
        cli_flag='--share-user',
        help='Share the host user namespace (omit bwrap --unshare-user)',
    ),
    OptionSpec(
        kind='bool',
        key='share-ipc',
        dest='share_ipc',
        cli_flag='--share-ipc',
        help='Share the host IPC namespace (omit bwrap --unshare-ipc)',
    ),
    OptionSpec(
        kind='bool',
        key='share-pid',
        dest='share_pid',
        cli_flag='--share-pid',
        help='Share the host PID namespace (omit bwrap --unshare-pid)',
    ),
    OptionSpec(
        kind='bool',
        key='share-net',
        dest='share_net',
        cli_flag='--share-net',
        help='Share the host network namespace (omit bwrap --unshare-net)',
    ),
    OptionSpec(
        kind='bool',
        key='share-uts',
        dest='share_uts',
        cli_flag='--share-uts',
        help='Share the host UTS namespace (omit bwrap --unshare-uts)',
    ),
    # ----- bools: session / device -----
    OptionSpec(
        kind='bool',
        key='reuse-session',
        dest='reuse_session',
        cli_flag='--reuse-session',
        help='Do not create a new session (omit bwrap --new-session)',
    ),
    OptionSpec(
        kind='bool',
        key='dev-bind',
        dest='dev_bind',
        cli_flag='--dev-bind',
        help='Bind-mount host /dev into sandbox (enables device access)',
    ),
    # ----- mounts (handled by mount builder, skipped by emit loop) -----
    OptionSpec(
        kind='mount',
        key='rw',
        dest='rw',
        cli_flag='--rw',
        help='Read-write mount (can be repeated)',
        mount_mode='rw',
        cli_metavar='PATH',
    ),
    OptionSpec(
        kind='mount',
        key='ro',
        dest='ro',
        cli_flag='--ro',
        help='Read-only mount (can be repeated)',
        mount_mode='ro',
        cli_metavar='PATH',
    ),
    OptionSpec(
        kind='mount',
        key='tmpfs',
        dest='tmpfs',
        cli_flag='--tmpfs',
        help='Tmpfs mount (can be repeated)',
        mount_mode='tmpfs',
        cli_metavar='PATH',
    ),
    # ----- env / bwargs (emit order matches current behavior) -----
    OptionSpec(
        kind='pattern-list',
        key='unset-env',
        dest='unset_env',
        cli_flag='--unset-env',
        help=(
            'Unset env vars matching PATTERN inside sandbox (repeatable). '
            'Supports glob wildcards (*, ?, [abc]) and ${VAR} expansion'
        ),
        cli_metavar='PATTERN',
    ),
    OptionSpec(
        kind='kv-list',
        key='set-env',
        dest='set_env',
        cli_flag='--set-env',
        help='Set env var inside sandbox (repeatable). VALUE supports ${VAR} expansion',
        cli_metavar='KEY=VALUE',
    ),
    OptionSpec(
        kind='string-list',
        key='bwargs',
        dest='bwargs',
        cli_flag='--bwargs',
        help='Extra bwrap arguments (space-separated string)',
    ),
)
