"""Supported local entry point. Never import the parent shell's credentials."""

import argparse
import getpass
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def ensure_server():
    import json
    from urllib.error import URLError
    from urllib.request import ProxyHandler, Request, build_opener

    from free_claude_code.config.loader import get_settings
    from free_claude_code.config.paths import config_dir_path
    from free_claude_code.config.server_urls import local_proxy_root_url
    from free_claude_code.core.interprocess_lock import InterprocessFileLock

    settings = get_settings()
    opener = build_opener(ProxyHandler({}))

    def ready():
        try:
            with opener.open(
                Request(
                    local_proxy_root_url(settings) + "/",
                    headers={"Authorization": "Bearer " + settings.proxy_auth_token},
                ),
                timeout=2,
            ) as response:
                return json.loads(response.read()).get("status") == "ok"
        except URLError, OSError, ValueError:
            return False

    if ready():
        return
    lock = InterprocessFileLock(config_dir_path() / "server-start.lock")
    if not lock.acquire(wait=True, timeout=35):
        raise OSError("Another launcher is starting the proxy. Retry shortly.")
    try:
        if ready():
            return
        subprocess.Popen(
            [sys.executable, str(ROOT / "hardened.py"), "serve"],
            cwd=ROOT,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for _ in range(50):
            if ready():
                return
            time.sleep(0.2)
        raise OSError(
            "Proxy did not start. Run Run-Hardened.ps1 serve to see the startup error."
        )
    finally:
        lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description="FCC Hardened: local coding proxy")
    parser.add_argument(
        "command",
        choices=(
            "serve",
            "reset-password",
            "show-initial-password",
            "credential-helper",
            "ensure-server",
            "claude",
            "codex",
        ),
        nargs="?",
        default="serve",
    )
    args, rest = parser.parse_known_args(argv)
    from free_claude_code.harnesses.environment import client_environment

    clean = client_environment(dict(os.environ), proxy_root_url="http://127.0.0.1")
    os.environ.clear()
    os.environ.update(clean)
    os.environ["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    if args.command == "credential-helper":
        from free_claude_code.config.loader import get_settings

        ensure_server()
        print(get_settings().proxy_auth_token)
        return 0
    if args.command == "ensure-server":
        ensure_server()
        print("Local proxy is ready.")
        return 0
    if args.command == "show-initial-password":
        from free_claude_code.core.admin_accounts import AdminAccounts

        try:
            print("Username: admin")
            print("Temporary password: " + AdminAccounts().initial_password())
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    if args.command == "reset-password":
        if rest:
            parser.error(
                "Passwords are accepted through hidden prompts, never command arguments"
            )
        from free_claude_code.core.admin_accounts import AdminAccounts

        try:
            password = getpass.getpass(
                "New password for admin (at least 14 characters): "
            )
            confirmation = getpass.getpass("Confirm new password: ")
            if password != confirmation:
                raise ValueError("Passwords did not match. Nothing changed.")
            AdminAccounts().reset_password(password)
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 1
        finally:
            password = confirmation = ""
        print(
            "Password reset for admin. Existing sessions are invalid. Provider keys were preserved."
        )
        return 0
    if args.command == "serve":
        if rest:
            parser.error(
                "The server takes no additional arguments; configure it in Admin"
            )
        from free_claude_code.cli.entrypoints import serve

        serve([])
    else:
        from importlib import import_module

        ensure_server()
        import_module(f"free_claude_code.cli.launchers.{args.command}").launch(rest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
