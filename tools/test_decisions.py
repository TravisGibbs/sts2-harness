"""Typed Decisions API contract, menu, and spend-accounting tests; no network calls."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from decisions import DecisionsProvider, candidate_descriptions, candidate_menu, prompt_fingerprint
from providers import ProviderError, parse_model_spec, resolve_effort
from spire_agent import price_for, run_once
from system1 import text_prompt_fingerprint
from policies.common import parse

COMBAT = """COMBAT turn 1 | HP 60/80 Block 0 | Energy 1/3
Enemies:
e0 Cultist 20/40 Block 0 -> Attack 6
Hand:
0 Strike (1) Attack: Deal 6 damage. >enemy
1 Bash (2) Attack: Deal 8 damage. >enemy
2 Defend (1) Skill: Gain 5 Block.
Draw 4 | Discard 0 | Exhaust 0
Potions: 0 Fire Potion (1/3 slots)
Commands: play <hand#> [e#] | end | potion <#> [e#] | drop <#>"""


class Opener:
    def __init__(self, choice="a000", cost=0.00002):
        self.requests = []
        self.choice = choice
        self.cost = cost

    def open(self, request, timeout):
        self.requests.append(request)
        return io.BytesIO(json.dumps({"model": "typesafe/jev-1.14-served", "answers": {
            "action": {"choice": self.choice, "confidence": 0.9, "probabilities": {self.choice: 1}}},
            "usage": {"input_tokens": 100, "output_tokens": 10, "cost": self.cost}}).encode())


class FakeBridge:
    def __init__(self, finish=False):
        self.acts = []
        self.finish = finish
    def state(self): return "GAME_OVER" if self.finish and self.acts else COMBAT
    def info(self, query): return "Act 1 Floor 1"
    def act(self, command):
        self.acts.append(command)
        return self.state()
    def snapshot(self):
        return {"character": "ironclad", "floor": 1, "act": 1, "hp": 60, "victory": self.finish and bool(self.acts)}


class DecisionsTests(unittest.TestCase):
    def provider(self, opener=None):
        spec = parse_model_spec("decisions:typesafe/jev-1.14")
        return DecisionsProvider(spec.provider, spec.model_id, opener=opener or Opener())

    def test_protocol_and_price_are_not_policy(self):
        spec = parse_model_spec("decisions:typesafe/jev-1.14")
        self.assertEqual(spec.protocol, "decisions")
        self.assertFalse(spec.is_policy)
        self.assertEqual(resolve_effort(spec)[0], "default")
        self.assertEqual(price_for(spec)[0][:2], (0.042, 0))
        for bad in ("decisions:typesafe/jev-1.14@text", "decisions:typesafe/jev-1.14@low", "local:mock@decisions"):
            with self.assertRaises(ValueError): parse_model_spec(bad)

    def test_distinct_fingerprint(self):
        self.assertNotEqual(prompt_fingerprint("ironclad"), text_prompt_fingerprint("ironclad"))

    def test_candidate_menu_uses_visible_energy_and_targets(self):
        menu, truncated = candidate_menu(COMBAT)
        self.assertIn("play 0 e0", menu.values())
        self.assertIn("play 2", menu.values())
        self.assertNotIn("play 1 e0", menu.values())
        self.assertIn("end", menu.values())
        self.assertFalse(truncated)
        self.assertEqual(candidate_menu(COMBAT), (menu, truncated))

    def test_selection_combinations_bounded(self):
        state = "CARD_SELECT (pick exactly 2)\n" + "\n".join(f"{i} Card" for i in range(30)) + "\nCommands: select <#> [#...]"
        menu, truncated = candidate_menu(state)
        self.assertEqual(len(menu), 128)
        self.assertTrue(truncated)
        self.assertTrue(all(len(command.split()) == 3 for command in menu.values()))

    def test_potion_indices_are_not_quantities_and_legacy_recordings_work(self):
        for labels in ("0 Energy Potion, 1 Blood Potion", "[0] Energy Potion, [1] Blood Potion"):
            state = COMBAT.replace("0 Fire Potion (1/3 slots)", labels + " (2/3 slots)")
            self.assertEqual(parse(state).potions, [(0, "Energy Potion"), (1, "Blood Potion")])
            self.assertIn("potion 0", candidate_menu(state)[0].values())

    def test_potions_are_available_on_choice_screens(self):
        state = "MAP\n0 Monster\nPotions: [0] Blood Potion (1/3 slots)\nCommands: choose <#> | potion <#> | drop <#>"
        menu, _ = candidate_menu(state)
        self.assertEqual(parse(state).options, [(0, "Monster")])
        self.assertIn("potion 0", menu.values())
        self.assertIn("drop 0", menu.values())
        descriptions = candidate_descriptions(state, menu)
        self.assertIn("Potion: Blood Potion", descriptions[next(k for k, v in menu.items() if v == "potion 0")])

    def test_choice_screen_and_fallback(self):
        menu, _ = candidate_menu("EVENT\n0 Heal\n1 Leave\nCommands: choose <#>")
        self.assertEqual(list(menu.values()), ["choose 0", "choose 1"])
        self.assertEqual(list(candidate_menu("BUSY")[0].values()), ["wait"])

    def test_grounded_card_target_and_potion_descriptions(self):
        payload, candidates, _ = self.provider().build_payload(COMBAT, {})
        criteria = payload["questions"]["action"]["criteria"]
        descriptions = {command: criteria[key] for key, command in candidates.items()}
        strike = descriptions["play 0 e0"]
        self.assertIn("Card: Strike", strike)
        self.assertIn("displayed cost: 1", strike)
        self.assertIn("Deal 6 damage", strike)
        self.assertIn("Target: Cultist; HP 20/40; Block 0", strike)
        self.assertIn("Gain 5 Block", descriptions["play 2"])
        self.assertNotIn("Target:", descriptions["play 2"])
        self.assertIn("Potion: Fire Potion", descriptions["potion 0 e0"])
        self.assertIn("End the current player turn", descriptions["end"])
        self.assertEqual(candidates, candidate_menu(COMBAT)[0])

    def test_grounded_option_descriptions(self):
        state = "EVENT\n0 Heal 20 HP\n1 Leave with gold\nCommands: choose <#>"
        menu, _ = candidate_menu(state)
        descriptions = candidate_descriptions(state, menu)
        self.assertIn("Option 0: Heal 20 HP", descriptions["a000"])
        self.assertIn("Option 1: Leave with gold", descriptions["a001"])

    def test_typed_http_payload_and_distribution(self):
        opener = Opener()
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            reply = self.provider(opener).decide(COMBAT, {"summary": "Act 1 Floor 1"})
        request = opener.requests[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(payload["questions"]["action"]["type"], "choice")
        self.assertEqual(payload["state"]["game_state"], COMBAT)
        self.assertNotIn("messages", payload)
        self.assertNotIn("tools", payload)
        self.assertEqual(reply.text, ["end"])
        self.assertEqual(reply.extra["provider_cost"], 0.00002)
        self.assertEqual(reply.extra["decision_response"]["probabilities"], {"a000": 1})

    def test_unoffered_choice_is_never_executed(self):
        reply = self.provider(Opener(choice="quit")).decide(COMBAT, {})
        self.assertEqual(reply.text, [])
        self.assertIn("error", reply.extra["decision_response"])
        self.assertGreater(reply.extra["provider_cost"], 0)

    def test_invalid_reported_cost_rejected(self):
        for cost in (-1, float("nan"), float("inf")):
            with self.assertRaises(ProviderError): self.provider(Opener(cost=cost)).decide(COMBAT, {})

    def run_fake(self, opener, bridge, **limits):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.jsonl"
            result = run_once(bridge, "decisions:typesafe/jev-1.14", None, path,
                              provider=self.provider(opener), verbose=False, **limits)
            events = [json.loads(line) for line in path.read_text().splitlines()]
        return result, events

    def test_actual_cost_enforces_cap_and_is_logged(self):
        opener, bridge = Opener(cost=0.02), FakeBridge()
        end, events = self.run_fake(opener, bridge, max_cost=0.01)
        self.assertEqual(end["outcome"], "cost_limit")
        self.assertEqual(end["cost"], 0.02)
        self.assertEqual(len(opener.requests), 1)
        step = next(event for event in events if event["type"] == "step")
        self.assertEqual(step["cost"], step["provider_cost"])
        self.assertIn("candidates", step["decision_response"])
        self.assertEqual(events[0]["protocol"], "decisions")
        self.assertEqual(events[0]["cost_source"], "API usage.cost")

    def test_invalid_choice_still_charged_and_cap_prevents_retry(self):
        opener, bridge = Opener(choice="not-offered", cost=0.02), FakeBridge()
        end, events = self.run_fake(opener, bridge, max_cost=0.01)
        self.assertEqual(end["outcome"], "cost_limit")
        self.assertEqual(end["cost"], 0.02)
        self.assertEqual(bridge.acts, [])
        self.assertEqual(len(opener.requests), 1)

    def test_successful_run_keeps_decisions_metadata(self):
        end, events = self.run_fake(Opener(), FakeBridge(finish=True))
        self.assertEqual(end["outcome"], "victory")
        self.assertEqual(end["protocol"], "decisions")
        self.assertEqual(end["decisions"], 1)


if __name__ == "__main__": unittest.main()
