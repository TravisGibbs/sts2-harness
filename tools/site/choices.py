"""Gameplay choices per model, read from the step recordings: card picks, removals, rest sites, path, risk, shops.

    python tools/site/choices.py [--manifest site_build/manifest.json] [--out site_build/choices.json]

Uses the board's run set (manifest model["runs"]) and keeps only runs that ended in the game (victory or
defeat). Each recording is reduced once to plain facts (extract) and cached in site_build/choices_cache.json,
keyed by the recording's size and mtime and by this file's source, so a rebuild only re-reads new or changed
recordings. aggregate() then turns the facts into per-model numbers.

How each fact is read (every one is checked against the game state, not inferred from the reply text):
  * The screen the model acted on is the result of its previous `act` call. Batches stop when the screen
    changes, so a call issued from a screen acts on that screen.
  * Card rewards: each CARD_REWARD screen is one offer (deduped per floor and card list). It counts as a pick
    when the deck snapshot after a call made from that screen gained one of the offered cards; otherwise a
    skip. A REWARDS screen left (to the map) with a card reward still unopened counts as a skip with no offer.
  * Card types come from the screen text ("Name (cost) Attack: ..."), collected from every screen of every run.
  * Removals: cards that left the deck on a call made from a "CARD_SELECT: ... Remove" screen.
  * Rest sites: the option taken from the REST menu (choose <#> or its name), with HP before it. Rest is
    checked by HP going up (or already full), Smith by a card gaining a "+" before the floor ends.
  * Path: the map option taken (choose <#> on a MAP screen), checked against the room type entered.
  * Elites per act: rooms entered with room_type Elite. Boss HP: HP on first entering a Boss room.
  * Potions used: drops in the potion count on calls that sent a `potion` command.
  * Shops: deck / relic / potion / gold change between entering and leaving a Shop floor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from bridge import screen_kind  # noqa: E402
from rundata import read_events  # noqa: E402

DECIDED = ("victory", "defeat")
CACHE = ROOT / "site_build" / "choices_cache.json"
OUT = ROOT / "site_build" / "choices.json"
VERSION = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]  # code change -> re-extract
LOW_HP = 0.5  # "low HP" at a rest site: below half of max HP
CARD_LINE = re.compile(r"^\d+ (?:Card: )?(.+?) \((?:X|\?|-?\d+)\) (Attack|Skill|Power|Curse|Status|Quest)\b")
OPTION = re.compile(r"^(\d+) (.*)$")
MAP_OPTION = re.compile(r"^(\d+) (.+?) \(col")
NODE_ROOM = {"Elite": "Elite", "Monster": "Monster", "Rest": "RestSite", "Shop": "Shop", "Treasure": "Treasure",
             "Boss": "Boss"}


def base(card: str) -> str:
    return card.rstrip("+")


def screen_of(result: str) -> tuple[str, list[str]]:
    """(kind, lines from the header on): `!` notes and info text ahead of the state are dropped."""
    kind = screen_kind(result)
    lines = result.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(kind):
            return kind, lines[i:]
    return kind, []


def options(lines: list[str]) -> dict[int, str]:
    out = {}
    for line in lines[1:]:
        m = OPTION.match(line)
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def commands(call: dict) -> list[str]:
    return [c.strip() for c in str((call.get("input") or {}).get("commands") or "").split(";") if c.strip()]


def first_choice(cmds: list[str]) -> int | None:
    m = re.fullmatch(r"choose (\d+)", cmds[0]) if cmds else None
    return int(m.group(1)) if m else None


def deck_diff(before: dict | None, after: dict | None) -> tuple[Counter, Counter]:
    """(added, removed) by base card name, so an upgrade is neither."""
    if not before or not after or before.get("deck") is None or after.get("deck") is None:
        return Counter(), Counter()
    b, a = Counter(map(base, before["deck"])), Counter(map(base, after["deck"]))
    return a - b, b - a


def upgrades(snap: dict | None) -> int:
    return sum(c.endswith("+") for c in (snap or {}).get("deck") or [])


def hp_frac(snap: dict) -> float | None:
    hp, mx = snap.get("hp"), snap.get("max_hp")
    return round(hp / mx, 4) if isinstance(hp, int) and mx else None


# ---------------------------------------------------------------- one recording -> facts

def extract(path: Path) -> dict:
    events = read_events(path)
    start = next(e for e in events if e["type"] == "run_start")
    end = next((e for e in reversed(events) if e["type"] == "run_end"), None)
    types: dict[str, str] = {}

    def learn(lines: list[str]) -> None:
        for line in lines:
            m = CARD_LINE.match(line)
            if m:
                types.setdefault(base(m.group(1)), m.group(2))

    offers: dict[tuple, dict] = {}
    unopened = 0
    removals: list[dict] = []
    rests: dict[int, dict] = {}
    path_choices: list[dict] = []
    potions_used = 0
    pending_rest: list[dict] = []   # rest choices waiting for their floor to end (smith check)
    pending_map: list[dict] = []    # map choices waiting for the next room
    first_snap_on_floor: dict[int, dict] = {}
    last_snap_on_floor: dict[int, dict] = {}

    prev_snap = start.get("snapshot") or {}
    prev_screen = next((e.get("state") for e in events if e["type"] == "episode" and e.get("state")), "")
    all_snaps = [prev_snap]

    def see_snapshot(snap: dict) -> None:
        f = snap.get("floor")
        if not isinstance(f, int) or f < 1 or not snap.get("run"):
            return
        first_snap_on_floor.setdefault(f, snap)
        last_snap_on_floor[f] = snap
        for mc in pending_map[:]:
            if f != mc["from_floor"]:
                room = snap.get("room_type")
                want = NODE_ROOM.get(mc["node"])
                mc["room"] = room
                mc["verified"] = want is None or room == want
                pending_map.remove(mc)

    see_snapshot(prev_snap)
    for e in events:
        if e["type"] != "step":
            continue
        snap = e.get("snapshot") or {}
        acts = [c for c in e.get("calls") or [] if c.get("tool") == "act"]
        for i, call in enumerate(acts):
            last = i == len(acts) - 1
            before, after = prev_snap, (snap if last else None)
            kind, lines = screen_of(prev_screen)
            cmds = commands(call)
            result = call.get("result") or ""
            new_kind, new_lines = screen_of(result)
            learn(new_lines)
            added, removed = deck_diff(before, after)

            if kind == "CARD_REWARD":
                cards = [base(m.group(1)) for line in lines[1:] if (m := CARD_LINE.match(line))]
                if cards:
                    key = (before.get("floor"), tuple(cards))
                    o = offers.setdefault(key, {"floor": before.get("floor"), "act": before.get("act"),
                                                "cards": cards, "picked": None})
                    took = [c for c in cards if added.get(c)]
                    if took and o["picked"] is None:
                        o["picked"] = took[0]
            elif kind == "REWARDS" and new_kind not in ("REWARDS", "CARD_REWARD", "UNKNOWN") and not added:
                # A skipped card reward stays listed on REWARDS, so only the rest were never opened.
                listed = sum("Card reward" in line for line in lines[1:])
                skipped = sum(1 for (f, _), o in offers.items() if f == before.get("floor") and not o["picked"])
                unopened += max(0, listed - skipped)
            elif kind == "CARD_SELECT" and "Remove" in (lines[0] if lines else "") and removed:
                removals.append({"floor": before.get("floor"), "room": before.get("room_type"),
                                 "cards": sorted(removed.elements())})
            elif kind == "REST" and before.get("floor") not in rests:
                opts = {k: v.split(":")[0].strip() for k, v in options(lines).items()}
                if opts and cmds:
                    k = first_choice(cmds)
                    name = opts.get(k) if k is not None else next(
                        (v for v in opts.values() if v.lower() == cmds[0].lower()), None)
                    if name:
                        rc = {"floor": before.get("floor"), "act": before.get("act"), "choice": name,
                              "hp": hp_frac(before), "upg_before": upgrades(before), "hp_before": before.get("hp"),
                              "full": before.get("hp") == before.get("max_hp")}
                        if name == "Rest" and after:
                            rc["verified"] = (after.get("hp") or 0) > (before.get("hp") or 0) or rc["full"]
                        rests[rc["floor"]] = rc
                        pending_rest.append(rc)
            elif kind == "MAP":
                k = first_choice(cmds)
                opts = {}
                for line in lines[1:]:
                    m = MAP_OPTION.match(line)
                    if m:
                        opts[int(m.group(1))] = m.group(2)
                if k is not None and k in opts:
                    mc = {"from_floor": before.get("floor"), "act": before.get("act"), "node": opts[k],
                          "offered": sorted(set(opts.values()))}
                    path_choices.append(mc)
                    pending_map.append(mc)

            if any(c.split()[0] == "potion" for c in cmds) and after:
                n_cmd = sum(c.split()[0] == "potion" for c in cmds)
                drop = len(before.get("potions") or []) - len(after.get("potions") or [])
                potions_used += max(0, min(n_cmd, drop))

            prev_screen = result
        if snap:
            see_snapshot(snap)
            all_snaps.append(snap)
            prev_snap = snap
    if end and end.get("snapshot"):
        see_snapshot(end["snapshot"])
        all_snaps.append(end["snapshot"])

    # Smith check: a card upgraded by the end of that rest floor.
    for rc in pending_rest:
        if rc["choice"] == "Smith":
            rc["verified"] = upgrades(last_snap_on_floor.get(rc["floor"])) > rc["upg_before"]
        rc.pop("upg_before", None)

    rooms = []
    for f in sorted(first_snap_on_floor):
        s = first_snap_on_floor[f]
        rooms.append({"floor": f, "act": s.get("act"), "room": s.get("room_type"), "hp": hp_frac(s)})

    shops = []
    for f, s0 in sorted(first_snap_on_floor.items()):
        if s0.get("room_type") != "Shop":
            continue
        s1 = last_snap_on_floor[f]
        added, removed = deck_diff(s0, s1)
        shops.append({
            "floor": f,
            "gold_spent": max(0, (s0.get("gold") or 0) - (s1.get("gold") or 0)),
            "cards": sorted(added.elements()),
            "removed": sorted(removed.elements()),
            "relics": [r for r in s1.get("relics") or [] if r not in (s0.get("relics") or [])],
            "potions": sorted((Counter(s1.get("potions") or []) - Counter(s0.get("potions") or [])).elements()),
        })

    final = next((s for s in reversed(all_snaps) if s.get("run") and s.get("deck") is not None), {})
    max_act = max((s.get("act") or 0) for s in all_snaps if s.get("run")) if all_snaps else 0
    for line in prev_screen.splitlines():
        m = CARD_LINE.match(line)
        if m:
            types.setdefault(base(m.group(1)), m.group(2))
    return {
        "outcome": end.get("outcome") if end else "running",
        "max_act": max_act,
        "offers": list(offers.values()),
        "unopened": unopened,
        "removals": removals,
        "rests": list(rests.values()),
        "path": path_choices,
        "rooms": rooms,
        "potions_used": potions_used,
        "shops": shops,
        "final": {"deck_size": final.get("deck_size"), "gold": final.get("gold"),
                  "potions": len(final.get("potions") or []), "floor": final.get("floor")},
        "types": types,
    }


# ---------------------------------------------------------------- cache

def load_facts(paths: list[Path], cache_path: Path = CACHE) -> tuple[dict[str, dict], int]:
    """Facts per recording path, re-extracting only recordings whose size/mtime (or this code) changed."""
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("version") != VERSION:
            cache = {}
    except (OSError, ValueError):
        cache = {}
    entries = cache.get("runs") or {}
    out, parsed = {}, 0
    for p in paths:
        st = p.stat()
        key = f"{st.st_size}:{st.st_mtime_ns}"
        hit = entries.get(str(p))
        if hit and hit.get("key") == key:
            out[str(p)] = hit["facts"]
            continue
        facts = extract(p)
        entries[str(p)] = {"key": key, "facts": facts}
        out[str(p)] = facts
        parsed += 1
    if parsed or not cache_path.exists():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"version": VERSION, "runs": entries}), encoding="utf-8")
    return out, parsed


# ---------------------------------------------------------------- facts -> per-model numbers

def rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def model_choices(facts: list[dict], types: dict[str, str]) -> dict:
    offers = [o for f in facts for o in f["offers"]]
    picks = [o["picked"] for o in offers if o["picked"]]
    unopened = sum(f["unopened"] for f in facts)
    offered, picked = Counter(), Counter(picks)
    for o in offers:
        offered.update(set(o["cards"]))
    ranked = sorted(picked, key=lambda c: (-picked[c] / offered[c], -picked[c], c))
    top = [c for c in ranked if picked[c] >= 2] or ranked
    mix = Counter(types.get(c, "Other") for c in picks)

    removals = [r for f in facts for r in f["removals"]]
    removed = [c for r in removals for c in r["cards"]]

    rests = [r for f in facts for r in f["rests"]]
    choice = Counter(r["choice"] for r in rests)
    low = [r for r in rests if r["hp"] is not None and r["hp"] < LOW_HP]
    checked = [r for r in rests if r["choice"] in ("Rest", "Smith")]

    path = [p for f in facts for p in f["path"]]
    nodes = Counter(p["node"] for p in path if p["node"] not in ("Boss", "Treasure", "Ancient"))
    elite_offers = [p for p in path if "Elite" in p["offered"] and len(p["offered"]) > 1]

    elites_by_act, boss_hp = {}, []
    for a in (1, 2, 3):
        done = [f for f in facts if f["max_act"] > a or (f["outcome"] == "victory" and f["max_act"] >= a)]
        elites_by_act[str(a)] = {
            "runs": len(done),
            "mean": mean([sum(1 for r in f["rooms"] if r["act"] == a and r["room"] == "Elite") for f in done]),
        }
    for f in facts:
        boss_hp += [r["hp"] for r in f["rooms"] if r["room"] == "Boss"]

    deaths = [f for f in facts if f["outcome"] == "defeat"]
    shops = [s for f in facts for s in f["shops"]]
    bought = Counter(c for s in shops for c in s["cards"]) + Counter(r for s in shops for r in s["relics"])
    return {
        "runs": len(facts),
        "cards": {
            "offers": len(offers), "unopened": unopened, "picked": len(picks),
            "pick_rate": rate(len(picks), len(offers) + unopened),
            "skip_rate": rate(len(offers) + unopened - len(picks), len(offers) + unopened),
            "mix": {t: rate(mix[t], len(picks)) for t in ("Attack", "Skill", "Power")},
            "top": [{"card": c, "picked": picked[c], "offered": offered[c]} for c in top[:3]],
        },
        "deck_final": mean([f["final"]["deck_size"] for f in facts]),
        "removals": {
            "total": len(removed), "per_run": rate(len(removed), len(facts)),
            "at_shop": sum(len(r["cards"]) for r in removals if r["room"] == "Shop"),
            "elsewhere": sum(len(r["cards"]) for r in removals if r["room"] != "Shop"),
            "strike_defend": rate(sum(c in ("Strike", "Defend") for c in removed), len(removed)),
        },
        "rest": {
            "visits": len(rests), "choices": dict(choice.most_common()),
            "smith_rate": rate(choice["Smith"], len(rests)), "rest_rate": rate(choice["Rest"], len(rests)),
            "low_hp_visits": len(low), "low_hp_rest_rate": rate(sum(r["choice"] == "Rest" for r in low), len(low)),
            "verified": rate(sum(bool(r.get("verified")) for r in checked), len(checked)),
        },
        "path": {
            "choices": sum(nodes.values()),
            "mix": {n: rate(nodes[n], sum(nodes.values())) for n in ("Elite", "Monster", "Unknown", "Shop", "Rest")},
            "elite_offered": len(elite_offers),
            "elite_taken": sum(p["node"] == "Elite" for p in elite_offers),
            "elite_take_rate": rate(sum(p["node"] == "Elite" for p in elite_offers), len(elite_offers)),
            "verified": rate(sum(bool(p.get("verified")) for p in path if "verified" in p),
                             sum("verified" in p for p in path)),
        },
        "elites_per_act": elites_by_act,
        "boss_hp": {"fights": len(boss_hp), "mean": mean(boss_hp)},
        "death": {"runs": len(deaths), "gold": mean([f["final"]["gold"] for f in deaths]),
                  "potions_held": mean([f["final"]["potions"] for f in deaths])},
        "potions_used_per_run": mean([f["potions_used"] for f in facts]),
        "shops": {
            "visits": len(shops),
            "gold_per_visit": mean([s["gold_spent"] for s in shops]),
            "cards": sum(len(s["cards"]) for s in shops), "relics": sum(len(s["relics"]) for s in shops),
            "potions": sum(len(s["potions"]) for s in shops), "removals": sum(len(s["removed"]) for s in shops),
            "top": [{"item": k, "n": n} for k, n in bought.most_common(3)],
        },
    }


def aggregate(man: dict, cache_path: Path = CACHE) -> dict:
    runs = {r["id"]: r for r in man["runs"]}
    chosen = {m["model"]: [runs[i] for i in m["runs"] if i in runs and runs[i]["outcome"] in DECIDED]
              for m in man["models"]}
    paths = sorted({Path(r["path"]) for rs in chosen.values() for r in rs})
    facts, parsed = load_facts(paths, cache_path)
    types: dict[str, str] = {}
    for f in facts.values():
        for k, v in f["types"].items():
            types.setdefault(k, v)
    models = []
    for m in man["models"]:
        mine = [facts[r["path"]] for r in chosen[m["model"]]]
        if not mine:
            continue
        models.append({"model": m["model"], "name": m["name"], "rank": m["rank"], "scripted": m["scripted"],
                       "baseline": m["model"].startswith("decisions:"), **model_choices(mine, types)})
    return {"version": VERSION, "runs": len(paths), "parsed": parsed, "low_hp": LOW_HP, "models": models}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", default=str(ROOT / "site_build" / "manifest.json"))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    data = aggregate(man)
    Path(args.out).write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"choices: {len(data['models'])} models, {data['runs']} runs ({data['parsed']} re-read) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
