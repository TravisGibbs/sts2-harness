"""Reading the bridge's state text, for policies. Only the plain text a model sees is used.

    COMBAT turn 2 | HP 61/80 Block 0 | Energy 3/3
    Enemies:
    e0 Jaw Worm 30/42 Block 6 [Vulnerable 2] -> Attack 11
    Incoming: 14 (9 after Block)
    Hand:
    0 Strike (1) Attack: Deal 12 damage. >enemy
    1 Defend (1) Skill: Gain 5 Block.
    ...
    Commands: play <hand#> [e#] | end | potion <#> [e#] | drop <#>
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADER_KINDS = (
    "CARD_SELECT", "CARD_REWARD", "EVENT_SHOP", "RELIC_SELECT", "BUNDLE_SELECT", "CRYSTAL_SPHERE", "GAME_OVER",
    "COMBAT", "MAP", "REWARDS", "EVENT", "REST", "SHOP", "TREASURE", "MENU", "POPUP", "PAUSED", "SCREEN", "BUSY",
)
_HEADER = re.compile(r"(?:%s)(?=$|[: (])" % "|".join(HEADER_KINDS))


@dataclass
class Card:
    index: int
    name: str
    cost: int | None  # None: X or unknown
    type: str
    text: str
    targets_enemy: bool
    playable: bool

    @property
    def damage(self) -> int:
        """Total damage the card's text promises (hits included), roughly."""
        total = 0
        for m in re.finditer(r"Deal (\d+) damage(?:\s+(twice|(\d+) times))?", self.text):
            hits = 2 if m.group(2) == "twice" else int(m.group(3)) if m.group(3) else 1
            total += int(m.group(1)) * hits
        return total

    @property
    def block(self) -> int:
        return sum(int(m.group(1)) for m in re.finditer(r"Gain (\d+) Block", self.text))


@dataclass
class Enemy:
    index: int
    name: str
    hp: int
    max_hp: int
    block: int


@dataclass
class State:
    text: str
    kind: str
    header: str
    errors: list[str]  # the `!` lines in front of the state
    commands: list[str]  # entries of the Commands: line ("play <hand#> [e#]", "end", ...)
    options: list[tuple[int, str]] = field(default_factory=list)  # numbered lines outside combat: (index, label)
    hp: int | None = None
    max_hp: int | None = None
    energy: int | None = None
    incoming: int | None = None  # attack damage this turn
    unblocked: int | None = None  # of which current Block does not stop
    hand: list[Card] = field(default_factory=list)
    enemies: list[Enemy] = field(default_factory=list)
    potions: list[tuple[int, str]] = field(default_factory=list)

    @property
    def verbs(self) -> set[str]:
        return {c.split(" ", 1)[0] for c in self.commands if c}

    def failed_indices(self, verb: str) -> set[int]:
        """Indices a `!` line says failed with this verb (`! choose 2: ...`)."""
        out = set()
        for line in self.errors:
            m = re.match(rf"^! {verb} (\d+)\b", line)
            if m:
                out.add(int(m.group(1)))
        return out


def parse(text: str) -> State:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if _HEADER.match(line)), None)
    errors = [line for line in lines[: start if start is not None else len(lines)] if line.startswith("!")]
    body = lines[start:] if start is not None else [line for line in lines if not line.startswith("!")]
    header = body[0] if body else ""
    m = _HEADER.match(header)
    kind = m.group(0) if m else "UNKNOWN"
    commands_line = next((line for line in reversed(body) if line.startswith("Commands:")), "Commands: wait")
    commands = [c.strip() for c in commands_line[len("Commands:"):].split("|") if c.strip()]
    st = State(text=text, kind=kind, header=header, errors=errors, commands=commands)

    hp = re.search(r"\bHP (\d+)/(\d+)", text)
    if hp:
        st.hp, st.max_hp = int(hp.group(1)), int(hp.group(2))

    # Potion commands also appear on choice screens. Accept recordings from
    # both before and after the bridge made potion indices explicit with [].
    pot = next((line[len("Potions:"):].strip() for line in body if line.startswith("Potions:")), "")
    pot = re.sub(r"\s*\(\d+/\d+ slots\)\s*$", "", pot)
    for entry in pot.split(","):
        pm = re.fullmatch(r"\s*(?:\[(\d+)\]|(\d+))\s+(.+?)\s*", entry)
        if pm:
            st.potions.append((int(pm.group(1) or pm.group(2)), pm.group(3)))

    if kind == "COMBAT":
        energy = re.search(r"Energy (\d+)/\d+", header)
        st.energy = int(energy.group(1)) if energy else 0
        inc = re.search(r"^Incoming: (\d+)(?: \((\d+) after Block\))?", text, re.M)
        if inc:
            st.incoming = int(inc.group(1))
            st.unblocked = int(inc.group(2)) if inc.group(2) is not None else st.incoming
        section = None
        for line in body[1:]:
            if line in ("Enemies:", "Hand:"):
                section = line
                continue
            if section == "Enemies:":
                em = re.match(r"^e(\d+) (.+?) (\d+)/(\d+)(?: Block (\d+))?", line)
                if em:
                    st.enemies.append(Enemy(int(em.group(1)), em.group(2), int(em.group(3)), int(em.group(4)),
                                            int(em.group(5) or 0)))
                    continue
                section = None
            if section == "Hand:":
                cm = re.match(r"^(\d+) (.+?) \((X|\d+)\) (\w+): (.*)$", line)
                if cm:
                    rest = cm.group(5)
                    st.hand.append(Card(
                        index=int(cm.group(1)), name=cm.group(2),
                        cost=int(cm.group(3)) if cm.group(3).isdigit() else None,
                        type=cm.group(4), text=rest, targets_enemy=">enemy" in rest,
                        playable="[unplayable" not in rest,
                    ))
                    continue
                um = re.match(r"^(\d+) (.+)$", line)
                if um:  # a card line in an unexpected shape: keep it, unplayable unless proven otherwise
                    st.hand.append(Card(int(um.group(1)), um.group(2), None, "?", um.group(2),
                                        ">enemy" in line, False))
                    continue
                section = None
    else:
        for line in body[1:]:
            om = re.match(r"^(\d+) (.*)$", line)
            if om:
                st.options.append((int(om.group(1)), om.group(2)))
    return st


def pick_count(header: str) -> tuple[int, int]:
    """CARD_SELECT's '(pick exactly 2)' or '(pick 0 to 1)' as (min, max)."""
    m = re.search(r"pick exactly (\d+)", header)
    if m:
        return int(m.group(1)), int(m.group(1))
    m = re.search(r"pick (\d+) to (\d+)", header)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1, 1
