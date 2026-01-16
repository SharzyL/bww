"""BWW - BubbleWrap Wrapper: Simplify bubblewrap sandboxing with configuration."""

import argparse
import sys

from .config import ConfigError, build_runtime_config, load_config, validate_config
from .options import BOOL_OPTIONS
from .executor import (
    ExecutionError,
    build_bwrap_command,
    execute_bwrap,
    format_bwrap_command,
)
from .utils import command, error, success, warn


def create_parser() -> argparse.ArgumentParser:
    """Create and configure argument parser."""
    parser = argparse.ArgumentParser(
        prog='bww',
        usage='bww [OPTIONS...] [--] COMMAND [ARGS...]',
        description='BubbleWrap Wrapper - Simplify bubblewrap sandboxing with profiles',
    )

    parser.add_argument(
        '--config',
        metavar='PATH',
        help='Path to config file (default: $XDG_CONFIG_HOME/bww/config.kdl or ~/.config/bww/config.kdl)',
    )

    parser.add_argument(
        '-p',
        '--profile',
        metavar='NAME',
        help='Load profile from configuration (NAME, profiles.NAME, or defaults.NAME)',
    )

    parser.add_argument(
        '-n',
        '--no-default',
        action='store_true',
        help='Do not load [defaults.COMMAND] section',
    )

    parser.add_argument(
        '--rw',
        action='append',
        default=[],
        metavar='PATH',
        dest='rw',
        help='Read-write mount (can be repeated)',
    )

    parser.add_argument(
        '--ro',
        action='append',
        default=[],
        metavar='PATH',
        dest='ro',
        help='Read-only mount (can be repeated)',
    )

    parser.add_argument(
        '--tmpfs',
        action='append',
        default=[],
        metavar='PATH',
        dest='tmpfs',
        help='Tmpfs mount (can be repeated)',
    )

    parser.add_argument(
        '--bwargs',
        metavar='ARGS',
        help='Extra bwrap arguments (space-separated string)',
    )

    for opt in BOOL_OPTIONS:
        parser.add_argument(
            opt.cli_flag,
            action='store_true',
            dest=opt.dest,
            help=opt.help,
        )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='Print bwrap command before executing',
    )

    parser.add_argument(
        '--debug-tmpfs',
        action='store_true',
        dest='debug_tmpfs',
        help='Show tmpfs content after exit',
    )

    parser.add_argument(
        '--validate',
        action='store_true',
        help='Validate configuration and exit',
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        dest='dry_run',
        help='Show what would be executed without running',
    )

    parser.add_argument(
        'command',
        nargs='*',
        help='Command and arguments to run in sandbox',
    )

    return parser


def main() -> None:
    """Main entry point for BWW."""
    try:
        parser = create_parser()
        args = parser.parse_args()

        # ====================================================================
        # Handle --validate: Check config validity and exit
        # ====================================================================
        if args.validate:
            config = load_config(args.config)
            validate_config(config)
            success('Configuration is valid')
            return

        # ====================================================================
        # Normal operation: Load, validate, and execute
        # ====================================================================

        # Load configuration
        config = load_config(args.config)
        validate_config(config)

        # Check command provided
        if not args.command:
            error('No command specified')
            sys.exit(1)

        # Build runtime configuration
        runtime = build_runtime_config(config, args, args.command)

        # Build bwrap command
        bwrap_cmd = build_bwrap_command(runtime)

        # ====================================================================
        # Handle --dry-run: Show what would run without executing
        # ====================================================================
        if args.dry_run:
            command(format_bwrap_command(bwrap_cmd))
            return

        # ====================================================================
        # Debug output: Show command being executed
        # ====================================================================
        if args.debug:
            command(format_bwrap_command(bwrap_cmd))

        # ====================================================================
        # Execute bwrap
        # ====================================================================
        exit_code = execute_bwrap(bwrap_cmd, args.debug_tmpfs)
        sys.exit(exit_code)

    except ConfigError as e:
        error(f'Configuration error: {e}')
        sys.exit(1)
    except ExecutionError as e:
        error(f'Execution error: {e}')
        sys.exit(2)
    except KeyboardInterrupt:
        warn('Interrupted by user')
        sys.exit(130)
    except Exception as e:
        error(f'Unexpected error: {e}')
        sys.exit(3)
