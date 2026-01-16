"""Option definitions shared between CLI parsing and config parsing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BoolOption:
    key: str
    dest: str
    cli_flag: str
    help: str


BOOL_OPTIONS: tuple[BoolOption, ...] = (
    BoolOption('share-net', 'share_net', '--share-net', 'Share the host network namespace (omit bwrap --unshare-net)'),
    BoolOption('share-user', 'share_user', '--share-user', 'Share the host user namespace (omit bwrap --unshare-user)'),
    BoolOption('share-ipc', 'share_ipc', '--share-ipc', 'Share the host IPC namespace (omit bwrap --unshare-ipc)'),
    BoolOption('share-pid', 'share_pid', '--share-pid', 'Share the host PID namespace (omit bwrap --unshare-pid)'),
    BoolOption('share-uts', 'share_uts', '--share-uts', 'Share the host UTS namespace (omit bwrap --unshare-uts)'),
    BoolOption('dev-bind', 'dev_bind', '--dev-bind', 'Bind-mount host /dev into sandbox (enables device access)'),
    BoolOption(
        'reuse-session', 'reuse_session', '--reuse-session', 'Do not create a new session (omit bwrap --new-session)'
    ),
)
