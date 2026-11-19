"""Reference policy: a few fixed heuristics, one whole turn per decision.

Combat: if the attack damage Block does not stop (`Incoming: 14 (9 after Block)`) is more than
BLOCK_THRESHOLD, play the biggest affordable Block cards until it is covered or energy runs out; then
play the highest-damage affordable attacks at the weakest enemy; then any other affordable card
(powers, skills), since unspent energy is wasted; then `end`. All in one batch (indices refer to the
state as sent, which the bridge supports).
Elsewhere: take the first card reward, claim every other reward, rest below half HP (otherwise
upgrade), avoid elites below 70% HP, head for a rest site below half HP, leave shops without buying,
open chests, and take the first event option that has not just failed.

Deterministic. Meant as a floor: anything that plays the game should beat it.
"""

from __future__ import annotations

import re

from policies.common import parse, pick_count

BLOCK_THRESHOLD = 5


def combat(st) -> str:
    energy = st.energy or 0
    hand = [c for c in st.hand if c.playable and c.index not in st.failed_indices("play")]
    plays: list[str] = []
    used: set[int] = set()

    def cost(card) -> int:
        return energy if card.cost is None else card.cost

    def affordable(card) -> bool:
        return card.index not in used and cost(card) <= energy and (card.cost is not None or energy > 0)

    alive = sorted(st.enemies, key=lambda e: (e.hp + e.block, e.index))
    target = alive[0].index if alive else 0

    unblocked = st.unblocked if st.unblocked is not None else 0
    if unblocked > BLOCK_THRESHOLD:
        for card in sorted((c for c in hand if c.block > 0), key=lambda c: (-c.block, cost(c), c.index)):
            if unblocked <= 0:
                break
            if affordable(card):
                plays.append(f"play {card.index}" + (f" e{target}" if card.targets_enemy else ""))
                used.add(card.index)
                energy -= cost(card)
                unblocked -= card.block

    for card in sorted((c for c in hand if c.damage > 0), key=lambda c: (-c.damage, cost(c), c.index)):
        if affordable(card):
            plays.append(f"play {card.index}" + (f" e{target}" if card.targets_enemy else ""))
            used.add(card.index)
            energy -= cost(card)

    # Whatever else fits: powers first, then cheap skills. Curses and statuses are unplayable already.
    rest = sorted((c for c in hand if c.index not in used), key=lambda c: (c.type != "Power", cost(c), c.index))
    for card in rest:
        if affordable(card) and card.type in ("Power", "Skill", "Attack"):
            plays.append(f"play {card.index}" + (f" e{target}" if card.targets_enemy else ""))
            used.add(card.index)
            energy -= cost(card)

    return "; ".join(plays + ["end"])


def first(options, *, avoid=(), prefer=(), failed=()) -> int | None:
    pool = [(i, label) for i, label in options if i not in failed]
    for word in prefer:
        for i, label in pool:
            if word.lower() in label.lower():
                return i
    for i, label in pool:
        if not any(word.lower() in label.lower() for word in avoid):
            return i
    return pool[0][0] if pool else None


def act(state: str, context: dict | None = None) -> str:
    st = parse(state)
    verbs = st.verbs
    failed = st.failed_indices("choose")
    hp, max_hp = st.hp, st.max_hp
    if hp is None and context and context.get("summary"):
        m = re.search(r"\bHP (\d+)/(\d+)", context["summary"])
        if m:
            hp, max_hp = int(m.group(1)), int(m.group(2))
    ratio = (hp / max_hp) if hp is not None and max_hp else 1.0

    if st.kind == "COMBAT":
        return combat(st) if "end" in verbs else "wait"

    if st.kind == "CARD_SELECT":
        lo, _ = pick_count(st.header)
        if lo == 0:
            # An optional pick (an offered card, a removal): take the first, as with card rewards.
            return "select 0" if st.options else "select none"
        return "select " + " ".join(str(i) for i, _ in st.options[:lo])

    def choose(i):
        return f"choose {i}"

    if st.kind == "CARD_REWARD":
        i = first(st.options, failed=failed)
        if i is not None and "choose" in verbs:
            return choose(i)
    elif st.kind == "REST":
        i = first(st.options, prefer=("Rest",) if ratio < 0.5 else ("Smith", "Upgrade"), failed=failed)
        if i is not None and "choose" in verbs:
            return choose(i)
    elif st.kind == "MAP":
        prefer = ("Rest",) if ratio < 0.5 else ()
        avoid = ("Elite",) if ratio < 0.7 else ()
        i = first(st.options, prefer=prefer, avoid=avoid, failed=failed)
        if i is not None:
            return choose(i)
    elif st.kind in ("SHOP", "EVENT_SHOP"):
        for verb in ("proceed", "skip"):
            if verb in verbs:
                return verb
    elif st.kind == "TREASURE":
        if "open" in verbs:
            return "open"
    elif st.kind == "REWARDS":
        # Claim everything in order; a full potion belt makes that one fail, then move on.
        if not failed and st.options and "choose" in verbs:
            return choose(st.options[0][0])

    if "choose" in verbs and st.options:
        i = first(st.options, failed=failed)
        if i is not None:
            return choose(i)
    for verb in ("open", "proceed", "skip", "continue"):
        if verb in verbs:
            return verb
    return "wait"
