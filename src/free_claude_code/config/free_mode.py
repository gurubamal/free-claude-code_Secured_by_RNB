"""One automatic free-only route, with no client-controlled paid fallback."""

from copy import deepcopy

FREE_MODEL = "openrouter/free"
FREE_MODEL_REF = "open_router/" + FREE_MODEL


def free_request_body(body: dict) -> dict:
    result = deepcopy(body)
    result["model"] = FREE_MODEL
    # Do not allow paid plugins, alternate models or caller routing overrides.
    result.pop("extra_body", None)
    for key in (
        "models",
        "route",
        "plugins",
        "web_search_options",
        "transforms",
        "preset",
    ):
        result.pop(key, None)
    result["provider"] = {
        "allow_fallbacks": True,
        "require_parameters": True,
        "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
    }
    # Huge harness defaults can rule out every available free model.
    maximum = result.pop("max_completion_tokens", result.get("max_tokens", 8192))
    result["max_tokens"] = min(
        maximum if isinstance(maximum, int) and maximum > 0 else 8192, 8192
    )
    return result
