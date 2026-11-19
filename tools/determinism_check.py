"""Check that a seed reproduces the same run.

Starts the same (character, ascension, seed) twice, makes the same fixed
choices each time, and compares everything the game showed. Run with the game
up and at the main menu:

    python tools/determinism_check.py --seed BENCH01 --character ironclad
"""

from __future__ import annotations

import argparse
import difflib
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:15527"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path: str) -> str:
    with _opener.open(BASE + path, timeout=180) as r:
        return r.read().decode("utf-8").strip()


def act(commands: str) -> str:
    req = urllib.request.Request(BASE + "/act", data=commands.encode("utf-8"), method="POST")
    with _opener.open(req, timeout=240) as r:
        return r.read().decode("utf-8").strip()


def kind(state: str) -> str:
    for line in state.splitlines():
        if line and not line.startswith("!"):
            return line.split(":")[0].split(" ")[0]
    return "?"


def reach_menu() -> None:
    """Get from wherever the game is to a main menu with no saved run."""
    for _ in range(30):
        state = get("/state")
        k = kind(state)
        if k == "MENU":
            if "saved run exists" in state:
                act("abandon")
                continue
            if "NTimelineScreen" in state:
                act("timeline")
                continue
            if "main menu" in state:
                return
            act("choose 0")  # some other submenu: back out via its first button
        elif k == "GAME_OVER":
            act("continue")
        elif k == "POPUP":
            act("choose 0")
        elif k == "BUSY":
            act("wait")
        else:
            act("abandon")
    raise SystemExit("could not reach the main menu")


def trace(character: str, ascension: int, seed: str, steps: int) -> list[str]:
    """Play a fixed policy (always the first option, end turn in combat) and record what is shown."""
    reach_menu()
    out = [act(f"new {character} {ascension} {seed}")]
    out.append(get("/info?q=run"))
    out.append(get("/info?q=map"))
    for _ in range(steps):
        state = out[-1] if kind(out[-1]) not in ("Act", "Map,") else get("/state")
        k = kind(state)
        if k == "COMBAT":
            nxt = act("play 0; end")
        elif k == "CARD_SELECT":
            nxt = act("select 0")
        elif k == "REWARDS":
            nxt = act("choose 0") if "\n0 " in state else act("proceed")
        elif k in ("GAME_OVER", "MENU"):
            break
        elif k == "BUSY":
            nxt = act("wait")
        elif "\n0 " in state:
            nxt = act("choose 0")
        elif "proceed" in state:
            nxt = act("proceed")
        elif "open" in state.splitlines()[-1]:
            nxt = act("open")
        else:
            nxt = act("wait")
        out.append(nxt)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default="BENCH01")
    parser.add_argument("--character", default="ironclad")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--instance", type=int, default=0, help="game instance to use (see launch.ps1 -Instance)")
    args = parser.parse_args()
    global BASE
    BASE = f"http://127.0.0.1:{15527 + args.instance}"

    first = trace(args.character, args.ascension, args.seed, args.steps)
    second = trace(args.character, args.ascension, args.seed, args.steps)
    reach_menu()

    a = "\n---\n".join(first).splitlines()
    b = "\n---\n".join(second).splitlines()
    diff = list(difflib.unified_diff(a, b, "run 1", "run 2", lineterm="", n=1))
    print(f"seed {args.seed}: {len(first)} recorded states, {len(a)} lines")
    if not diff:
        print("IDENTICAL: both runs showed exactly the same thing at every step")
        print("\nFirst states of the run:\n" + "\n---\n".join(first[:3])[:1800])
        return 0
    print(f"DIFFERENT: {len(diff)} diff lines")
    print("\n".join(diff[:60]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
