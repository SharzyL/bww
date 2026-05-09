"""Integration tests for BWW CLI and command execution."""

import subprocess
import sys
import tempfile
from pathlib import Path


# Find project root (directory containing pyproject.toml)
def _get_project_root() -> Path:
    """Get the project root directory."""
    current = Path(__file__).parent.parent
    while current != current.parent:
        if (current / 'pyproject.toml').exists():
            return current
        current = current.parent
    raise RuntimeError('Could not find project root')


PROJECT_ROOT = _get_project_root()


def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences for robust assertions."""
    import re

    return re.sub(r'\x1b\[[0-9;]*m', '', s)


def run_bww(*args: str) -> tuple[int, str, str]:
    """Run bww command and return (exit_code, stdout, stderr)."""
    import os

    # Build command: run bww via Python module directly
    cmd = [sys.executable, '-m', 'bww'] + list(args)

    # Create temp directory for config to avoid interfering with user's ~/.config
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env['XDG_CONFIG_HOME'] = tmpdir
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            env=env,
        )
    return result.returncode, result.stdout, result.stderr


def run_bww_with_env(env_overrides: dict[str, str], *args: str) -> tuple[int, str, str]:
    """Run bww with environment overrides and return (exit_code, stdout, stderr)."""
    import os

    cmd = [sys.executable, '-m', 'bww'] + list(args)
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env['XDG_CONFIG_HOME'] = tmpdir
        env.update(env_overrides)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            env=env,
        )
    return result.returncode, result.stdout, result.stderr


class TestCLIParsing:
    """Test CLI parsing and argument handling."""

    def test_help(self) -> None:
        """Test --help works."""
        code, stdout, stderr = run_bww('--help')
        assert code == 0
        assert 'BubbleWrap Wrapper' in stdout
        assert '--config' in stdout
        assert '--profile' in stdout
        assert '--share-net' in stdout
        assert '--share-user' in stdout
        assert '--share-ipc' in stdout
        assert '--share-pid' in stdout
        assert '--share-uts' in stdout
        assert '--dev-bind' in stdout

    def test_no_command_error(self) -> None:
        """Test error when no command provided."""
        code, stdout, stderr = run_bww()
        assert code == 1
        assert '[ERROR]' in stderr
        assert 'command' in stderr.lower()

    def test_dry_run_shows_command(self) -> None:
        """Test --dry-run shows what would be executed."""
        import shutil

        code, stdout, stderr = run_bww('--dry-run', 'echo', 'hello')
        assert code == 0
        clean = _strip_ansi(stdout)
        assert 'bwrap' in clean
        assert '--chdir' in clean
        assert str(PROJECT_ROOT) in clean
        # Executed binary's resolved target is auto-mounted read-only.
        resolved_echo = shutil.which('echo')
        if resolved_echo:
            target = str(Path(resolved_echo).resolve())
            assert f'--ro-bind {target} {target}' in clean
        assert '--argv0 echo' in clean
        assert 'echo' in clean

    def test_exe_ro_bind_elided_when_target_ancestor_in_place_ro(self) -> None:
        """When the resolved exe target sits under an in-place ro bind ancestor,
        the auto target ro-bind is elided as redundant.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            real_dir = Path(tmpdir) / 'real'
            real_dir.mkdir()
            real = real_dir / 'echo'
            real.write_text('#!/bin/sh\necho "$@"\n')
            real.chmod(0o755)

            kdl_cfg = f"""
profiles.p {{
  ro "{real_dir}"
}}
"""
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(kdl_cfg)
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'profiles.p', '--dry-run', str(real), 'hi')
            assert code == 0
            clean = _strip_ansi(stdout)
            assert f'--ro-bind {real_dir} {real_dir}' in clean
            assert f'--ro-bind {real} {real}' not in clean

    def test_redundant_inplace_child_ro_elided(self) -> None:
        """An in-place ro child whose nearest ancestor is an in-place ro bind is
        elided — the parent bind already exposes the child path.
        """
        kdl_cfg = """
profiles.p {
  ro "/etc"
  ro "/etc/hosts"
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(kdl_cfg)
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'profiles.p', '--dry-run', 'echo')
            assert code == 0
            clean = _strip_ansi(stdout)
            assert '--ro-bind /etc /etc' in clean
            assert '--ro-bind /etc/hosts /etc/hosts' not in clean

    def test_inplace_child_kept_when_parent_mode_differs(self) -> None:
        """Cross-mode parent + child are both kept; lex sort on dest puts the
        parent before the child so the child override isn't shadowed by a
        later parent bind.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / 'parent'
            parent.mkdir()
            child = parent / 'inner'
            child.mkdir()

            kdl_cfg = f"""
profiles.p {{
  rw "{parent}"
  ro "{child}"
}}
"""
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(kdl_cfg)
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'profiles.p', '--dry-run', 'echo')
            assert code == 0
            clean = _strip_ansi(stdout)
            assert f'--bind {parent} {parent}' in clean
            assert f'--ro-bind {child} {child}' in clean
            # Parent must precede child in argv (otherwise the rw would shadow the ro override).
            assert clean.index(f'--bind {parent} {parent}') < clean.index(f'--ro-bind {child} {child}')

    def test_symlink_src_rewritten_to_target_bind_plus_symlink(self) -> None:
        """An in-place mount whose src is a host symlink is rewritten: the
        resolved target gets ro/rw-bound at the target path, and a `--symlink
        target original` is emitted so the sandbox preserves the symlink at
        the original path.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            real_dir = Path(tmpdir) / 'real-bin'
            real_dir.mkdir()
            link = Path(tmpdir) / 'link-bin'
            link.symlink_to(real_dir)

            kdl_cfg = f"""
profiles.p {{
  ro "{link}"
}}
"""
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(kdl_cfg)
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'profiles.p', '--dry-run', 'echo')
            assert code == 0
            clean = _strip_ansi(stdout)
            assert f'--ro-bind {real_dir} {real_dir}' in clean
            assert f'--symlink {real_dir} {link}' in clean
            # The original symlink path should NOT be bound directly.
            assert f'--ro-bind {link} {link}' not in clean

    def test_symlink_src_skipped_when_bind_ancestor_present(self) -> None:
        """When the original symlink path has a bind ancestor, the synthetic
        --symlink would EEXIST (the host symlink is exposed via the parent
        bind), so it's skipped. The resolved-target bind still goes through.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            real_dir = Path(tmpdir) / 'real-bin'
            real_dir.mkdir()
            parent_dir = Path(tmpdir) / 'links'
            parent_dir.mkdir()
            link = parent_dir / 'link-bin'
            link.symlink_to(real_dir)

            kdl_cfg = f"""
profiles.p {{
  ro "{parent_dir}"
  ro "{link}"
}}
"""
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(kdl_cfg)
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'profiles.p', '--dry-run', 'echo')
            assert code == 0
            clean = _strip_ansi(stdout)
            assert f'--ro-bind {parent_dir} {parent_dir}' in clean
            assert f'--symlink {real_dir} {link}' not in clean

    def test_inplace_child_kept_when_ancestor_is_tmpfs(self) -> None:
        """A tmpfs ancestor doesn't elide an in-place bind child — the tmpfs
        starts empty and the child mount paints content into it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir) / 'root'
            tmp_root.mkdir()
            inner = tmp_root / 'inner'
            inner.mkdir()

            kdl_cfg = f"""
profiles.p {{
  tmpfs "{tmp_root}"
  rw "{inner}"
}}
"""
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(kdl_cfg)
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'profiles.p', '--dry-run', 'echo')
            assert code == 0
            clean = _strip_ansi(stdout)
            assert f'--tmpfs {tmp_root}' in clean
            assert f'--bind {inner} {inner}' in clean

    def test_exe_symlink_recreated_when_no_bind_ancestor(self) -> None:
        """When the exe is a symlink and no ancestor is bind-mounted, we ro-bind
        the target and emit `--symlink target exe` so the user-facing exe path
        still resolves inside the sandbox.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            real_dir = Path(tmpdir) / 'real'
            real_dir.mkdir()
            real = real_dir / 'echo'
            real.write_text('#!/bin/sh\necho "$@"\n')
            real.chmod(0o755)

            link_dir = Path(tmpdir) / 'links'
            link_dir.mkdir()
            link = link_dir / 'echo'
            link.symlink_to(real)

            code, stdout, stderr = run_bww('--dry-run', str(link), 'hi')
            assert code == 0
            clean = _strip_ansi(stdout)
            assert f'--ro-bind {real} {real}' in clean
            assert f'--symlink {real} {link}' in clean

    def test_exe_symlink_not_recreated_when_bind_ancestor_present(self) -> None:
        """When the exe is a symlink and a bind ancestor exposes it, we ro-bind
        the resolved target only — emitting --symlink would EEXIST or EROFS.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            real_dir = Path(tmpdir) / 'real'
            real_dir.mkdir()
            real = real_dir / 'echo'
            real.write_text('#!/bin/sh\necho "$@"\n')
            real.chmod(0o755)

            link_dir = Path(tmpdir) / 'links'
            link_dir.mkdir()
            link = link_dir / 'echo'
            link.symlink_to(real)

            kdl_cfg = f"""
profiles.p {{
  ro "{link_dir}"
}}
"""
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(kdl_cfg)
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'profiles.p', '--dry-run', str(link), 'hi')
            assert code == 0
            clean = _strip_ansi(stdout)
            assert f'--ro-bind {real} {real}' in clean
            assert f'--symlink {real} {link}' not in clean

    def test_share_net_flag(self) -> None:
        """Test --share-net omits --unshare-net."""
        from pathlib import Path

        code, stdout, stderr = run_bww('--share-net', '--dry-run', 'echo', 'hello')
        assert code == 0
        clean = _strip_ansi(stdout)
        assert 'bwrap ' in clean
        assert '--unshare-net' not in clean
        # share-net implies we include common resolver-related files when present.
        if Path('/etc/hosts').exists():
            assert '--ro-bind /etc/hosts /etc/hosts' in clean
        if Path('/etc/resolv.conf').exists():
            assert '--ro-bind /etc/resolv.conf /etc/resolv.conf' in clean
        if Path('/etc/nsswitch.conf').exists():
            assert '--ro-bind /etc/nsswitch.conf /etc/nsswitch.conf' in clean

    def test_dev_bind_flag(self) -> None:
        """Test --dev-bind uses --dev-bind /dev /dev instead of --dev /dev."""
        code, stdout, stderr = run_bww('--dev-bind', '--dry-run', 'echo', 'hello')
        assert code == 0
        assert '--dev-bind /dev /dev' in stdout
        assert '--dev /dev' not in stdout

    def test_reuse_session_flag(self) -> None:
        """Test --reuse-session removes --new-session."""
        code, stdout, stderr = run_bww('--reuse-session', '--dry-run', 'echo', 'hello')
        assert code == 0
        clean = _strip_ansi(stdout)
        assert '--new-session' not in clean

    def test_share_uts_flag(self) -> None:
        """Test --share-uts omits --unshare-uts."""
        code, stdout, stderr = run_bww('--share-uts', '--dry-run', 'echo', 'hello')
        assert code == 0
        clean = _strip_ansi(stdout)
        assert '--unshare-uts' not in clean

    def test_profile_flag_accepts_defaults_namespace(self) -> None:
        """Test -p defaults.NAME uses a defaults profile."""
        from pathlib import Path

        code, stdout, stderr = run_bww('--config', 'example/config.kdl', '-p', 'defaults.firefox', '--dry-run', 'echo')
        assert code == 0
        clean = _strip_ansi(stdout)
        # defaults.firefox inherits desktop, which enables device access.
        assert '--dev-bind /dev /dev' in clean
        home = str(Path.home())
        assert f'--bind {home}/.mozilla {home}/.mozilla' in clean

    def test_debug_flag(self) -> None:
        """--debug enables DEBUG-level logging on stderr."""
        code, stdout, stderr = run_bww('--debug', 'true')
        # bwrap may not exist or fail, but the debug log should always appear.
        clean = _strip_ansi(stderr)
        assert '[DEBUG]' in clean
        assert 'running bwrap' in clean

    def test_cli_mount_rw(self) -> None:
        """Test --rw CLI argument."""
        code, stdout, stderr = run_bww('--rw', '/home', '--dry-run', 'echo', 'test')
        assert code == 0
        assert '--bind' in stdout
        assert '/home' in stdout

    def test_cli_mount_rw_relative_is_normalized(self) -> None:
        """Test mount paths are expanded to absolute for bwrap."""
        code, stdout, stderr = run_bww('--rw', '.', '--dry-run', 'echo', 'test')
        assert code == 0
        assert str(PROJECT_ROOT) in stdout
        # Ensure tmpfs comes before bind mounts (requested ordering).
        assert stdout.index('--tmpfs') < stdout.index('--bind')

    def test_exec_path_is_expanded_when_pathlike(self) -> None:
        """Test exec path expansion."""
        code, stdout, stderr = run_bww('--dry-run', './README.md')
        assert code == 0
        assert str(PROJECT_ROOT / 'README.md') in stdout

    def test_exec_is_resolved_via_path(self) -> None:
        """Test bare executable names are resolved via PATH before building bwrap argv."""
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            exe = Path(tmpdir) / 'fakecmd'
            exe.write_text('#!/bin/sh\nexit 0\n')
            exe.chmod(0o755)

            env = {'PATH': f'{tmpdir}{os.pathsep}{os.environ.get("PATH", "")}'}
            code, stdout, stderr = run_bww_with_env(env, '--dry-run', 'fakecmd')
            assert code == 0
            assert str(exe) in stdout

    def test_cli_mount_ro(self) -> None:
        """Test --ro CLI argument."""
        code, stdout, stderr = run_bww('--ro', '/etc', '--dry-run', 'echo', 'test')
        assert code == 0
        assert '--ro-bind' in stdout
        assert '/etc' in stdout

    def test_cli_mount_tmpfs(self) -> None:
        """Test --tmpfs CLI argument."""
        code, stdout, stderr = run_bww('--tmpfs', '/tmp', '--dry-run', 'echo', 'test')
        assert code == 0
        assert '--tmpfs' in stdout

    def test_cli_bwargs(self) -> None:
        """Test --bwargs for extra arguments."""
        code, stdout, stderr = run_bww('--bwargs', '--keep-fd 3:4', '--dry-run', 'echo', 'test')
        assert code == 0
        assert '--keep-fd' in stdout
        assert '3:4' in stdout

    def test_multiple_rw_mounts(self) -> None:
        """Test multiple read-write mounts."""
        code, stdout, stderr = run_bww('--rw', '/home', '--rw', '/var', '--dry-run', 'test')
        assert code == 0
        assert stdout.count('--bind') >= 2

    def test_validate_flag(self) -> None:
        """Test --validate flag for config checking."""
        code, stdout, stderr = run_bww('--validate')
        assert code == 0
        assert '[OK]' in stdout
        assert 'valid' in stdout.lower()

    def test_validate_with_config_file(self) -> None:
        """Test --validate with example config file."""
        code, stdout, stderr = run_bww('--config', 'example/config.kdl', '--validate')
        assert code == 0
        assert '[OK]' in stdout

    def test_set_env_cli(self) -> None:
        """--set-env produces a --setenv arg in the bwrap command."""
        code, stdout, stderr = run_bww('--set-env', 'FOO=bar', '--dry-run', 'echo')
        assert code == 0
        clean = _strip_ansi(stdout)
        assert '--setenv FOO bar' in clean

    def test_set_env_cli_value_expanded(self) -> None:
        """--set-env value supports ${VAR} expansion against parent shell."""
        code, stdout, stderr = run_bww_with_env(
            {'BWW_TARGET': '/some/path'}, '--set-env', 'TGT=${BWW_TARGET}/sub', '--dry-run', 'echo'
        )
        assert code == 0
        clean = _strip_ansi(stdout)
        assert '--setenv TGT /some/path/sub' in clean

    def test_set_env_cli_invalid_format(self) -> None:
        """--set-env without '=' is rejected with a clear error."""
        code, stdout, stderr = run_bww('--set-env', 'NOEQUALS', '--dry-run', 'echo')
        assert code == 1
        assert 'KEY=VALUE' in stderr

    def test_unset_env_cli_wildcards(self) -> None:
        """--unset-env expands wildcards against the calling environment."""
        code, stdout, stderr = run_bww_with_env(
            {'BWW_INTEG_A': '1', 'BWW_INTEG_B': '2'},
            '--unset-env',
            'BWW_INTEG_*',
            '--dry-run',
            'echo',
        )
        assert code == 0
        clean = _strip_ansi(stdout)
        assert '--unsetenv BWW_INTEG_A' in clean
        assert '--unsetenv BWW_INTEG_B' in clean

    def test_set_env_kdl_with_expansion(self) -> None:
        """KDL set-env "KEY" "${HOST_VAR}/sub" expands at build time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(
                """
profiles.envp {
  set-env "TGT" "${BWW_HOST_VAR}/sub"
  unset-env "BWW_DROP_*"
}
"""
            )
            code, stdout, stderr = run_bww_with_env(
                {'BWW_HOST_VAR': '/host', 'BWW_DROP_X': '1', 'BWW_DROP_Y': '2'},
                '--config',
                str(cfg),
                '-p',
                'envp',
                '--dry-run',
                'echo',
            )
            assert code == 0, stderr
            clean = _strip_ansi(stdout)
            assert '--setenv TGT /host/sub' in clean
            assert '--unsetenv BWW_DROP_X' in clean
            assert '--unsetenv BWW_DROP_Y' in clean

    def test_set_env_excludes_var_from_unset_in_dry_run(self) -> None:
        """A var listed in set-env should not also be emitted as --unsetenv."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(
                """
profiles.envp {
  set-env "BWW_KEEP_X" "kept"
  unset-env "BWW_KEEP_*"
}
"""
            )
            code, stdout, stderr = run_bww_with_env(
                {'BWW_KEEP_X': 'orig', 'BWW_KEEP_Y': 'gone'},
                '--config',
                str(cfg),
                '-p',
                'envp',
                '--dry-run',
                'echo',
            )
            assert code == 0
            clean = _strip_ansi(stdout)
            assert '--setenv BWW_KEEP_X kept' in clean
            assert '--unsetenv BWW_KEEP_X' not in clean
            assert '--unsetenv BWW_KEEP_Y' in clean

    def test_non_in_place_mount_in_dry_run(self) -> None:
        """KDL 2-arg form produces `--bind SRC DEST` with distinct paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(
                """
profiles.bind {
  rw "/tmp/host-cache" "/sandbox-cache"
}
"""
            )
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'bind', '--dry-run', 'echo')
            assert code == 0, stderr
            clean = _strip_ansi(stdout)
            assert '--bind /tmp/host-cache /sandbox-cache' in clean

    def test_unknown_kdl_key_warns(self) -> None:
        """A typo in profile body emits [WARN] but doesn't fail the run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(
                """
profiles.p {
  rw "/home"
  setenv "FOO" "bar"   // typo: should be set-env
}
"""
            )
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'p', '--dry-run', 'echo')
            assert code == 0
            assert '[WARN]' in stderr
            assert 'setenv' in stderr
            assert 'unknown key' in stderr.lower()

    def test_unknown_top_level_key_warns(self) -> None:
        """Unknown top-level KDL nodes emit [WARN] but don't fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / 'config.kdl'
            cfg.write_text(
                """
flapdoodle "what"
profiles.p {
  rw "/home"
}
"""
            )
            code, stdout, stderr = run_bww('--config', str(cfg), '-p', 'p', '--dry-run', 'echo')
            assert code == 0
            assert '[WARN]' in stderr
            assert 'flapdoodle' in stderr

    def test_no_default_flag(self) -> None:
        """Test --no-default flag prevents command defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'config.kdl'
            config_file.write_text(
                """
defaults.test {
  rw "/should/not/appear"
}
"""
            )
            code, stdout, stderr = run_bww('--config', str(config_file), '-n', '--dry-run', 'test')
            assert code == 0
            # The /should/not/appear should not be in the bwrap command
            assert '/should/not/appear' not in stdout
