"""Offline regressions for spend validation, repeated invalid-action stopping and lookup-loop stopping."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from bridge import BridgeError, game_error_lines
from providers import Reply, ToolCall, parse_model_spec
from spire_agent import (HARNESS_VERSION, INVALID_ACTION_LIMIT, LOOKUP_CALL_LIMIT, LOOKUP_GRACE_REQUESTS,
                         LOOKUP_REQUEST_LIMIT, InvalidActionWatchdog, LookupGuard, load_prices, price_for,
                         prompt_fingerprint, run_once)

STATE = "COMBAT turn 1 | HP 80/80 Block 0 | Energy 0/3\nHand:\n0 Strike (1) Attack\nCommands: play <hand#> [e#] | end"
ERROR = "! play 0: cannot play Strike: not enough energy\n" + STATE


def act_call(result=ERROR, is_error=False):
    return {"tool": "act", "result": result, "is_error": is_error}


class PriceGuardTests(unittest.TestCase):
    def test_rejects_invalid_numbers_in_all_price_fields(self):
        for bad in (-1, float("nan"), float("inf"), -float("inf"), True):
            for index in range(4):
                row = [1, 2, 3, 4]
                row[index] = bad
                with self.subTest(bad=bad, index=index), self.assertRaises(ValueError):
                    load_prices(json.dumps({"model": row}))
            with self.assertRaises(ValueError):
                load_prices(json.dumps({"model": {"input": 1, "output": bad}}))

    def test_dynamic_price_cannot_bypass_loader(self):
        with self.assertRaisesRegex(ValueError, "finite and nonnegative"):
            price_for(parse_model_spec("openrouter:typesafe/jev-router"),
                      {"typesafe/jev-router": [-1, -1, -1, -1]})

    def test_zero_and_default_cache_prices(self):
        self.assertEqual(load_prices('{"free":[0,0],"paid":{"input":1,"output":2}}'),
                         {"free": (0, 0, 0, 0), "paid": (1, 2, 1, 1)})

    def test_bad_shapes_fail_clearly(self):
        for value in (None, 1, "1234", [], [1], [1, 2, 3, 4, 5], {"input": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_prices(json.dumps({"model": value}))


class WatchdogTests(unittest.TestCase):
    def test_repeated_game_rejections_stop_at_limit(self):
        guard = InvalidActionWatchdog(STATE)
        for i in range(INVALID_ACTION_LIMIT):
            self.assertEqual(guard.observe([act_call()]), i == INVALID_ACTION_LIMIT - 1)
        self.assertEqual(guard.detail()["requests"], INVALID_ACTION_LIMIT)

    def test_many_calls_in_one_reply_count_once(self):
        guard = InvalidActionWatchdog(STATE)
        guard.observe([act_call()] * 20)
        self.assertEqual(guard.streak, 1)

    def test_lookups_and_notes_are_neutral(self):
        guard = InvalidActionWatchdog(STATE)
        guard.observe([act_call()])
        for _ in range(30):
            guard.observe([{"tool": "info", "result": "boss", "is_error": False},
                           {"tool": "set_notes", "result": "saved", "is_error": False}])
        self.assertEqual(guard.streak, 1)

    def test_successful_action_and_state_change_reset(self):
        for result in (STATE, ERROR.replace("turn 1", "turn 2")):
            guard = InvalidActionWatchdog(STATE)
            guard.observe([act_call()])
            guard.observe([act_call(result)])
            self.assertEqual(guard.streak, 0)

    def test_normal_batch_stops_are_not_errors(self):
        result = "! stopped after 'play 0' (new cards drawn); skipped 1 later command(s)\n" + STATE
        self.assertEqual(game_error_lines(result), [])
        guard = InvalidActionWatchdog(STATE)
        guard.observe([act_call()])
        guard.observe([act_call(result)])
        self.assertEqual(guard.streak, 0)
        self.assertEqual(game_error_lines("! skipped 2 later command(s)\n" + ERROR),
                         ["! play 0: cannot play Strike: not enough energy"])

    def test_bridge_errors_do_not_count_as_bad_play(self):
        for error in ("Bridge error: unavailable", "! could not render COMBAT: failure\n" + STATE,
                      "! info 'run' failed: failure\n" + STATE):
            guard = InvalidActionWatchdog(STATE)
            guard.observe([act_call()])
            guard.observe([act_call(error, True)])
            self.assertEqual(guard.streak, 0)

    def test_rejected_json_without_game_call_counts(self):
        guard = InvalidActionWatchdog(STATE)
        guard.observe([act_call("Arguments must be an object", True)])
        self.assertEqual(guard.streak, 1)

    def test_unknown_state_does_not_trigger(self):
        guard = InvalidActionWatchdog("unrecognized")
        for _ in range(20):
            self.assertFalse(guard.observe([act_call("bad arguments", True)]))

    def test_runtime_flags_errors_and_records_auditable_stop(self):
        class Bridge:
            def state(self): return STATE
            def snapshot(self): return {"character": "ironclad", "floor": 1, "act": 1, "hp": 80}
            def info(self, query): return "Act 1 Floor 1"
            def act(self, commands): return ERROR

        class Provider:
            def __init__(self): self.errors = []
            def describe(self, tools, effort): return {"provider": "mock"}
            def request(self, *args):
                return Reply([], [], [ToolCall("c", "act", {"commands": "play 0"})],
                             "tool_use", None, {"input": 1, "output": 1}, "mock", {})
            def tool_result_messages(self, results):
                self.errors.extend(error for _, _, error in results)
                return [{"role": "tool", "content": result} for _, result, _ in results]

        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            end = run_once(Bridge(), "local:mock", None, path, provider=provider, verbose=False)
            events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(end["outcome"], "stalled")
        self.assertEqual(end["usage"]["requests"], INVALID_ACTION_LIMIT)
        self.assertEqual(end["stop_detail"]["reason"], "repeated_invalid_actions_unchanged_state")
        self.assertTrue(all(provider.errors))
        self.assertEqual(len([e for e in events if e["type"] == "watchdog"]), 1)
        self.assertEqual(events[0]["watchdog"]["request_limit"], INVALID_ACTION_LIMIT)


def lookup(guard, query, tool="info", state=STATE):
    """One non-act call through the guard, as Session.play makes it: (refused kind or None)."""
    tool_input = {"query": query} if tool == "info" else {"notes": query}
    refusal = guard.check(tool, tool_input, state)
    if refusal is None:
        guard.ran(tool, tool_input, False)
        return None
    return refusal[0]


class LookupGuardTests(unittest.TestCase):
    def test_identical_repeat_refused_after_normalising(self):
        guard = LookupGuard()
        self.assertIsNone(lookup(guard, "hand"))
        guard.end_request()
        for query in ("hand", " Hand ", "HAND"):
            self.assertEqual(lookup(guard, query), "repeat")
        self.assertEqual(guard.repeats_refused, 3)
        self.assertEqual(guard.calls, 4)  # refused repeats still count toward the streak

    def test_refusal_text_says_act_and_shows_last_state(self):
        guard = LookupGuard()
        lookup(guard, "hand")
        kind, text = guard.check("info", {"query": "hand"}, ERROR)
        self.assertEqual(kind, "repeat")
        self.assertIn("act now", text)
        self.assertIn("COMBAT turn 1 | HP 80/80 Block 0 | Energy 0/3", text)
        self.assertIn("Commands: play <hand#> [e#] | end", text)
        self.assertNotIn("Strike", text)  # short: header and commands only
        self.assertLess(len(text), 400)

    def test_identical_notes_refused(self):
        guard = LookupGuard()
        self.assertIsNone(lookup(guard, "plan A", tool="set_notes"))
        self.assertEqual(lookup(guard, "plan  A", tool="set_notes"), "repeat")
        self.assertIsNone(lookup(guard, "plan B", tool="set_notes"))

    def test_distinct_lookups_allowed(self):
        # The longest legitimate streak recorded: 31 distinct lookups over 9 requests (DeepSeek, catalog research).
        guard = LookupGuard()
        queries = [f"card {i}" for i in range(31)]
        for request in range(9):
            for query in queries[request * 4:(request + 1) * 4]:
                self.assertIsNone(lookup(guard, query))
            self.assertFalse(guard.end_request())
        self.assertEqual((guard.repeats_refused, guard.cap_refused), (0, 0))

    def test_failed_lookup_may_be_retried(self):
        guard = LookupGuard()
        self.assertIsNone(guard.check("info", {"query": "boss"}, STATE))
        guard.ran("info", {"query": "boss"}, True)  # a bridge error is not an answer
        self.assertIsNone(lookup(guard, "boss"))

    def test_act_resets_streak_and_repeats(self):
        guard = LookupGuard()
        for i in range(LOOKUP_CALL_LIMIT):
            lookup(guard, f"q{i}")
        guard.end_request()
        guard.acted()
        self.assertEqual((guard.calls, guard.requests, guard.capped_requests), (0, 0, 0))
        self.assertIsNone(lookup(guard, "q0"))  # the game may have changed: no longer a repeat
        self.assertEqual(guard.longest_streak, LOOKUP_CALL_LIMIT)

    def test_call_cap_refuses_then_stalls(self):
        guard = LookupGuard()
        for i in range(LOOKUP_CALL_LIMIT):
            self.assertIsNone(lookup(guard, f"q{i}"))
        self.assertEqual(lookup(guard, "new"), "lookup_cap")
        self.assertEqual(lookup(guard, "boss", tool="set_notes"), "lookup_cap")  # notes too
        stalls = [guard.end_request()]
        for _ in range(LOOKUP_GRACE_REQUESTS - 1):
            lookup(guard, "another")
            stalls.append(guard.end_request())
        self.assertEqual(stalls, [False] * (LOOKUP_GRACE_REQUESTS - 1) + [True])
        self.assertEqual(guard.detail()["reason"], "lookup_loop")

    def test_request_cap_refuses_lookups(self):
        guard = LookupGuard()
        for _ in range(LOOKUP_REQUEST_LIMIT):
            guard.end_request()  # e.g. replies with no tool call at all
        self.assertEqual(lookup(guard, "deck"), "lookup_cap")

    def test_grace_counts_only_consecutive_capped_requests(self):
        guard = LookupGuard()
        for _ in range(LOOKUP_REQUEST_LIMIT):
            guard.end_request()
        lookup(guard, "deck")
        self.assertFalse(guard.end_request())
        self.assertFalse(guard.end_request())  # no call refused in this one
        self.assertEqual(guard.capped_requests, 0)


class FakeGame:
    """Enough of the bridge for Session.play: a fixed combat that ends after `acts_to_win` acts."""

    HAND = "Card Hand Trick (1) Skill: Gain 7 Block. ... (a name lookup of cards containing 'Hand')"

    def __init__(self, acts_to_end=None):
        self.acts_to_end, self.acts, self.queries = acts_to_end, 0, []

    def state(self): return STATE
    def snapshot(self): return {"character": "ironclad", "floor": 1, "act": 1, "hp": 80}

    def info(self, query):
        self.queries.append(query)
        return "Act 1 Floor 1" if query == "run" else (self.HAND if query == "hand" else f"text of {query}")

    def act(self, commands):
        self.acts += 1
        if self.acts_to_end is not None and self.acts >= self.acts_to_end:
            return "GAME_OVER defeat on floor 1"
        return STATE.replace("turn 1", f"turn {self.acts + 1}")


class ScriptedModel:
    """Replays a script of replies (each a list of (tool, input)); the last one repeats forever."""

    def __init__(self, script):
        self.script, self.n, self.results = script, 0, []

    def describe(self, tools, effort): return {"provider": "mock"}

    def request(self, *args):
        calls = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        return Reply([], [], [ToolCall(f"c{self.n}.{i}", tool, dict(args)) for i, (tool, args) in enumerate(calls)],
                     "tool_use", None, {"input": 1, "output": 1}, "mock", {})

    def tool_result_messages(self, results):
        self.results.extend(results)
        return [{"role": "tool", "content": result} for _, result, _ in results]


def play(script, game):
    model = ScriptedModel(script)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "run.jsonl"
        end = run_once(game, "local:mock", None, path, provider=model, verbose=False, max_requests=500)
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return end, events, model


class LookupLoopRuntimeTests(unittest.TestCase):
    def test_info_loop_ends_as_stalled_with_reason(self):
        game = FakeGame()
        end, events, model = play([[("info", {"query": "hand"})]], game)
        self.assertEqual(end["outcome"], "stalled")
        self.assertEqual(end["usage"]["requests"], LOOKUP_REQUEST_LIMIT + LOOKUP_GRACE_REQUESTS)
        self.assertEqual(end["stop_detail"]["reason"], "lookup_loop")
        self.assertEqual(game.queries.count("hand"), 1)  # repeats never reached the game
        self.assertEqual(end["lookup_guard"], {"repeats_refused": LOOKUP_REQUEST_LIMIT - 1,
                                               "cap_refused": LOOKUP_GRACE_REQUESTS, "longest_streak":
                                               LOOKUP_REQUEST_LIMIT + LOOKUP_GRACE_REQUESTS})
        steps = [e for e in events if e["type"] == "step"]
        self.assertNotIn("lookup_refused", steps[0])
        self.assertEqual(steps[1]["lookup_refused"], {"repeat": 1, "lookup_cap": 0})
        self.assertEqual(steps[1]["calls"][0]["refused"], "repeat")
        self.assertTrue(steps[1]["calls"][0]["is_error"])
        self.assertEqual(steps[-1]["calls"][0]["refused"], "lookup_cap")
        self.assertEqual([e["reason"] for e in events if e["type"] == "watchdog"], ["lookup_loop"])
        self.assertTrue(all(error for _, _, error in model.results[1:]))

    def test_run_start_records_guard_and_version(self):
        _, events, _ = play([[("act", {"commands": "end"})]], FakeGame(acts_to_end=1))
        start = events[0]
        self.assertEqual(start["harness_version"], HARNESS_VERSION)
        self.assertEqual(start["lookup_guard"]["kind"], "lookup-loop-v1")
        self.assertEqual(start["lookup_guard"]["call_limit"], LOOKUP_CALL_LIMIT)
        self.assertEqual(len(start["lookup_guard"]["text_fingerprint"]), 12)
        # The guard lives in tool results only: the system prompt and tool definitions are untouched.
        self.assertEqual(start["prompt_fingerprint"], prompt_fingerprint("ironclad"))

    def test_normal_play_unaffected(self):
        game = FakeGame(acts_to_end=4)
        script = [[("info", {"query": "boss"}), ("info", {"query": "paths"})],
                  [("act", {"commands": "play 0 e0; end"})],
                  [("info", {"query": "enemy"}), ("set_notes", {"notes": "block big hits"})],
                  [("act", {"commands": "end"})],
                  [("info", {"query": "enemy"})],  # same lookup as before, but an act came between
                  [("act", {"commands": "end"})],
                  [("act", {"commands": "end"})]]
        end, events, model = play(script, game)
        self.assertEqual(end["outcome"], "defeat")
        self.assertEqual(game.queries.count("enemy"), 2)
        self.assertEqual(end["lookup_guard"], {"repeats_refused": 0, "cap_refused": 0, "longest_streak": 2})
        steps = [e for e in events if e["type"] == "step"]
        self.assertFalse(any("lookup_refused" in s or any("refused" in c for c in s["calls"]) for s in steps))
        self.assertEqual([s["lookup_streak"] for s in steps], [2, 0, 2, 0, 1, 0, 0])
        self.assertFalse(any(error for _, _, error in model.results))


if __name__ == "__main__":
    unittest.main()
