"""Estimate model input separately from opaque protocol transport bytes.

The original payload is never modified. Text retains the conservative byte/2
estimate; each image reserves 16k tokens rather than tokenizing its base64 wire
representation. These are admission estimates, not provider tokenizer limits.
"""

import json

from free_claude_code.core.history_replay import (
    HistoryReplayError,
    decode_replay,
    is_replay,
    readable_reasoning,
)

IMAGE_TOKEN_RESERVE = 16384


def estimated_input_capacity(payload):
    images = 0

    def reasoning(block, key):
        value = block.get(key)
        readable = []
        if is_replay(value):
            try:
                readable = [
                    text for text, _ in readable_reasoning(decode_replay(value).native)
                ]
            except HistoryReplayError:
                # Malformed replay is still rejected by the adapter. Do not let
                # it reduce the estimate or bypass normal request validation.
                return block
        text = block.get("thinking")
        if not readable and not (key == "signature" and isinstance(text, str) and text):
            # Opaque-only state has no safe readable projection. Retain the
            # original estimate rather than treating unknown content as empty.
            return block
        result = {k: v for k, v in block.items() if k != key}
        extra = [part for part in readable if part != text]
        if extra:
            result["_capacity_replay_text"] = extra
        return result

    def content(value):
        nonlocal images
        if not isinstance(value, list):
            return value
        result = []
        for block in value:
            if not isinstance(block, dict):
                result.append(block)
                continue
            kind = block.get("type")
            if kind in {"image", "image_url", "input_image"}:
                images += 1
                result.append({"type": kind})
            elif kind == "thinking":
                result.append(reasoning(block, "signature"))
            elif kind == "redacted_thinking":
                result.append(reasoning(block, "data"))
            elif kind == "reasoning":
                result.append(reasoning(block, "encrypted_content"))
            elif kind == "tool_result":
                result.append({**block, "content": content(block.get("content"))})
            elif kind == "function_call_output":
                result.append({**block, "output": content(block.get("output"))})
            elif kind == "message" or block.get("role") in {
                "user",
                "assistant",
                "system",
                "developer",
                "tool",
            }:
                result.append({**block, "content": content(block.get("content"))})
            else:
                # Tool arguments, text, schemas, documents and unknown blocks
                # remain literal. Never interpret arbitrary user JSON as replay.
                result.append(block)
        return result

    projected = dict(payload)
    if isinstance(projected.get("messages"), list):
        projected["messages"] = [
            {**message, "content": content(message.get("content"))}
            if isinstance(message, dict)
            else message
            for message in projected["messages"]
        ]
    for key in ("input", "system"):
        if key in projected:
            projected[key] = content(projected[key])
    raw = json.dumps(projected, ensure_ascii=False, default=str)
    return len(raw.encode("utf-8")) // 2 + images * IMAGE_TOKEN_RESERVE, bool(images)
