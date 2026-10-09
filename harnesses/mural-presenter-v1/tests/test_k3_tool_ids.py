from __future__ import annotations

import copy
from dataclasses import replace
import unittest

from runtime.agent import AgentProtocolError, AgentRuntime, _canonicalize_tool_use_ids
from runtime.config import ProviderSettings
from runtime.recovery import tool_boundary
from runtime.session import NormalizedAssistantResponse, ProviderChannel, TransportResponse, to_openai_messages
from runtime.tools import ToolContext, ToolDefinition, ToolRuntime


def response(*blocks):
    return NormalizedAssistantResponse(tuple(blocks), (), "tool_use" if any(
        block.get("type") == "tool_use" for block in blocks) else "end_turn", {})


def call(path, tool_id="read_file_0"):
    return {"type": "tool_use", "id": tool_id, "name": "read_file", "input": {"path": path}}


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, _channel, request, *, total_timeout_s):
        self.requests.append(copy.deepcopy(request))
        return TransportResponse(self.responses.pop(0), first_byte_s=0.01)


class K3ToolIdsTest(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.tools = ToolRuntime()
        self.tools.register(ToolDefinition("read_file", "read", {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"], "additionalProperties": False,
        }, lambda context, args: self.executed.append((context.owner_id, args["path"])) or "Evidence.",
            roles=frozenset({"slide"})))
        self.settings = ProviderSettings(api_format="openai_chat_compatible", model="kimi-k3",
            base_url="https://provider.test", max_attempts=1,
            total_timeout_s=30, streaming=False, prompt_cache=False, channel_cooldown_s=0)

    def run_agent(self, transport, owner="owner-one", checkpoint=None, settings=None):
        return AgentRuntime(settings=settings or self.settings,
            channels=(ProviderChannel("fixture-key", "fixture"),), transport=transport,
            tools=self.tools).run(agent_id=owner, role="slide", system="Work.",
                tool_context=ToolContext(owner, "slide"), checkpoint=checkpoint,
                initial_messages=() if checkpoint else ({"role": "user", "content": "Start."},),
                max_turns=8, max_tokens=128, thinking=True, effort="high")

    def test_cross_turn_reuse_preserves_reasoning_arguments_and_pairing(self):
        thinking = {"type": "thinking", "thinking": "private returned reasoning", "signature": None}
        original = response(thinking, call("first.md"))
        transport = Transport([original, response(call("second.md")),
                               response({"type": "text", "text": "Done."})])
        result = self.run_agent(transport)
        self.assertEqual(self.executed, [("owner-one", "first.md"), ("owner-one", "second.md")])
        self.assertEqual(original.content[1]["id"], "read_file_0")
        wire = to_openai_messages("Work.", result.checkpoint.messages, preserve_structured=True)
        ids = [item["id"] for message in wire for item in message.get("tool_calls", [])]
        results = [message["tool_call_id"] for message in wire if message["role"] == "tool"]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(ids, results)
        first_assistant = next(message for message in wire if message["role"] == "assistant")
        self.assertEqual(first_assistant["reasoning_content"], thinking["thinking"])
        self.assertTrue(tool_boundary(result.checkpoint.messages).is_closed)

    def test_fresh_owners_are_isolated_even_with_empty_shared_id_snapshots(self):
        a, b = [call("same.md")], [call("same.md")]
        _canonicalize_tool_use_ids(a, set(), namespace="segment-a:1", namespace_provider_ids=True)
        _canonicalize_tool_use_ids(b, set(), namespace="segment-b:1", namespace_provider_ids=True)
        self.assertNotEqual(a[0]["id"], b[0]["id"])
        for owner in ("owner-one", "owner-two"):
            self.run_agent(Transport([response(call("same.md")),
                                      response({"type": "text", "text": "Done."})]), owner=owner)
        self.assertEqual(len(self.executed), 2)

    def test_resume_does_not_replay_completed_tools(self):
        previous = self.run_agent(Transport([response(call("first.md")),
                                            response({"type": "text", "text": "Done."})]))
        resumed = self.run_agent(Transport([response(call("second.md")),
                                           response({"type": "text", "text": "Done."})]),
                                 checkpoint=previous.checkpoint)
        self.assertEqual([path for _, path in self.executed], ["first.md", "second.md"])
        self.assertTrue(tool_boundary(resumed.checkpoint.messages).is_closed)

    def test_legacy_short_ids_resume_without_rewriting_or_replaying_history(self):
        previous = self.run_agent(Transport([response(call("first.md")),
                                            response({"type": "text", "text": "Done."})]),
                                  settings=replace(self.settings, model="fixture-legacy"))
        legacy_messages = copy.deepcopy(previous.checkpoint.messages)
        transport = Transport([response(call("second.md")),
                               response({"type": "text", "text": "Done."})])
        resumed = self.run_agent(transport, checkpoint=previous.checkpoint)
        self.assertEqual(previous.checkpoint.messages, legacy_messages)
        self.assertEqual([path for _, path in self.executed], ["first.md", "second.md"])
        wire = to_openai_messages("Work.", resumed.checkpoint.messages, preserve_structured=True)
        ids = [item["id"] for message in wire for item in message.get("tool_calls", [])]
        self.assertEqual(ids[0], "read_file_0")
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(ids, [message["tool_call_id"] for message in wire if message["role"] == "tool"])
        self.assertTrue(tool_boundary(resumed.checkpoint.messages).is_closed)

    def test_same_response_duplicates_still_fail_before_execution(self):
        with self.assertRaisesRegex(AgentProtocolError, "duplicate tool_use_id"):
            self.run_agent(Transport([response(call("first.md"), call("second.md"))]))
        self.assertEqual(self.executed, [])

    def test_other_providers_keep_strict_ids(self):
        native = replace(self.settings, api_format="anthropic", model="model-test")
        with self.assertRaisesRegex(AgentProtocolError, "reused prior tool_use_id"):
            self.run_agent(Transport([response(call("first.md")), response(call("second.md"))]),
                           settings=native)
        self.assertEqual(len(self.executed), 1)


if __name__ == "__main__":
    unittest.main()
