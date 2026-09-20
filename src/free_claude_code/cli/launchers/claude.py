"""Installed Claude launcher: native command handling with an FCC connection."""

import json
from collections.abc import Sequence

from free_claude_code.harnesses.claude import CLAUDE_BINARY_NAME, build_claude_proxy_env
from free_claude_code.harnesses.launch import PreparedLaunch
from free_claude_code.harnesses.resources import LaunchResources

from .runner import HarnessSpec, LaunchContext, launch_harness


def _configure(
    ctx: LaunchContext, args: list[str], _files: LaunchResources
) -> PreparedLaunch:
    blocked = {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--permission-mode",
        "--settings",
        "--setting-sources",
        "--mcp-config",
        "--plugin-dir",
    }
    if any(arg.split("=", 1)[0] in blocked for arg in args):
        raise ValueError(
            "This launcher owns its permission, settings and MCP boundaries"
        )
    env = build_claude_proxy_env(
        proxy_root_url=ctx.proxy_root_url,
        auth_token=ctx.auth_token,
        base_env=ctx.base_env,
    )
    effort = ctx.settings.reasoning_policy.value
    if effort in {"low", "medium", "high", "xhigh", "max"}:
        # The environment form persists max for each launched session, including
        # its subagents. Claude's model/organization capability caps still apply.
        env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    return PreparedLaunch(
        [
            ctx.binary_path,
            *args,
            "--permission-mode",
            "default",
            "--setting-sources",
            "",
            "--settings",
            json.dumps({"disableAllHooks": True}),
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        ],
        env,
    )


SPEC = HarnessSpec(
    binary_name=CLAUDE_BINARY_NAME,
    display_name="Claude Code",
    install_hint="Install Claude Code with: npm install -g @anthropic-ai/claude-code",
    configure=_configure,
)


def launch(argv: Sequence[str] | None = None) -> None:
    launch_harness(SPEC, argv)
