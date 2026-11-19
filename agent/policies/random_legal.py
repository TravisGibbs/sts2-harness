"""Reference policy: one uniformly random legal-looking command per decision.

Candidates come from the state alone: in combat every playable, affordable hand card (at a random
enemy when it targets one), `end`, and each held potion; elsewhere `choose <#>` for every numbered
option plus the argument-free verbs on the Commands line (proceed, skip, open, ...); on a card
selection, a random set of the size the prompt asks for. Indices a `!` line just reported as failed
are left out. `wait`, `drop` and lookups are never chosen. Reproducible: the harness hands each run
its own random.Random seeded from (seed, attempt).

The floor every model should clear by a wide margin: a model that does not beat it is not playing.
"""

from __future__ import annotations

import random

from policies.common import parse, pick_count

NEVER = {"wait", "drop", "info", "abandon", "quit", "speed", "new", "menu_new", "timeline", "continue"}


def candidates(text: str, rng: random.Random) -> list[str]:
    st = parse(text)
    out: list[str] = []
    if st.kind == "COMBAT":
        failed = st.failed_indices("play")
        enemies = [e.index for e in st.enemies] or [0]
        for card in st.hand:
            if not card.playable or card.index in failed:
                continue
            if card.cost is not None and st.energy is not None and card.cost > st.energy:
                continue
            out.append(f"play {card.index}" + (f" e{rng.choice(enemies)}" if card.targets_enemy else ""))
        if "potion" in st.verbs:
            for i, _ in st.potions:
                out.append(f"potion {i} e{rng.choice(enemies)}")
        if "end" in st.verbs:
            out.append("end")
        return out

    if st.kind == "CARD_SELECT":
        lo, hi = pick_count(st.header)
        indices = [i for i, _ in st.options]
        sizes = [n for n in range(lo, hi + 1) if n <= len(indices)]
        if sizes:
            n = rng.choice(sizes)
            out.append("select none" if n == 0 else "select " + " ".join(map(str, sorted(rng.sample(indices, n)))))
        return out

    failed = st.failed_indices("choose")
    if "choose" in st.verbs:
        out += [f"choose {i}" for i, _ in st.options if i not in failed]
    for command in st.commands:
        verb = command.split(" ", 1)[0]
        if " " not in command and verb not in NEVER and verb != "choose":
            out.append(verb)
    # Menus and the game-over screen: `continue` is the only way on.
    if not out and "continue" in st.verbs:
        out.append("continue")
    return out


def act(state: str, context: dict) -> str:
    rng = context.get("rng") or random.Random(0)
    options = candidates(state, rng)
    return rng.choice(options) if options else "wait"
