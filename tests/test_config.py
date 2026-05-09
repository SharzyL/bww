"""Unit tests for BWW configuration module."""

import os
import tempfile
from pathlib import Path

import pytest

from bww.config import (
    Config,
    ConfigError,
    Mount,
    Profile,
    build_runtime_config,
    load_config,
    resolve_profile,
    validate_config,
)


class TestConfigLoading:
    """Test configuration file loading."""

    def test_load_empty_config(self) -> None:
        """Test loading when config file doesn't exist."""
        config = load_config('/nonexistent/path.toml')
        assert config.profiles == {}
        assert config.defaults == {}

    def test_load_simple_config(self) -> None:
        """Test loading simple config file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'config.kdl'
            config_file.write_text(
                """
profiles.test {
  rw "/home"
  ro "/etc"
  tmpfs "/tmp"
}
"""
            )
            config = load_config(str(config_file))
            assert 'test' in config.profiles
            assert config.profiles['test'].rw == ['/home']
            assert config.profiles['test'].ro == ['/etc']
            assert config.profiles['test'].tmpfs == ['/tmp']

    def test_load_config_with_defaults(self) -> None:
        """Test loading config with command defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'config.kdl'
            config_file.write_text(
                """
defaults.firefox {
  rw "~/.mozilla"
}
"""
            )
            config = load_config(str(config_file))
            assert 'firefox' in config.defaults
            assert config.defaults['firefox'].rw == ['~/.mozilla']

    def test_load_invalid_toml(self) -> None:
        """Test error on invalid KDL syntax."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'bad.kdl'
            config_file.write_text('profiles { test { rw "." ')  # missing closing braces
            with pytest.raises(ConfigError, match='Invalid KDL'):
                load_config(str(config_file))

    def test_load_mount_as_string(self) -> None:
        """Test loading mount path as string (not list)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'config.kdl'
            config_file.write_text(
                """
profiles.test {
  rw "/home"
}
"""
            )
            config = load_config(str(config_file))
            assert config.profiles['test'].rw == ['/home']

    def test_load_mount_as_list(self) -> None:
        """Test loading mount paths as list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'config.kdl'
            config_file.write_text(
                """
profiles.test {
  rw "/home" "/var"
}
"""
            )
            config = load_config(str(config_file))
            assert set(config.profiles['test'].rw) == {'/home', '/var'}

    def test_load_inherit_as_string(self) -> None:
        """Test inherit as single string."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'config.kdl'
            config_file.write_text(
                """
profiles.parent {
  rw "/home"
}
profiles.child {
  inherit "parent"
}
"""
            )
            config = load_config(str(config_file))
            assert config.profiles['child'].inherit == ['parent']

    def test_load_inherit_as_list(self) -> None:
        """Test inherit as array of strings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'config.kdl'
            config_file.write_text(
                """
profiles.a {
  rw "/a"
}
profiles.b {
  rw "/b"
}
profiles.c {
  inherit "a" "b"
}
"""
            )
            config = load_config(str(config_file))
            assert config.profiles['c'].inherit == ['a', 'b']


class TestProfileResolution:
    """Test profile inheritance resolution."""

    def test_resolve_no_inheritance(self) -> None:
        """Test resolving profile with no inheritance."""
        profile = Profile(name='test', rw=['/home'], ro=['/etc'])
        all_profiles = {'test': profile}
        resolved = resolve_profile(profile, all_profiles)
        assert resolved.rw == ['/home']
        assert resolved.ro == ['/etc']

    def test_resolve_single_inheritance(self) -> None:
        """Test resolving profile with single parent."""
        parent = Profile(name='parent', rw=['/home'])
        child = Profile(name='child', inherit=['parent'], ro=['/etc'])
        all_profiles = {'parent': parent, 'child': child}
        resolved = resolve_profile(child, all_profiles)
        assert resolved.rw == ['/home']
        assert resolved.ro == ['/etc']

    def test_resolve_inheritance_list_concatenation(self) -> None:
        """Test that lists are concatenated during inheritance."""
        parent = Profile(name='parent', rw=['/a', '/b'])
        child = Profile(name='child', inherit=['parent'], rw=['/c'])
        all_profiles = {'parent': parent, 'child': child}
        resolved = resolve_profile(child, all_profiles)
        # Parent values first, then child
        assert resolved.rw == ['/a', '/b', '/c']

    def test_resolve_multiple_parents(self) -> None:
        """Test resolving profile with multiple parents."""
        parent_a = Profile(name='parent_a', rw=['/a'])
        parent_b = Profile(name='parent_b', rw=['/b'])
        child = Profile(name='child', inherit=['parent_a', 'parent_b'], rw=['/c'])
        all_profiles = {'parent_a': parent_a, 'parent_b': parent_b, 'child': child}
        resolved = resolve_profile(child, all_profiles)
        assert resolved.rw == ['/a', '/b', '/c']

    def test_resolve_deep_inheritance(self) -> None:
        """Test resolving multi-level inheritance chain."""
        grandparent = Profile(name='grandparent', rw=['/gp'])
        parent = Profile(name='parent', inherit=['grandparent'], rw=['/p'])
        child = Profile(name='child', inherit=['parent'], rw=['/c'])
        all_profiles = {
            'grandparent': grandparent,
            'parent': parent,
            'child': child,
        }
        resolved = resolve_profile(child, all_profiles)
        assert resolved.rw == ['/gp', '/p', '/c']

    def test_resolve_circular_inheritance(self) -> None:
        """Test that circular inheritance is detected."""
        profile_a = Profile(name='a', inherit=['b'])
        profile_b = Profile(name='b', inherit=['a'])
        all_profiles = {'a': profile_a, 'b': profile_b}
        with pytest.raises(ConfigError, match='[Cc]ircular'):
            resolve_profile(profile_a, all_profiles)

    def test_resolve_unknown_parent(self) -> None:
        """Test error on unknown parent profile."""
        child = Profile(name='child', inherit=['nonexistent'])
        all_profiles = {'child': child}
        with pytest.raises(ConfigError, match='Unknown parent'):
            resolve_profile(child, all_profiles)


class TestConfigValidation:
    """Test configuration validation."""

    def test_validate_empty_config(self) -> None:
        """Test validating empty config succeeds."""
        config = Config()
        validate_config(config)  # Should not raise

    def test_validate_valid_config(self) -> None:
        """Test validating valid config succeeds."""
        config = Config(
            profiles={
                'test': Profile(name='test', rw=['/home']),
            }
        )
        validate_config(config)  # Should not raise

    def test_validate_detects_circular_inheritance(self) -> None:
        """Test validation detects circular inheritance."""
        config = Config(
            profiles={
                'a': Profile(name='a', inherit=['b']),
                'b': Profile(name='b', inherit=['a']),
            }
        )
        with pytest.raises(ConfigError, match='[Cc]ircular'):
            validate_config(config)


class TestMount:
    """Test Mount dataclass."""

    def test_mount_hashable(self) -> None:
        """Test Mount objects are hashable."""
        mount1 = Mount(path='/home', mode='rw')
        mount2 = Mount(path='/home', mode='rw')
        mount3 = Mount(path='/etc', mode='ro')

        # Should work in sets
        mounts = {mount1, mount2, mount3}
        assert len(mounts) == 2  # mount1 and mount2 are equal

    def test_mount_equality(self) -> None:
        """Test Mount equality."""
        mount1 = Mount(path='/home', mode='rw')
        mount2 = Mount(path='/home', mode='rw')
        assert mount1 == mount2

    def test_mount_inequality_path(self) -> None:
        """Test Mount with different paths are not equal."""
        mount1 = Mount(path='/home', mode='rw')
        mount2 = Mount(path='/var', mode='rw')
        assert mount1 != mount2

    def test_mount_inequality_mode(self) -> None:
        """Test Mount with different modes are not equal."""
        mount1 = Mount(path='/home', mode='rw')
        mount2 = Mount(path='/home', mode='ro')
        assert mount1 != mount2


class TestPathExpansion:
    """Test path expansion functionality."""

    def test_expand_tilde(self) -> None:
        """Test ~ shell-style expansion."""
        from bww.config import expand_path

        expanded = expand_path('~/test', Path('/tmp'))
        assert expanded.endswith('/test')
        assert Path(expanded).is_absolute()
        assert '~' not in expanded

    def test_expand_home_specifier(self) -> None:
        """Test %h systemd specifier expansion."""
        from bww.config import expand_path

        expanded = expand_path('%h/test', Path('/tmp'))
        assert expanded.endswith('/test')
        assert Path(expanded).is_absolute()
        assert '%h' not in expanded

    def test_expand_config_specifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test %E specifier expansion to XDG config home."""
        from bww.config import expand_path

        monkeypatch.setenv('XDG_CONFIG_HOME', '/tmp/xdg-config')
        expanded = expand_path('%E/gtk-4.0', Path('/tmp'))
        assert expanded.startswith('/tmp/xdg-config/')
        assert Path(expanded).is_absolute()

    def test_expand_cache_specifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test %C specifier expansion to XDG cache home."""
        from bww.config import expand_path

        monkeypatch.setenv('XDG_CACHE_HOME', '/tmp/xdg-cache')
        expanded = expand_path('%C/bww', Path('/tmp'))
        assert expanded.startswith('/tmp/xdg-cache/')
        assert Path(expanded).is_absolute()

    def test_expand_state_specifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test %S specifier expansion to XDG state home."""
        from bww.config import expand_path

        monkeypatch.setenv('XDG_STATE_HOME', '/tmp/xdg-state')
        expanded = expand_path('%S/bww', Path('/tmp'))
        assert expanded.startswith('/tmp/xdg-state/')
        assert Path(expanded).is_absolute()

    def test_expand_data_specifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test %D specifier expansion to XDG data home."""
        from bww.config import expand_path

        monkeypatch.setenv('XDG_DATA_HOME', '/tmp/xdg-data')
        expanded = expand_path('%D/bww', Path('/tmp'))
        assert expanded.startswith('/tmp/xdg-data/')
        assert Path(expanded).is_absolute()

    def test_expand_runtime_specifier(self) -> None:
        """Test %t specifier expansion."""
        from bww.config import expand_path

        expanded = expand_path('%t/myapp', Path('/tmp'))
        # %t should expand to XDG_RUNTIME_DIR
        assert '~' not in expanded
        assert Path(expanded).is_absolute()

    def test_expand_percent_escape(self) -> None:
        """Test %% escape for literal percent."""
        from bww.config import expand_path

        expanded = expand_path('100%% complete', Path('/tmp'))
        assert '%%' not in expanded
        assert '100% complete' in expanded

    def test_expand_relative_path(self) -> None:
        """Test relative path becomes absolute."""
        from bww.config import expand_path

        expanded = expand_path('./test', Path('/tmp'))
        assert Path(expanded).is_absolute()

    def test_expand_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test ${ENV_NAME} expansion."""
        from bww.config import expand_path

        monkeypatch.setenv('BWW_TEST_ENV', '/tmp/bww-env')
        expanded = expand_path('${BWW_TEST_ENV}/x', Path('/tmp'))
        assert expanded.startswith('/tmp/bww-env/')

    def test_expand_env_var_missing_raises(self) -> None:
        """Test missing ${ENV_NAME} raises a ConfigError."""
        from bww.config import ConfigError, expand_path

        with pytest.raises(ConfigError, match='Environment variable not set'):
            expand_path('${BWW_MISSING_ENV}/x', Path('/tmp'))


class TestRuntimeConfigBuilding:
    """Test runtime configuration building."""

    def test_build_runtime_config_with_profile(self) -> None:
        """Test building runtime config from profile."""
        import argparse
        import shutil

        config = Config(profiles={'test': Profile(name='test', rw=['/home'])})
        args = argparse.Namespace(
            config=None,
            profile='test',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo', 'test'])
        assert runtime.argv0 == 'echo'
        assert runtime.command[1:] == ['test']
        assert runtime.command[0] == (shutil.which('echo') or 'echo')

    def test_command_defaults_use_basename_not_resolved_path(self) -> None:
        """Defaults lookup should use the unexpanded command name (basename)."""
        import argparse

        config = Config(
            profiles={},
            defaults={'echo': Profile(name='echo', rw=['/home'])},
        )
        args = argparse.Namespace(
            config=None,
            profile=None,
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['/bin/echo', 'test'])
        assert any(m.path == '/home' and m.mode == 'rw' for m in runtime.mounts)

    def test_share_net_merges_from_profile(self) -> None:
        """share-net should flow from profile/default into runtime config."""
        import argparse

        config = Config(profiles={'p': Profile(name='p', share_net=True)})
        args = argparse.Namespace(
            config=None,
            profile='p',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            share_net=False,
            dev_bind=False,
            reuse_session=False,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo'])
        assert runtime.share_net is True

    def test_dev_bind_merges_from_profile(self) -> None:
        """dev-bind should flow from profile/default into runtime config."""
        import argparse

        config = Config(profiles={'p': Profile(name='p', dev_bind=True)})
        args = argparse.Namespace(
            config=None,
            profile='p',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            share_net=False,
            dev_bind=False,
            reuse_session=False,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo'])
        assert runtime.dev_bind is True

    def test_profile_flag_accepts_defaults_namespace(self) -> None:
        """-p should accept defaults.NAME references."""
        import argparse

        config = Config(defaults={'x': Profile(name='x', share_net=True)})
        args = argparse.Namespace(
            config=None,
            profile='defaults.x',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            share_net=False,
            dev_bind=False,
            reuse_session=False,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo'])
        assert runtime.share_net is True

    def test_reuse_session_merges_from_profile(self) -> None:
        """reuse-session should flow from profile/default into runtime config."""
        import argparse

        config = Config(profiles={'p': Profile(name='p', reuse_session=True)})
        args = argparse.Namespace(
            config=None,
            profile='p',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            share_net=False,
            dev_bind=False,
            reuse_session=False,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo'])
        assert runtime.reuse_session is True

    def test_share_namespaces_merge_from_profile(self) -> None:
        """share-user/share-ipc/share-pid should flow from profile/default into runtime config."""
        import argparse

        config = Config(
            profiles={
                'p': Profile(name='p', share_user=True, share_ipc=True, share_pid=True, share_uts=True),
            }
        )
        args = argparse.Namespace(
            config=None,
            profile='p',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            share_net=False,
            share_user=False,
            share_ipc=False,
            share_pid=False,
            share_uts=False,
            dev_bind=False,
            reuse_session=False,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo'])
        assert runtime.share_user is True
        assert runtime.share_ipc is True
        assert runtime.share_pid is True
        assert runtime.share_uts is True

    def test_build_runtime_config_with_cli_args(self) -> None:
        """Test CLI arguments override profile."""
        import argparse

        config = Config(profiles={'test': Profile(name='test', rw=['/home'])})
        args = argparse.Namespace(
            config=None,
            profile='test',
            no_default=False,
            rw=['/var'],
            ro=['/etc'],
            tmpfs=['/tmp'],
            bwargs=None,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['test'])
        # Should have mounts from profile AND CLI
        assert any(m.path == '/home' for m in runtime.mounts)
        assert any(m.path == '/var' for m in runtime.mounts)
        assert any(m.path == '/etc' for m in runtime.mounts)
        assert any(m.path == '/tmp' for m in runtime.mounts)

    def test_set_env_merges_from_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """set-env values flow into runtime, with ${VAR} expanded against parent env."""
        import argparse

        monkeypatch.setenv('SOME_HOME', '/tmp/x')

        config = Config(profiles={'p': Profile(name='p', set_env=[('FOO', 'bar'), ('BAZ', '${SOME_HOME}/q')])})
        args = argparse.Namespace(
            config=None,
            profile='p',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            set_env=[],
            unset_env=[],
            share_net=False,
            share_user=False,
            share_ipc=False,
            share_pid=False,
            share_uts=False,
            dev_bind=False,
            reuse_session=False,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo'])
        assert ('FOO', 'bar') in runtime.set_env
        assert ('BAZ', '/tmp/x/q') in runtime.set_env

    def test_unset_env_wildcards_match_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """unset-env patterns match against current environ via fnmatch."""
        import argparse

        # Clean any existing matches and set a known set.
        for k in list(os.environ):
            if k.startswith('BWW_TEST_'):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv('BWW_TEST_A', '1')
        monkeypatch.setenv('BWW_TEST_B', '2')
        monkeypatch.setenv('BWW_OTHER', '3')

        config = Config(profiles={'p': Profile(name='p', unset_env=['BWW_TEST_*'])})
        args = argparse.Namespace(
            config=None,
            profile='p',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            set_env=[],
            unset_env=[],
            share_net=False,
            share_user=False,
            share_ipc=False,
            share_pid=False,
            share_uts=False,
            dev_bind=False,
            reuse_session=False,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo'])
        assert 'BWW_TEST_A' in runtime.unset_env
        assert 'BWW_TEST_B' in runtime.unset_env
        assert 'BWW_OTHER' not in runtime.unset_env

    def test_unset_env_pattern_supports_env_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """${VAR} expansion happens before fnmatch matching of unset-env patterns."""
        import argparse

        for k in list(os.environ):
            if k.startswith('BWW_PFX_'):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv('BWW_PFX_X', '1')
        monkeypatch.setenv('BWW_PFX_Y', '2')
        monkeypatch.setenv('BWW_PREFIX', 'BWW_PFX')

        config = Config(profiles={'p': Profile(name='p', unset_env=['${BWW_PREFIX}_*'])})
        args = argparse.Namespace(
            config=None,
            profile='p',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            set_env=[],
            unset_env=[],
            share_net=False,
            share_user=False,
            share_ipc=False,
            share_pid=False,
            share_uts=False,
            dev_bind=False,
            reuse_session=False,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo'])
        assert 'BWW_PFX_X' in runtime.unset_env
        assert 'BWW_PFX_Y' in runtime.unset_env

    def test_set_env_excludes_var_from_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A var named in set-env should not appear in resolved unset-env."""
        import argparse

        monkeypatch.setenv('BWW_KEEP_A', 'x')
        monkeypatch.setenv('BWW_KEEP_B', 'y')

        config = Config(
            profiles={
                'p': Profile(
                    name='p',
                    set_env=[('BWW_KEEP_A', 'replaced')],
                    unset_env=['BWW_KEEP_*'],
                )
            }
        )
        args = argparse.Namespace(
            config=None,
            profile='p',
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            set_env=[],
            unset_env=[],
            share_net=False,
            share_user=False,
            share_ipc=False,
            share_pid=False,
            share_uts=False,
            dev_bind=False,
            reuse_session=False,
            debug=False,
            debug_tmpfs=False,
        )
        runtime = build_runtime_config(config, args, ['echo'])
        assert ('BWW_KEEP_A', 'replaced') in runtime.set_env
        assert 'BWW_KEEP_A' not in runtime.unset_env
        assert 'BWW_KEEP_B' in runtime.unset_env

    def test_set_env_inheritance_concatenates(self) -> None:
        """set-env from parent and child concatenate; child appended last (wins on bwrap apply)."""
        parent = Profile(name='parent', set_env=[('FOO', 'parent_val')])
        child = Profile(name='child', inherit=['parent'], set_env=[('FOO', 'child_val'), ('BAR', 'b')])
        all_profiles = {'parent': parent, 'child': child}
        resolved = resolve_profile(child, all_profiles)
        assert resolved.set_env == [('FOO', 'parent_val'), ('FOO', 'child_val'), ('BAR', 'b')]

    def test_unset_env_inheritance_concatenates(self) -> None:
        """unset-env patterns from parent and child concatenate."""
        parent = Profile(name='parent', unset_env=['A_*'])
        child = Profile(name='child', inherit=['parent'], unset_env=['B_*'])
        all_profiles = {'parent': parent, 'child': child}
        resolved = resolve_profile(child, all_profiles)
        assert resolved.unset_env == ['A_*', 'B_*']

    def test_load_set_env_unset_env_from_kdl(self) -> None:
        """KDL set-env "KEY" "VALUE" and unset-env "PAT" "PAT2" parse correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'config.kdl'
            config_file.write_text(
                """
profiles.test {
  set-env "FOO" "bar"
  set-env "BAZ" "qux"
  unset-env "SSH_*"
  unset-env "AWS_*" "GOOGLE_*"
}
"""
            )
            config = load_config(str(config_file))
            assert config.profiles['test'].set_env == [('FOO', 'bar'), ('BAZ', 'qux')]
            assert config.profiles['test'].unset_env == ['SSH_*', 'AWS_*', 'GOOGLE_*']

    def test_load_set_env_wrong_arity_raises(self) -> None:
        """set-env with the wrong number of args is a clear error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / 'config.kdl'
            config_file.write_text(
                """
profiles.test {
  set-env "FOO"
}
"""
            )
            with pytest.raises(ConfigError, match='set-env'):
                load_config(str(config_file))

    def test_build_runtime_config_no_command_error(self) -> None:
        """Test error when no command provided."""
        import argparse

        config = Config()
        args = argparse.Namespace(
            config=None,
            profile=None,
            no_default=False,
            rw=[],
            ro=[],
            tmpfs=[],
            bwargs=None,
            debug=False,
            debug_tmpfs=False,
        )
        with pytest.raises(ConfigError, match='command'):
            build_runtime_config(config, args, [])
