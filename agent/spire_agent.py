"""LLM agent loop that plays Slay the Spire 2 through the SpireBridge mod.

The mod serves the game as compact text on 127.0.0.1 and executes commands by
calling game logic directly, so the model never sees a screenshot and nothing
touches the mouse or keyboard. This script is the other half: it gives a model
three tools (act, info, set_notes), keeps the context small, tracks spend, and
records everything that happened to a JSONL file for replay and analysis.

    python agent/spire_agent.py --model claude-sonnet-5-5 --max-cost 2
    python agent/spire_agent.py --model openai:gpt-5.1@low
    python agent/spire_agent.py --model local:qwen3:32b

Models come from any provider in agent/providers.py (Anthropic, or any
OpenAI-compatible Chat Completions endpoint). Every model gets the same system
prompt, tools, game text, context policy and limits; what a provider forces to
differ (effort controls, caching) is recorded in run_start.

A spec ending in `@text` plays the System 1 text protocol instead (agent/system1.py:
one stateless request per decision, commands parsed from plain text, no tools), and
`policy:<name>` plays a non-LLM policy under it (agent/policy.py). run_start records
the protocol; everything below describes the default `tools` protocol.

Context strategy: the conversation is reset every time the run moves to a new
floor. Everything the model needs is re-read from the game at that point (run
summary, its own notes, the current screen), so history never accumulates
across rooms and each request stays a few thousand tokens.

Prompt: one general system prompt for every character, plus an optional
character playbook (agent/playbooks/<character>.md) appended to it for the
whole run. Both are covered by the prompt fingerprint recorded in run_start.
SPIRE_NO_PLAYBOOK=1 or --no-playbook plays without the playbook.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import DEFAULT_BRIDGE, Bridge, BridgeError, game_error_lines, screen_kind  # noqa: E402
from providers import (  # noqa: E402,F401  (FALLBACK_MODELS is re-exported for older imports)
    FALLBACK_MODELS,
    ModelSpec,
    Provider,
    ProviderError,
    RateLimited,
    TransientError,
    make_provider,
    parse_model_spec,
    resolve_effort,
    safe_filename,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
# 0.3: the tools protocol refuses repeated lookups and ends lookup-only loops (LookupGuard).
HARNESS_VERSION = "0.3"

# USD per million tokens: (input, output, cache read, cache write).
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-2": (8.00, 40.00, 0.20, 10.00),
}

# Output cap per request, thinking or reasoning included. The same for every model.
MAX_OUTPUT_TOKENS = 8000
INVALID_ACTION_LIMIT = 12

# Lookup-loop guard (tools protocol). A streak is everything since the last `act` call. Across the 143
# tools-protocol recordings under bench/*/runs/ before this guard (the one info-loop run aside), the
# longest legitimate streak was 31 non-act calls (spread over 9 requests) and the most requests in a row
# without an act was 9; the loop that prompted this guard ran 197 calls in 197 requests.
LOOKUP_CALL_LIMIT = 40       # non-act calls since the last act; later ones are refused
LOOKUP_REQUEST_LIMIT = 20    # model requests since the last act; lookups in later ones are refused
LOOKUP_GRACE_REQUESTS = 3    # requests in a row with a refused-at-cap call and no act end the run as stalled
LOOKUP_TOOLS = ("info", "set_notes")  # tools whose identical repeat (no act between) is refused


def validated_price_row(value, name: str) -> tuple[float, float, float, float]:
    """A finite, nonnegative price row. Dynamic/unknown rates must remain unpriced."""
    try:
        if isinstance(value, dict):
            raw = [value["input"], value["output"], value.get("cache_read", value["input"]),
                   value.get("cache_write", value["input"])]
        elif isinstance(value, (list, tuple)) and 2 <= len(value) <= 4:
            raw = list(value) + [value[0]] * (4 - len(value))
        else:
            raise ValueError("need input/output and optional cache prices")
        if any(isinstance(v, bool) for v in raw):
            raise ValueError("boolean rates are not prices")
        values = tuple(float(v) for v in raw)
        if any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("rates must be finite and nonnegative; dynamic/unknown rates cannot be priced")
        return values
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"prices for {name}: {e}") from e


def load_prices(path: str | None = None) -> dict[str, tuple[float, float, float, float]]:
    """Prices for models not in PRICES: a JSON file named by `path`, else by SPIRE_PRICES.

    Keys are model names as recorded (`openai:gpt-5.1`), full specs, or bare model ids. Values are USD per
    million tokens, either [input, output, cache_read, cache_write] or {"input": .., "output": ..,
    "cache_read": .., "cache_write": ..}; a missing cache price is charged as input. SPIRE_PRICES may also
    hold the JSON itself.
    """
    source = path or os.environ.get("SPIRE_PRICES", "").strip()
    if not source:
        return {}
    text = source if source.lstrip().startswith("{") else Path(source).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("prices must be a JSON object mapping model names to rates")
    return {str(key): validated_price_row(value, str(key)) for key, value in data.items()}


def price_for(spec: ModelSpec, extra: dict | None = None):
    """(USD per million tokens as (input, output, cache read, cache write), where it came from), or (None, None)."""
    if spec.is_policy:
        return (0.0, 0.0, 0.0, 0.0), "policy (no model tokens)"
    for key in (spec.spec, spec.name, spec.model_id):
        if extra and key in extra:
            return validated_price_row(extra[key], key), "prices file"
    if spec.provider.kind == "decisions" and spec.model_id == "typesafe/jev-1.14":
        # Verified 2026-11-16: https://openrouter.ai/typesafe/jev-1.14
        # The Decisions response's usage.cost takes precedence during execution.
        return (0.042, 0.0, 0.042, 0.042), "OpenRouter Jev 1.14 model page (2026-11-16)"
    if spec.provider.kind == "anthropic" and spec.model_id in PRICES:
        return PRICES[spec.model_id], "builtin"
    return None, None

SYSTEM_PROMPT = """\
You are playing Slay the Spire 2, a roguelike deckbuilder, through a text interface. Your goal is to win the run: climb every act and defeat the final boss without your HP reaching 0.

## Interface
- `act` sends one or more commands and returns the resulting game state. It is your only way to change the game. The state it returns is always current; you never need to re-read it.
- Each state ends with a `Commands:` line listing what is valid on that screen. Lines starting with `!` report a command that failed or a batch that stopped early; read them.
- Numbers in a state (`0 Strike`, `e1 Louse`) are indices for that state only. Within one `act` call, indices keep referring to the state you last saw even as cards leave your hand, so `play 0 e0; play 2; end` is safe. A batch stops early if something you could not have planned for happens (new cards drawn, a card-selection prompt, the screen changing).
- Batch aggressively: plan the whole turn, then send every play plus `end` in a single `act` call. Fewer calls is better. After a fight, claim rewards in one call (`choose 0; choose 1`), and from the card screen pick and leave in one call (`choose 2; proceed`).
- A `Terms:` block explains keywords and statuses the first time they appear on a floor. It is the game's own tooltip text; trust it over what you remember from other games.
- `info` looks things up without changing the game; it costs nothing in the run, and several lookups can go in one turn.
  - This run: `deck`, `relics`, `potions`, `draw`, `discard`, `exhaust`, `enemies`, `map`.
  - `boss`: this act's boss (also named on the `Boss:` line of the run summary) with its HP, moves and pattern.
  - `enemy [name]`: the moves of any monster; with no name, of the enemies in front of you.
  - `paths`: for each room you can enter next, how many elites, rests, shops, treasures and unknowns lie on the routes from it to the boss, plus a suggested route.
  - `catalog [filters]`: every card you can be offered, one line each. Filter with words such as `attack`, `skill`, `power`, `common`, `uncommon`, `rare`, `colorless`, or any text (`catalog rare power`, `catalog vulnerable`).
  - The name of any card, relic, potion or status returns its exact text (cards include their upgraded version).
- `set_notes` saves a short note to your future self. Your conversation memory is wiped each time you reach a new floor; only the notes, the run summary and the current state carry over.

## Habits that decide runs
- Look things up at decision points; a lookup is far cheaper than a wrong guess. `info boss` at the start of each act, and again at the rest site before the boss to check that your deck can beat it. `info enemy` on turn 1 against any elite or boss, and whenever an enemy or status is new to you. `info catalog` when deciding what the deck is building toward, and `info <card>` before taking a card you do not know.
- `info paths` at every map screen that offers two or more rooms, not just the first. A room's `then ...` shows only one room beyond it, and once a branch has a single way on its elites are forced, so the choice that matters can look like a coin flip. Compare the branches' elite, rest and shop counts and follow the suggested route unless you have a reason not to.
- Keep notes, because nothing else survives the floor. Early in each act, after `info boss`, save: what the deck wins with, what this act's boss demands and how you will prepare, which cards or relics to look for and which to skip, and the route you intend. Add anything learned the hard way (an enemy's trick, a rule you misjudged). Update them when the plan changes, not every floor.

## How to play well
- Enemy intents show what each enemy does next turn. `Attack 6x3` means three hits of 6. In combat, `Incoming: 14 (9 after Block)` is the total attack damage aimed at you this turn and how much of it your current Block does not stop: use it to decide how much to block. Block expires at the start of your next turn, so block what is coming and spend the rest of your energy on damage. Unspent energy is wasted.
- Killing an enemy before it acts prevents its damage entirely. Check for lethal before blocking.
- HP is a resource for the whole run, not just this fight. Avoid needless damage, but do not waste turns over-blocking against small hits when you could end the fight sooner.
- Deck quality beats deck size. Skip card rewards that do not fit your plan. Removing basic Strikes and Defends is valuable. Upgrade your best cards first.
- Elites give relics but cost a lot of HP; take one only when healthy and your deck kills quickly, otherwise pick the branch with fewer elites. Rest sites: heal when low (roughly under half HP before an elite or boss), otherwise upgrade.
- Gold does nothing until you reach a shop. When holding 150 or more, route through one and spend it on what your deck lacks.
- Use potions when they change the outcome of a hard fight (elites, bosses, or to avoid dying). Do not hoard them into a full belt.
- Numbers on cards in your hand already include your own modifiers (Strength, Weak, and so on). When there is exactly one enemy they also include that enemy's statuses; with several enemies they do not, so add 50% yourself against a Vulnerable target.

## Style
Think briefly, then act. Do not narrate the game back or explain at length; one short sentence of intent before a tool call is plenty. Never ask the user questions: nobody is watching in real time. Keep playing until the run ends.
"""

TOOLS = [
    {
        "name": "act",
        "description": (
            "Send game commands and get the new state back. Separate multiple commands with ';' or newlines; "
            "they run in order and stop at the first failure. Valid commands are listed on the last line of each state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "commands": {
                    "type": "string",
                    "description": "Commands to run, e.g. 'play 0 e0; play 3; end' or 'choose 1'.",
                }
            },
            "required": ["commands"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "info",
        "description": (
            "Look up details without changing the game. Costs nothing in the run; use it whenever the answer "
            "could change a decision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "One of: deck (every card with text), relics, potions (what you hold, with text), "
                        "draw, discard, exhaust (pile contents; draw order hidden), enemies (statuses explained), "
                        "map (paths ahead), boss (this act's boss), enemy [name] (a monster's moves), "
                        "paths (what each next room leads to, with a suggested route), "
                        "catalog [filters] (cards you can be offered, e.g. 'catalog rare power'). "
                        "Or the name of any card, relic, potion or status to read its exact text."
                    ),
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "set_notes",
        "description": "Replace your persistent notes (kept across floors). Maximum about 150 words.",
        "input_schema": {
            "type": "object",
            "properties": {"notes": {"type": "string"}},
            "required": ["notes"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


# Shown in place of the notes while the model has not saved any.
NOTES_EMPTY = "(empty: save a plan with set_notes before you leave this floor)"

# Per-character strategy notes, appended to the system prompt: playbooks/<character id, lower case>.md
PLAYBOOK_DIR = Path(__file__).resolve().parent / "playbooks"


def playbooks_enabled() -> bool:
    """SPIRE_NO_PLAYBOOK=1 turns character playbooks off (for comparing runs with and without them)."""
    return os.environ.get("SPIRE_NO_PLAYBOOK", "").strip().lower() not in ("1", "true", "yes", "on")


def load_playbook(character: str | None) -> tuple[str | None, str]:
    """The playbook for a character as (name, text); (None, "") when there is none or playbooks are off.

    `character` is the game's character id in any case ("IRONCLAD"); the file is its lower-case form.
    """
    if not character or not playbooks_enabled():
        return None, ""
    name = re.sub(r"[^a-z0-9_]", "", str(character).strip().lower())
    path = PLAYBOOK_DIR / f"{name}.md"
    if not name or not path.is_file():
        return None, ""
    # Universal newlines on read, so the fingerprint does not depend on how the file was checked out.
    text = path.read_text(encoding="utf-8").strip()
    return (name, text) if text else (None, "")


def build_system_prompt(playbook: str = "") -> str:
    """The whole system prompt for a run: the general prompt, then the character playbook if there is one."""
    if not playbook:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT.rstrip("\n") + "\n\n" + playbook.strip() + "\n"


def fingerprint_of(system_prompt: str) -> str:
    blob = system_prompt + NOTES_EMPTY + json.dumps(TOOLS, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def prompt_fingerprint(character: str | None = None) -> str:
    """Identifies the exact prompt and tool definitions a run was played with.

    With a character it covers that character's playbook too, which is what a run with that character is given;
    without one it covers the general prompt alone.
    """
    return fingerprint_of(build_system_prompt(load_playbook(character)[1]))


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
        )
        commit = out.stdout.strip() or "unknown"
        # Flag uncommitted changes to the harness or mod, so a recording never claims a commit it did not run.
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no", "--", "agent", "mod"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        return commit + ("+dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def load_api_key(name: str = "ANTHROPIC_API_KEY") -> None:
    """Make an API key env var available: process env, then .env, then the Windows user environment."""
    if os.environ.get(name) or (name == "ANTHROPIC_API_KEY" and os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return

    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}=") and len(line) > len(f"{name}="):
                os.environ[name] = line.split("=", 1)[1].strip().strip('"').strip("'")
                return

    # `setx` writes the user environment in the registry, which processes that
    # were already running (and their children) do not see. Read it directly.
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
                if value:
                    os.environ[name] = value
        except OSError:
            pass


def key_problem(spec: ModelSpec) -> str | None:
    """Load the key a model's provider needs; return why it cannot be used, or None."""
    if spec.provider.api_key_env:
        load_api_key(spec.provider.api_key_env)
    return make_provider(spec).key_problem()


def floor_of(run_summary: str) -> str | None:
    match = re.search(r"Act (\d+)(?: of \d+)? Floor (\d+)", run_summary)
    return f"{match.group(1)}.{match.group(2)}" if match else None


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    requests: int = 0

    @staticmethod
    def of(usage) -> "Usage":
        return Usage(
            input=usage.input_tokens or 0,
            output=usage.output_tokens or 0,
            cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            requests=1,
        )

    @staticmethod
    def of_counts(counts: dict) -> "Usage":
        """From a provider Reply's normalised usage: one request."""
        return Usage(
            input=counts.get("input", 0),
            output=counts.get("output", 0),
            cache_read=counts.get("cache_read", 0),
            cache_write=counts.get("cache_write", 0),
            requests=1,
        )

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.requests += other.requests

    def cost(self, prices) -> float | None:
        """USD at `prices`: (input, output, cache read, cache write) per million tokens, or a PRICES key."""
        if isinstance(prices, str):
            prices = PRICES.get(prices)
        if not prices:
            return None
        p_in, p_out, p_read, p_write = prices
        return (
            self.input * p_in + self.output * p_out + self.cache_read * p_read + self.cache_write * p_write
        ) / 1_000_000

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }

    def summary(self, prices) -> str:
        cost = self.cost(prices)
        return (
            f"{self.requests} requests | in {self.input:,} + cache-read {self.cache_read:,} + cache-write "
            f"{self.cache_write:,} | out {self.output:,} | " + (f"~${cost:.3f}" if cost is not None else "cost n/a")
        )


class InvalidActionWatchdog:
    """Count failed action replies on one unchanged state, not legitimate deliberation.

    Only invalid act attempts count. Successful actions (including valid waits), state
    changes and bridge errors reset the streak. Lookups/notes leave it alone. At most
    one failure is counted per model request, even if it emits many bad tool calls.
    """

    def __init__(self, state: str):
        self.state = self.state_key(state)
        self.streak = 0

    @staticmethod
    def state_key(text: str) -> str | None:
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if screen_kind(line) != "UNKNOWN"), None)
        # Ignore error and lookup prefixes, but preserve all displayed state details.
        return "\n".join(lines[start:]).strip() if start is not None else None

    def observe(self, calls: list[dict]) -> bool:
        failed = False
        for call in calls:
            if call["tool"] != "act":
                continue
            result = call["result"]
            errors = game_error_lines(result)
            if result.startswith("Bridge error:") or any(
                    line.startswith(("! could not render ", "! info ")) for line in errors):
                self.streak, failed = 0, False
                continue
            current = self.state_key(result)
            invalid = call["is_error"] or bool(errors)
            if current is not None and current != self.state:
                self.state, self.streak, failed = current, 0, False
            elif not invalid:
                self.streak, failed = 0, False
            elif self.state is not None and (current is not None or call["is_error"]):
                # is_error without a state is a rejected tool call (e.g. bad JSON),
                # which never reached the bridge. The last observed state still applies.
                failed = True
        if failed:
            self.streak += 1
        return self.streak >= INVALID_ACTION_LIMIT

    def detail(self) -> dict:
        return {"reason": "repeated_invalid_actions_unchanged_state", "requests": self.streak,
                "limit": INVALID_ACTION_LIMIT,
                "state_fingerprint": hashlib.sha256((self.state or "").encode("utf-8")).hexdigest()[:12]}


# Tool results the lookup guard sends instead of running the call. They reach the model only in a
# lookup loop; their exact wording is fingerprinted separately in run_start (lookup_guard.text_fingerprint).
REPEAT_INFO_TEXT = ("You already looked up info '{query}' since your last act, and nothing has changed: "
                    "the answer is above. Do not repeat lookups; act now.")
REPEAT_NOTES_TEXT = "Those notes are already saved, unchanged. Act now."
LOOKUP_CAP_TEXT = ("Refused: {calls} lookups over {requests} requests since your last act. Lookups are "
                   "refused until you act, and the run ends if you keep looking things up. Send a command "
                   "with the act tool now.")
LAST_STATE_TEXT = "\nLast state: {brief}"


def state_brief(state: str | None, limit: int = 300) -> str:
    """A state's header line and its Commands line: enough to act on, short enough to repeat."""
    lines = (state or "").splitlines()
    header = next((line for line in lines if screen_kind(line) != "UNKNOWN"), "")
    commands = next((line for line in reversed(lines) if line.startswith("Commands:")), "")
    brief = " / ".join(part.strip() for part in (header, commands) if part.strip())
    return brief[:limit] if brief else "(see the last state above)"


class LookupGuard:
    """Stop a model from looking things up forever instead of playing (tools protocol only).

    `info` and `set_notes` never change the game, so between two `act` calls an identical lookup can only
    return what the model already has: it is refused with a short error instead of reaching the game. Past
    LOOKUP_CALL_LIMIT non-act calls or LOOKUP_REQUEST_LIMIT requests since the last act, every non-act call is
    refused; LOOKUP_GRACE_REQUESTS requests in a row with such a refusal and no act end the run as stalled.
    Any `act` call resets the streak (failing acts are the InvalidActionWatchdog's business).
    """

    def __init__(self):
        self.calls = 0              # non-act calls since the last act, refused ones included
        self.requests = 0           # completed model requests since the last act
        self.capped_requests = 0    # consecutive such requests in which a call was refused at the cap
        self.seen: set = set()
        self.repeats_refused = 0    # run totals
        self.cap_refused = 0
        self.longest_streak = 0
        self.last_refused: dict | None = None
        self._capped_now = False

    @staticmethod
    def key(name: str, tool_input) -> tuple[str, str]:
        if name == "info":
            raw = str((tool_input or {}).get("query", ""))
        elif name == "set_notes":
            raw = str((tool_input or {}).get("notes", ""))
        else:
            raw = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
        return name, " ".join(raw.split()).lower()

    def capped(self) -> bool:
        return self.calls > LOOKUP_CALL_LIMIT or self.requests >= LOOKUP_REQUEST_LIMIT

    def acted(self) -> None:
        self.calls = self.requests = self.capped_requests = 0
        self.seen.clear()

    def count(self) -> None:
        """A non-act call that never reaches a tool (cut off, unreadable arguments)."""
        self.calls += 1
        self.longest_streak = max(self.longest_streak, self.calls)

    def check(self, name: str, tool_input, last_state: str | None) -> tuple[str, str] | None:
        """Count a non-act call about to run; (kind, result text) when it must be refused instead."""
        self.count()
        brief = LAST_STATE_TEXT.format(brief=state_brief(last_state))
        query = self.key(name, tool_input)[1]
        if self.capped():
            self.cap_refused += 1
            self._capped_now = True
            self.last_refused = {"kind": "lookup_cap", "tool": name, "query": query[:80]}
            return "lookup_cap", LOOKUP_CAP_TEXT.format(calls=self.calls, requests=self.requests + 1) + brief
        if name in LOOKUP_TOOLS and self.key(name, tool_input) in self.seen:
            self.repeats_refused += 1
            self.last_refused = {"kind": "repeat", "tool": name, "query": query[:80]}
            text = REPEAT_NOTES_TEXT if name == "set_notes" else REPEAT_INFO_TEXT.format(query=query[:80])
            return "repeat", text + brief
        return None

    def ran(self, name: str, tool_input, is_error: bool) -> None:
        """A non-act call that ran: a successful lookup is an answer the model now has."""
        if name in LOOKUP_TOOLS and not is_error:
            self.seen.add(self.key(name, tool_input))

    def end_request(self) -> bool:
        """After a model request with no act in it. True: the run should end as stalled."""
        self.requests += 1
        self.capped_requests = self.capped_requests + 1 if self._capped_now else 0
        self._capped_now = False
        return self.capped_requests >= LOOKUP_GRACE_REQUESTS

    def detail(self) -> dict:
        return {"reason": "lookup_loop", "calls": self.calls, "requests": self.requests,
                "call_limit": LOOKUP_CALL_LIMIT, "request_limit": LOOKUP_REQUEST_LIMIT,
                "grace_requests": LOOKUP_GRACE_REQUESTS, "repeats_refused": self.repeats_refused,
                "cap_refused": self.cap_refused, "last_refused": self.last_refused}

    def totals(self) -> dict:
        return {"repeats_refused": self.repeats_refused, "cap_refused": self.cap_refused,
                "longest_streak": self.longest_streak}

    @staticmethod
    def describe() -> dict:
        """What run_start records: the limits, and a fingerprint of the texts the model can be shown."""
        texts = json.dumps([REPEAT_INFO_TEXT, REPEAT_NOTES_TEXT, LOOKUP_CAP_TEXT, LAST_STATE_TEXT])
        return {"kind": "lookup-loop-v1", "repeat_refusal": list(LOOKUP_TOOLS), "call_limit": LOOKUP_CALL_LIMIT,
                "request_limit": LOOKUP_REQUEST_LIMIT, "grace_requests": LOOKUP_GRACE_REQUESTS,
                "text_fingerprint": hashlib.sha256(texts.encode("utf-8")).hexdigest()[:12]}


class Recorder:
    """Appends one JSON object per line: everything needed to replay or score a run."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._start = time.time()

    def write(self, kind: str, **fields) -> None:
        event = {"type": kind, "t": round(time.time() - self._start, 2), **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


@dataclass
class Session:
    bridge: Bridge
    provider: Provider
    model: str  # the model's recorded name (providers.ModelSpec.name)
    effort: str
    recorder: Recorder
    # USD per million tokens (input, output, cache read, cache write); None: cost unknown, recorded as null.
    prices: tuple | None = None
    max_cost: float | None = None
    max_requests: int = 2000
    max_floor: int | None = None
    verbose: bool = True
    notes: str = ""
    # Fixed for the whole run, so it stays one cached block: the general prompt plus any character playbook.
    system_prompt: str = SYSTEM_PROMPT
    usage: Usage = field(default_factory=Usage)
    messages: list = field(default_factory=list)
    floor: str | None = None
    acted_this_episode: bool = False
    step: int = 0
    last_snapshot: dict = field(default_factory=dict)
    reasoning_tokens: int | None = None  # summed where the provider reports them
    stop_detail: dict | None = None
    lookup_guard: LookupGuard = field(default_factory=LookupGuard)
    last_state: str | None = None  # the latest state the model was shown, for the guard's short reminder

    def say(self, text: str) -> None:
        if self.verbose:
            print(text, flush=True)

    @staticmethod
    def _step_extras(reply) -> dict:
        """Step fields only some providers have; left out when they would say nothing (as for Claude)."""
        extras = {}
        if reply.raw_stop_reason is not None and reply.raw_stop_reason != reply.stop_reason:
            extras["raw_stop_reason"] = reply.raw_stop_reason
        if reply.reasoning_tokens is not None:
            extras["reasoning_tokens"] = reply.reasoning_tokens
        if not reply.usage_reported:
            extras["usage_reported"] = False
        return extras

    # ---- episodes

    def start_episode(self, state: str) -> None:
        """Begin a fresh conversation from what the game can tell us right now."""
        try:
            summary = self.bridge.info("run")
        except BridgeError:
            summary = ""
        self.floor = floor_of(summary)
        parts = []
        if summary:
            parts.append("Run summary:\n" + summary)
        parts.append("Your notes:\n" + (self.notes or NOTES_EMPTY))
        parts.append("Current state:\n" + state)
        self.messages = [{"role": "user", "content": "\n\n".join(parts)}]
        self.acted_this_episode = False
        self.last_state = state
        self.recorder.write("episode", floor=self.floor, summary=summary, notes=self.notes, state=state)
        self.say(f"\n===== floor {self.floor or '?'} =====\n{state}")

    def moved_to_new_floor(self) -> bool:
        try:
            now = floor_of(self.bridge.info("run"))
        except BridgeError:
            return False
        return now is not None and now != self.floor

    # ---- tools

    def run_tool(self, name: str, tool_input: dict) -> tuple[str, bool]:
        """Returns (result text, is_error)."""
        try:
            if name == "act":
                commands = str(tool_input.get("commands", "")).strip()
                if not commands:
                    return "No commands given.", True
                self.acted_this_episode = True
                result = self.bridge.act(commands)
                # If the game is still animating, wait here rather than spending a model call on it.
                for _ in range(6):
                    if screen_kind(result) != "BUSY":
                        break
                    notes = [line for line in result.splitlines() if line.startswith("!")]
                    result = "\n".join(notes + [self.bridge.act("wait")])
                return result, bool(game_error_lines(result))
            if name == "info":
                return self.bridge.info(str(tool_input.get("query", ""))), False
            if name == "set_notes":
                self.notes = str(tool_input.get("notes", "")).strip()[:1500]
                return "Notes saved.", False
            return f"Unknown tool {name}.", True
        except BridgeError as e:
            return f"Bridge error: {e}", True

    # ---- model

    def request(self):
        """One model request: the same system prompt, tools and limits for every provider."""
        return self.provider.request(self.system_prompt, TOOLS, self.messages, self.effort, MAX_OUTPUT_TOKENS)

    def over_budget(self) -> str | None:
        cost = self.usage.cost(self.prices)
        if self.max_cost is not None and cost is not None and cost >= self.max_cost:
            return "cost_limit"
        if self.usage.requests >= self.max_requests:
            return "request_limit"
        return None

    # ---- main loop

    def play(self) -> str:
        """Play until the run ends or a limit is hit. Returns the outcome."""
        state = self.bridge.state()
        if screen_kind(state) in ("MENU", "GAME_OVER"):
            self.say(state)
            return "no_run"
        self.last_snapshot = self.bridge.snapshot()
        self.start_episode(state)
        watchdog = InvalidActionWatchdog(state)

        idle_turns = 0
        api_failures = 0
        while True:
            if (reason := self.over_budget()) is not None:
                return reason

            started = time.time()
            try:
                reply = self.request()
                api_failures = 0
            except RateLimited as e:
                wait = int(e.retry_after)
                self.say(f"[rate limited; waiting {wait}s]")
                time.sleep(wait)
                continue
            except TransientError as e:
                api_failures += 1
                if api_failures > 6:
                    return "api_error"
                self.say(f"[API problem ({e.status or 'connection'}); retry {api_failures} in 15s]")
                time.sleep(15)
                continue
            latency = time.time() - started

            step_usage = Usage.of_counts(reply.usage)
            self.usage.add(step_usage)
            if reply.reasoning_tokens is not None:
                self.reasoning_tokens = (self.reasoning_tokens or 0) + reply.reasoning_tokens
            self.step += 1

            if reply.stop_reason == "refusal":
                refused_cost = step_usage.cost(self.prices)
                self.recorder.write("step", n=self.step, stop_reason="refusal", usage=step_usage.as_dict(),
                                    cost=round(refused_cost, 6) if refused_cost is not None else None,
                                    latency_s=round(latency, 2), served_by=reply.served_by,
                                    **self._step_extras(reply))
                return "refusal"

            self.messages.append(reply.assistant_message)

            calls, results = [], []
            latest_state: str | None = None
            guard = self.lookup_guard
            refused = {"repeat": 0, "lookup_cap": 0}
            acted = False
            for line in reply.text:
                self.say(f"[model] {line}")
            for call in reply.tool_calls:
                refusal = None
                if call.name == "act":
                    acted = True
                    guard.acted()
                if reply.stop_reason == "max_tokens":
                    result, is_error = "Tool call was cut off before it finished; send it again.", True
                    if call.name != "act":
                        guard.count()
                elif call.error is not None:
                    # Arguments the harness could not read: tell the model, as for any failed tool call.
                    self.say(f"[{call.name}] unreadable arguments: {call.input.get('_unparsed', '')[:200]}")
                    result, is_error = call.error, True
                    if call.name != "act":
                        guard.count()
                elif call.name != "act" and (refusal := guard.check(call.name, call.input, self.last_state)):
                    # A lookup loop: the call never reaches the game.
                    kind, result = refusal
                    is_error = True
                    refused[kind] += 1
                    self.say(f"[{call.name}] {json.dumps(call.input, ensure_ascii=False)} refused ({kind})")
                else:
                    self.say(f"[{call.name}] {json.dumps(call.input, ensure_ascii=False)}")
                    result, is_error = self.run_tool(call.name, call.input)
                    if call.name != "info":
                        self.say(result)
                    if call.name != "act":
                        guard.ran(call.name, call.input, is_error)
                    # Failed commands can still return the current state, and a batch
                    # may have made progress before a later command failed.
                    if call.name == "act" and screen_kind(result) != "UNKNOWN":
                        latest_state = result
                        self.last_state = result
                entry = {"tool": call.name, "input": call.input, "result": result, "is_error": is_error}
                if getattr(call, "repaired", None):
                    entry["repaired"] = call.repaired
                if refusal:
                    entry["refused"] = refusal[0]
                calls.append(entry)
                results.append((call.id, result, is_error))
            stalled_lookups = False if acted else guard.end_request()

            snapshot = self.bridge.snapshot() or self.last_snapshot
            self.last_snapshot = snapshot
            step_cost = step_usage.cost(self.prices)
            stalled_invalid = watchdog.observe(calls)
            guard_fields = {"lookup_streak": guard.calls}
            if any(refused.values()):
                guard_fields["lookup_refused"] = refused
            self.recorder.write(
                "step",
                n=self.step,
                latency_s=round(latency, 2),
                stop_reason=reply.stop_reason,
                served_by=reply.served_by,
                usage=step_usage.as_dict(),
                cost=round(step_cost, 6) if step_cost is not None else None,
                thinking="\n\n".join(reply.thinking),
                text="\n".join(reply.text),
                calls=calls,
                snapshot=snapshot,
                invalid_action_streak=watchdog.streak,
                **guard_fields,
                **self._step_extras(reply),
            )
            tool_results = self.provider.tool_result_messages(results) if results else []

            if latest_state is not None and screen_kind(latest_state) in ("GAME_OVER", "MENU"):
                if snapshot.get("victory") is True:
                    return "victory"
                return "abandoned" if snapshot.get("abandoned") else "defeat"

            if self.max_floor is not None and snapshot.get("floor", 0) > self.max_floor:
                return "floor_limit"

            if stalled_invalid:
                self.stop_detail = watchdog.detail()
                self.recorder.write("watchdog", **self.stop_detail)
                return "stalled"

            if stalled_lookups:
                self.stop_detail = guard.detail()
                self.recorder.write("watchdog", **self.stop_detail)
                return "stalled"

            if tool_results:
                idle_turns = 0
                # New floor: drop the history and rebuild context from the game itself.
                if self.acted_this_episode and latest_state is not None and self.moved_to_new_floor():
                    self.start_episode(latest_state)
                else:
                    self.messages.extend(tool_results)
                continue

            # The model answered without calling a tool. Nudge it; give up if it keeps happening.
            idle_turns += 1
            if idle_turns >= 3:
                return "stalled"
            self.messages.append(
                {"role": "user", "content": "Keep playing: use the act tool to continue the run."}
            )


def run_once(
    bridge: Bridge,
    model: str,
    effort: str | None,
    record_path: Path,
    *,
    max_cost: float | None = None,
    max_requests: int = 2000,
    max_floor: int | None = None,
    notes: str = "",
    verbose: bool = True,
    extra_meta: dict | None = None,
    use_playbook: bool = True,
    prices: dict | None = None,
    provider: Provider | None = None,
) -> dict:
    """Play the run currently loaded in the game and record it. Returns the run_end event.

    `model` is a model spec (see agent/providers.py): `claude-sonnet-5-5`, `openai:gpt-5.1@low`,
    `local:qwen3:32b`. `effort` applies when the spec names none (None: the default for its provider).
    `prices` adds prices for models not in PRICES (see load_prices); without one their cost is null.

    The character's playbook (agent/playbooks/<character>.md) is appended to the system prompt when it exists,
    unless `use_playbook` is False or SPIRE_NO_PLAYBOOK=1 is set.
    """
    spec = parse_model_spec(model)
    effort, _ = resolve_effort(spec, effort)
    provider = provider or make_provider(spec)
    price_row, price_source = price_for(spec, load_prices() if prices is None else prices)

    recorder = Recorder(record_path)
    snapshot = bridge.snapshot()
    # Read once: the same text is fingerprinted and then sent on every request of the run.
    playbook_name, playbook_text = load_playbook(snapshot.get("character")) if use_playbook else (None, "")
    protocol = spec.protocol
    if protocol in ("text", "decisions"):
        # The System 1 protocol (agent/system1.py): its own prompt, fingerprint and fairness record.
        import system1

        if protocol == "decisions":
            import decisions

            system_prompt = decisions.build_system_prompt(playbook_text)
            fingerprint = decisions.fingerprint_of(system_prompt)
        elif spec.is_policy:
            if (problem := provider.key_problem()) is not None:
                raise ProviderError(problem)
            playbook_name, system_prompt = None, ""
            fingerprint = system1.policy_fingerprint()
        else:
            system_prompt = system1.build_text_system_prompt(playbook_text)
            fingerprint = system1.text_fingerprint_of(system_prompt)
        fairness = provider.describe_text(effort)
        output_cap = None if spec.is_policy or protocol == "decisions" else system1.TEXT_MAX_OUTPUT_TOKENS
        context_policy = "stateless: one request per decision (run summary + current state), no notes"
        protocol_meta = {"protocol_version": decisions.PROTOCOL_VERSION if protocol == "decisions" else system1.TEXT_PROTOCOL_VERSION,
                         "fallback": "one retry with the error shown, then 'end' in combat or 'wait'"}
    else:
        system_prompt = build_system_prompt(playbook_text)
        fingerprint = fingerprint_of(system_prompt)
        # How this provider is driven: what is the same for every model is fingerprinted above (system prompt and
        # the neutral tool definitions); what a provider forces to differ is spelled out here.
        fairness = provider.describe(TOOLS, effort)
        output_cap = MAX_OUTPUT_TOKENS
        context_policy = "reset each floor"
        protocol_meta = {"watchdog": {"kind": "invalid-actions-unchanged-state-v1",
                                       "request_limit": INVALID_ACTION_LIMIT},
                         "lookup_guard": LookupGuard.describe()}
    recorder.write(
        "run_start",
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model=spec.name,
        effort=effort,
        harness_version=HARNESS_VERSION,
        harness_commit=git_commit(),
        prompt_fingerprint=fingerprint,
        protocol=protocol,
        playbook=playbook_name,
        limits={"max_cost": max_cost, "max_requests": max_requests, "max_floor": max_floor},
        seed=snapshot.get("seed"),
        character=snapshot.get("character"),
        ascension=snapshot.get("ascension"),
        game_version=snapshot.get("game_version"),
        mod_version=snapshot.get("mod_version"),
        snapshot=snapshot,
        model_spec=spec.spec,
        **fairness,
        max_output_tokens=output_cap,
        context_policy=context_policy,
        **protocol_meta,
        prices=(dict(zip(("input", "output", "cache_read", "cache_write"), price_row), source=price_source)
                if price_row else None),
        **(extra_meta or {}),
    )

    if protocol in ("text", "decisions"):
        session = system1.TextSession(
            bridge, provider, spec.name, effort, recorder, prices=price_row, max_cost=max_cost,
            max_requests=max_requests, max_floor=max_floor, verbose=verbose, system_prompt=system_prompt,
            run_info={"seed": snapshot.get("seed"), "attempt": (extra_meta or {}).get("attempt", 1),
                      "character": snapshot.get("character"), "ascension": snapshot.get("ascension")},
        )
    else:
        session = Session(
            bridge=bridge,
            provider=provider,
            model=spec.name,
            effort=effort,
            recorder=recorder,
            prices=price_row,
            max_cost=max_cost,
            max_requests=max_requests,
            max_floor=max_floor,
            verbose=verbose,
            notes=notes,
            system_prompt=system_prompt,
        )
    started = time.time()
    error = None
    try:
        outcome = session.play()
    except KeyboardInterrupt:
        outcome = "interrupted"
    except BridgeError as e:
        outcome, error = "bridge_error", str(e)
    except ProviderError as e:
        outcome, error = "api_error", str(e)

    final = bridge.snapshot() or session.last_snapshot
    cost = session.cost_total if hasattr(session, "cost_total") else session.usage.cost(price_row)
    end = dict(
        outcome=outcome,
        error=error,
        floor=final.get("floor"),
        act=final.get("act"),
        hp=final.get("hp"),
        max_hp=final.get("max_hp"),
        duration_s=round(time.time() - started, 1),
        usage=session.usage.as_dict(),
        cost=round(cost, 9 if protocol == "decisions" else 4) if cost is not None else None,
        notes=session.notes,
        snapshot=final,
        # What actually happened, for the providers where it is not under the harness's control.
        caching_observed=session.usage.cache_read > 0,
    )
    if session.reasoning_tokens is not None:
        end["reasoning_tokens"] = session.reasoning_tokens
    if getattr(session, "stop_detail", None):
        end["stop_detail"] = session.stop_detail
    if isinstance(getattr(session, "lookup_guard", None), LookupGuard):
        end["lookup_guard"] = session.lookup_guard.totals()
    if protocol in ("text", "decisions"):
        end["protocol"] = protocol
        end.update(session.counters())
    recorder.write("run_end", **end)
    return end


def main() -> int:
    parser = argparse.ArgumentParser(description="Play Slay the Spire 2 with an LLM via the SpireBridge mod.")
    parser.add_argument("--model", default="claude-opus-5-5",
                        help="model spec [provider:]model[@effort][@protocol], e.g. claude-sonnet-5-5, "
                             "openai:gpt-5.1@high, local:qwen3:32b, openrouter:meta-llama/llama-3.1-8b-instruct@text "
                             "(System 1 text protocol), policy:greedy (default: %(default)s)")
    parser.add_argument("--effort", default=None,
                        help="effort per decision when the spec names none: low ... max for Claude (default low); "
                             "passed through for providers that take one; 'default' sends none")
    parser.add_argument("--prices", default=None,
                        help="JSON file of USD-per-million-token prices for models not built in (or set SPIRE_PRICES)")
    parser.add_argument("--max-cost", type=float, default=5.0, help="stop when estimated spend reaches this many USD")
    parser.add_argument("--max-requests", type=int, default=2000, help="stop after this many model requests")
    parser.add_argument("--max-floor", type=int, default=None, help="stop once the run is past this floor")
    parser.add_argument("--new", metavar="CHARACTER", help="start a fresh standardized run with this character first")
    parser.add_argument("--seed", default=None, help="seed for --new (random if omitted)")
    parser.add_argument("--ascension", type=int, default=0, help="ascension for --new")
    parser.add_argument("--bridge", default=os.environ.get("SPIREBRIDGE_URL", DEFAULT_BRIDGE), help="mod base URL")
    parser.add_argument("--notes", default="", help="initial notes/strategy to hand the model")
    parser.add_argument("--no-playbook", action="store_true",
                        help="do not append the character playbook (agent/playbooks/<character>.md) to the prompt")
    parser.add_argument("--quiet", action="store_true", help="only write the recording, do not echo to the terminal")
    args = parser.parse_args()

    try:
        spec = parse_model_spec(args.model)
        effort, warning = resolve_effort(spec, args.effort)
        prices = load_prices(args.prices)
    except (ValueError, OSError) as e:
        parser.error(str(e))
    if warning:
        print(f"note: {warning}", file=sys.stderr)
    if (problem := key_problem(spec)) is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    bridge = Bridge(args.bridge)
    try:
        print(bridge.health())
        if args.new:
            seed_arg = f" {args.seed}" if args.seed else ""
            bridge.reach_menu()
            bridge.act(f"new {args.new} {args.ascension}{seed_arg}")
    except BridgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # The protocol is in the file name too, so one model's runs under the two protocols never share a name.
    tag = "" if spec.protocol == "tools" else f"-{spec.protocol}"
    record_path = REPO_ROOT / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-{safe_filename(spec.name)}{tag}.jsonl"
    if price_for(spec, prices)[0] is None:
        print(f"note: no price for {spec.name}; cost is recorded as n/a and --max-cost cannot apply", file=sys.stderr)
    print(f"model {spec.name} via {spec.provider.name} (effort {effort}, {spec.protocol} protocol) | "
          f"budget ${args.max_cost:.2f} | recording {record_path}")

    end = run_once(
        bridge,
        args.model,
        effort,
        record_path,
        prices=prices,
        max_cost=args.max_cost,
        max_requests=args.max_requests,
        max_floor=args.max_floor,
        notes=args.notes,
        verbose=not args.quiet,
        use_playbook=not args.no_playbook,
    )
    cost = f"${end['cost']:.3f}" if end["cost"] is not None else "n/a"
    print(f"\noutcome: {end['outcome']} on floor {end['floor']} | {end['usage']['requests']} requests | {cost}")
    print(f"replay:  python agent/replay.py {record_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
