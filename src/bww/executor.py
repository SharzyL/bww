"""Execution layer: bubblewrap command building and execution."""

import os
import shlex
import subprocess
from typing import TYPE_CHECKING

from .options import OPTIONS, OptionSpec

if TYPE_CHECKING:
    from .models import RuntimeConfig


class ExecutionError(Exception):
    """Raised when bwrap execution fails."""

    pass


# A "group" is one logical bwrap argument cluster: a single zero-arg flag,
# or a multi-token option like ['--ro-bind', SRC, DEST]. The leading group
# is conventionally ['bwrap']; the final group is ['--', *command].
BwrapGroups = list[list[str]]


def to_argv(groups: BwrapGroups) -> list[str]:
    """Flatten groups into a flat argv suitable for subprocess.run."""
    return [tok for g in groups for tok in g]


def format_bwrap_command(groups: BwrapGroups) -> str:
    """Format groups as a readable, line-broken bwrap command.

    - Leading 'bwrap' group plus subsequent single-token groups (zero-arg
      flags) are packed onto the first line.
    - Multi-token groups go on their own lines.
    - The final group (starting with '--') is rendered as the last line.
    - Non-final lines get trailing ' \\' for shell copy/paste.
    """
    if not groups:
        return ''
    if not groups[0] or groups[0][0] != 'bwrap':
        # Defensive: not our shape.
        return shlex.join(to_argv(groups))

    lines: list[str] = []
    i = 0

    # First line: 'bwrap' + consecutive single-token flag groups
    first: list[str] = list(groups[i])
    i += 1
    while i < len(groups) and len(groups[i]) == 1 and groups[i][0].startswith('--'):
        first.extend(groups[i])
        i += 1
    lines.append(shlex.join(first))

    # Subsequent groups, one per line
    while i < len(groups):
        g = groups[i]
        lines.append('  ' + shlex.join(g))
        i += 1

    if len(lines) <= 1:
        return '\n'.join(lines)

    # Trailing line continuations for all but the last
    return '\n'.join(line + ' \\' if idx < len(lines) - 1 else line for idx, line in enumerate(lines))


def _emit_option(spec: OptionSpec, runtime: 'RuntimeConfig') -> BwrapGroups:
    """Emit bwrap groups for one option. Dispatches by option name.

    Mount specs are owned by the mount builder (`_emit_mounts`) and return
    [] here. Each non-mount option's emit is a self-contained case so
    idiosyncratic options (like dev-bind owning both true/false branches)
    live in one place.
    """
    if spec.kind == 'mount':
        return []  # owned by _emit_mounts
    match spec.key:
        # --- bools ---
        case 'share-net':
            return [['--unshare-net']] if not runtime.share_net else []
        case 'share-user':
            return [['--unshare-user']] if not runtime.share_user else []
        case 'share-ipc':
            return [['--unshare-ipc']] if not runtime.share_ipc else []
        case 'share-pid':
            return [['--unshare-pid']] if not runtime.share_pid else []
        case 'share-uts':
            return [['--unshare-uts']] if not runtime.share_uts else []
        case 'reuse-session':
            return [['--new-session']] if not runtime.reuse_session else []
        case 'dev-bind':
            # Mutually exclusive with the static prelude — owns both branches.
            return [['--dev-bind', '/dev', '/dev']] if runtime.dev_bind else [['--dev', '/dev']]
        # --- list-shaped options ---
        case 'bwargs':
            return [list(runtime.bwargs)] if runtime.bwargs else []
        case 'set-env':
            return [['--setenv', k, v] for k, v in runtime.set_env]
        case 'unset-env':
            return [['--unsetenv', v] for v in runtime.unset_env]
        case _:
            raise ValueError(f'_emit_option: no case for spec.key={spec.key!r}')


def _emit_mounts(runtime: 'RuntimeConfig') -> BwrapGroups:
    """Emit mount groups in dest lexicographic order — a valid topological
    order for the parent-before-child constraint on normalized paths.

    Mode is no longer part of the sort key: when a tree mixes modes (e.g.
    `rw /etc` + `ro /etc/foo`), parent must still come first so the child's
    override isn't shadowed by a later parent bind.
    """
    out: BwrapGroups = []
    for m in sorted(runtime.mounts, key=lambda m: m.dest):
        if m.mode == 'tmpfs':
            out.append(['--tmpfs', m.dest])
        elif m.mode == 'rw':
            assert m.src is not None
            out.append(['--bind', m.src, m.dest])
        elif m.mode == 'ro':
            assert m.src is not None
            out.append(['--ro-bind', m.src, m.dest])
    return out


def build_bwrap_command(runtime_config: 'RuntimeConfig') -> BwrapGroups:
    """Build the bwrap command as a list of token groups.

    Group layout: [['bwrap'], <zero-arg flag groups>, <multi-token option
    groups>, ['--chdir', cwd], ['--argv0', argv0], ['--', *command]].
    Use `to_argv(...)` to flatten for execution; pass the groups directly to
    `format_bwrap_command(...)` for display.
    """
    groups: BwrapGroups = [['bwrap']]

    # Iterate OPTIONS in registry order. Static separators are inserted at
    # well-defined points to keep the bwrap argv layout stable.
    for spec in OPTIONS:
        groups.extend(_emit_option(spec, runtime_config))
        if spec.key == 'share-uts':
            # After all unshare-* flags, before the rest of the prelude.
            groups.append(['--unshare-cgroup'])
            groups.append(['--die-with-parent'])
        elif spec.key == 'dev-bind':
            # After the /dev decision, before configured mounts.
            groups.append(['--proc', '/proc'])
            groups.append(['--tmpfs', '/tmp'])
            groups.extend(_emit_mounts(runtime_config))
            # Synthetic symlinks come after all mounts: the skip predicate in
            # build_runtime_config has already excluded cases where a bind
            # ancestor would shadow / EEXIST against this; what's left is
            # tmpfs ancestors (writable) or no ancestor at all (bwrap will
            # create intermediate dirs).
            for target, link in runtime_config.symlinks:
                groups.append(['--symlink', target, link])

    # Postlude
    groups.append(['--chdir', os.getcwd()])
    groups.append(['--argv0', runtime_config.argv0])
    groups.append(['--', *runtime_config.command])

    return groups


def execute_bwrap(cmd: list[str], debug_tmpfs: bool) -> int:
    """Execute bwrap argv (already flattened) and return exit code."""
    try:
        result = subprocess.run(cmd)
        return result.returncode
    except FileNotFoundError:
        raise ExecutionError('bwrap not found in PATH') from None
    except Exception as e:
        raise ExecutionError(f'Failed to execute bwrap: {e}') from e
