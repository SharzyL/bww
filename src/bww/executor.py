"""Execution layer: bubblewrap command building and execution."""

import contextlib
import ctypes
import fcntl
import json
import os
import shlex
import signal
import subprocess
import time
from typing import Any

from .models import RuntimeConfig
from .options import OPTIONS, OptionSpec
from .utils import logger

# Linux nsfs ioctl: NS_GET_USERNS returns a new fd referring to the
# *owning* user namespace of the namespace pointed to by `fd`. The
# owner is fixed at namespace creation time and stable across later
# unshare/setns of the original process — so opening a sibling
# namespace (net, ipc, ...) and asking for its userns gives us the
# inner sandbox userns even after bwrap performs additional userns
# work between info-fd write and block-fd read.
# _IO(NSIO=0xb7, 0x1) == 0xb701.
_NS_GET_USERNS = 0xB701

# prctl(2) op: claim subreaper status for orphaned descendants. Daemons
# spawned from setup-script (e.g. `tun2socks ... &`) reparent here when
# their immediate parent (bash) exits, instead of escaping to PID 1. We
# can then collect and kill them once bwrap is done.
_PR_SET_CHILD_SUBREAPER = 36

# Placeholder token inserted by `build_bwrap_command` where the
# nameserver-driven `--ro-bind-data` fd would go. `execute_bwrap`
# substitutes it with the real fd number right before Popen. The
# placeholder makes `--dry-run` output truthful without claiming an
# fd number we can't predict ahead of execution.
_RESOLV_FD_PLACEHOLDER = '<resolv-fd>'


def set_subreaper() -> None:
    """Make bww the subreaper for orphaned descendants (Linux ≥ 3.4)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        errno_val = ctypes.get_errno()
        logger.warning(f'PR_SET_CHILD_SUBREAPER failed: {os.strerror(errno_val)}')


def _snapshot_setup_descendants(exclude_pid: int) -> set[int]:
    """Read bww's current direct children, excluding `exclude_pid` (bwrap
    or the unshare wrapper). Call after setup-scripts finish but before
    block-fd is written — that snapshot is exactly the daemons that
    setup-script backgrounded (their bash has exited and they
    reparented to us as subreaper). Anything that drifts in *after*
    proc.wait is bwrap's own teardown stragglers and not our problem.
    """
    try:
        with open(f'/proc/self/task/{os.getpid()}/children') as f:
            pids = {int(p) for p in f.read().split()}
    except OSError:
        return set()
    pids.discard(exclude_pid)
    return pids


def _reap_pids(targets: set[int], grace: float = 0.5) -> None:
    """SIGTERM `targets`, give them `grace` seconds, SIGKILL the rest.

    Then waitpid-drain so we don't leave zombies (we're the subreaper).
    `targets` is the snapshot taken before bwrap was released, so this
    is precisely the setup-script daemons — bwrap's own descendants
    that orphan to us during its teardown are left alone (just drained).
    """
    if not targets:
        # Still drain any zombies that built up during proc.wait.
        _drain_zombies()
        return
    logger.debug(f'reaping setup-script daemons reparented to bww: {sorted(targets)}')
    # Only track pids where SIGTERM actually landed. Building `remaining`
    # from the loop side-steps the "mutate-set-during-iteration"
    # RuntimeError that biting `remaining.discard(pid)` inside the loop
    # would cause on the first already-exited target.
    remaining: set[int] = set()
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
            remaining.add(pid)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while remaining and time.monotonic() < deadline:
        try:
            rpid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if rpid == 0:
            time.sleep(0.05)
            continue
        remaining.discard(rpid)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _drain_zombies()


def _drain_zombies() -> None:
    """Reap any pending zombie children via waitpid(WNOHANG) in a loop."""
    while True:
        try:
            rpid, _ = os.waitpid(-1, os.WNOHANG)
            if rpid == 0:
                break
        except ChildProcessError:
            break


def _read_bwrap_info(info_r: int) -> dict[str, Any]:
    """Read bwrap's --info-fd into a JSON object.

    Reads incrementally rather than waiting for EOF: with `unshare --fork`
    the unshare-parent inherits info_w via pass_fds and keeps it open
    until bwrap exits, so info_r would never EOF during the setup window.
    Returns as soon as the buffer parses as a complete JSON object.
    """
    buf = b''
    while True:
        chunk = os.read(info_r, 4096)
        if not chunk:
            raise ExecutionError('info-fd closed before a complete JSON object arrived')
        buf += chunk
        try:
            return json.loads(buf.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Partial JSON or a multi-byte UTF-8 codepoint split across
            # reads — keep accumulating.
            continue


def _open_bwrap_userns_fd(child_pid: int) -> int:
    """Open an nsfs fd for the userns owning the bwrap sandbox netns.

    BWRAP_USERNS has no live process (it's an intermediate userns bwrap
    unshared past), so no /proc/<pid>/ns/user exists for it. Compute it
    via NS_GET_USERNS on the bwrap netns fd; the returned fd is exposed
    to setup-scripts via /proc/<bww_pid>/fd/<N>.
    """
    net_fd = os.open(f'/proc/{child_pid}/ns/net', os.O_RDONLY)
    try:
        return fcntl.ioctl(net_fd, _NS_GET_USERNS)
    finally:
        os.close(net_fd)


def _log_ns_readlinks(
    *,
    unshare_pid: int,
    child_pid: int,
    bwrap_user_fd: int,
    extra_unshare_net: bool,
) -> None:
    """Debug-log each namespace handle as exported to setup-script.

    Format: `get NAME=PATH -> link:[inode] (how, when nontrivial)`. The
    inode matches what `lsns` shows. Order: EXTRA (unshare wrapper)
    first, then BWRAP, mirroring the `lsns` tree layout. Pids are
    logged once up front so the reader can locate them in lsns/ps.
    """
    bww_pid = os.getpid()
    logger.debug(f'ns sources: unshare-parent pid={unshare_pid}, bwrap sandbox-init pid={child_pid}')
    items: list[tuple[str, str, str | None]] = []
    if extra_unshare_net:
        items.append(('EXTRA_USERNS', f'/proc/{unshare_pid}/ns/user', None))
        items.append(('EXTRA_NETNS', f'/proc/{unshare_pid}/ns/net', None))
    items.append(
        (
            'BWRAP_USERNS',
            f'/proc/{bww_pid}/fd/{bwrap_user_fd}',
            f'NS_GET_USERNS on /proc/{child_pid}/ns/net; no process lives in this userns so no procfs path is available',
        )
    )
    items.append(('BWRAP_NETNS', f'/proc/{child_pid}/ns/net', None))
    for name, path, how in items:
        try:
            link = os.readlink(path)
        except OSError as e:
            link = f'<readlink failed: {e}>'
        suffix = f' ({how})' if how else ''
        logger.debug(f'get {name}={path} -> {link}{suffix}')


def _run_setup_scripts(
    child_pid: int,
    scripts: list[str],
    *,
    unshare_pid: int,
    bwrap_user_fd: int,
    extra_unshare_net: bool,
) -> None:
    """Run each entry in `scripts` via `bash -c` on the host, in order.

    Naming convention: env var holds a **path** (not a raw fd number) —
    typically a procfs ns symlink (`/proc/<pid>/ns/net`); a magic-link
    to bww's fd table (`/proc/<bww_pid>/fd/<N>`) is used only when no
    process lives in the target namespace. Tools like `nsenter`, `ip
    ... netns`, `pasta --netns`, etc. take paths.

    Exported into bash:
      CHILD_PID      bwrap's sandbox-init pid.
      BWRAP_NETNS    /proc/<child_pid>/ns/net — the netns bwrap created
                     with `--unshare-net`.
      BWRAP_USERNS   /proc/<bww_pid>/fd/<N> — the userns that *owns*
                     BWRAP_NETNS (obtained via NS_GET_USERNS ioctl).
                     **Only useful from a process in bww's userns** —
                     accessing /proc/<bww>/fd/* from a sandbox-side
                     userns context fails with EACCES on the kernel's
                     cross-userns ptrace_may_access check.
      EXTRA_NETNS    present only when `extra-unshare-net` is on.
                     /proc/<unshare_pid>/ns/net — the netns the outer
                     `unshare --fork --net` wrapper created. Stable
                     because `--fork` keeps the unshare process parked
                     there.
      EXTRA_USERNS   present only when `extra-unshare-net` is on.
                     /proc/<unshare_pid>/ns/user — the userns the same
                     wrapper created, where the current user keeps
                     CAP_SYS_ADMIN.

    Reserved (not currently exported, name kept for forward consistency):
      EXEC_USERNS    the userns the executed payload ends up in. Only
                     knowable after we release bwrap via block-fd; bwrap
                     may setns again between block-fd and execvp.
                     setup-scripts run *before* block-fd.

    A non-zero exit aborts the whole run: subsequent scripts are
    skipped, bwrap is killed (instead of being released via block-fd),
    and the caller raises ExecutionError. Use shell-level `|| true` in
    a script to opt out of this for a specific step.
    """
    if not scripts:
        return

    bww_pid = os.getpid()
    env = {
        **os.environ,
        'CHILD_PID': str(child_pid),
        'BWRAP_NETNS': f'/proc/{child_pid}/ns/net',
        'BWRAP_USERNS': f'/proc/{bww_pid}/fd/{bwrap_user_fd}',
    }
    if extra_unshare_net:
        env['EXTRA_USERNS'] = f'/proc/{unshare_pid}/ns/user'
        env['EXTRA_NETNS'] = f'/proc/{unshare_pid}/ns/net'

    for i, script in enumerate(scripts):
        logger.debug(f'setup-script[{i}]: bash -c {script!r}')
        rc = subprocess.run(['bash', '-c', script], env=env).returncode
        if rc != 0:
            raise SetupScriptFailed(i, rc, script)


class ExecutionError(Exception):
    """Raised when bwrap execution fails."""

    pass


class SetupScriptFailed(Exception):
    """Raised when a setup-script entry exits non-zero. Carries the
    index, exit code, and the bash snippet so the caller can format a
    useful error and abort bwrap before it ever runs the payload."""

    def __init__(self, index: int, returncode: int, script: str) -> None:
        self.index = index
        self.returncode = returncode
        self.script = script
        super().__init__(f'setup-script[{index}] exited with {returncode}')


# A "group" is one logical bwrap argument cluster: a single zero-arg flag,
# or a multi-token option like ['--ro-bind', SRC, DEST]. The leading group
# is conventionally ['bwrap']; the final group is ['--', *command].
BwrapGroups = list[list[str]]


def to_argv(groups: BwrapGroups) -> list[str]:
    """Flatten groups into a flat argv suitable for subprocess.run."""
    return [tok for g in groups for tok in g]


def format_bwrap_command(groups: BwrapGroups) -> str:
    """Format groups as a readable, line-broken command.

    - Any groups preceding 'bwrap' (the outer wrapper, e.g. `unshare ... --`)
      get one line each.
    - The 'bwrap' group plus subsequent single-token zero-arg flag groups
      are packed onto one line.
    - Multi-token groups go on their own lines, indented.
    - The final group (starting with '--') is rendered as the last line.
    - Non-final lines get trailing ' \\' for shell copy/paste.
    """
    if not groups:
        return ''

    bwrap_idx = next((i for i, g in enumerate(groups) if g and g[0] == 'bwrap'), None)
    if bwrap_idx is None:
        return shlex.join(to_argv(groups))

    lines: list[str] = []
    # Outer-wrapper groups (each on its own line) before bwrap.
    for g in groups[:bwrap_idx]:
        lines.append(shlex.join(g))

    # 'bwrap' + consecutive single-token flag groups, packed.
    i = bwrap_idx
    first: list[str] = list(groups[i])
    i += 1
    while i < len(groups) and len(groups[i]) == 1 and groups[i][0].startswith('--'):
        first.extend(groups[i])
        i += 1
    lines.append(shlex.join(first))

    while i < len(groups):
        lines.append('  ' + shlex.join(groups[i]))
        i += 1

    if len(lines) <= 1:
        return '\n'.join(lines)
    return '\n'.join(line + ' \\' if idx < len(lines) - 1 else line for idx, line in enumerate(lines))


def _emit_option(spec: OptionSpec, runtime: RuntimeConfig) -> BwrapGroups:
    """Emit bwrap groups for one option. Dispatches by option name.

    Mount specs are owned by the mount builder (`_emit_mounts`) and return
    [] here. Each non-mount option's emit is a self-contained case so
    idiosyncratic options (like dev-bind owning both true/false branches)
    live in one place.
    """
    if spec.kind == 'mount':
        return []  # owned by _emit_mounts
    if spec.key in ('setup-script', 'extra-unshare-net'):
        return []  # consumed by execute_bwrap / build wrapper, not bwrap flags
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
        case 'nameserver':
            # The fd is allocated and substituted by execute_bwrap.
            if not runtime.nameserver:
                return []
            return [['--ro-bind-data', _RESOLV_FD_PLACEHOLDER, '/etc/resolv.conf']]
        case _:
            raise ValueError(f'_emit_option: no case for spec.key={spec.key!r}')


def _emit_mounts(runtime: RuntimeConfig) -> BwrapGroups:
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


def build_bwrap_command(runtime_config: RuntimeConfig) -> BwrapGroups:
    """Build the bwrap command as a list of token groups.

    Group layout: optional wrapper prefix groups (e.g. `unshare ...
    --`), then ['bwrap'], <zero-arg flag groups>, <multi-token option
    groups>, ['--chdir', cwd], ['--argv0', argv0], ['--', *command].
    Use `to_argv(...)` to flatten for execution; pass the groups directly to
    `format_bwrap_command(...)` for display.
    """
    groups: BwrapGroups = []
    # Optional outer wrapper. `unshare --fork --kill-child` makes the
    # unshare(1) process stay parked in the wrapper-created namespaces
    # and fork bwrap as a child — so /proc/<unshare_pid>/ns/{net,user}
    # are stable nsfs entries that execute_bwrap can capture for
    # setup-script use. --kill-child propagates SIGKILL down if the
    # wrapper itself dies.
    #
    # uid layering when extra_unshare_net is on:
    #   host kuid 1000
    #     └─ EXTRA_USERNS (`--map-root-user`)  → uid 0 here
    #         └─ BWRAP_USERNS (bwrap's own unshare)
    #             └─ payload uid = `--uid host_uid` ⇒ 1000
    # `--map-root-user` is required so processes setns'ing into
    # EXTRA_USERNS land as uid 0 — uid 0 in a userns triggers the
    # kernel's legacy-root cap_bprm_set_creds path on exec, so caps
    # survive across `exec ip ...` etc. without ambient/file caps.
    # bwrap then `--uid <host_uid> --gid <host_gid>` maps that uid 0
    # back to the host's original uid in the sandbox payload, keeping
    # `id -u` transparent inside the sandbox.
    if runtime_config.extra_unshare_net:
        groups.append(['unshare', '--fork', '--kill-child', '--user', '--map-root-user', '--net', '--'])
    groups.append(['bwrap'])
    if runtime_config.extra_unshare_net:
        groups.append(['--uid', str(os.getuid())])
        groups.append(['--gid', str(os.getgid())])

    # Iterate OPTIONS in registry order. Static separators are inserted at
    # well-defined points to keep the bwrap argv layout stable.
    for spec in OPTIONS:
        groups.extend(_emit_option(spec, runtime_config))
        if spec.key == 'share-uts':
            # End of the unshare prelude.
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


def _kill_bwrap_tree(proc: subprocess.Popen[bytes], child_pid: int | None) -> None:
    """SIGKILL bwrap's paused sandbox-init (if known) plus the wrapper.

    bwrap is a multi-process tree (wrapper → intermediate → sandbox-init),
    and the only process actually blocked on `read(block_fd)` is the
    sandbox-init (`child-pid` from info-fd) — that's the one about to
    `execvp` the payload. SIGKILL'ing only the top of the tree leaves
    the sandbox-init as an orphan picked up by our subreaper; once
    block_w closes, its read returns 0 and it runs the payload. So
    target the sandbox-init directly first, then SIGKILL the wrapper
    for cleanup. SIGTERM is unreliable here too: unshare(1) in --fork
    mode ignores it, and bwrap retries EINTR. Idempotent — safe to
    call again from outer except handlers.
    """
    if child_pid is not None:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()
    proc.wait()


def _build_nameserver_fd(nameservers: list[str]) -> int:
    """Open a pipe pre-filled with a resolv.conf body for bwrap to copy.

    bwrap's `--ro-bind-data FD DEST` reads from FD into a tmpfile and
    bind-mounts it read-only at DEST — no host-side file needed. The
    resolv.conf content is tiny (a handful of bytes per nameserver,
    well under the 64KiB default pipe buffer on Linux), so we write
    it synchronously and close the write end. bwrap then drains the
    read end as part of its setup.
    """
    body = 'options single-request-reopen\n'
    body += ''.join(f'nameserver {ns}\n' for ns in nameservers)
    r, w = os.pipe()
    try:
        os.write(w, body.encode())
    finally:
        os.close(w)
    return r


def _inject_after_bwrap(cmd: list[str], extra: list[str]) -> list[str]:
    """Return a copy of `cmd` with `extra` spliced right after 'bwrap'.

    When extra-unshare-net is on, cmd[0] is `unshare` and the wrapped
    `bwrap` shows up further in. Anchor on the 'bwrap' token so bwrap
    flags don't leak onto the wrapper.
    """
    try:
        bwrap_idx = cmd.index('bwrap')
    except ValueError as e:
        raise ExecutionError("'bwrap' token not found in command") from e
    return [*cmd[: bwrap_idx + 1], *extra, *cmd[bwrap_idx + 1 :]]


def execute_bwrap(
    cmd: list[str],
    debug: bool = False,
    setup_script: list[str] | None = None,
    extra_unshare_net: bool = False,
    nameserver: list[str] | None = None,
) -> int:
    """Execute bwrap argv (already flattened) and return exit code.

    `nameserver`, if non-empty, is materialized into a pipe and handed
    to bwrap as `--ro-bind-data <fd> /etc/resolv.conf` — no host-side
    tempfile is created. The pipe-read fd is passed through `pass_fds`
    so bwrap (and the unshare wrapper, when present) inherits it.

    Whenever `debug` is on or `setup_script` is non-empty, we pass
    `--info-fd <fd>` and `--block-fd <fd>` to bwrap on pipes we
    control. bwrap writes its JSON to info-fd after setting up
    namespaces, then blocks on block-fd before exec-ing the payload.

    We read the info synchronously on the main thread, capture nsfs
    fds for the namespaces a setup-script may want to address (inner
    netns/userns always; outer netns/userns when `extra_unshare_net`
    is on), then run each setup-script in declaration order on the
    host, then write to block-fd to release bwrap — no reader thread,
    no race against the payload's output.

    Failure handling: once bwrap is running and parked on block-fd,
    any unhandled exception below — info-fd parse error, ioctl
    failure, KeyboardInterrupt — would otherwise leak block_w and let
    sandbox-init see EOF and proceed to execvp at parent GC time.
    The outer try wraps the live-bwrap window in a `_kill_bwrap_tree`
    teardown so the payload never runs on an error path.
    """
    scripts = list(setup_script or [])
    nameservers = list(nameserver or [])
    ns_fd = -1
    try:
        if nameservers:
            ns_fd = _build_nameserver_fd(nameservers)
            # build_bwrap_command emitted `--ro-bind-data <resolv-fd-placeholder>
            # /etc/resolv.conf` for us; swap the placeholder for the real fd.
            try:
                idx = cmd.index(_RESOLV_FD_PLACEHOLDER)
            except ValueError as e:
                raise ExecutionError(
                    'nameserver set but resolv-fd placeholder missing from cmd '
                    '(build_bwrap_command and execute_bwrap are out of sync)'
                ) from e
            cmd = [*cmd[:idx], str(ns_fd), *cmd[idx + 1 :]]
        if not debug and not scripts:
            pass_fds = [ns_fd] if ns_fd >= 0 else ()
            return subprocess.run(cmd, pass_fds=pass_fds).returncode
        return _run_with_blockfd(cmd, debug, scripts, extra_unshare_net, ns_fd)
    except FileNotFoundError as e:
        # Can come from bwrap/unshare itself (the fast path or Popen)
        # or from `bash` inside _run_setup_scripts. `e.filename` is set
        # by subprocess for ENOENT exec failures — preserve it so the
        # message names the actual missing binary.
        missing = e.filename or 'bwrap'
        raise ExecutionError(f'{missing}: command not found') from None
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f'Failed to execute bwrap: {e}') from e
    finally:
        if ns_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(ns_fd)


def _run_with_blockfd(
    cmd: list[str],
    debug: bool,
    scripts: list[str],
    extra_unshare_net: bool,
    ns_fd: int = -1,
) -> int:
    """Spawn bwrap with --info-fd / --block-fd pipes and orchestrate
    setup-scripts in the gap between bwrap setup and payload exec.

    Cleanup contract for the live-bwrap window (Popen → release/kill):
      - `except BaseException`: SIGKILL bwrap (sandbox-init first) so
        closing block_w can't release the payload on any error path —
        whether a setup-script aborts, an ioctl fails, or the user
        sends KeyboardInterrupt.
      - `finally`: drop any fds the success path didn't already drop
        (bwrap_user_fd, block_w on the abort path) and reap any
        setup-script daemons that reparented to us.

    The --info-fd / --block-fd args go right after `bwrap`, not after
    cmd[0] — when extra-unshare-net is on, cmd[0] is `unshare` and
    these flags belong on bwrap, not on the wrapper.
    """
    # `-1` sentinels so the cleanup loop below knows which fds actually
    # got allocated: the second `os.pipe()` can raise EMFILE, leaving the
    # first pair allocated; `Popen` can raise FileNotFoundError after
    # both pairs are allocated. In either case we must close everything
    # we own, since Popen never takes ownership of pass_fds on failure.
    info_r = info_w = block_r = block_w = -1
    try:
        info_r, info_w = os.pipe()
        block_r, block_w = os.pipe()
        cmd_with_fds = _inject_after_bwrap(cmd, ['--info-fd', str(info_w), '--block-fd', str(block_r)])
        pass_fds = [info_w, block_r] + ([ns_fd] if ns_fd >= 0 else [])
        proc = subprocess.Popen(cmd_with_fds, pass_fds=pass_fds)
    except BaseException:
        for fd in (info_r, info_w, block_r, block_w):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        raise
    # Popen succeeded; bwrap holds its own copies of info_w/block_r.
    os.close(info_w)
    os.close(block_r)

    # bwrap is live and paused on block_fd from here on.
    child_pid: int | None = None
    bwrap_user_fd: int | None = None
    setup_daemons: set[int] = set()
    released = False
    try:
        try:
            parsed = _read_bwrap_info(info_r)
        finally:
            os.close(info_r)
        if debug:
            logger.debug('bwrap --info-fd:\n' + json.dumps(parsed, indent=2))
        raw_pid = parsed.get('child-pid')
        if not isinstance(raw_pid, int) or raw_pid <= 0:
            raise ExecutionError(f"bwrap info-fd: missing/invalid 'child-pid' (got {raw_pid!r})")
        child_pid = raw_pid

        # BWRAP_NETNS, EXTRA_*: handed out as procfs paths directly (they
        # have live processes in them, so /proc/<pid>/ns/* works from any
        # userns). BWRAP_USERNS is the odd one — no process lives in
        # that intermediate userns, so we open it via NS_GET_USERNS and
        # expose the resulting fd through /proc/<bww>/fd/<N>.
        bwrap_user_fd = _open_bwrap_userns_fd(child_pid)
        if debug:
            _log_ns_readlinks(
                unshare_pid=proc.pid,
                child_pid=child_pid,
                bwrap_user_fd=bwrap_user_fd,
                extra_unshare_net=extra_unshare_net,
            )

        # _run_setup_scripts may raise SetupScriptFailed — let it
        # propagate to the outer except, which kills bwrap before
        # block_w closes (otherwise sandbox-init would EOF and exec
        # the payload). The finally snapshots daemons spawned by any
        # already-completed scripts (subprocess.run has returned for
        # each, so their bash is dead and daemons reparented to us).
        try:
            _run_setup_scripts(
                child_pid,
                scripts,
                unshare_pid=proc.pid,
                bwrap_user_fd=bwrap_user_fd,
                extra_unshare_net=extra_unshare_net,
            )
        except SetupScriptFailed as e:
            logger.error(str(e))
            raise ExecutionError(f'setup-script[{e.index}] exited with {e.returncode}; sandbox aborted') from e
        finally:
            setup_daemons = _snapshot_setup_descendants(exclude_pid=proc.pid)

        # Normal release path: write block_w to let sandbox-init exec
        # the payload, then wait for the full bwrap tree to exit.
        os.write(block_w, b'\n')
        os.close(block_w)
        released = True
        return proc.wait()
    except BaseException:
        _kill_bwrap_tree(proc, child_pid)
        raise
    finally:
        if bwrap_user_fd is not None:
            with contextlib.suppress(OSError):
                os.close(bwrap_user_fd)
        if not released:
            with contextlib.suppress(OSError):
                os.close(block_w)
        _reap_pids(setup_daemons)
