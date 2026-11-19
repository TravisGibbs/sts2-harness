"""HTTP client for the SpireBridge mod, plus helpers to steer the game between runs."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

BASE_PORT = 15527
DEFAULT_BRIDGE = "http://127.0.0.1:15527"


def instance_url(instance: int) -> str:
    """Base URL of game instance n as started by `launch.ps1 -Instance n` (0 is the ordinary game)."""
    if instance < 0:
        raise ValueError("instance must be 0 or greater")
    return f"http://127.0.0.1:{BASE_PORT + instance}"


class BridgeError(RuntimeError):
    pass


class BridgeUnreachable(BridgeError):
    """No answer at all (game not running, crashed, or hung), as opposed to an error reply."""


# Every screen kind the mod renders. A state's header line is the kind alone, "KIND: ...", "KIND (...)" or
# "COMBAT turn ...". Longer names come first so EVENT_SHOP is not read as EVENT, nor CARD_REWARD as something shorter.
SCREEN_KINDS = (
    "CARD_SELECT", "CARD_REWARD", "EVENT_SHOP", "RELIC_SELECT", "BUNDLE_SELECT", "CRYSTAL_SPHERE", "GAME_OVER",
    "COMBAT", "MAP", "REWARDS", "EVENT", "REST", "SHOP", "TREASURE", "MENU", "POPUP", "PAUSED", "SCREEN", "BUSY",
)
_HEADER = re.compile(r"(?:%s)(?=$|[: (])" % "|".join(SCREEN_KINDS))


def screen_kind(state: str) -> str:
    """The kind of screen a state shows (COMBAT, MAP, GAME_OVER, ...), or UNKNOWN when it has no state header.

    An `act` reply can put `!` notes and the text of `info` lookups from the batch ahead of the state, so this
    looks for the first line that is a state header rather than taking the first line.
    """
    for line in state.splitlines():
        match = _HEADER.match(line)
        if match:
            return match.group(0)
    return "UNKNOWN"


def game_error_lines(result: str) -> list[str]:
    """Actual bridge errors, excluding normal batch-stop and skipped-command notices."""
    return [line for line in result.splitlines()
            if line.startswith("!") and not line.startswith(("! stopped after ", "! skipped "))]


class Bridge:
    """Plain text both ways, except /snapshot which is JSON telemetry the model never sees."""

    def __init__(self, base_url: str = DEFAULT_BRIDGE):
        self.base = base_url.rstrip("/")
        # Ignore system proxies: this is always a loopback call.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def for_instance(cls, instance: int) -> "Bridge":
        """The bridge of game instance n (see `launch.ps1 -Instance n`)."""
        return cls(instance_url(instance))

    def _request(self, path: str, body: str | None = None, timeout: float = 240) -> str:
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8").strip()
        except urllib.error.HTTPError as e:
            raise BridgeError(f"bridge returned {e.code}: {e.read().decode('utf-8', 'replace')}") from e
        except (urllib.error.URLError, OSError) as e:
            raise BridgeUnreachable(
                f"cannot reach the game at {self.base} ({e}). Is Slay the Spire 2 running with the SpireBridge mod enabled?"
            ) from e

    def health(self) -> str:
        return self._request("/health", timeout=5)

    def state(self) -> str:
        return self._request("/state")

    def act(self, commands: str) -> str:
        return self._request("/act", body=commands)

    def info(self, query: str) -> str:
        return self._request("/info?q=" + urllib.parse.quote(query))

    def snapshot(self) -> dict:
        try:
            return json.loads(self._request("/snapshot", timeout=30))
        except (json.JSONDecodeError, BridgeError):
            return {}

    # ---- steering between runs (used by the benchmark runner, never by the model)

    def reach_menu(self) -> None:
        """Get from wherever the game is to the main menu with no run saved."""
        for _ in range(40):
            state = self.state()
            kind = screen_kind(state)
            if kind == "MENU":
                if "saved run exists" in state:
                    self.act("abandon")
                elif "NTimelineScreen" in state:
                    self.act("timeline")
                elif "main menu" in state:
                    return
                else:
                    self.act("choose 0")
            elif kind == "GAME_OVER":
                self.act("continue")
            elif kind == "POPUP":
                self.act("choose 0")
            elif kind == "BUSY":
                self.act("wait")
            else:
                # Still inside a run: give it up.
                self.act("abandon")
        raise BridgeError("could not get the game back to the main menu")

    def start_run(self, character: str, ascension: int, seed: str) -> str:
        """Start a standardized run: the same (character, ascension, seed) is the same run on any profile."""
        self.reach_menu()
        state = self.act(f"new {character} {ascension} {seed}")
        if screen_kind(state) == "MENU" or state.startswith("! new"):
            raise BridgeError(f"the run did not start: {state.splitlines()[0]}")
        return state
