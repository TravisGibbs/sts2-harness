"""Typed action choices via OpenRouter's Decisions API, not its Jev chat router.

Reference: https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request
The candidate menu is a bounded approximation from visible state, not an oracle
for legal actions. Its contents and the full returned distribution are recorded.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import urllib.error
import urllib.request

from policies.common import parse, pick_count
from providers import Provider, ProviderError, RateLimited, Reply, TransientError, _host_of

PROTOCOL_VERSION = "decisions-3"
MAX_CANDIDATES = 128
QUESTION = "Which candidate action best advances survival and eventual victory in this Slay the Spire 2 run?"
CANDIDATE_POLICY = ("Visible-state single-command menu, capped at 128 in deterministic order; "
                    "criteria include observed card/target/option/potion descriptions, with potions on all screens; "
                    "not exhaustive and not guaranteed legal. No lookups, notes, or compound combat turns.")


def build_system_prompt(playbook: str = "") -> str:
    from spire_agent import SYSTEM_PROMPT
    strategy = SYSTEM_PROMPT.split("## How to play well", 1)[1].split("## Style", 1)[0].strip()
    prompt = ("Select one of the supplied candidate commands. The game state and run summary are your "
              "entire observation; there is no history or lookup tool. The menu may omit legal actions "
              "or include invalid actions. Prefer a playable action.\n\n" + strategy)
    if playbook:
        prompt += "\n\nCharacter advice (lookups mentioned here are unavailable):\n" + playbook.strip()
    return prompt


def fingerprint_of(prompt: str) -> str:
    material = json.dumps([PROTOCOL_VERSION, QUESTION, CANDIDATE_POLICY, MAX_CANDIDATES, prompt])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def prompt_fingerprint(character: str | None = None) -> str:
    from spire_agent import load_playbook
    return fingerprint_of(build_system_prompt(load_playbook(character)[1]))


def candidate_menu(state: str) -> tuple[dict[str, str], bool]:
    st = parse(state)
    commands = []

    def add(command):
        if command not in commands:
            commands.append(command)

    # Always preserve non-targeted progression choices before any bounded enumeration.
    for verb in ("end", "proceed", "skip", "open", "continue"):
        if verb in st.verbs:
            add(verb)
    if "play" in st.verbs:
        for card in st.hand:
            if not card.playable or (card.cost is not None and st.energy is not None and card.cost > st.energy):
                continue
            if card.targets_enemy:
                for enemy in st.enemies:
                    if enemy.hp > 0:
                        add(f"play {card.index} e{enemy.index}")
            else:
                add(f"play {card.index}")
    if "potion" in st.verbs:
        for index, _ in st.potions:
            add(f"potion {index}")
            for enemy in st.enemies:
                if enemy.hp > 0:
                    add(f"potion {index} e{enemy.index}")
    if "drop" in st.verbs:
        for index, _ in st.potions:
            add(f"drop {index}")
    if "choose" in st.verbs:
        for index, _ in st.options:
            add(f"choose {index}")
    if "select" in st.verbs:
        lower, upper = pick_count(st.header)
        if lower == 0:
            add("select none")
        indices = [index for index, _ in st.options]
        # Generate only enough combinations to identify truncation, never an exponential list.
        for count in range(max(1, lower), min(upper, len(indices)) + 1):
            for values in itertools.combinations(indices, count):
                add("select " + " ".join(map(str, values)))
                if len(commands) > MAX_CANDIDATES:
                    break
            if len(commands) > MAX_CANDIDATES:
                break
    if not commands:
        add("wait")
    truncated = len(commands) > MAX_CANDIDATES
    return {f"a{i:03d}": cmd for i, cmd in enumerate(commands[:MAX_CANDIDATES])}, truncated


def candidate_descriptions(state: str, candidates: dict[str, str]) -> dict[str, str]:
    """Annotate each command with visible facts, without simulating its consequences."""
    st = parse(state)
    cards = {str(card.index): card for card in st.hand}
    enemies = {f"e{enemy.index}": enemy for enemy in st.enemies}
    options = {str(index): label for index, label in st.options}
    potions = {str(index): label for index, label in st.potions}
    descriptions = {}
    for key, command in candidates.items():
        words = command.split()
        verb = words[0]
        parts = [f"Execute game command: {command}"]
        if verb == "end":
            parts.append("End the current player turn.")
        elif verb == "play" and len(words) > 1 and words[1] in cards:
            card = cards[words[1]]
            cost = str(card.cost) if card.cost is not None else "X/unknown"
            parts.append(f"Card: {card.name}; displayed cost: {cost}; {card.type}: {card.text}")
        elif verb in ("potion", "drop") and len(words) > 1 and words[1] in potions:
            parts.append(f"Potion: {potions[words[1]]}")
        elif verb in ("choose", "select"):
            for index in words[1:]:
                if index in options:
                    parts.append(f"Option {index}: {options[index]}")
            if words[1:] == ["none"]:
                parts.append("Select no cards.")
        for word in words[2:]:
            if word in enemies:
                enemy = enemies[word]
                parts.append(f"Target: {enemy.name}; HP {enemy.hp}/{enemy.max_hp}; Block {enemy.block}")
        descriptions[key] = " | ".join(parts)
    return descriptions


class DecisionsProvider(Provider):
    def __init__(self, config, model_id, opener=None):
        self.config, self.model_id = config, model_id
        self.opener = opener or urllib.request.build_opener()

    def key_problem(self):
        if self.config.api_key_env and not os.environ.get(self.config.api_key_env):
            return f"no API key for provider decisions; set {self.config.api_key_env}"
        return None

    def build_payload(self, state, context):
        candidates, truncated = candidate_menu(state)
        payload = {
            "model": self.model_id,
            "state": {"run_summary": context.get("summary", ""), "game_state": state,
                      "strategy": context.get("system_prompt", build_system_prompt())},
            "questions": {"action": {"type": "choice", "instructions": QUESTION,
                                     "criteria": candidate_descriptions(state, candidates)}},
        }
        return payload, candidates, truncated

    def _post(self, payload):
        key = os.environ.get(self.config.api_key_env or "OPENROUTER_API_KEY", "")
        req = urllib.request.Request(self.config.base_url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                                     method="POST")
        try:
            with self.opener.open(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    retry = max(0, float(e.headers.get("Retry-After", "20")))
                except (ValueError, TypeError):
                    retry = 20
                raise RateLimited("Decisions endpoint rate limited", retry) from e
            if e.code >= 500:
                raise TransientError(f"Decisions endpoint returned HTTP {e.code}", e.code) from e
            raise ProviderError(f"Decisions endpoint returned HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise TransientError("Decisions endpoint connection failed") from e
        except (json.JSONDecodeError, UnicodeError) as e:
            raise ProviderError("Decisions endpoint returned invalid JSON") from e

    def decide(self, state, context):
        payload, candidates, truncated = self.build_payload(state, context)
        response = self._post(payload)
        if not isinstance(response, dict) or response.get("error"):
            raise ProviderError("Decisions endpoint returned an error response")
        usage = response.get("usage")
        if not isinstance(usage, dict):
            raise ProviderError("Decisions response omitted required usage; cannot account for spend")
        try:
            cost = float(usage["cost"])
            tokens = [int(usage[field]) for field in ("input_tokens", "output_tokens")]
            if not math.isfinite(cost) or cost < 0 or any(value < 0 for value in tokens):
                raise ValueError()
        except (ValueError, TypeError, KeyError, OverflowError) as e:
            raise ProviderError("Decisions response has invalid usage/cost") from e
        answers = response.get("answers")
        answer = answers.get("action") or {} if isinstance(answers, dict) else {}
        choice = answer.get("choice") if isinstance(answer, dict) else None
        command = candidates.get(choice) if isinstance(choice, str) else None
        detail = {"candidates": candidates, "criteria": payload["questions"]["action"]["criteria"],
                  "menu_truncated": truncated, "choice": choice, "command": command,
                  "confidence": answer.get("confidence") if isinstance(answer, dict) else None,
                  "probabilities": answer.get("probabilities") if isinstance(answer, dict) else None}
        if command is None:
            detail["error"] = "response did not choose an offered candidate"
        return Reply(text=[command] if command else [], thinking=[], tool_calls=[], stop_reason="end_turn",
                     raw_stop_reason=None, usage={"input": tokens[0], "output": tokens[1], "cache_read": 0, "cache_write": 0},
                     served_by=response.get("model") or self.model_id, assistant_message={},
                     extra={"decision_response": detail, "provider_cost": cost})

    def describe_text(self, effort):
        return {"provider": self.config.name, "api": "openrouter-decisions", "model_id": self.model_id,
                "base_url_host": _host_of(self.config.base_url), "tools_format": None,
                "reasoning": {"sent": False, "effort": "default", "summary_visible": False},
                "caching": None, "candidate_policy": CANDIDATE_POLICY, "max_candidates": MAX_CANDIDATES,
                "cost_source": "API usage.cost"}
