"""Offline behavior checks for the no-tools protocol; no game or API key needed."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from providers import Reply, ToolCall, make_provider, parse_model_spec, recover_arguments
from spire_agent import prompt_fingerprint, run_once
from system1 import parse_commands, text_prompt_fingerprint

COMBAT = "COMBAT turn 1 | HP 80/80 Block 0 | Energy 3/3\nCommands: play <hand#> [e#] | end | potion <#> [e#] | drop <#>"


def reply(text, reason="end_turn", calls=None):
    return Reply([text], [], calls or [], reason, None,
                 {"input": 20, "output": 5, "cache_read": 0, "cache_write": 0},
                 "mock", {"role": "assistant", "content": text})


class FakeProvider:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, system, messages, effort, max_tokens):
        self.requests.append((system, messages, effort, max_tokens))
        return next(self.replies)

    def describe_text(self, effort):
        return {"provider": "local", "tools_format": None}


class FakeBridge:
    def __init__(self, win_after=1, reject=None):
        self.commands = []
        self.win_after = win_after
        self.reject = reject
        self.successes = 0

    def state(self):
        return "GAME_OVER" if self.successes >= self.win_after else COMBAT

    def info(self, query):
        if query != "run":
            raise AssertionError("Text protocol must not perform model-directed lookups")
        return "Act 1 Floor 1"

    def act(self, commands):
        self.commands.append(commands)
        if commands == self.reject:
            return f"! {commands}: invalid target\n{COMBAT}"
        self.successes += 1
        return self.state()

    def snapshot(self):
        return {"floor": 1, "act": 1, "hp": 80, "max_hp": 80, "character": "ironclad",
                "seed": "TEST01", "ascension": 0, "victory": self.successes >= self.win_after}


class TextProtocolTests(unittest.TestCase):
    def run_fake(self, responses, bridge=None, **limits):
        bridge = bridge or FakeBridge()
        provider = FakeProvider(responses)
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "run.jsonl"
            end = run_once(bridge, "local:mock@text", None, recording, provider=provider,
                           verbose=False, use_playbook=False, **limits)
            events = [json.loads(line) for line in recording.read_text(encoding="utf-8").splitlines()]
        return end, events, bridge, provider

    def test_model_specs_keep_tools_default_and_support_text(self):
        self.assertEqual(parse_model_spec("local:mock").protocol, "tools")
        self.assertEqual(parse_model_spec("openrouter:test@default@text").protocol, "text")
        self.assertEqual(parse_model_spec("openrouter:test@text@low").effort, "low")
        for spec in ("local:mock@text@tools", "local:mock@low@high", "local:mock@text@low@high"):
            with self.assertRaises(ValueError):
                parse_model_spec(spec)

    def test_text_request_has_no_tool_parameters(self):
        provider = make_provider(parse_model_spec("local:mock@text"))
        params = provider.build_text_request("system", [{"role": "user", "content": "state"}], "default", 1024)
        self.assertNotIn("tools", params)
        self.assertNotIn("tool_choice", params)
        self.assertNotIn("parallel_tool_calls", params)
        self.assertEqual(params["messages"][0]["role"], "system")

    def test_tool_argument_recovery_is_preserved(self):
        self.assertEqual(recover_arguments('{"commands":"end"}{"commands":"wait"}')[0], {"commands": "end"})
        self.assertEqual(recover_arguments('"{\\"commands\\":\\"end\\"}"')[0], {"commands": "end"})

    def test_parser_fences_bullets_and_inline_actions(self):
        for text in ("```text\nplay 0 e0; end\n```", "- play 0 e0\n- end", "My action: `play 0 e0; end`"):
            self.assertEqual(parse_commands(text, COMBAT), ("play 0 e0; end", None))
        self.assertEqual(parse_commands("<think>end</think>\nplay 0 e0", COMBAT)[0], "play 0 e0")

    def test_parser_rejects_forbidden_and_wrong_screen_commands(self):
        for text in ("quit", "abandon", "info deck", "new ironclad", "choose 0", ""):
            self.assertIsNone(parse_commands(text, COMBAT)[0])

    def test_fingerprint_separates_protocols(self):
        self.assertNotEqual(prompt_fingerprint("ironclad"), text_prompt_fingerprint("ironclad"))

    def test_stateless_requests_and_recording(self):
        end, events, bridge, provider = self.run_fake([reply("end"), reply("end")], FakeBridge(win_after=2))
        self.assertEqual(end["outcome"], "victory")
        self.assertEqual(end["decisions"], 2)
        self.assertTrue(all(len(request[1]) == 1 for request in provider.requests))
        self.assertEqual(events[0]["protocol"], "text")
        self.assertIn("stateless", events[0]["context_policy"])
        self.assertEqual(events[0]["max_output_tokens"], 1024)

    def test_retry_then_fallback_is_recorded(self):
        end, events, bridge, provider = self.run_fake([reply("nonsense"), reply("still nonsense")])
        self.assertEqual(bridge.commands, ["end"])
        self.assertEqual((end["invalid_replies"], end["retries"], end["fallbacks"]), (2, 1, 1))
        self.assertIn("no usable commands", provider.requests[1][1][0]["content"])
        steps = [e for e in events if e["type"] == "step"]
        self.assertTrue(steps[-1]["calls"][0]["fallback"])

    def test_bridge_error_retries_current_state(self):
        end, events, bridge, provider = self.run_fake([reply("play 9 e0"), reply("end")], FakeBridge(reject="play 9 e0"))
        self.assertEqual(bridge.commands, ["play 9 e0", "end"])
        self.assertIn("invalid target", provider.requests[1][1][0]["content"])
        self.assertEqual(end["fallbacks"], 0)

    def test_truncated_reply_does_not_execute_prefix(self):
        end, events, bridge, provider = self.run_fake([reply("play 0 e0", "max_tokens"), reply("end")])
        self.assertEqual(bridge.commands, ["end"])
        self.assertEqual(end["invalid_replies"], 1)

    def test_unexpected_tool_call_does_not_execute_text(self):
        end, events, bridge, provider = self.run_fake([
            reply("play 0 e0", calls=[ToolCall("c", "act", {"commands": "play 0 e0"})]), reply("end")])
        self.assertEqual(bridge.commands, ["end"])
        self.assertEqual(end["invalid_replies"], 1)

    def test_request_cap_applies_before_retry(self):
        end, events, bridge, provider = self.run_fake([reply("nonsense")], max_requests=1)
        self.assertEqual(end["outcome"], "request_limit")
        self.assertEqual(bridge.commands, [])
        self.assertEqual(len(provider.requests), 1)

    def test_cost_cap_applies_before_retry(self):
        end, events, bridge, provider = self.run_fake([reply("nonsense")], max_cost=0.00001,
                                                     prices={"local:mock": (1, 1, 1, 1)})
        self.assertEqual(end["outcome"], "cost_limit")
        self.assertEqual(len(provider.requests), 1)

    def test_policy_runs_without_model_requests(self):
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            end = run_once(bridge, "policy:greedy", None, Path(directory) / "policy.jsonl", verbose=False)
        self.assertEqual(end["outcome"], "victory")
        self.assertEqual(end["cost"], 0)
        self.assertEqual(end["usage"]["input"], 0)

    def test_refusal_is_charged_and_recorded(self):
        end, events, bridge, provider = self.run_fake([reply("no", "refusal")],
                                                     prices={"local:mock": (100, 100, 100, 100)})
        step = next(event for event in events if event["type"] == "step")
        self.assertEqual(end["outcome"], "refusal")
        self.assertGreater(step["cost"], 0)
        self.assertEqual(step["cost"], end["cost"])


if __name__ == "__main__":
    unittest.main()
