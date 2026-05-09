"""BWW - BubbleWrap Wrapper: Simplify bubblewrap sandboxing with configuration."""

import argparse
import sys

from .dispatch import add_cli_arg
from .loader import load_config
from .models import ConfigError
from .options import OPTIONS
from .runtime import build_runtime_config, validate_config
from .executor import (
    ExecutionError,
    build_bwrap_command,
    execute_bwrap,
    format_bwrap_command,
    to_argv,
)
from .utils import configure_logging, logger


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

    # All per-profile options come from the OPTIONS registry — argparse setup
    # is dispatcher-driven, so adding a new option of a known kind is just a
    # registry entry plus the matching dataclass fields.
    for spec in OPTIONS:
        add_cli_arg(parser, spec)

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
        configure_logging(debug=bool(args.debug))

        # ====================================================================
        # Handle --validate: Check config validity and exit
        # ====================================================================
        if args.validate:
            config = load_config(args.config)
            validate_config(config)
            logger.success('Configuration is valid')
            return

        # ====================================================================
        # Normal operation: Load, validate, and execute
        # ====================================================================

        # Load configuration
        config = load_config(args.config)
        validate_config(config)

        # Check command provided
        if not args.command:
            logger.error('No command specified')
            sys.exit(1)

        # Build runtime configuration
        runtime = build_runtime_config(config, args, args.command)

        # Build bwrap command (as token groups; flatten only for exec)
        bwrap_groups = build_bwrap_command(runtime)
        formatted = format_bwrap_command(bwrap_groups)

        # --dry-run: print the bwrap command and exit (primary program output;
        # plain stdout, no log prefix, so it's pipeable).
        if args.dry_run:
            print(formatted)
            return

        # --debug: log the bwrap command before exec.
        logger.debug(f'running bwrap:\n{formatted}')

        exit_code = execute_bwrap(to_argv(bwrap_groups), args.debug_tmpfs)
        sys.exit(exit_code)

    except ConfigError as e:
        logger.error(f'Configuration error: {e}')
        sys.exit(1)
    except ExecutionError as e:
        logger.error(f'Execution error: {e}')
        sys.exit(2)
    except KeyboardInterrupt:
        logger.warning('Interrupted by user')
        sys.exit(130)
    except Exception as e:
        logger.error(f'Unexpected error: {e}')
        sys.exit(3)
