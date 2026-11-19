"""The text protocol: benchmark "System 1" players, which map a state to commands in one fast step.

The default `tools` protocol (spire_agent.Session) is a multi-turn tool conversation: the model can
look things up, keep notes and think before acting. The `text` protocol removes all of that:

  - one request per decision, stateless: a compact system prompt, the run summary and the current
    state, and nothing else (no history, no notes, no lookups);
  - the answer is plain text holding one batch of commands (`play 0 e0; play 2; end`), parsed
    tolerantly (code fences, bullets, chatter around the commands);
  - no tools, and thinking off (or as low as the model allows);
  - an unusable answer (no command in it, or its first command failed) is shown back once with the
    error; a second one plays a safe fallback (`end` in combat, `wait` elsewhere). Every retry and
    fallback is counted and recorded.

Non-LLM policies (agent/policy.py) play this protocol too, with the state text as their only input.

Why no notes: notes are deliberation spread across decisions (plan, write, re-read), which is what
System 1 leaves out; without them each decision depends only on what the game shows, which is also
exactly what a policy gets, so models and policies are measured on the same observation. The run
summary already carries what lasts (act, floor, boss, HP, gold, deck, relics, potions).
Why stateless: same reason, and it keeps every request a few hundred tokens after the cached
system prompt. The one exception is the retry, whose state is prefixed with why the first answer
was rejected.

Runs under this protocol record `protocol: "text"` and their own prompt fingerprint, and every
report labels them, so they are never pooled with tool-protocol runs.
"""

from __future__ import annotations

import hashlib
import random
import re
import time
import uuid

from bridge import BridgeError, screen_kind
from providers import RateLimited, TransientError
from spire_agent import SYSTEM_PROMPT, Recorder, Usage, floor_of

PROTOCOL = "text"
# Bumped whenever the parser, the message layout or the retry/fallback rules change: part of the fingerprint.
TEXT_PROTOCOL_VERSION = "text-2"
# Output cap per decision. Commands are a line or two; this leaves room for a model that cannot stop thinking.
TEXT_MAX_OUTPUT_TOKENS = 1024
# Decisions in a row that only sent `wait` (by choice or as the fallback) before the run counts as stalled.
STALL_AFTER = 6

# Verbs the parser never accepts: lookups are not part of this protocol, and the rest end or leave the run.
FORBIDDEN_VERBS = {"info", "abandon", "quit", "speed", "new", "menu_new", "timeline"}

_STRATEGY = SYSTEM_PROMPT.split("## How to play well", 1)[1].split("## Style", 1)[0].strip()

TEXT_SYSTEM_PROMPT = f"""\
You are playing Slay the Spire 2, a roguelike deckbuilder, through a text interface. Your goal is to win the run: climb every act and defeat the final boss without your HP reaching 0.

## Interface
- Each message gives you the run summary and the current game state. Answer with the commands to send and nothing else: no explanation, no reasoning, no markdown.
- Each state ends with a `Commands:` line listing what is valid on that screen. Lines starting with `!` report a command that failed or a batch that stopped early; read them.
- Separate several commands with `;` or put one per line. Numbers in a state (`0 Strike`, `e1 Louse`) are indices for that state only. Within one answer they keep referring to the state you were shown even as cards leave your hand, so `play 0 e0; play 2; end` is safe. A batch stops early if something you could not have planned for happens (new cards drawn, a card-selection prompt, the screen changing); you are then shown the new state.
- Plan the whole turn and send every play plus `end` in one answer. After a fight, claim rewards in one answer (`choose 0; choose 1`), and from the card screen pick and leave in one answer (`choose 2; proceed`).
- A `Terms:` block explains keywords and statuses the first time they appear on a floor. It is the game's own tooltip text; trust it over what you remember from other games.
- You have no memory between answers: each decision is made from the run summary and the state alone. Lookups (`info`) and notes are not available.
- An answer with no valid command, or whose first command fails, is shown back to you once with the error. If the second answer fails too, `end` (in combat) or `wait` is played for you.

## How to play well
{_STRATEGY}

## Answer format
Commands only, on one line or one per line. For example:
play 1 e0; play 0; end
"""

PLAYBOOK_LEAD = ("## Character notes\nThese strategy notes were written for a mode with lookups; where they say to use "
                 "`info`, rely on the state and the run summary instead.")

USER_TEMPLATE = "Run summary:\n{summary}\n\nCurrent state:\n{state}\n\nYour commands:"
NO_SUMMARY = "(not available)"

# What a policy is given; covered by its fingerprint in place of a prompt.
POLICY_OBSERVATION = ("policy observation: the state text (with any `!` lines), plus context "
                      "{summary, seed, attempt, character, ascension, step, retry, rng, memory, run_id} for Python policies")


def build_text_system_prompt(playbook: str = "") -> str:
    if not playbook:
        return TEXT_SYSTEM_PROMPT
    return TEXT_SYSTEM_PROMPT.rstrip("\n") + "\n\n" + PLAYBOOK_LEAD + "\n\n" + playbook.strip() + "\n"


def text_fingerprint_of(system_prompt: str) -> str:
    """Never equal to a tool-protocol fingerprint: the protocol, its version and the message layout are hashed in."""
    blob = f"protocol={PROTOCOL}|{TEXT_PROTOCOL_VERSION}|" + system_prompt + "|" + USER_TEMPLATE + "|" + NO_SUMMARY
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def policy_fingerprint() -> str:
    blob = f"protocol={PROTOCOL}|{TEXT_PROTOCOL_VERSION}|" + POLICY_OBSERVATION
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def text_prompt_fingerprint(character: str | None = None, *, policy: bool = False) -> str:
    """The text protocol's counterpart of spire_agent.prompt_fingerprint."""
    if policy:
        return policy_fingerprint()
    from spire_agent import load_playbook

    return text_fingerprint_of(build_text_system_prompt(load_playbook(character)[1]))


def user_message(summary: str, state: str) -> str:
    return USER_TEMPLATE.format(summary=summary.strip() or NO_SUMMARY, state=state.strip())


# ---------------------------------------------------------------- parsing answers

_THINK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)
_BULLET = re.compile(r"^(?:[-*>•]+|\d+[.)])\s+")
_PREFIX = re.compile(r"^(?:commands?|answer|actions?|reply|output|response|move)\s*[:=-]\s*", re.IGNORECASE)
_TOKEN = re.compile(r"^[A-Za-z0-9_+.\-]+$")
_SPAN = re.compile(r"`([^`\n]+)`")


def allowed_verbs(state: str) -> set[str]:
    """Verbs on the state's Commands line, plus `wait`, minus the forbidden ones."""
    line = next((ln for ln in reversed(state.splitlines()) if ln.startswith("Commands:")), None)
    verbs = {"wait"}
    if line is not None:
        verbs |= {c.strip().split(" ", 1)[0].lower() for c in line[len("Commands:"):].split("|") if c.strip()}
    else:
        verbs |= {"play", "end", "potion", "drop", "select", "choose", "proceed", "skip", "open", "continue"}
    return verbs - FORBIDDEN_VERBS


def _clean(line: str) -> str:
    line = line.strip()
    line = _BULLET.sub("", line)
    line = _PREFIX.sub("", line)
    line = line.strip().strip("`\"'").strip()
    return line.rstrip(".").strip()


def _as_commands(line: str, verbs: set[str]) -> list[str] | None:
    """The commands in a line, if every part of it is one; otherwise None."""
    for text in (line, line.replace(",", ";")):
        parts = [p.strip() for p in text.split(";") if p.strip()]
        if not parts:
            continue
        out = []
        for part in parts:
            tokens = part.split()
            if not tokens or len(tokens) > 8 or tokens[0].lower() not in verbs or not all(_TOKEN.match(t) for t in tokens):
                out = None
                break
            out.append(" ".join([tokens[0].lower()] + tokens[1:]))
        if out:
            return out
    return None


def parse_commands(reply: str, state: str) -> tuple[str | None, str | None]:
    """(commands joined with '; ', None), or (None, why nothing usable was found).

    Takes the last block of consecutive lines that are entirely commands valid on this screen; failing
    that, the last `inline code` span that is. Code fences, bullets and prefixes such as `Commands:` are ignored.
    """
    verbs = allowed_verbs(state)
    text = _THINK.sub("", reply or "")
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
    parsed = [_as_commands(_clean(ln), verbs) if ln.strip() else None for ln in lines]
    end = next((i for i in range(len(parsed) - 1, -1, -1) if parsed[i]), None)
    if end is not None:
        start = end
        while start > 0 and parsed[start - 1]:
            start -= 1
        return "; ".join(c for block in parsed[start:end + 1] for c in block), None
    for span in reversed(_SPAN.findall(text)):
        commands = _as_commands(_clean(span), verbs)
        if commands:
            return "; ".join(commands), None

    if not text.strip():
        return None, "the answer was empty"
    used = {(_clean(ln).split() or [""])[0].lower() for ln in lines if ln.strip()}
    if used & FORBIDDEN_VERBS:
        bad = ", ".join(sorted(used & FORBIDDEN_VERBS))
        return None, f"{bad} is not available here; valid commands: {', '.join(sorted(verbs))}"
    return None, f"no line of the answer was a valid command; valid here: {', '.join(sorted(verbs))}"


def first_command_error(commands: str, result: str) -> str | None:
    """The bridge's `! <first command>: ...` line when the batch's first command failed (nothing happened), else None."""
    first = commands.split(";")[0].strip()
    for line in result.splitlines():
        if not line.startswith("!"):
            break
        if line.startswith(f"! {first}:"):
            return line
    return None


def fallback_command(state: str) -> str:
    return "end" if screen_kind(state) == "COMBAT" and "end" in allowed_verbs(state) else "wait"


# ---------------------------------------------------------------- the loop


class _Stop(Exception):
    def __init__(self, outcome: str):
        super().__init__(outcome)
        self.outcome = outcome


class TextSession:
    """Plays a run under the text protocol: one stateless request (or policy call) per decision."""

    def __init__(self, bridge, provider, model: str, effort: str, recorder: Recorder, *, prices=None,
                 max_cost=None, max_requests: int = 2000, max_floor=None, verbose: bool = True,
                 system_prompt: str = TEXT_SYSTEM_PROMPT, max_output_tokens: int = TEXT_MAX_OUTPUT_TOKENS,
                 run_info: dict | None = None):
        self.bridge = bridge
        self.provider = provider
        self.model = model
        self.effort = effort
        self.recorder = recorder
        self.prices = prices
        self.max_cost = max_cost
        self.max_requests = max_requests
        self.max_floor = max_floor
        self.verbose = verbose
        self.system_prompt = system_prompt
        self.max_output_tokens = max_output_tokens
        self.uses_decide = hasattr(provider, "decide")
        self.is_policy = getattr(getattr(provider, "config", None), "kind", None) == "policy"
        info = run_info or {}
        seed, attempt = info.get("seed"), info.get("attempt") or 1
        # The policy's own context: the same for every decision of the run, plus per-decision fields.
        self.context = {
            "seed": seed, "attempt": attempt, "character": info.get("character"), "ascension": info.get("ascension"),
            "rng": random.Random(f"{seed}:{attempt}"), "memory": {}, "run_id": uuid.uuid4().hex[:12],
        }
        self.usage = Usage()
        self.cost_total: float | None = 0.0
        self.notes = ""  # this protocol keeps none; run_end records the empty string like the tool protocol
        self.reasoning_tokens: int | None = None
        self.step = 0
        self.decisions = 0
        self.invalid = 0
        self.retries = 0
        self.fallbacks = 0
        self.floor: str | None = None
        self.last_snapshot: dict = {}

    def say(self, text: str) -> None:
        if self.verbose:
            print(text, flush=True)

    def counters(self) -> dict:
        return {"decisions": self.decisions, "invalid_replies": self.invalid, "retries": self.retries,
                "fallbacks": self.fallbacks}

    def over_budget(self) -> str | None:
        cost = self.cost_total
        if self.max_cost is not None and cost is not None and cost >= self.max_cost:
            return "cost_limit"
        if self.usage.requests >= self.max_requests:
            return "request_limit"
        return None

    # ---- game

    def _summary(self) -> str:
        try:
            return self.bridge.info("run")
        except BridgeError:
            return ""

    def _act(self, commands: str) -> str:
        result = self.bridge.act(commands)
        for _ in range(6):  # still animating: wait here rather than spend a decision on it
            if screen_kind(result) != "BUSY":
                break
            notes = [line for line in result.splitlines() if line.startswith("!")]
            result = "\n".join(notes + [self.bridge.act("wait")])
        return result

    # ---- the player

    def _ask(self, summary: str, observation: str, retry: bool):
        failures = 0
        while True:
            started = time.time()
            try:
                if self.uses_decide:
                    ctx = dict(self.context, summary=summary, step=self.decisions, retry=retry,
                               system_prompt=self.system_prompt)
                    reply = self.provider.decide(observation, ctx)
                else:
                    messages = [{"role": "user", "content": user_message(summary, observation)}]
                    reply = self.provider.complete(self.system_prompt, messages, self.effort, self.max_output_tokens)
                return reply, time.time() - started
            except RateLimited as e:
                self.say(f"[rate limited; waiting {int(e.retry_after)}s]")
                time.sleep(int(e.retry_after))
            except TransientError as e:
                failures += 1
                if failures > 6:
                    raise _Stop("api_error") from e
                self.say(f"[{'policy' if self.is_policy else 'API'} problem ({e.status or 'connection'}); "
                         f"retry {failures} in 15s]")
                time.sleep(15)

    def play(self) -> str:
        try:
            return self._play()
        except _Stop as stop:
            return stop.outcome

    def _play(self) -> str:
        state = self.bridge.state()
        if screen_kind(state) in ("MENU", "GAME_OVER"):
            self.say(state)
            return "no_run"
        if screen_kind(state) == "BUSY":
            state = self._act("wait")
        self.last_snapshot = self.bridge.snapshot()
        idle = 0
        while True:
            if (reason := self.over_budget()) is not None:
                return reason
            summary = self._summary()
            floor = floor_of(summary)
            if floor is not None and floor != self.floor:
                self.floor = floor
                self.recorder.write("episode", floor=floor, summary=summary, notes="", state=state)
                self.say(f"\n===== floor {floor} =====")
            self.say(state)
            self.decisions += 1
            observation = state
            outcome_state = state
            sent: list[str] = []
            for attempt in (1, 2):
                if attempt == 2 and (reason := self.over_budget()) is not None:
                    return reason
                reply, latency = self._ask(summary, observation, retry=attempt > 1)
                step_usage = Usage.of_counts(reply.usage)
                self.usage.add(step_usage)
                if reply.reasoning_tokens is not None:
                    self.reasoning_tokens = (self.reasoning_tokens or 0) + reply.reasoning_tokens
                self.step += 1
                text = "\n".join(reply.text)
                extras = {}
                if reply.raw_stop_reason is not None and reply.raw_stop_reason != reply.stop_reason:
                    extras["raw_stop_reason"] = reply.raw_stop_reason
                if reply.reasoning_tokens is not None:
                    extras["reasoning_tokens"] = reply.reasoning_tokens
                if not reply.usage_reported:
                    extras["usage_reported"] = False
                if "decision_response" in reply.extra:
                    extras["decision_response"] = reply.extra["decision_response"]
                if "provider_cost" in reply.extra:
                    extras["provider_cost"] = reply.extra["provider_cost"]
                step_cost = reply.extra.get("provider_cost", step_usage.cost(self.prices))
                if self.cost_total is not None and step_cost is not None:
                    self.cost_total += step_cost
                else:
                    self.cost_total = None
                if reply.stop_reason == "refusal":
                    self.recorder.write("step", n=self.step, stop_reason="refusal", usage=step_usage.as_dict(), cost=step_cost,
                                        attempt=attempt, **extras)
                    return "refusal"

                # A valid-looking prefix is not necessarily the action the model intended.
                # Never execute a response cut short by the output cap.
                if reply.stop_reason == "max_tokens":
                    commands, invalid = None, "the answer was cut off; send a complete command batch"
                elif reply.tool_calls:
                    commands, invalid = None, "tool calls are unavailable; answer with plain commands"
                elif "decision_response" in reply.extra:
                    choice = reply.extra["decision_response"]
                    command = choice.get("command")
                    if command and choice.get("candidates", {}).get(choice.get("choice")) == command:
                        commands, invalid = command, None
                    else:
                        commands, invalid = None, "the typed answer did not select an offered action"
                else:
                    commands, invalid = parse_commands(text, observation)
                self.say(f"[{'policy' if self.is_policy else 'model'}] {text[:300]}")
                calls = []
                result = None
                if commands:
                    result = self._act(commands)
                    error = first_command_error(commands, result)
                    calls.append({"tool": "act", "input": {"commands": commands}, "result": result,
                                  "is_error": error is not None})
                    if error is None:
                        outcome_state = result
                        sent.append(commands)
                    else:
                        invalid = "the first command failed: " + error[2:]
                    self.say(result)
                fallback = None
                if invalid and attempt == 2:
                    fallback = fallback_command(observation)
                    result = self._act(fallback)
                    calls.append({"tool": "act", "input": {"commands": fallback}, "result": result,
                                  "is_error": False, "fallback": True})
                    self.fallbacks += 1
                    outcome_state = result
                    sent.append(fallback)
                    self.say(f"[fallback] {fallback}\n{result}")

                snapshot = (self.bridge.snapshot() if calls else {}) or self.last_snapshot
                self.last_snapshot = snapshot
                decision = {"decision": self.decisions, "attempt": attempt, "parsed": commands}
                if invalid:
                    decision["invalid"] = invalid
                if fallback:
                    decision["fallback"] = fallback
                self.recorder.write(
                    "step",
                    n=self.step,
                    latency_s=round(latency, 3),
                    stop_reason=reply.stop_reason,
                    served_by=reply.served_by,
                    usage=step_usage.as_dict(),
                    cost=round(step_cost, 9 if "provider_cost" in reply.extra else 6) if step_cost is not None else None,
                    thinking="\n\n".join(reply.thinking),
                    text=text,
                    calls=calls,
                    snapshot=snapshot,
                    **decision,
                    **extras,
                )
                if not invalid:
                    break
                self.invalid += 1
                if attempt == 1:
                    self.retries += 1
                    # The retry sees the state with why the answer was rejected in front, as the bridge reports errors.
                    if result is not None:
                        observation = result
                    else:
                        observation = f"! your answer had no usable commands ({invalid})\n{state}"

            state = outcome_state
            if screen_kind(state) in ("GAME_OVER", "MENU"):
                if self.last_snapshot.get("victory") is True:
                    return "victory"
                return "abandoned" if self.last_snapshot.get("abandoned") else "defeat"
            if self.max_floor is not None and self.last_snapshot.get("floor", 0) > self.max_floor:
                return "floor_limit"
            only_waited = all(c.strip() == "wait" for batch in sent for c in batch.split(";")) if sent else True
            idle = idle + 1 if only_waited else 0
            if idle >= STALL_AFTER:
                return "stalled"
