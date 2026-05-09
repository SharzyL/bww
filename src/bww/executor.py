"""Execution layer: bubblewrap command building and execution."""

import os
import shlex
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Mount, RuntimeConfig


class ExecutionError(Exception):
    """Raised when bwrap execution fails."""

    pass


def format_bwrap_command(argv: list[str]) -> str:
    """
    Format a bwrap argv as a readable, grouped multi-line string.

    - First line: "bwrap" plus global flags.
    - Following lines: one option group per line (e.g. "--ro-bind A B").
    - Final line: "--" plus the command to execute.
    """
    if not argv:
        return ''

    # Be defensive: we only special-case formatting when this is actually bwrap.
    if argv[0] != 'bwrap':
        return shlex.join(argv)

    flag_opts = {
        '--unshare-user',
        '--unshare-ipc',
        '--unshare-pid',
        '--unshare-net',
        '--unshare-uts',
        '--unshare-cgroup',
        '--die-with-parent',
        '--new-session',
    }

    # Options with fixed arity we use in this project.
    fixed_arity: dict[str, int] = {
        '--dev': 1,
        '--dev-bind': 2,
        '--proc': 1,
        '--tmpfs': 1,
        '--chdir': 1,
        '--argv0': 1,
        '--bind': 2,
        '--ro-bind': 2,
        '--setenv': 2,
        '--unsetenv': 1,
    }

    i = 1
    flags: list[str] = []
    while i < len(argv) and argv[i] in flag_opts:
        flags.append(argv[i])
        i += 1

    lines: list[str] = [shlex.join(['bwrap', *flags])]

    while i < len(argv):
        tok = argv[i]

        if tok == '--':
            lines.append('  ' + shlex.join(argv[i:]))
            break

        # Known fixed-arity options.
        if tok in fixed_arity:
            n = fixed_arity[tok]
            group = argv[i : i + 1 + n]
            lines.append('  ' + shlex.join(group))
            i += 1 + n
            continue

        # Known global flags that might appear later.
        if tok in flag_opts:
            lines.append('  ' + tok)
            i += 1
            continue

        # Heuristic for other options (often from user-provided bwargs): group with a single
        # non-option argument if present to avoid splitting "--foo value" across lines.
        if tok.startswith('-'):
            if i + 1 < len(argv) and argv[i + 1] != '--' and not argv[i + 1].startswith('-'):
                lines.append('  ' + shlex.join([tok, argv[i + 1]]))
                i += 2
            else:
                lines.append('  ' + tok)
                i += 1
            continue

        # Fallback: shouldn't happen for well-formed bwrap argv, but don't crash.
        lines.append('  ' + shlex.join([tok]))
        i += 1

    if len(lines) <= 1:
        return '\n'.join(lines)

    # Add line continuation for easy copy/paste into a shell.
    continued: list[str] = []
    for idx, line in enumerate(lines):
        if idx == len(lines) - 1:
            continued.append(line)
        else:
            continued.append(f'{line} \\')

    return '\n'.join(continued)


def merge_mounts(
    *mount_lists: list['Mount'],
) -> set['Mount']:
    """
    Merge mount lists with last-in-wins conflict resolution.

    Merges mounts from multiple sources (defaults, profile, CLI) where
    later sources override earlier ones. For the same path, last-in wins.
    Uses set deduplication to handle mounts efficiently.

    Args:
        *mount_lists: Variable number of mount lists to merge

    Returns:
        Set of Mount objects with conflicts resolved
    """
    mounts_by_path: dict[str, 'Mount'] = {}

    for mount_list in mount_lists:
        for mount in mount_list:
            mounts_by_path[mount.path] = mount

    return set(mounts_by_path.values())


def build_bwrap_command(runtime_config: 'RuntimeConfig') -> list[str]:
    """
    Build complete bwrap command line from runtime configuration.

    Constructs the full bwrap invocation with:
    - Namespace isolation flags
    - Standard device and proc mounts
    - Custom mount points from config
    - Additional bwrap arguments
    - The command to execute

    Args:
        runtime_config: RuntimeConfig with mounts, args, and command

    Returns:
        Complete argv list ready for subprocess execution
    """
    cmd: list[str] = ['bwrap']

    # Match prior --unshare-all behavior, but allow selectively sharing namespaces.
    if not getattr(runtime_config, 'share_user', False):
        cmd.append('--unshare-user')
    if not getattr(runtime_config, 'share_ipc', False):
        cmd.append('--unshare-ipc')
    if not getattr(runtime_config, 'share_pid', False):
        cmd.append('--unshare-pid')
    if not getattr(runtime_config, 'share_net', False):
        cmd.append('--unshare-net')
    if not getattr(runtime_config, 'share_uts', False):
        cmd.append('--unshare-uts')
    cmd.append('--unshare-cgroup')

    cmd += [
        '--die-with-parent',
        *([] if getattr(runtime_config, 'reuse_session', False) else ['--new-session']),
        # Default to a minimal /dev (tmpfs-based). If enabled, bind the host /dev to
        # allow device access.
        *(['--dev-bind', '/dev', '/dev'] if getattr(runtime_config, 'dev_bind', False) else ['--dev', '/dev']),
        '--proc',
        '/proc',
        '--tmpfs',
        '/tmp',
    ]

    # Add configured mounts
    # bubblewrap CLI is order-sensitive for readability (and some edge-cases),
    # so keep a stable ordering: tmpfs mounts first, then read-only, then bind.
    mode_order = {'tmpfs': 0, 'ro': 1, 'rw': 2}
    mounts = sorted(runtime_config.mounts, key=lambda m: (mode_order.get(m.mode, 99), m.path))

    for mount in mounts:
        if mount.mode == 'tmpfs':
            cmd.extend(['--tmpfs', mount.path])
        elif mount.mode == 'rw':
            cmd.extend(['--bind', mount.path, mount.path])
        elif mount.mode == 'ro':
            cmd.extend(['--ro-bind', mount.path, mount.path])

    # Emit env directives. Unset first so set-env wins on overlap (bwrap applies
    # args in order). Both lists are already resolved/expanded by build_runtime_config.
    for var in runtime_config.unset_env:
        cmd.extend(['--unsetenv', var])
    for key, value in runtime_config.set_env:
        cmd.extend(['--setenv', key, value])

    # Add custom bwrap arguments
    cmd.extend(runtime_config.bwargs)

    # Start the command in the caller's working directory (if it's mounted).
    cmd.extend(['--chdir', os.getcwd()])

    # Add command separator and target command
    cmd.extend(['--argv0', runtime_config.argv0])
    cmd.append('--')
    cmd.extend(runtime_config.command)

    return cmd


def execute_bwrap(cmd: list[str], debug_tmpfs: bool) -> int:
    """
    Execute bwrap command and return exit code.

    Runs the bwrap command via subprocess.run(), passing through all
    stdin/stdout/stderr directly. Returns the process exit code.

    Args:
        cmd: Complete bwrap command line
        debug_tmpfs: If True, would print tmpfs contents after exit
                     (placeholder for future implementation)

    Returns:
        Process exit code

    Raises:
        ExecutionError: If bwrap cannot be found or execution fails
    """
    try:
        result = subprocess.run(cmd)
        return result.returncode
    except FileNotFoundError:
        raise ExecutionError('bwrap not found in PATH') from None
    except Exception as e:
        raise ExecutionError(f'Failed to execute bwrap: {e}') from e
