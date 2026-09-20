"""Lightweight entry points for installed Free Claude Code commands."""

import sys
from collections.abc import Sequence

from free_claude_code.core.version import package_version


def serve(argv: Sequence[str] | None = None) -> None:
    """Start the FastAPI server (registered as ``fcc-server``)."""
    if _print_version_if_requested(argv):
        return

    from free_claude_code.core.admin_accounts import AdminAccounts

    temporary_password = AdminAccounts().initialize()
    if temporary_password:
        print("FCC Hardened Admin username: admin", flush=True)
        print(f"Temporary Admin password: {temporary_password}", flush=True)
        print(
            "Change it at first login. To recover later, run Run-Hardened.ps1 reset-password.",
            flush=True,
        )

    # Keep the server composition root off metadata-only command paths.
    from free_claude_code.cli.commands import serve as run_server

    run_server()


def _print_version_if_requested(argv: Sequence[str] | None) -> bool:
    args = sys.argv[1:] if argv is None else argv
    if "--version" not in args:
        return False
    print(f"free-claude-code {package_version()}")
    return True
