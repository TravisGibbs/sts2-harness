"""Render recorded runs into short "generated replay" videos for the benchmark site.

    python tools/site/video.py runs   [--manifest site_build/manifest.json] [--out site_build/sts2]
                                      [--only ID[,ID...]] [--workers 3] [--force]
    python tools/site/video.py rollup [--manifest ...] [--out ...] [--force]

`runs` writes annex/videos/<id>.mp4 (960x540) and annex/videos/<id>.jpg (480px poster) per run in
the manifest; `rollup` writes rollup.mp4 (1920x1080, the 8x8 grid) and rollup.jpg at the out root.
Renders are incremental: an mp4 newer than its JSONL is skipped unless --force. Frames are drawn with
Pillow and streamed to ffmpeg (libx264) as raw RGB; the encode is pinned (threads, bitexact, no
metadata) so the same recording gives the same bytes.

Frame convention: frame k shows the state AFTER decision k (the step's own snapshot) together with
what the model sent in decision k; frame 0 is the run_start state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
from rundata import call_diagnostics, read_events  # noqa: E402

FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
FFMPEG = shutil.which("ffmpeg") or r"C:\Bin\ffmpeg\ffmpeg.exe"
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

MAX_FLOOR = 48
ACT_FLOORS = (17, 33, 48)

# ---------------------------------------------------------------- palette (dark, flat)
BG = (16, 18, 23)
PANEL = (25, 28, 35)
PANEL2 = (33, 37, 46)
LINE = (45, 50, 61)
TRACK = (38, 42, 52)
TEXT = (233, 235, 239)
MUTED = (148, 155, 168)
DIM = (92, 99, 112)
HP = (226, 75, 80)
HP_BG = (58, 33, 38)
BLOCK = (92, 160, 255)
ENERGY = (245, 186, 70)
GOLD = (236, 196, 92)
ERR = (222, 128, 124)
ACTS = [(80, 186, 138), (232, 168, 66), (170, 124, 236)]
ROOMS = {  # room_type -> (map letter, colour, label)
    "Monster": ("M", (154, 163, 180), "Monster"),
    "Elite": ("E", (236, 132, 62), "Elite"),
    "Boss": ("B", (232, 76, 82), "Boss"),
    "Event": ("?", (100, 168, 236), "Event"),
    "Shop": ("$", (236, 196, 92), "Shop"),
    "RestSite": ("R", (88, 186, 124), "Rest site"),
    "Treasure": ("T", (214, 170, 84), "Treasure"),
    "Map": ("·", (130, 137, 150), "Map"),
}
SCREEN_TITLES = {
    "COMBAT": "Combat", "MAP": "Map", "REWARDS": "Rewards", "EVENT": "Event", "CARD_REWARD": "Card reward",
    "REST": "Rest site", "CARD_SELECT": "Card select", "SHOP": "Shop", "TREASURE": "Treasure",
    "GAME_OVER": "Game over", "BUNDLE_SELECT": "Bundle select", "EVENT_SHOP": "Event shop",
}
CHIPS = {  # caption chip -> (background, text)
    "ACT": ((44, 74, 122), (214, 228, 255)),
    "INFO": ((30, 88, 88), (196, 240, 236)),
    "NOTES": ((78, 56, 120), (226, 214, 255)),
    "SAYS": ((52, 57, 68), (210, 214, 222)),
    "THINKS": ((52, 57, 68), (210, 214, 222)),
    "PICKS": ((52, 57, 68), (210, 214, 222)),
    "ERROR": ((96, 40, 42), (255, 214, 212)),
    "START": ((52, 57, 68), (210, 214, 222)),
    "TOOL": ((52, 57, 68), (210, 214, 222)),
}


def mix(a, b, t: float):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def act_of_floor(floor: int) -> int:
    for i, top in enumerate(ACT_FLOORS):
        if floor <= top:
            return i + 1
    return len(ACT_FLOORS)


@lru_cache(maxsize=None)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size)


SERIF_B = "georgiab.ttf"
SANS = "segoeui.ttf"
SANS_SB = "seguisb.ttf"
SANS_B = "segoeuib.ttf"
SANS_I = "segoeuii.ttf"
MONO = "consola.ttf"
SYM = "seguisym.ttf"


def text_w(s: str, f) -> float:
    return f.getlength(s)


def fit(s: str, f, maxw: float) -> str:
    """Truncate to fit maxw pixels, with an ellipsis."""
    if text_w(s, f) <= maxw:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_w(s[:mid].rstrip() + "…", f) <= maxw:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo].rstrip() + "…"


def one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def clock(t: float) -> str:
    t = int(t or 0)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------- reading a run
@dataclass
class Screen:
    kind: str = ""
    detail: str = ""
    options: list = field(default_factory=list)
    enemies: dict = field(default_factory=dict)  # index -> (powers, intent)
    incoming: str = ""
    you: str = ""


def parse_screen(text: str) -> Screen | None:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip() and not ln.startswith("!")]
    if not lines:
        return None
    m = re.match(r"^([A-Z_]+)(?::\s*|\s+)(.*)$", lines[0])
    if not m or m.group(1) not in SCREEN_TITLES:
        return None
    sc = Screen(kind=m.group(1), detail=re.sub(r"\s*\('[^)]*\)\s*", " ", m.group(2)).strip(" .,"))
    in_terms = False
    for ln in lines[1:]:
        if ln.startswith("Commands:"):
            break
        if ln.startswith("Terms:"):
            in_terms = True
            continue
        if in_terms:
            continue
        if sc.kind == "COMBAT":
            em = re.match(r"^e(\d+) .*?\d+/\d+(?: Block \d+)?(?: \[(.*?)\])?(?: -> (.*))?$", ln)
            if em:
                sc.enemies[int(em.group(1))] = (em.group(2) or "", em.group(3) or "")
            elif ln.startswith("Incoming:"):
                sc.incoming = ln[len("Incoming:"):].strip()
            elif ln.startswith("You:"):
                sc.you = ln[len("You:"):].strip()
        else:
            om = re.match(r"^(\d+) (.*)$", ln)
            if om:
                sc.options.append((om.group(1), om.group(2)))
    return sc


@dataclass
class Caption:
    items: list = field(default_factory=list)   # (chip, text)
    errors: list = field(default_factory=list)
    snippet: tuple | None = None                # (chip, text)


def caption_of(step: dict) -> Caption:
    cap = Caption()
    sent = []
    for c in step.get("calls") or []:
        tool = re.sub(r"<[^>]*>", "", str(c.get("tool") or "?")).strip()
        inp = c.get("input") if isinstance(c.get("input"), dict) else {}
        if tool == "act":
            cmd = one_line(str(inp.get("commands", ""))) or "(empty)"
            cap.items.append(("ACT", cmd))
            sent.append(cmd)
            d = call_diagnostics(c)
            cap.errors += d["game_errors"] + d["bridge_errors"]
            if c.get("is_error") and not d["game_errors"]:
                first = next((ln for ln in str(c.get("result") or "").splitlines() if ln.strip()), "tool error")
                cap.errors.append(first)
        elif tool.split(" ")[0] == "info":
            q = one_line(str(inp.get("query") or tool[4:]))
            cap.items.append(("INFO", f"looked up: {q}" if q else "looked up"))
            if c.get("is_error"):
                cap.errors.append(one_line(str(c.get("result") or "lookup failed")))
        elif tool == "set_notes":
            cap.items.append(("NOTES", "updated notes: " + one_line(str(inp.get("notes", "")))))
        else:
            cap.items.append(("TOOL", tool))
    if step.get("invalid"):
        cap.errors.append("reply could not be parsed as a command")
    if step.get("fallback"):
        cap.errors.append("fell back to a default command")
    cap.errors = [one_line(e.lstrip("! ")) for e in cap.errors if one_line(e)]

    dr = step.get("decision_response")
    text, thinking = one_line(step.get("text")), one_line(step.get("thinking"))
    norm = lambda s: re.sub(r"[\s;]+", " ", s.lower()).strip()  # noqa: E731
    if isinstance(dr, dict) and len(dr.get("probabilities") or {}) > 1:
        probs = dr["probabilities"]
        p = probs.get(dr.get("choice"), 0)
        cap.snippet = ("PICKS", f"{dr.get('command', '?')} at p={p:.2f} from {len(probs)} candidate"
                                f"{'s' if len(probs) != 1 else ''}")
    elif text and norm(text) not in {norm(s) for s in sent} | {norm("; ".join(sent))}:
        cap.snippet = ("SAYS", text)
    elif thinking:
        cap.snippet = ("THINKS", thinking)
    if cap.snippet and len(cap.snippet[1]) > 150:
        cap.snippet = (cap.snippet[0], cap.snippet[1][:149].rstrip() + "…")
    return cap


@dataclass
class State:
    k: int
    snap: dict
    t: float
    cost: float
    screen: Screen | None
    caption: Caption | None


@dataclass
class Run:
    id: str
    name: str
    seed: str
    attempt: int
    model: str
    outcome: str
    final_floor: int
    states: list
    end: dict
    start: dict
    reason: str
    killer: str


def stop_reason(events: list, start: dict, end: dict) -> str:
    outcome = end.get("outcome", "running")
    wd = next((e for e in reversed(events) if e.get("type") == "watchdog"), None) or end.get("stop_detail") or {}
    why = wd.get("reason")
    if outcome == "stalled":
        return {
            "no_tool_call": "stopped sending commands",
            "repeated_invalid_actions_unchanged_state": "repeated invalid moves with no progress",
        }.get(why, "made no progress")
    if outcome == "cost_limit":
        cap = (start.get("limits") or {}).get("max_cost")
        return f"reached the ${cap:.2f} budget" if cap else "reached its budget"
    if outcome == "request_limit":
        cap = (start.get("limits") or {}).get("max_requests")
        return f"reached {cap} requests" if cap else "reached the request limit"
    return outcome.replace("_", " ")


def load_run(rec: dict) -> Run:
    events = read_events(Path(rec["path"]))
    start = next((e for e in events if e.get("type") == "run_start"), {})
    end = next((e for e in reversed(events) if e.get("type") == "run_end"), {}) or {}
    outcome = rec.get("outcome") or end.get("outcome") or "running"
    first_ep = next((e for e in events if e.get("type") == "episode"), {})
    screen = parse_screen(first_ep.get("state", ""))
    prev = dict(start.get("snapshot") or {})
    states = [State(0, prev, 0.0, 0.0, screen, None)]
    cost = 0.0
    k = 0
    for e in events:
        if e.get("type") != "step":
            continue
        k += 1
        snap = dict(e.get("snapshot") or prev)
        # The game reports 0 HP on the post-victory screen; keep the HP the run won with.
        if snap.get("screen") == "GAME_OVER" and outcome == "victory":
            snap["hp"] = prev.get("hp", snap.get("hp"))
        for c in e.get("calls") or []:
            if c.get("tool") == "act" and c.get("result"):
                screen = parse_screen(c["result"]) or screen
        cost += e.get("cost") or 0.0
        states.append(State(k, snap, float(e.get("t") or states[-1].t), cost, screen, caption_of(e)))
        prev = snap
    killer = ""
    if outcome == "defeat":
        for st in reversed(states):
            enemies = ((st.snap.get("combat") or {}).get("enemies")) or []
            if st.snap.get("in_combat") and enemies:
                names = []
                for en in enemies:
                    n = en.get("name") or "?"
                    if (en.get("hp") or 0) > 0 and n not in names:
                        names.append(n)
                killer = ", ".join(names[:3])
                break
    return Run(rec["id"], rec.get("name") or start.get("model", "?"), rec.get("seed") or start.get("seed", "?"),
               rec.get("attempt") or 1, rec.get("model") or start.get("model", "?"), outcome,
               rec.get("final_floor") or end.get("floor") or prev.get("floor") or 0, states, end, start,
               stop_reason(events, start, end), killer)


# ---------------------------------------------------------------- drawing helpers
def rrect(d: ImageDraw.ImageDraw, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def bar(d, x0, y0, x1, y1, frac, fg, bg, r=None):
    r = (y1 - y0) // 2 if r is None else r
    rrect(d, (x0, y0, x1, y1), r, fill=bg)
    frac = max(0.0, min(1.0, frac))
    if frac > 0:
        xe = x0 + (x1 - x0) * frac
        if xe - x0 < 2 * r + 3:
            d.rectangle((x0, y0, max(x0 + 1, xe), y1), fill=fg)
        else:
            rrect(d, (x0, y0, xe, y1), r, fill=fg)


def chip(d, x, y_mid, label, f=None) -> float:
    """Small rounded label; returns its right edge."""
    f = f or font(SANS_B, 11)
    bgc, fgc = CHIPS.get(label, CHIPS["TOOL"])
    w = text_w(label, f) + 14
    rrect(d, (x, y_mid - 10, x + w, y_mid + 10), 5, fill=bgc)
    d.text((x + 7, y_mid), label, font=f, fill=fgc, anchor="lm")
    return x + w


def small_caps(d, xy, s, fill=MUTED, size=11, anchor="ls"):
    d.text(xy, s.upper(), font=font(SANS_SB, size), fill=fill, anchor=anchor)


def room_badge(d, cx, cy, room: str, r=13):
    letter, col, _ = ROOMS.get(room, ("·", MUTED, room or "?"))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=mix(PANEL, col, 0.22), outline=col, width=2)
    d.text((cx, cy + 1), letter, font=font(SANS_B, int(r * 1.15)), fill=col, anchor="mm")


# ---------------------------------------------------------------- per-run frames (960x540)
W, H = 960, 540
PAD = 24


def draw_floor_track(d, floor: int, y0: int, y1: int, x0=PAD, x1=W - PAD):
    gap = 2
    cw = (x1 - x0 - gap * (MAX_FLOOR - 1)) / MAX_FLOOR
    for f in range(1, MAX_FLOOR + 1):
        a = act_of_floor(f) - 1
        cx0 = x0 + (f - 1) * (cw + gap)
        cx1 = cx0 + cw
        boss = f in ACT_FLOORS
        if f < floor:
            col = mix(PANEL, ACTS[a], 0.55)
            d.rectangle((cx0, y0, cx1, y1), fill=col)
        elif f == floor:
            d.rectangle((cx0, y0 - 4, cx1, y1 + 4), fill=ACTS[a])
        else:
            d.rectangle((cx0, y0, cx1, y1), fill=TRACK)
        if boss and f > floor:
            d.rectangle((cx0, y0, cx1, y1), outline=mix(TRACK, ROOMS["Boss"][1], 0.7), width=1)
    # act labels centred under each act's span; boss floor numbers under the boss cells
    lo = 1
    for i, top in enumerate(ACT_FLOORS):
        lx = x0 + (lo - 1) * (cw + gap)
        bx = x0 + (top - 1) * (cw + gap) + cw / 2
        col = ACTS[i] if floor >= lo else DIM
        small_caps(d, ((lx + bx) / 2, y1 + 18), f"Act {i + 1}", fill=col, size=11, anchor="ms")
        small_caps(d, (bx, y1 + 18), f"boss {top}", fill=mix(DIM, ROOMS["Boss"][1], 0.45 if floor < top else 0.9),
                   size=10, anchor="rs" if i == len(ACT_FLOORS) - 1 else "ms")
        lo = top + 1


def draw_top(d, run: Run, st: State, total: int):
    d.text((PAD, 40), fit(run.name, font(SERIF_B, 26), 520), font=font(SERIF_B, 26), fill=TEXT, anchor="ls")
    sub = f"{run.seed}  ·  attempt {run.attempt}  ·  Ironclad, Ascension 0"
    d.text((PAD, 62), sub, font=font(SANS, 14), fill=MUTED, anchor="ls")
    right = W - PAD
    dec_f = font(SANS_SB, 18)
    d.text((right, 38), f"{total}", font=dec_f, fill=MUTED, anchor="rs")
    wt = text_w(f"{total}", dec_f)
    d.text((right - wt - 4, 38), f"Decision {st.k} /", font=dec_f, fill=TEXT, anchor="rs")
    extra = f"game clock {clock(st.t)}"
    if run.model and not run.model.startswith("policy:"):
        extra += f"   ·   ${st.cost:.2f} spent"
    d.text((right, 62), extra, font=font(SANS, 14), fill=MUTED, anchor="rs")
    floor = st.snap.get("floor") or 0
    draw_floor_track(d, floor, 80, 90)


def draw_spark(d, hist: list, total: int, x0, y0, x1, y1):
    """hist: (hp fraction, act) per state so far; drawn as act-coloured columns over a dark band."""
    d.rectangle((x0, y0, x1, y1), fill=(30, 33, 41))
    if not hist or total <= 0:
        return
    w = x1 - x0
    hgt = y1 - y0
    lx = x0 + int((len(hist) - 1) * (w - 1) / total)
    for cx in range(0, lx - x0 + 1):
        i = min(len(hist) - 1, int(cx * total / (w - 1)))
        fr, a = hist[i]
        top = y1 - hgt * max(0.0, min(1.0, fr))
        d.line((x0 + cx, top, x0 + cx, y1), fill=mix((30, 33, 41), ACTS[a - 1], 0.45))
        d.point((x0 + cx, top), fill=ACTS[a - 1])
    d.line((lx, y0, lx, y1), fill=MUTED)


def draw_player(d, st: State, hp_hist: list, total: int):
    s = st.snap
    x0, y0, x1, y1 = PAD, 124, 318, 414
    rrect(d, (x0, y0, x1, y1), 10, fill=PANEL)
    ix = x0 + 16
    hp, mhp = s.get("hp"), s.get("max_hp")
    combat = s.get("combat") if s.get("in_combat") else None
    small_caps(d, (ix, y0 + 26), "Hit points")
    if isinstance(hp, int) and isinstance(mhp, int) and mhp > 0:
        hf = font(SANS_SB, 40)
        d.text((ix, y0 + 72), str(hp), font=hf, fill=TEXT, anchor="ls")
        d.text((ix + text_w(str(hp), hf) + 6, y0 + 72), f"/ {mhp}", font=font(SANS, 20), fill=MUTED, anchor="ls")
        bar(d, ix, y0 + 84, x1 - 16, y0 + 96, hp / mhp, HP, HP_BG)
    else:
        d.text((ix, y0 + 72), "—", font=font(SANS_SB, 40), fill=MUTED, anchor="ls")
    if combat and (combat.get("block") or 0) > 0:
        bx = x1 - 16
        bt = str(combat["block"])
        bf = font(SANS_SB, 18)
        w = text_w(bt, bf) + 34
        rrect(d, (bx - w, y0 + 48, bx, y0 + 74), 6, fill=mix(PANEL, BLOCK, 0.25))
        d.text((bx - w + 9, y0 + 62), "⛨", font=font(SYM, 15), fill=BLOCK, anchor="lm")
        d.text((bx - 9, y0 + 62), bt, font=bf, fill=BLOCK, anchor="rm")
    # energy
    ey = y0 + 126
    small_caps(d, (ix, ey), "Energy")
    mx = s.get("max_energy") or 3
    cur = combat.get("energy") if combat else None
    px = ix + 70
    pips = max(mx, cur or 0)
    for i in range(min(pips, 8)):
        cx = px + i * 22
        on = cur is not None and i < cur
        d.ellipse((cx, ey - 13, cx + 15, ey + 2), fill=ENERGY if on else TRACK,
                  outline=mix(TRACK, ENERGY, 0.5) if not on else None)
    if cur is None:
        d.text((px + min(pips, 8) * 22 + 4, ey), "out of combat", font=font(SANS, 12), fill=DIM, anchor="ls")
    # one row of stats
    cells = [("Gold", s.get("gold"), GOLD), ("Deck", s.get("deck_size"), TEXT),
             ("Relics", len(s.get("relics") or []), TEXT), ("Potions", len(s.get("potions") or []), TEXT)]
    colw = (x1 - 16 - ix) / 4
    for i, (lab, val, col) in enumerate(cells):
        cx = ix + i * colw
        small_caps(d, (cx, y0 + 158), lab)
        d.text((cx, y0 + 186), "—" if val is None else str(val), font=font(SANS_SB, 22), fill=col, anchor="ls")
    # HP over the run so far, on a fixed x scale of the whole run
    small_caps(d, (ix, y0 + 214), "HP over the run")
    draw_spark(d, hp_hist, total, ix, y0 + 222, x1 - 16, y0 + 252)
    pots = s.get("potions") or []
    if pots:
        line = "Potions: " + ", ".join(pots)
    elif s.get("relics"):
        line = "Newest relic: " + s["relics"][-1]
    else:
        line = ""
    d.text((ix, y1 - 16), fit(line, font(SANS, 13), x1 - ix - 14), font=font(SANS, 13), fill=MUTED, anchor="ls")


def draw_main(d, st: State):
    s = st.snap
    x0, y0, x1, y1 = 334, 124, W - PAD, 414
    rrect(d, (x0, y0, x1, y1), 10, fill=PANEL)
    ix, rx = x0 + 18, x1 - 18
    room = s.get("room_type") or ""
    if room in ROOMS and room != "Map":
        room_badge(d, ix + 13, y0 + 24, room)
        d.text((ix + 34, y0 + 30), ROOMS[room][2], font=font(SANS_SB, 16), fill=TEXT, anchor="ls")
    else:
        d.text((ix, y0 + 30), "Between rooms" if room == "Map" else (room or "—"), font=font(SANS_SB, 16),
               fill=MUTED, anchor="ls")
    floor = s.get("floor") or 0
    if floor:
        a = act_of_floor(floor)
        ft = f"Floor {floor}"
        ff = font(SANS_SB, 16)
        d.text((rx, y0 + 30), f"Act {a}", font=font(SANS, 14), fill=ACTS[a - 1], anchor="rs")
        d.text((rx - text_w(f"Act {a}", font(SANS, 14)) - 10, y0 + 30), ft, font=ff, fill=TEXT, anchor="rs")
    combat = s.get("combat") if s.get("in_combat") else None
    sc = st.screen
    kind = s.get("screen") or (sc.kind if sc else "")
    if combat:
        title = f"Combat  ·  turn {combat.get('turn', '?')}"
    else:
        title = SCREEN_TITLES.get(kind, kind.replace("_", " ").title() or "—")
        if sc and sc.kind == kind and sc.detail and kind not in ("GAME_OVER",):
            det = sc.detail[0].upper() + sc.detail[1:]
            title = f"{title}  ·  {det}"
        elif kind == "GAME_OVER" and sc and sc.kind == "GAME_OVER":
            title = sc.detail.split(" on floor")[0].title() if sc.detail else title
    tf = font(SERIF_B, 21)
    d.text((ix, y0 + 66), fit(title, tf, rx - ix - (190 if combat else 0)), font=tf, fill=TEXT, anchor="ls")
    if combat and sc and sc.kind == "COMBAT" and sc.you:
        d.text((rx, y0 + 66), fit("You: " + sc.you, font(SANS, 13), 200), font=font(SANS, 13), fill=MUTED,
               anchor="rs")
    d.line((ix, y0 + 80, rx, y0 + 80), fill=LINE, width=1)
    if combat:
        draw_enemies(d, combat, sc if sc and sc.kind == "COMBAT" else None, ix, y0 + 92, rx, y1 - 36)
        piles = (f"Hand {combat.get('hand', 0)}   ·   Draw {combat.get('draw', 0)}   ·   "
                 f"Discard {combat.get('discard', 0)}   ·   Exhaust {combat.get('exhaust', 0)}")
        d.text((rx, y1 - 14), piles, font=font(SANS, 13), fill=MUTED, anchor="rs")
        if sc and sc.kind == "COMBAT" and sc.incoming:
            inc = sc.incoming
            d.text((ix, y1 - 14), "Incoming " + inc, font=font(SANS_SB, 13),
                   fill=ERR if not inc.startswith("0") else MUTED, anchor="ls")
    elif sc and sc.kind == kind and sc.options:
        of = font(SANS, 15)
        y = y0 + 112
        shown = sc.options[:6] if len(sc.options) <= 6 else sc.options[:5]
        for idx, txt in shown:
            rrect(d, (ix, y - 15, ix + 26, y + 5), 5, fill=PANEL2)
            d.text((ix + 13, y - 5), idx, font=font(SANS_SB, 12), fill=MUTED, anchor="mm")
            right = rx
            pm = re.search(r"\s+-\s+(\d+g)\s*(\[[^\]]*\])?$", txt)
            if pm:  # shop price in its own right-aligned column; dim when the game says it is unaffordable
                d.text((rx, y), pm.group(1), font=font(SANS_SB, 15), fill=DIM if pm.group(2) else GOLD, anchor="rs")
                right = rx - 56
                txt = txt[:pm.start()]
            d.text((ix + 36, y), fit(txt, of, right - ix - 36), font=of, fill=TEXT, anchor="ls")
            y += 30
        more = len(sc.options) - len(shown)
        if more > 0:
            d.text((ix + 36, y), f"+ {more} more", font=font(SANS, 13), fill=DIM, anchor="ls")
    else:
        msg = {"MAP": "Choosing the next room", "GAME_OVER": "The run is over"}.get(kind, "")
        if msg:
            d.text((ix, y0 + 116), msg, font=font(SANS, 15), fill=MUTED, anchor="ls")


def draw_enemies(d, combat: dict, sc: Screen | None, x0, y0, x1, y1):
    enemies = combat.get("enemies") or []
    if not enemies:
        d.text((x0, y0 + 24), "No enemies left", font=font(SANS, 15), fill=MUTED, anchor="ls")
        return
    n = min(len(enemies), 5)
    row = min(64, (y1 - y0) / n)
    top = y0 + max(0, ((y1 - y0) - row * n) / 2 - 6)
    bh = 14 if n <= 2 else 10
    nf = font(SANS_SB, 15)
    for i, en in enumerate(enemies[:n]):
        y = top + i * row
        name = en.get("name") or "?"
        hp, mhp, blk = en.get("hp") or 0, en.get("max_hp") or 0, en.get("block") or 0
        powers, intent = sc.enemies.get(i, ("", "")) if sc else ("", "")
        dead = hp <= 0
        intent_txt = "" if dead else intent
        iw = 0
        if intent_txt:
            itf = font(SANS_SB, 14)
            label = "→ " + intent_txt
            label = fit(label, itf, 190)
            iw = text_w(label, itf)
            d.text((x1, y + 18), label, font=itf, fill=ERR if intent_txt.startswith(("Attack", "DeathBlow")) else MUTED,
                   anchor="rs")
        nm = fit(name, nf, x1 - x0 - iw - 20)
        d.text((x0, y + 18), nm, font=nf, fill=DIM if dead else TEXT, anchor="ls")
        if powers and not dead:
            px = x0 + text_w(nm, nf) + 10
            room = x1 - iw - 16 - px
            if room > 40:
                d.text((px, y + 18), fit(powers, font(SANS, 12), room), font=font(SANS, 12), fill=DIM, anchor="ls")
        bx1 = x1 - 150
        bar(d, x0, y + 26, bx1, y + 26 + bh, hp / mhp if mhp else 0, HP if not dead else DIM, HP_BG)
        d.text((bx1 + 12, y + 26 + bh), f"{max(hp, 0)} / {mhp}", font=font(SANS_SB, 14),
               fill=TEXT if not dead else DIM, anchor="ls")
        if blk > 0:
            d.text((x1, y + 26 + bh), f"⛨ {blk}", font=font(SYM, 14), fill=BLOCK, anchor="rs")
    if len(enemies) > n:
        d.text((x1, y1), f"+{len(enemies) - n} more", font=font(SANS, 12), fill=DIM, anchor="rs")


def draw_caption(d, st: State):
    x0, y0, x1, y1 = PAD, 426, W - PAD, H - 14
    rrect(d, (x0, y0, x1, y1), 10, fill=PANEL)
    ix, rx = x0 + 14, x1 - 14
    cap = st.caption
    rows_y = [y0 + 22, y0 + 52, y0 + 80]
    if cap is None:
        x = chip(d, ix, rows_y[0], "START")
        d.text((x + 10, rows_y[0]), "Run begins. Each frame shows the state after one decision.",
               font=font(SANS, 15), fill=MUTED, anchor="lm")
        return
    mono = font(MONO, 16)
    x = ix
    if not cap.items:
        x = chip(d, x, rows_y[0], "ACT")
        d.text((x + 10, rows_y[0]), "no command sent", font=font(SANS_I, 15), fill=DIM, anchor="lm")
    item_rows = 3 - bool(cap.errors) - bool(cap.snippet)
    r = 0
    for j, (label, txt) in enumerate(cap.items):
        if x > rx - 120:  # no room left on this row: next row if there is one
            if r + 1 < item_rows:
                r, x = r + 1, ix
            else:
                d.text((rx, rows_y[r]), "…", font=mono, fill=MUTED, anchor="rm")
                break
        x = chip(d, x, rows_y[r], label)
        f = mono if label == "ACT" else font(SANS, 15)
        col = TEXT if label == "ACT" else (MUTED if label != "INFO" else (178, 226, 222))
        tx = x + 9
        rest = txt
        last_item = j == len(cap.items) - 1
        while True:
            # wrap the last item onto spare rows (aligned under its first line); else truncate
            if text_w(rest, f) <= rx - tx or r + 1 >= item_rows or not last_item:
                t = fit(rest, f, rx - tx)
                d.text((tx, rows_y[r]), t, font=f, fill=col, anchor="lm")
                x = tx + text_w(t, f) + 16
                break
            cut = len(fit(rest, f, rx - tx)) - 1
            sp = max(rest.rfind(" ", 0, cut), rest.rfind(";", 0, cut) + 1)
            cut = sp if sp > cut // 3 else cut
            d.text((tx, rows_y[r]), rest[:cut].rstrip(), font=f, fill=col, anchor="lm")
            rest = rest[cut:].lstrip()
            r += 1
    r += 1
    if cap.errors:
        x = chip(d, ix, rows_y[r], "ERROR")
        err = cap.errors[0] + (f"  (+{len(cap.errors) - 1} more)" if len(cap.errors) > 1 else "")
        d.text((x + 10, rows_y[r]), fit(err, font(SANS, 14), rx - x - 10), font=font(SANS, 14), fill=ERR,
               anchor="lm")
        r += 1
    if cap.snippet:
        label, txt = cap.snippet
        x = chip(d, ix, rows_y[r], label)
        f = font(SANS_I, 14) if label != "PICKS" else font(SANS, 14)
        q = f"“{txt}”" if label in ("SAYS", "THINKS") else txt
        d.text((x + 10, rows_y[r]), fit(q, f, rx - x - 10), font=f, fill=MUTED, anchor="lm")


def hp_history(run: Run) -> list:
    out, last = [], (1.0, 1)
    for st in run.states:
        hp, mhp = st.snap.get("hp"), st.snap.get("max_hp")
        a = act_of_floor(st.snap.get("floor") or 1)
        last = (hp / mhp, a) if isinstance(hp, (int, float)) and mhp else (last[0], a)
        out.append(last)
    return out


def render_state(run: Run, st: State, total: int, hist: list) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    draw_top(d, run, st, total)
    draw_player(d, st, hist[:st.k + 1], total)
    draw_main(d, st)
    draw_caption(d, st)
    return im


def render_end_card(run: Run, last: Image.Image) -> Image.Image:
    im = Image.blend(last, Image.new("RGB", (W, H), BG), 0.78)
    d = ImageDraw.Draw(im)
    cw, ch = 600, 280
    x0, y0 = (W - cw) // 2, (H - ch) // 2
    o = run.outcome
    if o == "victory":
        accent, head, sub = GOLD, "VICTORY", "Beat the Act 3 boss on floor 48"
    elif o == "defeat":
        accent, head = (214, 104, 104), "DEFEAT"
        sub = f"Died on floor {run.final_floor}, act {act_of_floor(run.final_floor or 1)}"
        if run.killer:
            sub += f"  ·  {run.killer}"
    else:
        accent, head = (176, 182, 194), o.replace("_", " ").upper()
        sub = f"Cut short on floor {run.final_floor}: {run.reason}"
    rrect(d, (x0, y0, x0 + cw, y0 + ch), 14, fill=PANEL, outline=mix(PANEL, accent, 0.8), width=2)
    d.text((W // 2, y0 + 44), run.name, font=font(SANS_SB, 16), fill=MUTED, anchor="ms")
    d.text((W // 2, y0 + 112), head, font=font(SERIF_B, 58), fill=accent, anchor="ms")
    d.text((W // 2, y0 + 148), fit(sub, font(SANS, 17), cw - 40), font=font(SANS, 17), fill=TEXT, anchor="ms")
    last_st = run.states[-1]
    hp, mhp = last_st.snap.get("hp"), last_st.snap.get("max_hp")
    cost = run.end.get("cost") if run.end.get("cost") is not None else last_st.cost
    stats = [("Floor", str(run.final_floor)), ("HP", f"{hp}/{mhp}" if hp is not None else "—"),
             ("Decisions", str(len(run.states) - 1)), ("Game clock", clock(last_st.t))]
    if not run.model.startswith("policy:"):
        stats.append(("Cost", f"${cost:.2f}"))
    colw = (cw - 40) / len(stats)
    for i, (lab, val) in enumerate(stats):
        cx = x0 + 20 + colw * (i + 0.5)
        small_caps(d, (cx, y0 + 202), lab, anchor="ms")
        d.text((cx, y0 + 234), val, font=font(SANS_SB, 22), fill=TEXT, anchor="ms")
    d.text((W // 2, y0 + ch - 16), f"{run.seed}  ·  attempt {run.attempt}", font=font(SANS, 13), fill=DIM,
           anchor="ms")
    return im


# ---------------------------------------------------------------- encoding
def ffmpeg_cmd(w, h, fps, out: Path, crf: int, gop: int, threads: int) -> list:
    return [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
            "-an", "-c:v", "libx264", "-preset", "slow", "-tune", "animation", "-crf", str(crf),
            "-me_method", "umh", "-me_range", "24",
            "-g", str(gop), "-pix_fmt", "yuv420p", "-threads", str(threads),
            "-x264-params", "threads=%d:lookahead-threads=1" % threads,
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1", "-map_chapters", "-1",
            "-metadata", "encoder=", "-movflags", "+faststart", "-f", "mp4", str(out)]


class Encoder:
    def __init__(self, w, h, fps, out: Path, crf: int, gop: int, threads: int = 2):
        self.out = out
        self.tmp = out.with_name(out.stem + ".part.mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        self.p = subprocess.Popen(ffmpeg_cmd(w, h, fps, self.tmp, crf, gop, threads), stdin=subprocess.PIPE,
                                  stderr=subprocess.PIPE, creationflags=BELOW_NORMAL)
        self.frames = 0

    def write(self, im: Image.Image, repeat: int = 1):
        b = im.tobytes()
        for _ in range(repeat):
            self.p.stdin.write(b)
        self.frames += repeat

    def close(self):
        self.p.stdin.close()
        err = self.p.stderr.read().decode("utf-8", "replace")
        if self.p.wait() != 0:
            raise RuntimeError(f"ffmpeg failed for {self.out.name}: {err.strip()[-400:]}")
        os.replace(self.tmp, self.out)

    def abort(self):
        try:
            self.p.kill()
            self.p.wait()
        finally:
            self.tmp.unlink(missing_ok=True)


def save_poster(im: Image.Image, path: Path, width: int):
    h = round(im.height * width / im.width)
    im.resize((width, h), Image.LANCZOS).save(path, "JPEG", quality=80, optimize=True, progressive=False)


def lower_priority():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        except Exception:
            pass


# ---------------------------------------------------------------- per-run video
RUN_FPS = 6
FRAMES_PER_STEP = 2
HOLD_FIRST = 6
HOLD_END = 12
RUN_CRF = 32
RUN_GOP = RUN_FPS * 30  # keyframe every 30 s: seeking stays cheap, files stay small


def render_run(rec: dict, out_dir: Path) -> dict:
    lower_priority()
    t0 = time.time()
    run = load_run(rec)
    vid = out_dir / f"{rec['id']}.mp4"
    total = len(run.states) - 1
    enc = Encoder(W, H, RUN_FPS, vid, crf=RUN_CRF, gop=RUN_GOP, threads=2)
    try:
        last = None
        hist = hp_history(run)
        for i, st in enumerate(run.states):
            last = render_state(run, st, total, hist)
            enc.write(last, HOLD_FIRST if i == 0 else FRAMES_PER_STEP)
        card = render_end_card(run, last)
        enc.write(card, HOLD_END)
    except BaseException:
        enc.abort()
        raise
    enc.close()
    save_poster(card, out_dir / f"{rec['id']}.jpg", 480)
    return {"id": rec["id"], "steps": total, "frames": enc.frames, "bytes": vid.stat().st_size,
            "seconds": round(time.time() - t0, 1)}


def up_to_date(out: Path, inputs: list[Path]) -> bool:
    if not out.exists():
        return False
    m = out.stat().st_mtime
    return all(p.exists() and p.stat().st_mtime < m for p in inputs)


def cmd_runs(args, manifest: dict) -> int:
    out_dir = Path(args.out) / "annex" / "videos"
    runs = manifest["runs"]
    if args.only:
        want = [w.strip() for w in args.only.split(",") if w.strip()]
        ids = {r["id"] for r in runs}
        runs = [r for r in runs if r["id"] in want or any(w not in ids and w in r["id"] for w in want)]
        if not runs:
            print("no run matches --only", file=sys.stderr)
            return 1
    todo = []
    for r in runs:
        vid, jpg = out_dir / f"{r['id']}.mp4", out_dir / f"{r['id']}.jpg"
        if not args.force and jpg.exists() and up_to_date(vid, [Path(r["path"])]):
            continue
        todo.append(r)
    print(f"{len(runs) - len(todo)} up to date, {len(todo)} to render -> {out_dir}")
    if not todo:
        return 0
    t0 = time.time()
    failed = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers), initializer=lower_priority) as ex:
        futs = {ex.submit(render_run, r, out_dir): r["id"] for r in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                res = fut.result()
                print(f"[{i}/{len(todo)}] {res['id']}: {res['steps']} steps, {res['frames'] / RUN_FPS:.0f}s video, "
                      f"{res['bytes'] / 1024:.0f} KB, rendered in {res['seconds']}s", flush=True)
            except Exception as e:  # keep going; report at the end
                failed += 1
                print(f"[{i}/{len(todo)}] {futs[fut]}: FAILED {e}", file=sys.stderr, flush=True)
    print(f"done in {time.time() - t0:.0f}s, {failed} failed")
    return 1 if failed else 0


# ---------------------------------------------------------------- rollup (1920x1080)
RW, RH = 1920, 1080
R_FPS = 24
R_HOLD_START = 36
R_HOLD_END = 3 * R_FPS
GRID_X0, GRID_X1 = 316, RW - 40
GRID_Y0, GRID_Y1 = 218, 1022
COL_GAP, ROW_GAP = 12, 10
TILE_W = (GRID_X1 - GRID_X0 - COL_GAP * 7) // 8
TILE_H = (GRID_Y1 - GRID_Y0 - ROW_GAP * 7) // 8
FLASH = 18  # frames of highlight when a run ends

TILE_SCREENS = {  # screen -> (label, colour)
    "MAP": ("map", (130, 137, 150)), "REWARDS": ("reward", (196, 200, 210)),
    "CARD_REWARD": ("reward", (196, 200, 210)), "EVENT": ("event", ROOMS["Event"][1]),
    "EVENT_SHOP": ("event", ROOMS["Event"][1]), "REST": ("rest", ROOMS["RestSite"][1]),
    "SHOP": ("shop", ROOMS["Shop"][1]), "TREASURE": ("chest", ROOMS["Treasure"][1]),
    "CARD_SELECT": ("cards", (196, 200, 210)), "BUNDLE_SELECT": ("cards", (196, 200, 210)),
}


def tile_screen(snap: dict):
    if snap.get("in_combat") or snap.get("screen") == "COMBAT":
        room = snap.get("room_type")
        if room == "Boss":
            return "boss", ROOMS["Boss"][1]
        if room == "Elite":
            return "elite", ROOMS["Elite"][1]
        return "fight", (206, 112, 112)
    return TILE_SCREENS.get(snap.get("screen"), ("", MUTED))


@dataclass
class Track:
    run: Run
    pts: list  # per state: (floor, hp, max_hp, snap)


def draw_tile(run: Run, snap: dict, hp: float, mhp: float, ended: bool, flash: float) -> Image.Image:
    tw, th = TILE_W, TILE_H
    floor = (run.final_floor if ended else snap.get("floor")) or 0
    o = run.outcome
    label, scol = tile_screen(snap)
    if ended:
        if o == "victory":
            bg, num_col, label, scol = mix(PANEL, GOLD, 0.12), GOLD, "WIN", GOLD
        elif o == "defeat":
            bg, num_col, label, scol = (20, 22, 27), (176, 124, 124), "died", (150, 112, 112)
        else:
            bg, num_col, label, scol = (24, 26, 31), (150, 156, 168), "stopped", (140, 146, 158)
    else:
        bg, num_col = mix(PANEL, scol, 0.08) if label else PANEL, TEXT
    im = Image.new("RGB", (tw, th), BG)
    d = ImageDraw.Draw(im)
    border = None
    if ended and o == "victory":
        border = GOLD
    if flash > 0:
        fc = GOLD if o == "victory" else ((236, 96, 96) if o == "defeat" else (200, 204, 214))
        border = mix(border or bg, fc, flash)
    rrect(d, (0, 0, tw - 1, th - 1), 8, fill=bg, outline=border, width=3 if border else 1)
    ix, rx = 12, tw - 12
    small_caps(d, (ix, 20), "floor", fill=DIM if not ended else mix(bg, num_col, 0.5), size=10)
    nf = font(SANS_SB, 36)
    if ended and o == "defeat":
        d.text((ix - 1, 56), "✝", font=font(SYM, 26), fill=num_col, anchor="ls")
        d.text((ix + 24, 56), str(floor), font=nf, fill=num_col, anchor="ls")
    else:
        d.text((ix - 1, 56), str(floor), font=nf, fill=num_col, anchor="ls")
    if label:
        d.text((rx, 22), label.upper() if ended else label, font=font(SANS_B if ended else SANS_SB, 13),
               fill=scol, anchor="rs")
    if mhp and not (ended and o == "defeat"):
        hp_txt = f"{int(round(hp))}/{int(mhp)}"
        d.text((rx, 52), hp_txt, font=font(SANS, 15), fill=MUTED if not ended else DIM, anchor="rs")
    # floor progress (coloured by act) and HP
    by0, by1 = th - 30, th - 25
    span = rx - ix
    dim = 0.45 if ended and o != "victory" else 1.0
    d.rectangle((ix, by0, rx, by1), fill=TRACK)
    lo = 0
    for a, top in enumerate(ACT_FLOORS):
        if floor > lo:
            hi = min(floor, top)
            d.rectangle((ix + span * lo / MAX_FLOOR, by0, ix + span * hi / MAX_FLOOR, by1),
                        fill=mix(TRACK, ACTS[a], dim))
        lo = top
    for top in ACT_FLOORS[:-1]:
        tx = ix + span * top / MAX_FLOOR
        d.line((tx, by0 - 2, tx, by1 + 2), fill=bg, width=2)
    frac = hp / mhp if mhp else 0
    bar(d, ix, th - 18, rx, th - 11, frac, mix(TRACK, HP, dim), HP_BG if not ended else (36, 30, 33))
    return im


def run_state_at(tr: Track, p: float):
    """State at fractional decision p: (snap, hp, max_hp, ended)."""
    n = len(tr.pts) - 1
    if p >= n:
        f, hp, mhp, snap = tr.pts[n]
        return snap, hp, mhp, tr.run.outcome != "running"
    k = int(p)
    fr = p - k
    f0, hp0, m0, snap = tr.pts[k]
    f1, hp1, m1, _ = tr.pts[k + 1]
    return snap, hp0 + (hp1 - hp0) * fr, m0 + (m1 - m0) * fr, False


def cmd_rollup(args, manifest: dict) -> int:
    out_root = Path(args.out)
    vid, jpg = out_root / "rollup.mp4", out_root / "rollup.jpg"
    roll = manifest["rollup"]
    by_id = {r["id"]: r for r in manifest["runs"]}
    rows = roll["rows"][:8]
    inputs = [Path(by_id[i]["path"]) for row in rows for i in row["runs"] if i in by_id]
    if not args.force and jpg.exists() and up_to_date(vid, inputs + [Path(args.manifest)]):
        print(f"{vid} is up to date")
        return 0
    lower_priority()
    t0 = time.time()
    grid = []
    for row in rows:
        tracks = []
        for rid in row["runs"][:8]:
            run = load_run(by_id[rid])
            pts = []
            last_f = 1
            for st in run.states:
                f = st.snap.get("floor") or last_f
                last_f = f
                hp = st.snap.get("hp")
                mhp = st.snap.get("max_hp") or (pts[-1][2] if pts else 80)
                hp = hp if isinstance(hp, (int, float)) else (pts[-1][1] if pts else mhp)
                pts.append((f, hp, mhp, st.snap))
            tracks.append(Track(run, pts))
        grid.append((row["name"], tracks))
    D = max(len(t.pts) - 1 for _, ts in grid for t in ts)
    run_frames = int(min(58 * R_FPS, max(45 * R_FPS, round(D * 2.4))))
    print(f"rollup: {len(grid)} rows, longest run {D} decisions -> {run_frames / R_FPS:.0f}s + holds")

    static = Image.new("RGB", (RW, RH), BG)
    d = ImageDraw.Draw(static)
    d.text((40, 78), "Slay the Spire 2, played by language models", font=font(SERIF_B, 44), fill=TEXT, anchor="ls")
    d.text((42, 116), "Ironclad, Ascension 0. Seeds BENCH01–04, two attempts each. "
                      "All 64 runs advance in lockstep, one decision at a time.",
           font=font(SANS, 20), fill=MUTED, anchor="ls")
    cols = roll.get("columns") or []
    for c in range(8):
        cx = GRID_X0 + c * (TILE_W + COL_GAP) + TILE_W / 2
        lab = cols[c] if c < len(cols) else ""
        d.text((cx, GRID_Y0 - 14), lab, font=font(SANS_SB, 15), fill=MUTED, anchor="ms")
    # legend
    ly = RH - 22
    x = 40
    for a in range(3):
        d.rectangle((x, ly - 11, x + 22, ly - 5), fill=ACTS[a])
        d.text((x + 30, ly), f"Act {a + 1}", font=font(SANS, 15), fill=MUTED, anchor="ls")
        x += 100
    d.rectangle((x, ly - 12, x + 22, ly - 4), fill=HP)
    d.text((x + 30, ly), "HP", font=font(SANS, 15), fill=MUTED, anchor="ls")
    x += 80
    legend = [("✝ 17", (176, 124, 124), "died on floor 17"), ("WIN", GOLD, "beat the Act 3 boss"),
              ("STOPPED", (150, 156, 168), "cut short (stalled or budget)")]
    for tag, col, txt in legend:
        f = font(SYM, 15) if tag.startswith("✝") else font(SANS_B, 14)
        d.text((x, ly), tag, font=f, fill=col, anchor="ls")
        x += text_w(tag, f) + 8
        d.text((x, ly), txt, font=font(SANS, 15), fill=MUTED, anchor="ls")
        x += text_w(txt, font(SANS, 15)) + 30
    d.text((RW - 40, ly), "Floor bars are coloured by act; tile label shows the current screen.",
           font=font(SANS, 15), fill=DIM, anchor="rs")

    ended_at: dict = {}
    cache: dict = {}
    enc = Encoder(RW, RH, R_FPS, vid, crf=24, gop=R_FPS * 4, threads=3)
    total = R_HOLD_START + run_frames + 1 + R_HOLD_END
    frame_img = None
    try:
        for fi in range(total):
            if fi < R_HOLD_START:
                p = 0.0
            elif fi < R_HOLD_START + run_frames:
                p = D * (fi - R_HOLD_START) / run_frames
            else:
                p = float(D)
            frame_img = static.copy()
            d = ImageDraw.Draw(frame_img)
            tally = {"victory": 0, "defeat": 0, "cut": 0, "alive": 0}
            for r, (name, tracks) in enumerate(grid):
                ty = GRID_Y0 + r * (TILE_H + ROW_GAP)
                best, wins, alive = 0, 0, 0
                for c, tr in enumerate(tracks):
                    snap, hp, mhp, ended = run_state_at(tr, p)
                    key = (r, c)
                    if ended and key not in ended_at:
                        ended_at[key] = fi
                    flash = 0.0
                    if ended:
                        age = fi - ended_at[key]
                        flash = max(0.0, 1 - age / FLASH) if ended_at[key] > R_HOLD_START else 0.0
                        o = tr.run.outcome
                        tally["victory" if o == "victory" else "defeat" if o == "defeat" else "cut"] += 1
                        wins += o == "victory"
                    else:
                        tally["alive"] += 1
                        alive += 1
                    floor = (tr.run.final_floor if ended else snap.get("floor")) or 0
                    best = max(best, floor)
                    hq = round(hp * 4) / 4
                    ck = (r, c, id(snap), hq, round(mhp), ended, round(flash, 2))
                    tile = cache.get(ck)
                    if tile is None:
                        tile = draw_tile(tr.run, snap, hq, mhp, ended, flash)
                        if len(cache) > 4000:
                            cache.clear()
                        cache[ck] = tile
                    frame_img.paste(tile, (GRID_X0 + c * (TILE_W + COL_GAP), ty))
                # row label with a live tally
                d.text((40, ty + TILE_H / 2 + 2), fit(name, font(SERIF_B, 25), GRID_X0 - 56), font=font(SERIF_B, 25),
                       fill=TEXT, anchor="ls")
                bits = [f"best floor {best}"]
                if wins:
                    bits.append(f"{wins} win" + ("s" if wins > 1 else ""))
                if alive:
                    bits.append(f"{alive} playing")
                d.text((41, ty + TILE_H / 2 + 26), "  ·  ".join(bits), font=font(SANS, 15),
                       fill=GOLD if wins else MUTED, anchor="ls")
            # header: decision counter, progress line and tallies
            dec = int(p)
            small_caps(d, (RW - 40, 44), "Decision", fill=MUTED, size=14, anchor="rs")
            d.text((RW - 40, 112), f"{dec}", font=font(SANS_SB, 68), fill=TEXT, anchor="rs")
            tl = [(f"{tally['victory']} won", GOLD), (f"{tally['defeat']} died", (200, 132, 132)),
                  (f"{tally['cut']} stopped", (160, 166, 178)), (f"{tally['alive']} still climbing", TEXT)]
            x = RW - 40 - 200
            tf = font(SANS_SB, 18)
            for txt, col in reversed(tl):
                d.text((x, 112), txt, font=tf, fill=col, anchor="rs")
                x -= text_w(txt, tf) + 26
            py = 150
            d.rectangle((40, py, RW - 40, py + 3), fill=TRACK)
            d.rectangle((40, py, 40 + (RW - 80) * (p / D if D else 1), py + 3), fill=MUTED)
            enc.write(frame_img)
    except BaseException:
        enc.abort()
        raise
    enc.close()
    save_poster(frame_img, jpg, 960)
    print(f"{vid}: {total} frames ({total / R_FPS:.0f}s), {vid.stat().st_size / 1024:.0f} KB, "
          f"rendered in {time.time() - t0:.0f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=["runs", "rollup"])
    ap.add_argument("--manifest", default=str(ROOT / "site_build" / "manifest.json"))
    ap.add_argument("--out", default=str(ROOT / "site_build" / "sts2"))
    ap.add_argument("--only", help="comma-separated run ids (or unique substrings of them)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--force", action="store_true", help="re-render even if the output is newer than its input")
    args = ap.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    lower_priority()
    return cmd_runs(args, manifest) if args.what == "runs" else cmd_rollup(args, manifest)


if __name__ == "__main__":
    raise SystemExit(main())
