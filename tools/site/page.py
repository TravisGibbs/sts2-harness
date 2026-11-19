"""Write the two static pages of the results site from the manifest.

    site_build/sts2/index.html        short post (how it works) + rollup video + benchmark table
    site_build/sts2/annex/index.html  every run: video, replay, raw recording

    python tools/site/page.py [--manifest site_build/manifest.json] [--out site_build/sts2]

All links are relative, so the pages work wherever the folder is served (e.g. /workspace/sts2/).
Manifest links are relative to the site root; the annex page strips the leading "annex/".
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from html import escape
from pathlib import Path

from choices_page import section as choices_section

ROOT = Path(__file__).resolve().parents[2]

# lobehub icons that are plain black and need inverting on a dark background
MONO_LOGOS = {"openai.svg", "xiaomimimo.svg", "zai.svg", "openrouter.svg"}
# color icons with a black (currentColor) part: invert, then rotate hue back so the color part keeps its hue
MIXED_LOGOS = {"kimi-color.svg"}
OUTCOME_LABEL = {"victory": "Win", "defeat": "Defeat"}  # the manifest only has runs that ended in the game
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def e(s) -> str:
    return escape(str(s), quote=True)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def fmt_date(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def fmt_cost(c: float | None) -> str:
    if c is None:
        return "–"
    if c == 0:
        return "$0"
    return f"${c:.2f}" if c >= 0.1 else f"${c:.3f}"


def fmt_duration(s: float | None) -> str:
    if not s:
        return "–"
    m = round(s / 60)
    if m < 1:
        return f"{round(s)} s"
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min"


def outcome_label(o: str) -> str:
    return OUTCOME_LABEL.get(o, o.replace("_", " ").capitalize())


def mark_kind(r: dict) -> str:
    return "win" if r["outcome"] == "victory" else "def"


def cost_source(src: str) -> str:
    return "billed" if src in ("reported", "billed") else "est."


def kind_tag(m: dict) -> str:
    if m["scripted"]:
        return "bot"
    if m["model"].startswith("decisions:"):
        return "baseline"
    if m["open_weights"]:
        return "open weights"
    return ""


# ---------------------------------------------------------------- logos

DIE = ('<svg class="glyph" viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">'
       '<rect x="2.5" y="2.5" width="15" height="15" rx="3.5" fill="none" stroke="currentColor" stroke-width="1.5"/>'
       '<circle cx="6.8" cy="6.8" r="1.4" fill="currentColor"/><circle cx="13.2" cy="6.8" r="1.4" fill="currentColor"/>'
       '<circle cx="10" cy="10" r="1.4" fill="currentColor"/>'
       '<circle cx="6.8" cy="13.2" r="1.4" fill="currentColor"/><circle cx="13.2" cy="13.2" r="1.4" fill="currentColor"/></svg>')


def gear_svg() -> str:
    pts = []
    teeth = 8
    for i in range(teeth * 4):
        a = math.pi * 2 * i / (teeth * 4)
        r = 8.2 if (i % 4) in (1, 2) else 6.2
        pts.append(f"{10 + r * math.cos(a):.2f},{10 + r * math.sin(a):.2f}")
    return ('<svg class="glyph" viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">'
            f'<polygon points="{" ".join(pts)}" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>'
            '<circle cx="10" cy="10" r="2.4" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>')


GEAR = gear_svg()


def logo(m: dict) -> str:
    if not m["logo"]:
        return f'<span class="logo">{DIE if "random" in m["model"] else GEAR}</span>'
    fname = m["logo"].rsplit("/", 1)[-1]
    cls = "logo-img inv" if fname in MONO_LOGOS else "logo-img invh" if fname in MIXED_LOGOS else "logo-img"
    return ('<span class="logo">'
            f'<img class="{cls}" src="{e(m["logo"])}" alt="" width="20" height="20" loading="lazy" '
            'referrerpolicy="no-referrer" onerror="this.parentNode.classList.add(\'nologo\');this.remove()">'
            f'<span class="mono" aria-hidden="true">{e(m["maker"][:1])}</span></span>')


# ---------------------------------------------------------------- shared css

CSS = """
:root{color-scheme:light;--bg:#fbfaf7;--ink:#111;--muted:#77736b;--link:#0000cc;--rule:#e6e3dc;--rule2:#d3cfc6;
--run:#2a78d6;--win:#eb6834;--hl:#fff1bf;--card:#f4f2ec}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#121212;--ink:#eee;--muted:#9b978f;
--link:#8fb3ff;--rule:#2a2a28;--rule2:#3d3c39;--run:#3987e5;--win:#d95926;--hl:#3b3320;--card:#1b1b1a}
:root:not([data-theme="light"]) .logo-img.inv{filter:invert(1)}
:root:not([data-theme="light"]) .logo-img.invh{filter:invert(1) hue-rotate(180deg)}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121212;--ink:#eee;--muted:#9b978f;--link:#8fb3ff;--rule:#2a2a28;
--rule2:#3d3c39;--run:#3987e5;--win:#d95926;--hl:#3b3320;--card:#1b1b1a}
:root[data-theme="dark"] .logo-img.inv{filter:invert(1)}
:root[data-theme="dark"] .logo-img.invh{filter:invert(1) hue-rotate(180deg)}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:18px/1.55 "Times New Roman",Times,serif}
a{color:var(--link)}
.wrap{max-width:52em;margin:0 auto;padding:0 16px}
.wide{max-width:72em;margin:0 auto;padding:0 16px}
.top{padding-top:1.6em}
.top a{text-decoration:none;font-size:.9em}
h1{font-weight:normal;font-size:2em;line-height:1.2;margin:.6em 0 .15em}
h2{font-weight:normal;font-size:1.4em;margin:1.8em 0 .5em}
.muted{color:var(--muted)}
.dateline{color:var(--muted);margin:0 0 1.5em}
code{font-family:ui-monospace,Consolas,Menlo,monospace;font-size:.82em;background:var(--card);padding:.05em .3em;border-radius:3px}
.logo{display:inline-flex;width:20px;height:20px;flex:none;align-items:center;justify-content:center;vertical-align:-4px;color:var(--ink)}
.logo img{display:block;width:20px;height:20px}
.logo .mono{display:none}
.logo.nologo .mono{display:flex;width:20px;height:20px;border-radius:50%;border:1px solid var(--muted);color:var(--muted);
align-items:center;justify-content:center;font-size:11px;line-height:1}
.glyph{display:block}
.tag{display:inline-block;font-size:.68em;line-height:1.4;padding:0 .4em;border:1px solid var(--rule2);border-radius:3px;
color:var(--muted);white-space:nowrap;vertical-align:1px}
footer{margin:4em auto 3em;color:var(--muted);font-size:.85em}
"""

# ---------------------------------------------------------------- index page

INDEX_CSS = """
figure.rollup{margin:0 0 .5em}
figure.rollup video{display:block;width:100%;height:auto;aspect-ratio:auto 16/9;background:var(--card);border-radius:2px}
figure.rollup figcaption{color:var(--muted);font-size:.85em;margin-top:.5em;max-width:52em}
ul.terse{padding-left:1.1em;margin:.4em 0}ul.terse li{margin:.15em 0}
.post p{margin:0 0 1em}
.legend{display:flex;flex-wrap:wrap;gap:.3em 1.4em;align-items:center;font-size:.85em;color:var(--muted);margin:.2em 0 .8em}
.legend span{display:inline-flex;align-items:center;gap:.4em}
.legend svg{overflow:visible}
.tbl{overflow-x:auto;-webkit-overflow-scrolling:touch;border-top:1px solid var(--rule2)}
table.bench{border-collapse:collapse;width:100%;font-size:.88em;font-variant-numeric:tabular-nums}
table.bench th,table.bench td{padding:.45em .5em;border-bottom:1px solid var(--rule);text-align:right;vertical-align:middle;white-space:nowrap}
table.bench th{font-weight:normal;color:var(--muted);font-size:.85em;vertical-align:bottom;line-height:1.25}
table.bench th.l,table.bench td.l{text-align:left}
td.rank{color:var(--muted);width:1.8em;padding-left:0}
table.bench td.model{white-space:normal;min-width:10.5em}
td.model .nm{display:flex;align-items:center;gap:.45em}
td.model .nm a{color:var(--ink);text-decoration:none}
td.model .nm a:hover{text-decoration:underline}
td.model .sub{font-size:.78em;color:var(--muted);padding-left:calc(20px + .55em);line-height:1.35}
td .sm{display:block;font-size:.76em;color:var(--muted);line-height:1.2}
th.strip,td.strip{width:40%;min-width:150px;padding-left:12px;padding-right:12px}
svg.strip{display:block;width:100%;overflow:visible}
svg.axis{display:block;width:100%;height:16px;overflow:visible}
svg.axis text{fill:var(--muted);font-size:11px;font-family:"Times New Roman",Times,serif}
.grid{stroke:var(--rule2);stroke-width:1}
.track{stroke:var(--rule);stroke-width:1}
.mk-def{fill:var(--run);stroke:var(--bg);stroke-width:2;paint-order:stroke}
.mk-win{fill:var(--win);stroke:var(--bg);stroke-width:2;paint-order:stroke}
a.run .hit{fill:var(--ink);fill-opacity:0;stroke:none}
a.run use{pointer-events:none}
a.run:hover .hit,a.run.on .hit{fill-opacity:.1}
a.run:focus{outline:none}
a.run:focus-visible .hit{fill-opacity:.12;stroke:var(--link);stroke-width:1.5}
#tip{position:fixed;z-index:10;pointer-events:none;background:var(--bg);color:var(--ink);border:1px solid var(--rule2);
border-radius:4px;padding:.35em .6em;font-size:14px;line-height:1.35;box-shadow:0 2px 10px rgba(0,0,0,.12);max-width:16em;display:none}
#tip b{font-weight:bold}
#tip .k{color:var(--muted)}
.notes{font-size:.82em;color:var(--muted);margin:.8em 0 0}
.notes p{margin:.2em 0}
.annexlink{margin:1.6em 0 0;font-size:1.05em}
@media (max-width:760px){
 .c-act1,.c-harness{display:none}
}
@media (max-width:560px){
 body{font-size:17px}
 h1{font-size:1.6em}
 .c-runs,.c-cost,.c-act2{display:none}
 table.bench{font-size:.84em}
 table.bench td.model{min-width:0;width:7.5em}
 td.model .nm{align-items:flex-start}
 td.model .sub .mk{display:none}
 th.strip,td.strip{min-width:130px;padding-left:8px;padding-right:8px}
 table.bench th,table.bench td{padding-left:.3em;padding-right:.3em}
}
"""

INDEX_JS = """
(function(){
  var tip=document.getElementById('tip');
  var cur=null;
  function show(a){
    if(cur&&cur!==a)cur.classList.remove('on');
    cur=a;a.classList.add('on');
    tip.textContent='';
    var b=document.createElement('b');b.textContent=a.dataset.out+', floor '+a.dataset.floor;tip.appendChild(b);
    var l1=document.createElement('div');l1.textContent=a.dataset.name;tip.appendChild(l1);
    var l2=document.createElement('div');l2.className='k';
    l2.textContent=a.dataset.seed+' \\u00b7 attempt '+a.dataset.att+' \\u00b7 '+a.dataset.cost;tip.appendChild(l2);
    var l3=document.createElement('div');l3.className='k';l3.textContent='Click to watch';tip.appendChild(l3);
    tip.style.display='block';
    var r=a.querySelector('use').getBoundingClientRect();
    var w=tip.offsetWidth,h=tip.offsetHeight,vw=document.documentElement.clientWidth;
    var x=r.left+r.width/2-w/2;x=Math.max(8,Math.min(vw-w-8,x));
    var y=r.top-h-6;if(y<8)y=r.bottom+6;
    tip.style.left=x+'px';tip.style.top=y+'px';
  }
  function hide(){tip.style.display='none';if(cur){cur.classList.remove('on');cur=null;}}
  document.querySelectorAll('a.run').forEach(function(a){
    a.addEventListener('pointerenter',function(){show(a)});
    a.addEventListener('pointerleave',hide);
    a.addEventListener('focus',function(){show(a)});
    a.addEventListener('blur',hide);
  });
  window.addEventListener('scroll',hide,{passive:true});
  var tb=document.querySelector('.tbl');if(tb)tb.addEventListener('scroll',hide,{passive:true});
})();
"""

MARK_DEFS = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false"><defs>'
             '<g id="mk-def"><circle class="mk-def" r="4.5"/></g>'
             '<g id="mk-win"><polygon class="mk-win" points="0,-5.8 5.8,0 0,5.8 -5.8,0"/></g>'
             '</defs></svg>')

LANE_STEP = 9     # px between stacked marks
LANE_SPAN = 30    # px: a tall stack squeezes its step so a row never grows past ~48px
LANE_GAP = 1.7    # floors: closer than this and two marks stack
HIT_HALF = 1.5    # floors: widest a mark's hit target reaches to each side


def pct(floor: float, max_floor: int) -> str:
    return f"{100 * floor / max_floor:.3f}%"


def place(runs: list[dict], max_floor: int) -> list[tuple[dict, float, int]]:
    """Give each run an x (floor) and a lane so marks at nearly the same floor stack instead of hiding each other."""
    pts = sorted(((r, max_floor if r["outcome"] == "victory" else r["final_floor"]) for r in runs),
                 key=lambda p: (p[1], p[0]["outcome"] != "victory", p[0]["seed"], p[0]["attempt"]))
    placed: list[tuple[dict, float, int]] = []
    for r, f in pts:
        for lane in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5):
            if all(pl != lane or abs(f - pf) >= LANE_GAP for _, pf, pl in placed):
                break
        placed.append((r, f, lane))
    return placed


def strip(m: dict, runs: dict, act_floors: list[int], max_floor: int) -> str:
    placed = place([runs[i] for i in m["runs"]], max_floor)
    top = max((-l for _, _, l in placed), default=0)
    bot = max((l for _, _, l in placed), default=0)
    pad = 9
    step = min(LANE_STEP, LANE_SPAN / (top + bot)) if top + bot else LANE_STEP
    h = round(pad * 2 + (top + bot) * step)
    cy = pad + top * step
    out = [f'<svg class="strip" height="{h}" role="group" aria-label="{e(m["name"])}: final floor of each run">']
    for f in act_floors[:-1]:
        out.append(f'<line class="grid" x1="{pct(f, max_floor)}" x2="{pct(f, max_floor)}" y1="2" y2="{h - 2}"/>')
    out.append(f'<line class="track" x1="0" x2="100%" y1="{cy}" y2="{cy}"/>')
    # Hit targets tile the strip: one lane step tall (22px for a single lane) and, along x, reaching halfway to the
    # nearest mark in the same lane, at most HIT_HALF floors each side. The marks themselves ignore the pointer, so
    # neighbours never steal each other's clicks.
    hit_h = 22.0 if not (top + bot) else min(22.0, step)
    for r, f, lane in placed:
        same = [pf for rr, pf, pl in placed if pl == lane and rr is not r]
        left = max([pf for pf in same if pf <= f] + [f - 2 * HIT_HALF])
        right = min([pf for pf in same if pf >= f] + [f + 2 * HIT_HALF])
        x0 = max(f - HIT_HALF, (left + f) / 2, -HIT_HALF)
        x1 = min(f + HIT_HALF, (right + f) / 2, max_floor + HIT_HALF)
        if x1 - x0 < 0.01:  # identical floor in the same lane (only when lanes run out): share the slot
            x0, x1 = f - HIT_HALF / 2, f + HIT_HALF / 2
        y = round(cy + lane * step, 1)
        x = pct(f, max_floor)
        kind = mark_kind(r)
        out_l = outcome_label(r["outcome"])
        if m["scripted"]:
            cost = label_cost = "no API cost"
        else:
            cost = f"{fmt_cost(r['cost'])} {cost_source(r['cost_source'])}"
            label_cost = f"{fmt_cost(r['cost'])} {'billed' if cost_source(r['cost_source']) == 'billed' else 'estimated'}"
        label = f"{r['seed']} attempt {r['attempt']}: {out_l}, floor {r['final_floor']}, {label_cost}. Watch the run."
        out.append(
            f'<a class="run" href="annex/#{e(r["id"])}" aria-label="{e(label)}" data-name="{e(m["name"])}" '
            f'data-seed="{e(r["seed"])}" data-att="{r["attempt"]}" data-out="{e(out_l)}" '
            f'data-floor="{r["final_floor"]}" data-cost="{e(cost)}">'
            f'<rect class="hit" x="{pct(x0, max_floor)}" y="{y - hit_h / 2:.1f}" width="{pct(x1 - x0, max_floor)}" '
            f'height="{hit_h:.1f}" rx="3"/>'
            f'<use href="#mk-{kind}" x="{x}" y="{y}"/></a>')
    out.append("</svg>")
    return "".join(out)


def axis(act_floors: list[int], max_floor: int) -> str:
    ticks = [0] + act_floors
    out = ['<svg class="axis" aria-hidden="true">']
    for f in ticks:
        anchor = "start" if f == 0 else "end" if f == max_floor else "middle"
        out.append(f'<text x="{pct(f, max_floor)}" y="12" text-anchor="{anchor}">{f}</text>')
    out.append("</svg>")
    return "".join(out)


def frac(d: dict) -> str:
    return f'{d["cleared"]}&#8202;/&#8202;{d["decided"]}'


def model_row(m: dict, runs: dict, act_floors, max_floor) -> str:
    s = m["stats"]
    tag = kind_tag(m)
    sub = f'<span class="mk">{e(m["maker"])}</span>' if not m["scripted"] else ""
    if tag:
        sub += (" " if sub else "") + f'<span class="tag">{e(tag)}</span>'
    if m.get("featured_game"):
        r = runs[m["featured_game"]]
        title = f"Watch {m['name']} play {r['seed']} (floor {r['final_floor']}) in the game"
        sub += (" " if sub else "") + (f'<a class="ig" href="annex/#{e(r["id"])}" title="{e(title)}">'
                                       '&#9654;&nbsp;in game</a>')
    wins = f'{s["wins"]}&#8202;/&#8202;{s["runs"]}'
    if m["scripted"]:
        cost = '<span class="muted">–</span>'
    else:
        src = "billed" if s["cost_source"] == "billed" else "est."
        cost = f'{fmt_cost(s["cost_per_run"])}<span class="sm">{src}</span>'
    harness = ", ".join(m["harness"])
    return (
        "<tr>"
        f'<td class="rank l">{m["rank"]}</td>'
        f'<td class="model l"><div class="nm">{logo(m)}<a href="annex/#m-{slug(m["name"])}">{e(m["name"])}</a></div>'
        f'<div class="sub">{sub}</div></td>'
        f'<td class="strip">{strip(m, runs, act_floors, max_floor)}</td>'
        f'<td class="c-runs">{s["runs"]}</td>'
        f"<td>{wins}</td>"
        f'<td class="c-act1">{frac(s["act1"])}</td>'
        f'<td class="c-act2">{frac(s["act2"])}</td>'
        f'<td>{s["mean_floor"]:.1f}</td>'
        f'<td class="c-cost">{cost}</td>'
        f'<td class="c-harness">{e(harness)}</td>'
        "</tr>")


def head(title: str, description: str, extra_css: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f"<title>{e(title)}</title>\n"
        f'<meta name="description" content="{e(description)}">\n'
        f"<style>{CSS}{extra_css}</style>\n</head>\n<body>\n")


def legend_svg(kind: str) -> str:
    return (f'<svg width="14" height="14" viewBox="-7 -7 14 14" aria-hidden="true">'
            f'<use href="#mk-{kind}"/></svg>')


INDEX_GAME_CSS = """
td.model .sub .ig{white-space:nowrap;font-size:1.05em}
"""


def has_footage(man: dict) -> bool:
    return any(r.get("game_video") for r in man["runs"])


def game_paragraph(man: dict) -> str:
    """The "How it works" note on game footage; empty when no run has any, so the page is unchanged."""
    filmed = sum(1 for r in man["runs"] if r.get("game_video"))
    if not filmed:
        return ""
    return (f"<li>Footage: {filmed} runs replayed in the real game, verified step by step "
            f"(<i>&#9654;&nbsp;in game</i>); fights as <code>.mcr</code>.</li>\n")


def rollup_figure(man: dict, rows: list, row_list: str) -> str:
    grid = man.get("game_grid")
    if not grid:
        return f"""<figure class="rollup">
<video src="rollup.mp4" poster="rollup.jpg" autoplay muted loop playsinline aria-label="{len(rows)} by {len(man['rollup']['columns'])} grid of runs playing at once"></video>
<figcaption>{len(rows)} models &times; {len(man['rollup']['columns'])} runs. Rows, top to bottom: {e(row_list)}.
Columns: the four seeds, two attempts each. Sped up; every tile advances one decision at a time, in step.</figcaption>
</figure>"""
    names = [r["name"] for r in grid["rows"]]
    per = len(grid["rows"][0]["runs"])
    listed = ", ".join(names[:-1]) + " and " + names[-1]
    runs = {r["id"]: r for r in man["runs"]}
    shown = [runs[i] for row in grid["rows"] for i in row["runs"]]
    wins = sum(r["outcome"] == "victory" for r in shown)
    return f"""<figure class="rollup">
<video src="rollup.mp4" poster="rollup.jpg" autoplay muted loop playsinline aria-label="Grid of {len(shown)} runs filmed in the game, playing at once"></video>
<figcaption>{e(listed)}. Typical runs (&#8531; and &#8532; up each model's range), {wins}/{len(shown)} win.</figcaption>
</figure>"""


def render_index(man: dict) -> str:
    models, runs = man["models"], {r["id"]: r for r in man["runs"]}
    act_floors, max_floor = man["act_floors"], man["max_floor"]
    updated = fmt_date(max(r["started_at"] for r in man["runs"] if r.get("started_at")))
    n_runs, n_models = len(man["runs"]), len(models)
    rows = man["rollup"]["rows"]
    names = [r["name"] for r in rows]
    row_list = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)
    seeds = man["seeds"]
    seed_span = f"{seeds[0]}–{seeds[-1][-2:]}" if len(seeds) > 1 else seeds[0]
    a1, a2, a3 = act_floors

    post = f"""
<h2>How it works</h2>
<ul class="terse">
<li>Game: Slay the Spire 2 v0.113.1, Ironclad, Ascension 0, seeds {seed_span}, mostly two attempts each.</li>
<li>Interface: a text mod; tools <code>act</code> (<code>play 2 e0</code>, <code>end</code>, <code>choose 1</code>), <code>info</code>, <code>set_notes</code>.</li>
<li>Limits: 600 requests and $0.50&ndash;2.50 per run; reasoning at low effort.</li>
<li>Rank: wins, then Act 2 cleared, Act 1 cleared, mean floor. Bosses on floors {a1}, {a2}, {a3}.</li>
<li>Baselines: Random bot, Greedy bot (block if needed, else attack), Jev 1.14 (OpenRouter Decisions API).</li>
<li>Harness: newest version per model; 0.4.2 fixed open-model reply parsing.</li>
<li>Cost: <i>est.</i> from list prices, <i>billed</i> from OpenRouter.</li>
{game_paragraph(man)}</ul>
"""

    table_rows = "\n".join(model_row(m, runs, act_floors, max_floor) for m in models)
    return head(
        "Slay the Spire 2 benchmark",
        "Language models playing Slay the Spire 2 on identical seeds: how it works, and results for every model.",
        INDEX_CSS + (INDEX_GAME_CSS if has_footage(man) else ""),
    ) + f"""{MARK_DEFS}
<div class="wrap top"><a href="../">&larr; Workspace</a></div>
<header class="wrap">
<h1>Slay the Spire 2, played by language models</h1>
<p class="dateline">Updated {updated} &middot; {n_models} players &middot; {n_runs} runs</p>
</header>
<div class="wide">
{rollup_figure(man, rows, row_list)}
</div>
<article class="wrap post">{post}</article>
<section class="wide" aria-labelledby="bench">
<h2 id="bench">Results</h2>
<div class="legend" aria-hidden="true">
<span>{legend_svg("win")}Win</span><span>{legend_svg("def")}Defeat</span><span>Click a mark to watch that run</span>
</div>
<div class="tbl">
<table class="bench">
<thead><tr>
<th class="l">#</th><th class="l">Model</th>
<th class="strip l">Final floor of each run{axis(act_floors, max_floor)}</th>
<th class="c-runs">Runs</th><th>Wins</th><th class="c-act1">Act&nbsp;1<br>cleared</th><th class="c-act2">Act&nbsp;2<br>cleared</th>
<th>Mean<br>floor</th><th class="c-cost">Cost<br>per run</th><th class="c-harness">Harness</th>
</tr></thead>
<tbody>
{table_rows}
</tbody>
</table>
</div>
<div class="notes">
<p>Only runs that ended in a win or a death count. Faint lines: Act 1 and 2 bosses (floors {a1}, {a2}).</p>
<p>Cost: <i>est.</i> from tokens and list prices; <i>billed</i> by OpenRouter. Bots have no API cost.</p>
</div>
{choices_section(man)}
<p class="annexlink"><a href="annex/">Every run: videos, step-by-step replays and raw recordings &rarr;</a></p>
</section>
<footer class="wrap">Generated from {n_runs} recorded runs. Updated {updated}.</footer>
<div id="tip" role="tooltip"></div>
<script>{INDEX_JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------- annex page

ANNEX_CSS = """
.intro p{margin:0 0 1em}
section.model{margin-top:2.6em}
section.model h2{display:flex;align-items:center;gap:.45em;margin:0 0 .1em}
section.model h2 .logo,section.model h2 .logo img,section.model h2 .logo .mono,section.model h2 .glyph{width:24px;height:24px}
section.model .meta{color:var(--muted);font-size:.85em;margin:0 0 .8em}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(15.5em,1fr));gap:1em 1.1em}
.card{scroll-margin-top:2em;border-radius:4px;padding:.4em;margin:-.4em;transition:background-color .6s}
.card.hl{background:var(--hl)}
.thumb{position:relative;display:block;width:100%;aspect-ratio:16/9;background:var(--card);border-radius:3px;overflow:hidden;
border:0;padding:0;cursor:pointer;color:var(--muted);font:inherit}
.thumb img,.thumb video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block}
.thumb .play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.85em}
.thumb img+.play{display:none}
.thumb:hover .play,.thumb:focus-visible .play{display:flex;background:rgba(0,0,0,.35);color:#fff}
.card .t{margin:.45em 0 0;font-size:.95em;line-height:1.3}
.card .t b{font-weight:normal}
.card .d{margin:.1em 0 0;font-size:.78em;color:var(--muted);line-height:1.35;font-variant-numeric:tabular-nums}
.card .links{margin:.25em 0 0;font-size:.85em}
.card .links a{margin-right:.8em}
.mark{display:inline-block;vertical-align:-1px;margin-right:.25em}
.mark.win{color:var(--win)}
.mark.def{color:var(--run)}
@media (max-width:560px){
 body{font-size:17px}
 .cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:1em .8em}
 .card .t{font-size:.88em}
 .card .links a{display:inline-block;margin-right:.7em}
}
"""

ANNEX_JS = """
(function(){
  function play(card){
    var th=card.querySelector('.thumb');
    if(!th||th.dataset.playing)return;
    th.dataset.playing='1';
    var v=document.createElement('video');
    v.src=th.dataset.video;v.controls=true;v.autoplay=true;v.muted=true;v.playsInline=true;v.preload='none';
    var p=th.getAttribute('data-poster');if(p)v.poster=p;
    th.textContent='';th.appendChild(v);
    var pr=v.play();if(pr&&pr.catch)pr.catch(function(){});
  }
  document.querySelectorAll('.card').forEach(function(card){
    var th=card.querySelector('.thumb');
    th.addEventListener('click',function(ev){ev.preventDefault();play(card);});
    card.querySelector('a.vid').addEventListener('click',function(ev){ev.preventDefault();play(card);});
  });
  function fromHash(){
    var id=decodeURIComponent(location.hash.slice(1));if(!id)return;
    var el=document.getElementById(id);if(!el)return;
    document.querySelectorAll('.card.hl').forEach(function(c){c.classList.remove('hl')});
    if(el.classList.contains('card')){
      el.classList.add('hl');
      el.scrollIntoView({block:'center'});
      play(el);
    }
  }
  window.addEventListener('hashchange',fromHash);
  fromHash();
})();
"""


ANNEX_GAME_CSS = """
.card details.ch{margin:.2em 0 0;font-size:.8em;line-height:1.35}
.card details.ch summary{cursor:pointer;color:var(--muted)}
.card details.ch p{margin:.2em 0;color:var(--muted)}
.card details.ch ol{list-style:none;margin:.2em 0 0;padding:0;columns:2;column-gap:1em}
.card details.ch li{break-inside:avoid}
.card details.ch .tm{color:var(--muted);font-variant-numeric:tabular-nums;margin-left:.3em}
"""

# Only on pages with game footage. Runs after ANNEX_JS: the thumb and the "In the game" link already play
# the footage there; this adds the summary-video switch and the floor list, which seeks the footage.
ANNEX_GAME_JS = """
(function(){
  function load(card,src,poster){
    var th=card.querySelector('.thumb');
    var v=th.querySelector('video');
    if(!v||v.getAttribute('src')!==src){
      th.dataset.playing='1';
      v=document.createElement('video');
      v.src=src;v.controls=true;v.muted=true;v.playsInline=true;v.preload='none';
      if(poster)v.poster=poster;
      th.textContent='';th.appendChild(v);
    }
    return v;
  }
  function start(v){var pr=v.play();if(pr&&pr.catch)pr.catch(function(){});}
  document.querySelectorAll('.card.game').forEach(function(card){
    var th=card.querySelector('.thumb');
    function game(){return load(card,th.dataset.video,th.getAttribute('data-poster'));}
    card.querySelector('a.vid').addEventListener('click',function(){start(game());});
    var sum=card.querySelector('a.sum');
    if(sum)sum.addEventListener('click',function(ev){
      ev.preventDefault();start(load(card,sum.getAttribute('href'),sum.getAttribute('data-poster')));
    });
    card.querySelectorAll('details.ch a').forEach(function(a){
      a.addEventListener('click',function(ev){
        ev.preventDefault();
        var v=game(),t=parseFloat(a.dataset.t)||0;
        function seek(){v.currentTime=t;}
        if(v.readyState>=1)seek();else{v.preload='metadata';v.addEventListener('loadedmetadata',seek,{once:true});}
        start(v);
      });
    });
  });
})();
"""


def fmt_clock(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def fmt_speed(x) -> str:
    return f"{x:g}&times;" if isinstance(x, (int, float)) and x else ""


def strip_annex(link: str) -> str:
    return link[len("annex/"):] if link.startswith("annex/") else "../" + link


def mark_glyph(kind: str) -> str:
    shape = {
        "win": '<polygon points="5,0 10,5 5,10 0,5" fill="currentColor"/>',
        "def": '<circle cx="5" cy="5" r="4.2" fill="currentColor"/>',
    }[kind]
    return f'<svg class="mark {kind}" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">{shape}</svg>'


def chapter_list(r: dict, video: str) -> str:
    g = r["game_render"]
    chapters = g.get("chapters") or []
    note = []
    if g.get("steps"):
        note.append(f"Re-played in the game from the recording: {g.get('steps_matched', g['steps'])} of {g['steps']} "
                    "steps matched")
    if fmt_speed(g.get("speed")):
        note.append(f"shown at {fmt_speed(g['speed'])} speed")
    note_html = f'<p>{"; ".join(note)}.</p>' if note else ""
    items = "".join(
        f'<li><a href="{e(video)}#t={c["t"]:g}" data-t="{c["t"]:g}">{e(c["label"])}</a>'
        f'<span class="tm">{fmt_clock(c["t"])}</span></li>' for c in chapters)
    summary = f"Floors ({len(chapters)})" if chapters else "About this video"
    return (f'<details class="ch"><summary>{summary}</summary>{note_html}'
            f'{f"<ol>{items}</ol>" if items else ""}</details>')


def run_card(r: dict, m: dict) -> str:
    kind = mark_kind(r)
    video, poster = strip_annex(r["video"]), strip_annex(r["poster"])
    replay, raw = strip_annex(r["replay"]), strip_annex(r["raw"])
    cost = "no API cost" if m["scripted"] else f"{fmt_cost(r['cost'])} {cost_source(r['cost_source'])}"
    details = [cost, fmt_duration(r["duration_s"]), f"{r['requests']} requests", f"harness {r['harness']}"]
    game = r.get("game_video")
    fights = (f'<a href="{e(strip_annex(r["fights_zip"]))}" download>Fights (.mcr)</a>'
              if r.get("fights_zip") else "")
    if game:
        main, main_poster = strip_annex(game), strip_annex(r.get("game_poster") or r["poster"])
        label = f"Play {m['name']} {r['seed']} attempt {r['attempt']} in the game"
        speed = fmt_speed(r["game_render"].get("speed"))
        play = f"&#9654; In the game{f' ({speed})' if speed else ''}"
        links = (f'<a class="vid" href="{e(main)}">In the game</a>'
                 f'<a class="sum" href="{e(video)}" data-poster="{e(poster)}">Summary video</a>')
        chapters = chapter_list(r, main)
    else:
        main, main_poster = video, poster
        label = f"Play the {m['name']} {r['seed']} attempt {r['attempt']} video"
        play = "&#9654; Play video"
        links = f'<a class="vid" href="{e(video)}">Video</a>'
        chapters = ""
    return (
        f'<div class="card{" game" if game else ""}" id="{e(r["id"])}">'
        f'<button class="thumb" type="button" data-video="{e(main)}" data-poster="{e(main_poster)}" aria-label="{e(label)}">'
        f'<img src="{e(main_poster)}" alt="" loading="lazy" onerror="this.remove()">'
        f'<span class="play">{play}</span></button>'
        f'<p class="t">{mark_glyph(kind)}<b>{e(outcome_label(r["outcome"]))}</b>, floor {r["final_floor"]}'
        f' <span class="muted">&middot; {e(r["seed"])} &middot; attempt {r["attempt"]}</span></p>'
        f'<p class="d">{" &middot; ".join(e(d) for d in details)}</p>'
        f'<p class="links">{links}<a href="{e(replay)}">Replay</a>'
        f'<a href="{e(raw)}" download>Raw (.jsonl.gz)</a>{fights}</p>'
        f"{chapters}</div>")


def render_annex(man: dict) -> str:
    runs = {r["id"]: r for r in man["runs"]}
    updated = fmt_date(max(r["started_at"] for r in man["runs"] if r.get("started_at")))
    sections = []
    for m in man["models"]:
        s = m["stats"]
        tag = kind_tag(m)
        meta = [m["maker"]] if not m["scripted"] else []
        meta.append(f"{s['runs']} run{'s' if s['runs'] != 1 else ''}")
        meta.append(f"{s['wins']} win{'s' if s['wins'] != 1 else ''}")
        meta.append(f"mean floor {s['mean_floor']:.1f}")
        meta.append("harness " + ", ".join(m["harness"]))
        if isinstance(m["effort"], str) and m["effort"] not in ("default",):
            meta.append(f"{m['effort']} effort")
        cards = "\n".join(run_card(runs[i], m) for i in m["runs"])
        tag_html = f' <span class="tag">{e(tag)}</span>' if tag else ""
        sections.append(
            f'<section class="model" id="m-{slug(m["name"])}">'
            f'<h2>{logo(m)}<span>{m["rank"]}. {e(m["name"])}</span>{tag_html}</h2>'
            f'<p class="meta">{" &middot; ".join(e(x) for x in meta)}</p>'
            f'<div class="cards">\n{cards}\n</div></section>')
    n = len(man["runs"])
    game = has_footage(man)
    extra = []
    if game:
        extra.append("<b>In the game</b>: real-game replay, verified step by step")
    if any(r.get("fights_zip") for r in man["runs"]):
        extra.append("<b>Fights</b>: <code>.mcr</code> combat replays")
    extra_html = "".join(f" &middot; {x}" for x in extra)
    game_js = f"\n<script>{ANNEX_GAME_JS}</script>" if game else ""
    return head(
        "Annex: every run",
        "Every Slay the Spire 2 benchmark run: a video, a step-by-step replay and the raw recording.",
        ANNEX_CSS + (ANNEX_GAME_CSS if game else ""),
    ) + f"""<div class="wrap top"><a href="../">&larr; Results</a></div>
<header class="wrap">
<h1>Annex: every run</h1>
<p class="dateline">{n} runs &middot; updated {updated}</p>
</header>
<div class="wrap intro">
<p><b>Video</b>: one frame per decision &middot; <b>Replay</b>: step by step &middot; <b>Raw</b>: JSONL{extra_html}.</p>
</div>
<div class="wide">
{chr(10).join(sections)}
</div>
<footer class="wrap"><a href="../">&larr; Results</a></footer>
<script>{ANNEX_JS}</script>{game_js}
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", default=str(ROOT / "site_build" / "manifest.json"))
    ap.add_argument("--out", default=str(ROOT / "site_build" / "sts2"))
    args = ap.parse_args()
    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    out = Path(args.out)
    (out / "annex").mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render_index(man), encoding="utf-8")
    (out / "annex" / "index.html").write_text(render_annex(man), encoding="utf-8")
    print(out / "index.html")
    print(out / "annex" / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
