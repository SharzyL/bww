## BWW: BubbleWrap Wrapper

BWW simplifies bubblewrap sandboxing through KDL-based configuration with profiles and inheritance. Run applications in isolated environments with minimal setup.

### Quick Start

```console
$ uv run bww --help
$ uv run bww --config example/config.kdl --validate
$ uv run bww --dry-run echo "test"
```

### Configuration Example

Place config at `$XDG_CONFIG_HOME/bww/config.kdl` or `~/.config/bww/config.kdl`:

```kdl
profiles.develop-tool {
  tmpfs "~"
  rw "."
}
profiles.desktop-app {
  ro "%E/gtk-4.0"
  ro "%E/mimeapps.list"
}
defaults.firefox {
  inherit "desktop-app"
  rw "~/.mozilla"
}
defaults.python-dev {
  inherit "develop-tool"
  rw "%h/.venv"
}
```

Mount entries take 1 or 2 path arguments:

- `rw "/host"` is in-place (host path = sandbox path).
- `rw "/host" "/sandbox"` is non-in-place (different src and dest).
- `tmpfs "/path"` has no src; 2-arg form is rejected.

In-place binds may use globs (`*`, `?`, `[abc]`) on the src path; explicit-dest binds may not.

When an in-place mount's src is a host symlink, bww follows the chain hop-by-hop, binds the final
resolved target, and emits one `--symlink` per host hop — preserving the symlink structure inside
the sandbox rather than collapsing it. In-place children covered by a same-mode in-place ancestor
are elided automatically (e.g. `ro /etc + ro /etc/hosts` emits only `--ro-bind /etc /etc`).

### Path Variables

Command (exec) paths support both shell-style and systemd-style expansion when you pass a path-like command
(e.g. `./script`, `~/bin/tool`, `%h/bin/tool`). Mount paths also support this expansion.
If you pass a bare command like `chromium`, bww resolves it via `PATH` (like `which chromium`) and passes the
absolute executable path to `bwrap`.
The exe's resolved target is auto ro-bound; if the exe path is itself a host symlink, bww also emits a `--symlink`
so the user-facing exe path keeps working inside the sandbox (skipped automatically when a bind ancestor or
another planned symlink already exposes it).

- `${ENV_NAME}` - Environment variable expansion
- `~` - User home directory (shell-style, only at path start like `~/dir`)
- `%h` - User home directory (systemd specifier, can be anywhere)
- `%E` - XDG_CONFIG_HOME directory (configuration root, e.g., `%E/gtk-4.0`)
- `%C` - XDG_CACHE_HOME directory (cache root, e.g., `%C/bww`)
- `%D` - XDG_DATA_HOME directory (data home, e.g., `%D/bww`)
- `%S` - XDG_STATE_HOME directory (state home, e.g., `%S/bww`)
- `%t` - XDG_RUNTIME_DIR directory (systemd specifier)
- `%u` - User name
- `%U` - User uid
- `%%` - Literal percent sign

### CLI Usage

```console
$ bww [OPTIONS...] [--] COMMAND [ARGS...]

Options:
  --config PATH       Path to config file
  -p, --profile NAME  Load profile from configuration (NAME, profiles.NAME, or defaults.NAME)
  -n, --no-default    Do not load [defaults.COMMAND] section
  --rw PATH           Read-write mount (repeatable)
  --ro PATH           Read-only mount (repeatable)
  --tmpfs PATH        Tmpfs mount (repeatable)
  --bwargs ARGS       Extra bwrap arguments (space-separated)
  --set-env KEY=VALUE Set env var inside sandbox (repeatable; ${VAR} expanded in VALUE)
  --unset-env PATTERN Unset env vars matching PATTERN inside sandbox (repeatable;
                      supports glob wildcards `*`, `?`, `[abc]` and ${VAR} expansion)
  --share-net         Share the host network namespace (omit bwrap --unshare-net)
  --share-user        Share the host user namespace (omit bwrap --unshare-user)
  --share-ipc         Share the host IPC namespace (omit bwrap --unshare-ipc)
  --share-pid         Share the host PID namespace (omit bwrap --unshare-pid)
  --share-uts         Share the host UTS namespace (omit bwrap --unshare-uts)
  --dev-bind          Bind-mount host /dev into sandbox (enables device access)
  --reuse-session     Do not create a new session (omit bwrap --new-session)
  --debug             Print bwrap command before executing
  --validate          Validate configuration and exit
  --dry-run           Show what would be executed without running
```

### Environment Variables

Profiles and the CLI can shape the sandbox environment with `set-env` and `unset-env`.

```kdl
profiles.scrub {
  set-env "PATH" "/usr/bin:${HOME}/.local/bin"   // ${ENV} expanded against parent shell
  unset-env "SSH_*" "AWS_*"                       // glob wildcards (fnmatch)
}
```

- `set-env "KEY" "VALUE"` — emits `--setenv KEY VALUE` to bwrap. `${VAR}` is expanded in `VALUE`
  against the calling shell's environment. Later definitions for the same `KEY`
  (down the inheritance chain or on the CLI) override earlier ones.
- `unset-env "PATTERN"` — patterns are fnmatch-style globs (`*`, `?`, `[abc]`).
  `${VAR}` is expanded in the pattern, then the result is matched against the
  calling shell's environment; each match emits `--unsetenv VAR` to bwrap. Vars
  also covered by `set-env` are skipped (set-env wins).

### Examples

```console
# Validate configuration
$ bww --validate

# Show what would be executed
$ bww --dry-run firefox

# Use specific profile with CLI overrides
$ bww -p develop-tool --rw "$(pwd)" --ro /var --dry-run echo "hello"

# Run with debug output
$ bww --debug --dry-run bash
```

## Development

### Running

```console
# Run the application
$ uv run bww --help

# Run with config validation
$ uv run bww --config example/config.kdl --validate
```

### Testing

```console
# Run all tests (unit + integration)
$ uv run pytest tests/ -v

# Run only unit tests
$ uv run pytest tests/test_config.py -v

# Run only integration tests
$ uv run pytest tests/test_integration.py -v
```

### Type Checking and Lint

```console
# Lint + type check
$ uv run ruff check src/bww tests
$ uv run basedpyright

# Full check with nix (build + tests + treefmt)
$ nix flake check
```

### Code Formatting

```console
# Format Python code
$ uv run ruff format src/bww tests

# Format Nix files
$ nix fmt
```

### Snapshots

`tests/snapshots/` captures byte-exact `bww --dry-run` output for `dev`,
`defaults.firefox`, and `defaults.chromium` profiles. Run
`bash tests/snapshots/check.sh` to diff the current output against the
saved snapshots after a change.
