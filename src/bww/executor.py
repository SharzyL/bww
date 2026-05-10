"""Execution layer: bubblewrap command building and execution."""

import json
import os
import shlex
import subprocess
import threading
from typing import TYPE_CHECKING

from .options import OPTIONS, OptionSpec
from .utils import logger

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

    # Collapse the unshare prelude to `--unshare-all` (plus a trailing
    # `--share-net` if network access is desired) whenever none of the
    # non-net share-* overrides are active. bwrap only ships `--share-net`
    # — there is no `--share-user`/`--share-ipc`/etc — so any of those
    # forces a fall-back to per-flag emission.
    non_net_shares = (
        runtime_config.share_user or runtime_config.share_ipc or runtime_config.share_pid or runtime_config.share_uts
    )
    use_unshare_all = not non_net_shares
    _share_keys = {'share-net', 'share-user', 'share-ipc', 'share-pid', 'share-uts'}

    # Iterate OPTIONS in registry order. Static separators are inserted at
    # well-defined points to keep the bwrap argv layout stable.
    for spec in OPTIONS:
        if not (use_unshare_all and spec.key in _share_keys):
            groups.extend(_emit_option(spec, runtime_config))
        if spec.key == 'share-uts':
            # End of the unshare prelude. --unshare-all already covers cgroup.
            if use_unshare_all:
                groups.append(['--unshare-all'])
                if runtime_config.share_net:
                    groups.append(['--share-net'])
            else:
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


def execute_bwrap(cmd: list[str], debug_tmpfs: bool, debug: bool = False) -> int:
    """Execute bwrap argv (already flattened) and return exit code.

    In `debug` mode we also pass `--info-fd <fd>` to bwrap on a pipe we
    control, drain it on a daemon thread, and log the JSON it produces
    after the child exits. Reading on a thread keeps us from deadlocking
    if a future bwrap version writes more than the pipe buffer can hold.
    """
    try:
        if not debug:
            return subprocess.run(cmd).returncode

        r, w = os.pipe()

        def _drain_and_log_info_fd() -> None:
            """Drain the info-fd pipe and log immediately on EOF.

            bwrap writes its JSON between sandbox setup and exec'ing the
            user command, then closes the fd. By logging from inside the
            reader thread (rather than after proc.wait()) we surface the
            info before the user command starts producing output, which
            matters for long-running / interactive children like a shell.
            """
            chunks: list[bytes] = []
            try:
                while True:
                    chunk = os.read(r, 4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                try:
                    os.close(r)
                except OSError:
                    pass
            text = b''.join(chunks).decode('utf-8', errors='replace').strip()
            if not text:
                return
            try:
                parsed = json.loads(text)
                logger.debug('bwrap --info-fd:\n' + json.dumps(parsed, indent=2))
            except json.JSONDecodeError:
                logger.debug(f'bwrap --info-fd (raw, not JSON): {text!r}')

        reader = threading.Thread(target=_drain_and_log_info_fd, daemon=True)
        reader.start()

        # `--info-fd FD` immediately after `bwrap`. bwrap will inherit `w`
        # via pass_fds; we close our parent-side copy so the reader sees
        # EOF as soon as bwrap closes its end (right after writing).
        cmd_with_info = [cmd[0], '--info-fd', str(w), *cmd[1:]]
        try:
            proc = subprocess.Popen(cmd_with_info, pass_fds=[w])
        finally:
            os.close(w)

        returncode = proc.wait()
        reader.join(timeout=2.0)
        return returncode
    except FileNotFoundError:
        raise ExecutionError('bwrap not found in PATH') from None
    except Exception as e:
        raise ExecutionError(f'Failed to execute bwrap: {e}') from e
