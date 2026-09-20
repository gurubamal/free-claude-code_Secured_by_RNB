"""Configure native Claude without copying provider credentials into its settings."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def configure():
    import json5

    from free_claude_code.config.loader import get_settings
    from free_claude_code.config.paths import config_dir_path
    from free_claude_code.config.server_urls import local_proxy_root_url
    from free_claude_code.core.private_storage import atomic_write_private_text
    from free_claude_code.harnesses.claude import claude_proxy_values
    from free_claude_code.harnesses.config_file import atomic_write_text

    settings = get_settings()
    target = Path.home() / ".claude" / "settings.json"
    before = target.read_text(encoding="utf-8-sig") if target.exists() else "{}"
    document = json5.loads(before)
    if not isinstance(document, dict):
        raise ValueError("Claude settings must be a JSON object")
    backup = (
        config_dir_path()
        / "backups"
        / (
            "claude-settings-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            + ".dpapi"
        )
    )
    atomic_write_private_text(backup, before)

    # PowerShell single quotes escape paths without executing their characters.
    def quote(text):
        return "'" + str(text).replace("'", "''") + "'"

    helper = (
        'powershell.exe -NoProfile -NonInteractive -Command "& '
        + quote(sys.executable)
        + " "
        + quote(ROOT / "hardened.py")
        + ' credential-helper"'
    )
    values = claude_proxy_values(local_proxy_root_url(settings), "")
    values.update(
        {
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    document["env"] = {**document.get("env", {}), **values}
    document["apiKeyHelper"] = helper
    document["model"] = "open_router/openrouter/free"
    atomic_write_text(target, json.dumps(document, indent=2) + "\n")
    # A launcher profile with hooks off is also usable without global/project settings.
    profile = {
        "env": values,
        "apiKeyHelper": helper,
        "model": document["model"],
        "disableAllHooks": True,
        "permissions": {"defaultMode": "default"},
    }
    profile_path = Path.home() / ".claude" / "fcc-hardened.settings.json"
    atomic_write_text(profile_path, json.dumps(profile, indent=2) + "\n")
    print(
        json.dumps(
            {
                "claude_configured": True,
                "provider_key_in_claude_settings": False,
                "base_url": local_proxy_root_url(settings),
                "profile": str(profile_path),
                "encrypted_backup": str(backup),
            }
        )
    )


if __name__ == "__main__":
    configure()
