"""Test-time sys.path repair for Nix dev-shell environments.

The Nix dev-shell injects propagated build inputs onto PYTHONPATH —
including `kdl-py 1.2.0` from nixpkgs, which lacks the v2-only
`Node.entries` API our parser uses. The flake's `shellHook` already
unsets PYTHONPATH on shell entry, but a stale shell or an outer wrapper
can still leak it in. To keep `pytest` deterministic regardless, hoist
the project's own venv site-packages to the front of `sys.path` here.
"""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path


def _venv_site_packages() -> Path | None:
    venv = Path(__file__).resolve().parent.parent / '.venv'
    if not venv.is_dir():
        return None
    purelib = sysconfig.get_paths(vars={'base': str(venv), 'platbase': str(venv)})['purelib']
    p = Path(purelib)
    return p if p.is_dir() else None


def _hoist_venv() -> None:
    site = _venv_site_packages()
    if site is None:
        return
    site_str = str(site)
    sys.path[:] = [site_str] + [p for p in sys.path if p != site_str]
    # If `kdl` was already imported from the wrong location, evict it so
    # the next `import kdl` (in src/bww/loader.py etc.) re-resolves.
    for modname in list(sys.modules):
        if modname == 'kdl' or modname.startswith('kdl.'):
            mod_file = getattr(sys.modules[modname], '__file__', '') or ''
            if not mod_file.startswith(site_str):
                del sys.modules[modname]


_hoist_venv()
