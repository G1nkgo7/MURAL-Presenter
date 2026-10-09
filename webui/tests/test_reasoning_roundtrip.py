from __future__ import annotations

import sys
from pathlib import Path


WEBUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WEBUI / "inference"))

from openai_backend import _resp_to_blocks, to_openai_messages


def test_openai_reasoning_survives_a_tool_round_trip() -> None:
    blocks = _resp_to_blocks({
        "content": "",
        "reasoning_content": "The vision reader found a green 3.2x label.",
        "tool_calls": [{
            "id": "call-edit-1",
            "type": "function",
            "function": {
                "name": "edit",
                "arguments": '{"path":"slide_08.html"}',
            },
        }],
    })
    durable = [{
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": blocks[0].thinking,
            },
            {
                "type": "tool_use",
                "id": blocks[1].id,
                "name": blocks[1].name,
                "input": blocks[1].input,
            },
        ],
    }]

    assert to_openai_messages("", durable) == [{
        "role": "assistant",
        "content": None,
        "reasoning_content": "The vision reader found a green 3.2x label.",
        "tool_calls": [{
            "id": "call-edit-1",
            "type": "function",
            "function": {
                "name": "edit",
                "arguments": '{"path": "slide_08.html"}',
            },
        }],
    }]
