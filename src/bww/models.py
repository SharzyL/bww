"""Core data classes for BWW: profile / runtime / mount + ConfigError.

Pure data — no IO, no parsing. Field shape is kept in sync with the
`options.OPTIONS` registry via `verify_options_invariant`, which is run
explicitly from the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Literal

from .options import OPTIONS


@dataclass(frozen=True)
class MountEntry:
    """Profile-level mount entry, before glob expansion.

    1-arg KDL form `rw "/home"` → MountEntry(src='/home', dest='/home').
    2-arg KDL form `rw "/host" "/sandbox"` → MountEntry(src='/host', dest='/sandbox').
    tmpfs has no src: `tmpfs "/work"` → MountEntry(src=None, dest='/work').
    """

    src: str | None
    dest: str

    @classmethod
    def in_place(cls, path: str) -> 'MountEntry':
        """Convenience constructor for src == dest."""
        return cls(src=path, dest=path)

    @classmethod
    def tmpfs_at(cls, dest: str) -> 'MountEntry':
        """Convenience constructor for tmpfs (no src)."""
        return cls(src=None, dest=dest)


@dataclass(frozen=True)
class Mount:
    """A single resolved mount point for bubblewrap.

    Distinguishes src (host path) from dest (sandbox path) to support
    non-in-place binds. tmpfs has no src (use src=None).
    """

    src: str | None
    dest: str
    mode: Literal['rw', 'ro', 'tmpfs']

    def __hash__(self) -> int:
        return hash((self.src, self.dest, self.mode))

    @classmethod
    def in_place(cls, path: str, mode: Literal['rw', 'ro', 'tmpfs']) -> 'Mount':
        """Convenience constructor for src == dest."""
        return cls(src=path, dest=path, mode=mode)


@dataclass
class Profile:
    """A configuration profile from `[profiles.NAME]` or `[defaults.NAME]`."""

    name: str
    inherit: list[str] = field(default_factory=list)
    rw: list[MountEntry] = field(default_factory=list)
    ro: list[MountEntry] = field(default_factory=list)
    tmpfs: list[MountEntry] = field(default_factory=list)
    bwargs: list[str] = field(default_factory=list)
    set_env: list[tuple[str, str]] = field(default_factory=list)
    unset_env: list[str] = field(default_factory=list)
    share_net: bool = False
    dev_bind: bool = False
    reuse_session: bool = False
    share_user: bool = False
    share_ipc: bool = False
    share_pid: bool = False
    share_uts: bool = False


@dataclass
class Config:
    """Complete parsed configuration from config.kdl."""

    profiles: dict[str, Profile] = field(default_factory=dict)
    defaults: dict[str, Profile] = field(default_factory=dict)


@dataclass
class RuntimeConfig:
    """Final merged configuration ready for execution."""

    argv0: str
    command: list[str]
    mounts: set[Mount] = field(default_factory=set)
    # Synthetic symlinks to materialize in the sandbox: (target, link_path).
    # Used to preserve a symlinked exe path when no bind ancestor exposes it.
    symlinks: list[tuple[str, str]] = field(default_factory=list)
    bwargs: list[str] = field(default_factory=list)
    set_env: list[tuple[str, str]] = field(default_factory=list)
    unset_env: list[str] = field(default_factory=list)
    share_net: bool = False
    dev_bind: bool = False
    reuse_session: bool = False
    share_user: bool = False
    share_ipc: bool = False
    share_pid: bool = False
    share_uts: bool = False
    debug: bool = False
    debug_tmpfs: bool = False


class ConfigError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""

    pass


def verify_options_invariant() -> None:
    """Raise AssertionError if any OPTIONS entry is missing dataclass fields.

    Mount specs aggregate into RuntimeConfig.mounts (one set), so they have
    a Profile field but no individual RuntimeConfig field — that's expected.
    Catches drift: adding an OPTIONS entry without the matching dataclass
    fields fails loudly here. Called from the test suite, not at import.
    """
    profile_fields = {f.name for f in fields(Profile)}
    runtime_fields = {f.name for f in fields(RuntimeConfig)}
    for spec in OPTIONS:
        if spec.dest not in profile_fields:
            raise AssertionError(f'OPTIONS spec {spec.key!r}: Profile is missing field {spec.dest!r}')
        if spec.kind != 'mount' and spec.dest not in runtime_fields:
            raise AssertionError(f'OPTIONS spec {spec.key!r}: RuntimeConfig is missing field {spec.dest!r}')
