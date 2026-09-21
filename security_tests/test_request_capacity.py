"""Large encoded history must not make a small model input wait for quota."""

import json
from copy import deepcopy

import pytest
from free_helpers import freeze_pool

from free_claude_code.application.free_pool import (
    AutomaticFreePool,
    FreeModel,
    request_needs,
)
from free_claude_code.application.request_capacity import IMAGE_TOKEN_RESERVE
from free_claude_code.config.settings import Settings
from free_claude_code.core.history_replay import (
    ReplayOrigin,
    ReplayRecord,
    encode_replay,
)


def signed_block(text="Keep this reasoning", signature_size=600000):
    native = {"type": "thinking", "thinking": text, "signature": "s" * signature_size}
    carrier = encode_replay(
        ReplayRecord(ReplayOrigin("synthetic", "messages", "", "", "synthetic"), native)
    )
    return {"type": "thinking", "thinking": text, "signature": carrier}


@pytest.mark.asyncio
async def test_signed_history_and_tool_screenshot_reach_272k_route_unchanged():
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "x" * 350000},
    }
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    signed_block(),
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "screenshot",
                        "input": {},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call", "content": [image]}
                ],
            },
        ],
        "max_tokens": 8192,
    }
    before = deepcopy(payload)
    assert len(json.dumps(payload).encode()) // 2 > 512000
    estimate, _, vision = request_needs(payload)
    assert IMAGE_TOKEN_RESERVE < estimate < 256000 and vision
    model = FreeModel(
        "openai", "subscription", 272000, 8192, True, True, "subscription"
    )
    pool = freeze_pool(AutomaticFreePool(), [model])
    assert await pool.select(Settings(allow_subscription_models=True), payload) == (
        model,
    )
    assert payload == before


@pytest.mark.parametrize(
    "key,wrapper",
    [
        (
            "messages",
            lambda b: [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "id", "content": [b]}
                    ],
                }
            ],
        ),
        ("messages", lambda b: [{"role": "user", "content": [b]}]),
        (
            "input",
            lambda b: [
                {"type": "function_call_output", "call_id": "id", "output": [b]}
            ],
        ),
        ("input", lambda b: [{"role": "user", "content": [b]}]),
    ],
)
@pytest.mark.parametrize("kind", ["image", "image_url", "input_image"])
def test_image_budget_depends_on_image_count_not_wire_bytes(key, wrapper, kind):
    small = {key: wrapper({"type": kind, "source": {"data": "x"}})}
    large = {key: wrapper({"type": kind, "source": {"data": "x" * 500000}})}
    assert request_needs(small) == request_needs(large)
    assert request_needs(large)[2]


def test_replay_only_readable_text_counts_and_malformed_or_opaque_state_stays_bounded():
    block = signed_block(text="r" * 100000)
    block["thinking"] = ""
    need = request_needs({"messages": [{"role": "assistant", "content": [block]}]})[0]
    assert 50000 < need < 100000
    for signature in ["fcc:history:v1:invalid" + "x" * 600000, "opaque" * 100000]:
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "", "signature": signature}
                    ],
                }
            ]
        }
        assert request_needs(payload)[0] > 272000


@pytest.mark.parametrize("field", ["input", "input_schema"])
def test_tool_arguments_and_schemas_are_never_interpreted_as_transport(field):
    literal = {"type": "thinking", "thinking": "text", "signature": "x" * 600000}
    if field == "input":
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "id",
                            "name": "echo",
                            "input": literal,
                        }
                    ],
                }
            ]
        }
    else:
        payload = {"tools": [{"name": "echo", "input_schema": literal}]}
    assert request_needs(payload)[0] > 272000


def test_each_image_reserves_capacity_and_text_that_looks_like_image_is_literal():
    image = {
        "type": "image_url",
        "image_url": {"url": "https://example.test/image.png"},
    }
    one = {"messages": [{"role": "user", "content": [image]}]}
    two = {"messages": [{"role": "user", "content": [image, image]}]}
    assert request_needs(two)[0] - request_needs(one)[0] >= IMAGE_TOKEN_RESERVE
    literal = {"messages": [{"role": "user", "content": json.dumps(image)}]}
    assert not request_needs(literal)[2]
