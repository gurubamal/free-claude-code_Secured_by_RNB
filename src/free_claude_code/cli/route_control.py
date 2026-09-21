"""Local route controls. Credentials stay in memory, outside arguments/output."""

import argparse
import sys

import httpx

from free_claude_code.config.loader import ManagedConfigStore
from free_claude_code.config.server_urls import local_proxy_root_url


def display(value):
    return "".join(c for c in str(value) if c.isprintable())


def print_status(data):
    if data.get("automatic") is False:
        print(
            "Automatic routing is disabled. Run route auto or route use to enable these controls."
        )
        return
    selection = data.get("selection", {})
    if selection.get("mode") == "selected":
        print(
            "First preference: "
            + display(selection.get("provider"))
            + " / "
            + display(selection.get("model") or "best eligible model")
            + " ("
            + display(selection.get("billing"))
            + ")"
        )
        print(
            "Selected routes currently available: "
            + str(selection.get("available_models", 0))
        )
    else:
        print("Automatic selection")
    print(
        "Automatic fallback: always enabled. Required context: more than 512,000 tokens."
    )
    print(
        "Billing order: " + " > ".join(map(display, data.get("billing_priority", [])))
    )
    last = data.get("last_success_details")
    recovery = data.get("recovery")
    if recovery:
        print(
            "Automatic recovery: "
            + display(recovery.get("waiting_requests", 0))
            + " waiting; connected requests wait up to "
            + display(recovery.get("max_wait_seconds", 0))
            + " seconds before returning a terminal error."
        )
        print(
            "Automatic retry check interval: "
            + display(recovery.get("check_seconds", 15))
            + " seconds; provider cooldowns still apply."
        )
        print(
            "Provider failure limit: "
            + display(recovery.get("provider_failure_limit", 3))
            + " per request, including recovery retries; failures rotate to the next eligible provider."
        )
    if last:
        print(
            "Last successful route: "
            + display(last.get("provider"))
            + " / "
            + display(last.get("model"))
            + " ("
            + display(last.get("billing"))
            + ")"
        )
    print(
        "Scope: new requests from all clients using this gateway; existing output is not replayed."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="Run-Hardened.ps1 route",
        description="Choose a first route; automatic fallback always stays enabled.",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status", help="Show saved selection and actual last route")
    listing = commands.add_parser(
        "list", help="List discovered eligible models (free by default)"
    )
    listing.add_argument("--provider")
    listing.add_argument(
        "--billing", choices=("free", "subscription", "paid_api", "all"), default="free"
    )
    use = commands.add_parser(
        "use", help="Prefer a provider and optionally an exact model"
    )
    use.add_argument("provider")
    use.add_argument("model", nargs="?")
    use.add_argument(
        "--billing", choices=("free", "subscription", "paid_api"), default="free"
    )
    commands.add_parser(
        "auto", help="Clear manual preference and restore automatic ordering"
    )
    args = parser.parse_args(argv)
    try:
        settings = ManagedConfigStore().read(env={}).settings
        headers = {
            "Authorization": "Bearer " + settings.proxy_auth_token,
            "X-FCC-Route-Control": "1",
        }
        with httpx.Client(
            base_url=local_proxy_root_url(settings),
            headers=headers,
            timeout=90,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            if args.action in {"auto", "use"}:
                body = (
                    {"mode": "automatic"}
                    if args.action == "auto"
                    else {
                        "mode": "selected",
                        "provider": args.provider,
                        "model": args.model,
                        "billing": args.billing,
                    }
                )
                response = client.post("/v1/routing/selection", json=body)
            else:
                response = client.get("/v1/free/status")
            data = response.json()
            if not response.is_success:
                detail = data.get("detail")
                print(
                    display(detail)
                    if isinstance(detail, str)
                    else "Route command rejected. Check the provider/model ID and Admin settings.",
                    file=sys.stderr,
                )
                return 1
        if args.action == "list":
            found = 0
            print("PROVIDER | MODEL | BILLING | CONTEXT | AVAILABILITY")
            print("Catalog checked: " + display(data.get("refreshed_at", "unknown")))
            for provider in data.get("providers", []):
                if args.provider and provider["provider"] != args.provider:
                    continue
                for model in provider.get("model_details", []):
                    if args.billing != "all" and model["billing"] != args.billing:
                        continue
                    print(
                        " | ".join(
                            map(
                                display,
                                [
                                    provider["provider"],
                                    model["id"],
                                    model["billing"],
                                    model["context_tokens"],
                                    "available"
                                    if model.get("available")
                                    else model.get("cooldown_reason")
                                    or "excluded/unavailable",
                                ],
                            )
                        )
                    )
                    found += 1
            if not found:
                print(
                    "No eligible models match. Open Admin > Routing controls for credentials, free-account confirmation and catalog status."
                )
            print(
                "Use exact IDs: Run-Hardened.ps1 route use PROVIDER MODEL (free by default). Catalog eligibility does not guarantee inference."
            )
        else:
            print_status(data)
        return 0
    except httpx.HTTPError, OSError, ValueError, KeyError:
        # Transport exceptions can contain request headers; never print them.
        print(
            "Routing command failed. Check the local gateway and try again; selection was not confirmed.",
            file=sys.stderr,
        )
        return 1
