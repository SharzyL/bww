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
    # ----- bool: outer-process wrapper -----
    OptionSpec(
        kind='bool',
        key='extra-unshare-net',
        dest='extra_unshare_net',
        cli_flag='--extra-unshare-net',
        help=(
            'Wrap bwrap in `unshare --fork --user --map-root-user --net --` '
            'so the sandbox netns is created at the outer level (owned by a '
            'userns where you keep full caps; uid 0 there gives setup-script '
            'tooling legacy-root caps across exec). bww auto-adds bwrap '
            '`--uid/--gid` to map back to your host uid inside the sandbox '
            'so payload `id` stays transparent.'
        ),
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
    # ----- DNS override -----
    OptionSpec(
        kind='pattern-list',
        key='nameserver',
        dest='nameserver',
        cli_flag='--nameserver',
        help=(
            'DNS nameserver to use inside the sandbox (repeatable). If '
            'any are specified, bww writes a temp file with one '
            '`nameserver <addr>` line per entry and ro-binds it as '
            '/etc/resolv.conf (overriding any host resolv.conf mount).'
        ),
        cli_metavar='ADDR',
    ),
    # ----- setup hook (runs while bwrap is paused on --block-fd) -----
    # Always runs on the host via `bash -c`. Use `nsenter`/`setns`-style
    # tools with the exported env vars to enter any sandbox namespace
    # yourself. Reuses 'pattern-list' value-shape: list of strings, appended
    # across profile-inheritance and CLI; each entry is one bash invocation.
    OptionSpec(
        kind='pattern-list',
        key='setup-script',
        dest='setup_script',
        cli_flag='--setup-script',
        help=(
            'Bash snippet to run on the host while bwrap is paused, before '
            'the payload exec. Repeatable; entries run in declaration order '
            '(profile-inheritance order, then CLI). Exported env: '
            "CHILD_PID = bwrap's sandbox-init pid; "
            'BWRAP_NETNS = /proc/<child_pid>/ns/net; '
            'BWRAP_USERNS = /proc/<bww_pid>/fd/<N> (fd handle; no proc '
            'lives in this userns); '
            'EXTRA_{NETNS,USERNS} = /proc/<unshare_pid>/ns/{net,user} '
            '(only when --extra-unshare-net is on). Daemons backgrounded '
            'here (e.g. `tun2socks ... &`) are SIGTERMed automatically '
            'when bwrap exits — bww runs as a subreaper. Procfs-path env '
            'vars survive cross-userns access (e.g. pasta after setns); '
            "the fd-handle one only works from bww's own userns."
        ),
        cli_metavar='CMD',
    ),
)
