"""Check that one seed plays identically on several game instances at once.

Starts the same (character, ascension, seed) on every instance, sends each the
same fixed commands step by step, and compares every state the games return.

    python tools/cross_instance_check.py --instances 1,2,3,4,5 --seed BENCH01 --steps 60
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from bridge import Bridge, screen_kind  # noqa: E402


def policy(state: str) -> str:
    """A fixed, state-dependent choice: the same state always gets the same command."""
    kind = screen_kind(state)
    if kind == "COMBAT":
        return "play 0 e0; play 0 e0; play 0 e0; end" if "unplayable" not in state.split("Hand:")[-1].splitlines()[1] else "end"
    if kind == "CARD_SELECT":
        return "select 0"
    if kind == "REWARDS":
        return "choose 0" if "\n0 " in state else "proceed"
    if kind in ("GAME_OVER", "MENU"):
        return ""
    if kind == "BUSY":
        return "wait"
    if "\n0 " in state:
        return "choose 0"
    last = state.splitlines()[-1] if state else ""
    if "proceed" in last:
        return "proceed"
    if "open" in last:
        return "open"
    return "wait"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", default="1,2,3,4,5")
    parser.add_argument("--seed", default="BENCH01")
    parser.add_argument("--character", default="ironclad")
    parser.add_argument("--steps", type=int, default=60)
    args = parser.parse_args()

    bridges = [Bridge.for_instance(int(n)) for n in args.instances.split(",")]
    with ThreadPoolExecutor(len(bridges)) as pool:
        states = list(pool.map(lambda b: b.start_run(args.character, 0, args.seed), bridges))

    compared = 0
    for step in range(args.steps):
        if len(set(states)) != 1:
            print(f"DIFFERENT at step {step}:")
            for b, s in zip(bridges, states):
                print(f"--- {b.base}\n{s[:600]}")
            return 1
        compared += 1
        command = policy(states[0])
        if not command:
            break
        # All instances act at the same time, as in a parallel benchmark.
        with ThreadPoolExecutor(len(bridges)) as pool:
            states = list(pool.map(lambda b: b.act(command), bridges))

    digest = hashlib.sha256("\n".join(states).encode()).hexdigest()[:12]
    snaps = [b.snapshot() for b in bridges]
    floors = {s.get("floor") for s in snaps}
    print(f"IDENTICAL: {len(bridges)} instances, {compared} states each, all equal at every step; "
          f"final floor {floors}, final state sha256 {digest}")
    with ThreadPoolExecutor(len(bridges)) as pool:
        list(pool.map(lambda b: b.reach_menu(), bridges))
    return 0


if __name__ == "__main__":
    sys.exit(main())
