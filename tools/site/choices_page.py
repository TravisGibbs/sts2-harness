"""The "Choices" section of the benchmark page: one table of gameplay choices per model, top picks, findings.

page.py calls section(man) once; the numbers come from choices.aggregate (cached per recording).
Styling reuses page.py's classes (.tbl, table.bench, .notes, .tag, td .sm); the few extra rules are scoped
to .choices.
"""

from __future__ import annotations

import re
from html import escape

import choices

CSS = """<style>
.choices h3{font-weight:normal;font-size:1.05em;margin:1.4em 0 .4em}
.choices table.bench th.grp{text-align:center;padding-bottom:.1em}
.choices table.bench td.model{min-width:8.5em}
.choices td.model a{color:var(--ink);text-decoration:none}
.choices td.model a:hover{text-decoration:underline}
.choices ul{margin:.3em 0 0;padding-left:1.1em;font-size:.88em}
.choices ul.picks{list-style:none;padding:0;columns:2 24em;column-gap:2em}
.choices ul.picks li{break-inside:avoid;margin:0 0 .25em}
.choices ul.find li{margin:0 0 .2em}
@media (max-width:760px){.choices .c-mix,.choices .c-rem,.choices .c-gold,.choices tr.grps{display:none}}
@media (max-width:560px){.choices .c-runs,.choices .c-deck,.choices .c-boss{display:none}}
</style>"""

# Findings only quote models with at least this much data behind the number.
MIN_RUNS = 4
MIN_VISITS = 20   # rest sites, card rewards
MIN_BOSSES = 5   # potion habits only mean something for runs that get far


def e(s) -> str:
    return escape(str(s), quote=True)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def pct(x: float | None) -> str:
    return "–" if x is None else f"{round(100 * x)}%"


def num(x: float | None, nd: int = 1) -> str:
    if x is None:
        return "–"
    return f"{x:.{nd}f}" if nd else str(round(x))


def sm(text: str) -> str:
    return f'<span class="sm">{text}</span>'


def row(m: dict) -> str:
    c, r, p, d, boss = m["cards"], m["rest"], m["path"], m["death"], m["boss_hp"]
    if c["picked"]:
        mix = "&#8202;/&#8202;".join(str(round(100 * (c["mix"][t] or 0))) for t in ("Attack", "Skill", "Power"))
    else:
        mix = "–"
    tag = "bot" if m["scripted"] else "baseline" if m["baseline"] else ""
    tag_html = f' <span class="tag">{tag}</span>' if tag else ""
    rewards = c["offers"] + c["unopened"]
    picked = pct(c["pick_rate"]) + (sm(f"of {rewards}") if rewards else "")
    smith = pct(r["smith_rate"]) + (sm(f'of {r["visits"]}') if r["visits"] else "")
    rest_low = pct(r["low_hp_rest_rate"]) + (sm(f'of {r["low_hp_visits"]}') if r["low_hp_visits"] else "")
    elite = pct(p["elite_take_rate"]) + (sm(f'{p["elite_taken"]} of {p["elite_offered"]}') if p["elite_offered"] else "")
    boss_hp = pct(boss["mean"]) + (sm(f'{boss["fights"]} fights') if boss["fights"] else "")
    gold = num(d["gold"], 0) if d["runs"] else "–"
    potions = num(m["potions_used_per_run"]) + (sm(f'{num(d["potions_held"])} held at death') if d["runs"] else "")
    return (
        "<tr>"
        f'<td class="model l"><a href="annex/#m-{slug(m["name"])}">{e(m["name"])}</a>{tag_html}</td>'
        f'<td class="c-runs">{m["runs"]}</td>'
        f"<td>{picked}</td>"
        f'<td class="c-mix">{mix}</td>'
        f'<td class="c-deck">{num(m["deck_final"], 0)}</td>'
        f'<td class="c-rem">{num(m["removals"]["per_run"])}</td>'
        f"<td>{smith}</td>"
        f"<td>{rest_low}</td>"
        f"<td>{elite}</td>"
        f'<td class="c-boss">{boss_hp}</td>'
        f'<td class="c-gold">{gold}</td>'
        f"<td>{potions}</td>"
        "</tr>")


def picks_line(m: dict) -> str:
    top = ", ".join(f'{e(t["card"])} {t["picked"]}/{t["offered"]}' for t in m["cards"]["top"]) or "–"
    s = m["shops"]
    bought = ", ".join(f'{e(t["item"])} &times;{t["n"]}' for t in s["top"] if t["n"] > 1)
    shop = f' &middot; <span class="muted">shop:</span> {bought}' if bought else ""
    return f'<li><b>{e(m["name"])}</b> {top}{shop}</li>'


def findings(models: list[dict]) -> list[str]:
    """3-5 one-line facts with numbers, from the models with enough data (bots and baselines left out)."""
    llm = [m for m in models if not m["scripted"] and not m["baseline"] and m["runs"] >= MIN_RUNS]
    out = []

    def ends(key, pool):
        pool = [m for m in pool if key(m) is not None]
        return (max(pool, key=key), min(pool, key=key)) if len(pool) >= 2 else (None, None)

    hi, lo = ends(lambda m: m["rest"]["smith_rate"], [m for m in llm if m["rest"]["visits"] >= MIN_VISITS])
    if hi:
        out.append(f'{e(hi["name"])} smiths {pct(hi["rest"]["smith_rate"])} of rest sites; '
                   f'{e(lo["name"])} {pct(lo["rest"]["smith_rate"])}.')
    offered = sum(m["path"]["elite_offered"] for m in llm)
    taken = sum(m["path"]["elite_taken"] for m in llm)
    if offered:
        out.append(f"Elites are dodged: models took the elite at {taken} of {offered} map forks that offered one "
                   f"({pct(taken / offered)}).")
    hi, lo = ends(lambda m: m["potions_used_per_run"], [m for m in llm if m["boss_hp"]["fights"] >= MIN_BOSSES])
    if hi:
        out.append(f'{e(hi["name"])} drinks {num(hi["potions_used_per_run"])} potions per run; '
                   f'{e(lo["name"])} {num(lo["potions_used_per_run"])}, and dies holding {num(lo["death"]["potions_held"])}.')
    hi, lo = ends(lambda m: m["cards"]["pick_rate"], [m for m in llm if m["cards"]["offers"] >= MIN_VISITS])
    if hi:
        out.append(f'{e(lo["name"])} takes a card from {pct(lo["cards"]["pick_rate"])} of rewards '
                   f'({lo["cards"]["unopened"]} never opened); {e(hi["name"])} {pct(hi["cards"]["pick_rate"])}.')
    hi, lo = ends(lambda m: m["death"]["gold"], [m for m in llm if m["death"]["runs"] >= 3])
    if hi:
        out.append(f'{e(hi["name"])} dies with {num(hi["death"]["gold"], 0)} gold unspent on average; '
                   f'{e(lo["name"])} {num(lo["death"]["gold"], 0)}.')
    return out[:5]


def section(man: dict) -> str:
    try:
        data = choices.aggregate(man)
    except FileNotFoundError:  # recordings not on this machine: leave the section out
        return ""
    models = data["models"]
    if not models:
        return ""
    rows = "\n".join(row(m) for m in models)
    picks = "\n".join(picks_line(m) for m in models)
    facts = "".join(f"<li>{f}</li>" for f in findings(models))
    low = round(100 * data["low_hp"])
    return f"""{CSS}
<div class="choices">
<h2 id="choices">Choices</h2>
<div class="tbl">
<table class="bench">
<thead><tr class="grps">
<th></th><th class="c-runs"></th><th class="grp" colspan="4">Cards</th><th class="grp" colspan="2">Rest sites</th>
<th class="grp" colspan="4">Risk</th>
</tr><tr>
<th class="l">Model</th><th class="c-runs">Runs</th><th>Card<br>taken</th><th class="c-mix">Atk&#8202;/&#8202;Skl<br>/&#8202;Pow %</th>
<th class="c-deck">Final<br>deck</th><th class="c-rem">Removed<br>per run</th><th>Smith</th><th>Rest at<br>&lt;{low}% HP</th>
<th>Elite when<br>offered</th><th class="c-boss">HP into<br>boss</th><th class="c-gold">Gold at<br>death</th><th>Potions<br>per run</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
<p class="notes">Wins and deaths only, same runs as above, read from each step's game state. Unopened card rewards count as skips.</p>
<ul class="find">{facts}</ul>
<h3>Top picks <span class="muted">(times taken / times offered)</span></h3>
<ul class="picks">
{picks}
</ul>
</div>"""
