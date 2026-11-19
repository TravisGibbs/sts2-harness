"""Exercise the info tools on a live seeded run and check they do not disturb the run.

Instance A calls every info topic along the way; instance B plays the same
seed with the same commands and no info calls. Their game states must match.

    python tools/tools_check.py --seed BENCH01 --a 1 --b 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from bridge import Bridge, screen_kind  # noqa: E402


def strip_terms(state: str) -> str:
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default="BENCH01")
    parser.add_argument("--a", type=int, default=1)
    parser.add_argument("--b", type=int, default=2)
    parser.add_argument("--show", type=int, default=2200, help="characters of each info answer to print")
    args = parser.parse_args()

    a, b = Bridge.for_instance(args.a), Bridge.for_instance(args.b)
    sa = a.start_run("ironclad", 0, args.seed)
    sb = b.start_run("ironclad", 0, args.seed)
    same = [sa == sb]

    def info(topic: str) -> None:
        answer = a.info(topic)
        print(f"\n===== info {topic}  ({len(answer)} chars, ~{len(answer) // 4} tokens) =====")
        print(answer[: args.show] + ("\n... [truncated]" if len(answer) > args.show else ""))

    for topic in ("run", "boss", "paths", "catalog attack rare", "catalog"):
        info(topic)

    # Same fixed policy on both: first option everywhere until the first fight.
    for _ in range(12):
        if screen_kind(sa) == "COMBAT":
            break
        cmd = "choose 0" if "\n0 " in sa else "proceed"
        sa, sb = a.act(cmd), b.act(cmd)
        same.append(sa == sb)

    print("\n===== first combat state (instance A) =====\n" + sa)
    info("enemy")
    info("paths")
    sa, sb = a.act("play 0; end"), b.act("play 0; end")
    same.append(sa == sb)
    print("\n===== after 'play 0; end' =====\n" + sa)

    print(f"\nstates compared: {len(same)}; identical with and without info calls: {all(same)}")
    for bridge in (a, b):
        bridge.reach_menu()
    return 0 if all(same) else 1


if __name__ == "__main__":
    sys.exit(main())
