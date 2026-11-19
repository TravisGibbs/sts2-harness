"""Build the benchmark report for a suite: survival by floor, stage success, and a replay for every run.

    python agent/report.py --suite pilot
    python agent/report.py --suite pilot --open
    python agent/report.py --compare pilot,pilot-v2 --name v1-vs-v2 --labels "prompt v1,prompt v2"

Reads bench/<suite>/runs/*.jsonl and writes bench/<suite>/report.html plus
bench/<suite>/replays/<run>.html. Safe to run while a suite is still playing:
unfinished runs are shown as "still running" and treated as cut short.

--compare puts the same models from several suites (a different prompt,
playbook or harness version) on one page, bench/_compare/<name>/report.html:
every series becomes (suite, model, effort, protocol), and a first card sets each
model's results side by side with what differed between the suites. List the
baseline suite first; differences are later suite minus the first. Replays
link to the pages in each suite's own replays/ folder; a run that has none
yet (or is still playing) gets one in the comparison's folder instead, so the
suites' folders are only read.

How "success chance" is computed (Kaplan-Meier): for each floor, of the runs
that got as far as attempting it, what share survived it. Multiplying those
shares floor by floor gives the chance of still being alive after each floor.
A run stopped by a cap (not by dying) counts for the floors it played and then
simply leaves the pool, so caps do not look like deaths.

Pass@k: when a (model, seed) is played several times, pass@k for a stage is the
chance that at least one of k attempts, drawn without replacement from the n
decided attempts of which c cleared it, cleared it: 1 - C(n-c, k) / C(n, k).
With several seeds the per-seed values are averaged. When every seed was played
once there are no repeats to draw from; the same formula is then applied across
seeds and labelled as such ("at least one of k seeds"), which is a different and
weaker statement.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import sys
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay  # noqa: E402
from rundata import read_events, series_key, summarize  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "templates" / "report.html"


def cost_audit(suite_dir: Path) -> dict:
    """Recorded request spend, including retry archives and documented missing costs.

    Adjustments preserve externally observed costs without inventing game actions.
    This is recorded/estimated spend, not a provider billing statement.
    """
    totals, unknown = {}, 0
    for folder in ("runs", "archive"):
        amount = 0.0
        for path in (suite_dir / folder).rglob("*.jsonl"):
            for event in read_events(path):
                if event.get("type") != "step":
                    continue
                cost = event.get("cost")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost):
                    amount += cost
                else:
                    unknown += 1
        totals[folder] = round(amount, 6)
    adjustments_path = suite_dir / "cost_adjustments.json"
    adjustments = json.loads(adjustments_path.read_text(encoding="utf-8")) if adjustments_path.exists() else []
    if not isinstance(adjustments, list):
        raise ValueError(f"{adjustments_path}: expected a list of documented adjustments")
    for entry in adjustments:
        amount = entry.get("amount_usd")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount) or amount < 0:
            raise ValueError(f"{adjustments_path}: invalid amount_usd")
    adjusted = sum(entry["amount_usd"] for entry in adjustments)
    return {"scored_recordings_usd": totals["runs"], "retry_archive_usd": totals["archive"],
            "adjustments_usd": round(adjusted, 6), "adjustments": adjustments,
            "total_recorded_usd": round(totals["runs"] + totals["archive"] + adjusted, 6),
            "unknown_cost_requests": unknown}


def survival(runs: list[dict], max_floor: int) -> dict:
    """Kaplan-Meier survival by floor for one series of runs."""
    alive = 1.0
    curve = [{"floor": 0, "alive": 1.0, "at_risk": len(runs), "deaths": 0}]
    censored_at = []
    for floor in range(1, max_floor + 1):
        at_risk = [r for r in runs if r["final_floor"] >= floor]
        deaths = [r for r in at_risk if r["final_floor"] == floor and r["outcome"] == "defeat"]
        if at_risk:
            alive *= 1 - len(deaths) / len(at_risk)
        curve.append({"floor": floor, "alive": round(alive, 4) if at_risk else None,
                      "at_risk": len(at_risk), "deaths": len(deaths)})
        # Runs that stopped here without dying (cut short, or still playing).
        for r in at_risk:
            if r["final_floor"] == floor and r["outcome"] not in ("defeat", "victory"):
                censored_at.append({"floor": floor, "alive": round(alive, 4), "seed": r["seed"], "outcome": r["outcome"]})
    return {"curve": curve, "censored": censored_at}


def stages(runs: list[dict], act_count: int) -> list[dict]:
    """Share of runs that cleared each act (the last one means winning the run)."""
    out = []
    for act in range(1, act_count + 1):
        last = act == act_count
        cleared = [r for r in runs if (r["outcome"] == "victory" if last else r["max_act"] > act)]
        # A run cut short before this stage tells us nothing about it either way.
        decided = [r for r in runs if r in cleared or r["outcome"] in ("defeat", "victory")]
        out.append({
            "stage": "Won the run" if last else f"Cleared Act {act}",
            "short": "Win" if last else f"Act {act}",
            "cleared": len(cleared),
            "decided": len(decided),
            "rate": round(len(cleared) / len(decided), 4) if decided else None,
            "undecided": len(runs) - len(decided),
            # Bounds on the observed batch's eventual rate, not confidence bounds:
            # every unresolved run fails versus every unresolved run clears.
            "batch_bounds": [round(len(cleared) / len(runs), 4),
                             round((len(cleared) + len(runs) - len(decided)) / len(runs), 4)] if runs else None,
        })
    return out


def pass_curve(decided: int, cleared: int) -> list[float]:
    """pass@k for k = 1..decided: the chance that k of the decided runs, drawn
    without replacement, include at least one that cleared the stage."""
    return [round(1 - math.comb(decided - cleared, k) / math.comb(decided, k), 4) for k in range(1, decided + 1)]


def pass_at_k(runs: list[dict], act_count: int, by_attempt: bool) -> list[dict]:
    """Pass@k per stage for one model.

    by_attempt: draws are repeated attempts at one seed. Each seed gets its own
    curve and the model's curve is their mean, for k up to the fewest decided
    attempts any seed has (so every k averages the same seeds).
    Otherwise every seed was played once and the draws are seeds.
    """
    seeds = sorted({r["seed"] for r in runs}, key=str)
    out = []
    for a, stage in enumerate(stages(runs, act_count)):
        by_seed = []
        if by_attempt:
            for seed in seeds:
                s = stages([r for r in runs if r["seed"] == seed], act_count)[a]
                by_seed.append({"seed": seed, "cleared": s["cleared"], "decided": s["decided"],
                                "curve": pass_curve(s["decided"], s["cleared"])})
            # Never silently drop a seed whose attempts are all unresolved:
            # that changes the seed mix and can make a failing model look better.
            k_max = min((p["decided"] for p in by_seed), default=0)
            curve = [round(statistics.mean(p["curve"][k] for p in by_seed), 4) for k in range(k_max)]
        else:
            curve = pass_curve(stage["decided"], stage["cleared"])
        out.append({
            "stage": stage["stage"],
            "short": stage["short"],
            "cleared": stage["cleared"],
            "decided": stage["decided"],
            "k_max": len(curve),
            "curve": curve,          # curve[k - 1] is pass@k
            "by_seed": by_seed,
        })
    return out


def floor_stats(runs: list[dict]) -> dict:
    """How far the runs got: last floor reached, over every attempt."""
    floors = sorted(r["final_floor"] for r in runs)
    q1, _, q3 = statistics.quantiles(floors, n=4, method="inclusive") if len(floors) > 1 else (floors[0],) * 3
    return {
        "best": floors[-1],
        "mean": round(statistics.mean(floors), 1),
        "median": statistics.median(floors),
        "min": floors[0],
        "max": floors[-1],
        "q1": q1,
        "q3": q3,
    }


def hp_by_floor(runs: list[dict], max_floor: int) -> list[dict]:
    """Median share of max HP that surviving runs left each floor with."""
    out = []
    for floor in range(1, max_floor + 1):
        values = []
        for r in runs:
            entry = next((f for f in r["floors"] if f["floor"] == floor), None)
            # The floor a run died on has no "HP after": it is not a survivor of that floor.
            if entry and entry.get("max_hp") and not (r["outcome"] == "defeat" and r["final_floor"] == floor):
                values.append(entry["hp"] / entry["max_hp"])
        out.append({"floor": floor, "median": round(statistics.median(values), 4) if values else None, "n": len(values)})
    return out


def wilson(cleared: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval (95%) for a proportion."""
    p = cleared / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def diff_interval(c1: int, n1: int, c2: int, n2: int) -> list[float] | None:
    """95% interval for p2 - p1 (Newcombe's hybrid score method, built from two
    Wilson intervals). Behaves sensibly at the small counts a suite has, where
    the textbook normal approximation does not."""
    if not n1 or not n2:
        return None
    p1, p2 = c1 / n1, c2 / n2
    l1, u1 = wilson(c1, n1)
    l2, u2 = wilson(c2, n2)
    d = p2 - p1
    return [round(d - math.sqrt((p2 - l2) ** 2 + (u1 - p1) ** 2), 4),
            round(d + math.sqrt((u2 - p2) ** 2 + (p1 - l1) ** 2), 4)]


def load_suite(suite: str) -> tuple[Path, dict, list[tuple[Path, dict]]]:
    """A suite's folder, its config (if any) and (recording, run) pairs."""
    suite_dir = REPO_ROOT / "bench" / suite
    runs_dir = suite_dir / "runs"
    if not runs_dir.exists():
        raise SystemExit(f"no recordings at {runs_dir}")
    config = {}
    if (suite_dir / "suite.json").exists():
        config = json.loads((suite_dir / "suite.json").read_text(encoding="utf-8"))
    pairs = []
    for path in sorted(runs_dir.glob("*.jsonl")):
        run = summarize(path)
        if run is not None:
            pairs.append((path, run))
    if not pairs:
        raise SystemExit(f"no usable recordings in {runs_dir}")
    return suite_dir, config, pairs


def config_order(config: dict) -> list[str]:
    """Series keys in the order the suite config lists its models (protocol included, see rundata.series_key)."""
    return [series_key(m["model"], m["effort"], m.get("protocol")) for m in config.get("models", [])]


def protocols_of(runs: list[dict]) -> list[str]:
    return sorted({r.get("protocol") or "tools" for r in runs})


def run_shape(runs: list[dict], cell_of) -> dict:
    """What every report needs to know about a set of runs before building series."""
    # Repeated attempts: the most times any series played any one seed. Counted
    # from the files rather than the attempt numbers, so a suite that is still
    # filling in (or has a gap) is described by what is actually there.
    per_cell: dict[tuple, int] = {}
    for r in runs:
        cell = cell_of(r)
        per_cell[cell] = per_cell.get(cell, 0) + 1
    max_attempts = max(per_cell.values())

    # First floor of each act, as observed, for the act markers on the floor axis.
    act_starts: dict[int, int] = {}
    for r in runs:
        for f in r["floors"]:
            if f["act"]:
                act_starts[f["act"]] = min(act_starts.get(f["act"], f["floor"]), f["floor"])
    return {
        "max_attempts": max_attempts,
        "by_attempt": max_attempts > 1,
        "max_floor": max(r["final_floor"] for r in runs),
        "act_count": max((r["act_count"] or 0) for r in runs) or 3,
        "act_starts": [{"act": a, "floor": f} for a, f in sorted(act_starts.items())],
    }


def series_entry(key: str, mine: list[dict], shape: dict) -> dict:
    finished = [r for r in mine if r["outcome"] != "running"]
    costs = [r["cost"] for r in finished if r["cost"] is not None]
    floors_played = sum(r["final_floor"] for r in mine) or 1
    action_calls = sum(r.get("action_quality", {}).get("action_calls", 0) for r in mine)
    rejected_actions = sum(r.get("action_quality", {}).get("rejected_action_calls", 0) for r in mine)
    return {
        "key": key,
        "label": mine[0]["series"],
        "model": mine[0]["model"],
        "effort": mine[0]["effort"],
        "protocol": mine[0].get("protocol", "tools"),
        "runs": len(mine),
        "seeds": len({r["seed"] for r in mine}),
        "wins": sum(r["outcome"] == "victory" for r in mine),
        "deaths": sum(r["outcome"] == "defeat" for r in mine),
        "cut_short": sum(r["censored"] for r in mine),
        "infrastructure_failures": sum(r.get("infrastructure_failure", False) for r in mine),
        "behavioral_failures": sum(r.get("behavioral_failure", False) for r in mine),
        "action_calls": action_calls,
        "rejected_action_calls": rejected_actions,
        "rejected_action_rate": round(rejected_actions / action_calls, 4) if action_calls else None,
        "tool_error_calls": sum(r.get("action_quality", {}).get("tool_error_calls", 0) for r in mine),
        "median_floor": statistics.median(r["final_floor"] for r in mine),
        "best_floor": max(r["final_floor"] for r in mine),
        "floors": floor_stats(mine),
        "mean_cost": round(statistics.mean(costs), 4) if costs else None,
        "total_cost": round(sum(costs), 4) if costs else None,
        "requests_per_floor": round(sum(r["requests"] for r in mine) / floors_played, 1),
        "seconds_per_request": round(sum(r["model_seconds"] for r in mine) / max(1, sum(r["requests"] for r in mine)), 1),
        "survival": survival(mine, shape["max_floor"]),
        "stages": stages(mine, shape["act_count"]),
        "pass_k": pass_at_k(mine, shape["act_count"], shape["by_attempt"]),
        "hp": hp_by_floor(mine, shape["max_floor"]),
    }


def write_page(out_dir: Path, data: dict, title: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload).replace("__TITLE__", title.replace("<", ""))
    out = out_dir / "report.html"
    out.write_text(html, encoding="utf-8")
    return out


def build(suite: str) -> Path:
    suite_dir, config, pairs = load_suite(suite)
    runs = []
    for path, run in pairs:
        replay.build(path, suite_dir / "replays" / (path.stem + ".html"))
        run["replay"] = f"replays/{path.stem}.html"
        runs.append(run)
    runs.sort(key=lambda r: (r["model"], r["effort"], r["protocol"], str(r["seed"]), r["attempt"], r["id"]))

    # Series order is fixed by the suite config, so a model keeps its color as runs are added.
    order = config_order(config)
    keys = sorted({r["key"] for r in runs}, key=lambda k: (order.index(k) if k in order else len(order), k))
    shape = run_shape(runs, lambda r: (r["key"], r["seed"]))
    series = [series_entry(key, [r for r in runs if r["key"] == key], shape) for key in keys]

    data = {
        "suite": suite,
        "cost_audit": cost_audit(suite_dir),
        "config": config,
        "max_floor": shape["max_floor"],
        "act_count": shape["act_count"],
        "max_attempts": shape["max_attempts"],
        # What one draw is in pass@k: a repeated attempt at a seed, or (no repeats) a seed.
        "pass_k_unit": "attempt" if shape["by_attempt"] else "seed",
        "act_starts": shape["act_starts"],
        "series": series,
        "runs": [{k: v for k, v in r.items() if k != "tokens"} for r in runs],
        "character": runs[0]["character"],
        "ascension": runs[0]["ascension"],
        "game_version": runs[0]["game_version"],
        "prompt_fingerprints": sorted({r["prompt_fingerprint"] for r in runs if r["prompt_fingerprint"]}),
        "protocols": protocols_of(runs),
        "harness_commits": sorted({r["harness_commit"] for r in runs if r["harness_commit"]}),
        "seeds": sorted({r["seed"] for r in runs if r["seed"]}),
    }
    return write_page(suite_dir, data, suite)


# ---- comparing suites

COMPARE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _distinct(values) -> list:
    return sorted({v for v in values if v is not None and v != ""}, key=str)


def _limits(runs: list[dict]) -> list[dict]:
    seen = {json.dumps(r.get("limits"), sort_keys=True) for r in runs if r.get("limits")}
    return [json.loads(s) for s in sorted(seen)]


def replay_link(path: Path, suite: str, suite_dir: Path, out_dir: Path) -> str:
    """Link to the replay page the suite's own report built. A run with no
    replay yet, or one recorded since (still playing), gets a fresh page in the
    comparison's own folder instead, so the suite's folder is never written."""
    own = suite_dir / "replays" / (path.stem + ".html")
    try:
        current = own.exists() and own.stat().st_mtime >= path.stat().st_mtime
    except OSError:
        current = False
    if current:
        target = own
    else:
        target = out_dir / "replays" / suite / (path.stem + ".html")
        replay.build(path, target)
    # Relative, so it works opened from disk and served from bench/ alike.
    return urllib.parse.quote(Path(os.path.relpath(target, out_dir)).as_posix())


def build_compare(suites: list[str], name: str | None = None, labels: list[str] | None = None) -> Path:
    if len(suites) < 2:
        raise SystemExit("--compare needs at least two suites")
    if len(set(suites)) != len(suites):
        raise SystemExit("--compare lists the same suite twice")
    labels = labels or list(suites)
    if len(labels) != len(suites):
        raise SystemExit(f"--labels gives {len(labels)} labels for {len(suites)} suites")
    if len(set(labels)) != len(labels) or not all(labels):
        raise SystemExit("--labels must be distinct and non-empty")
    name = name or "-vs-".join(suites)
    if not COMPARE_NAME.match(name):
        raise SystemExit(f"--name {name!r}: use letters, digits, '.', '_' and '-' only")
    out_dir = REPO_ROOT / "bench" / "_compare" / name
    # Replays this comparison built last time for runs that were still playing:
    # start clean so none goes stale once the suite's own report has one.
    if (out_dir / "replays").is_dir():
        shutil.rmtree(out_dir / "replays")

    runs, configs, model_order = [], [], []
    for j, (suite, label) in enumerate(zip(suites, labels)):
        suite_dir, config, pairs = load_suite(suite)
        configs.append(config)
        model_order += config_order(config)
        for path, run in pairs:
            run["replay"] = replay_link(path, suite, suite_dir, out_dir)
            run["suite"] = suite                       # the folder it was read from
            run["suite_label"] = label
            run["suite_index"] = j
            run["model_label"] = run["series"]
            run["model_key"] = run["key"]
            run["key"] = f"{suite}|{run['key']}"
            run["series"] = f"{run['series']} ({label})"
            runs.append(run)
    runs.sort(key=lambda r: (r["model"], r["effort"], r["protocol"], r["suite_index"], str(r["seed"]), r["attempt"], r["id"]))

    # A model keeps one hue across suites (its slot in suite-config order, as in
    # each suite's own report); series run model by model, suite by suite.
    model_keys = sorted({r["model_key"] for r in runs},
                        key=lambda k: (model_order.index(k) if k in model_order else len(model_order), k))
    shape = run_shape(runs, lambda r: (r["suite"], r["model_key"], r["seed"]))
    series = []
    for hue, mk in enumerate(model_keys):
        for j, (suite, label) in enumerate(zip(suites, labels)):
            key = f"{suite}|{mk}"
            mine = [r for r in runs if r["key"] == key]
            if not mine:
                continue
            s = series_entry(key, mine, shape)
            s.update({"suite": suite, "suite_label": label, "suite_index": j, "model_key": mk,
                      "model_label": mine[0]["model_label"], "hue": hue, "lines": [mine[0]["model_label"], label]})
            series.append(s)

    suite_info = []
    for j, (suite, label) in enumerate(zip(suites, labels)):
        mine = [r for r in runs if r["suite"] == suite]
        cells: dict[tuple, int] = {}
        for r in mine:
            cells[(r["model_key"], r["seed"])] = cells.get((r["model_key"], r["seed"]), 0) + 1
        suite_info.append({
            "name": suite,
            "label": label,
            "cost_audit": cost_audit(REPO_ROOT / "bench" / suite),
            "runs": len(mine),
            "running": sum(r["outcome"] == "running" for r in mine),
            "models": [s["model_label"] for s in series if s["suite"] == suite],
            "prompt_fingerprints": _distinct(r["prompt_fingerprint"] for r in mine),
            "protocols": protocols_of(mine),
            "harness_commits": _distinct(r["harness_commit"] for r in mine),
            "harness_version": configs[j].get("harness_version"),
            "playbooks": _distinct(r.get("playbook") for r in mine),
            "seeds": _distinct(r["seed"] for r in mine),
            "attempts": max(cells.values()),
            "planned_attempts": configs[j].get("attempts"),
            "limits": _limits(mine),
            "game_versions": _distinct(r["game_version"] for r in mine),
            "characters": _distinct(r["character"] for r in mine),
            "ascensions": _distinct(r["ascension"] for r in mine),
        })

    models = []
    for hue, mk in enumerate(model_keys):
        per = [next((s for s in series if s["model_key"] == mk and s["suite_index"] == j), None) for j in range(len(suites))]
        base = per[0]
        rows = []
        for a in range(shape["act_count"]):
            # Compare pooled success rates with the interval for that same estimator.
            # pass@1 in the separate pass@k panel averages seeds equally and can
            # differ when censoring leaves different numbers of attempts per seed.
            vals = [None if s is None else {"cleared": s["stages"][a]["cleared"], "decided": s["stages"][a]["decided"],
                                            "pass1": s["stages"][a]["rate"]}
                    for s in per]
            diffs = []
            for v in vals[1:]:
                if base is None or v is None or vals[0]["pass1"] is None or v["pass1"] is None:
                    diffs.append(None)
                    continue
                diffs.append({"d": round(v["pass1"] - vals[0]["pass1"], 4),
                              "ci": diff_interval(vals[0]["cleared"], vals[0]["decided"], v["cleared"], v["decided"])})
            rows.append({"kind": "stage", "label": (base or next(s for s in per if s))["pass_k"][a]["stage"], "values": vals, "diffs": diffs})

        def numeric(kind: str, label: str, value_of) -> dict:
            vals = [None if s is None else value_of([r for r in runs if r["key"] == s["key"]]) for s in per]
            diffs = [None if vals[0] is None or v is None or v["value"] is None or vals[0]["value"] is None
                     else {"d": round(v["value"] - vals[0]["value"], 4),
                           "rel": round(v["value"] / vals[0]["value"] - 1, 4) if vals[0]["value"] else None}
                     for v in vals[1:]]
            return {"kind": kind, "label": label, "values": vals, "diffs": diffs}

        def median_floor(mine):
            f = floor_stats(mine)
            return {"value": f["median"], "q1": f["q1"], "q3": f["q3"], "n": len(mine)}

        def finished_mean(field):
            def value_of(mine):
                xs = [r[field] for r in mine if r["outcome"] != "running" and r[field] is not None]
                return {"value": round(statistics.mean(xs), 4) if xs else None, "n": len(xs)}
            return value_of

        rows.append(numeric("floor", "Median last floor", median_floor))
        rows.append(numeric("floor", "Best last floor", lambda mine: {"value": max(r["final_floor"] for r in mine), "n": len(mine)}))
        rows.append(numeric("cost", "Mean cost per run", finished_mean("cost")))
        rows.append(numeric("count", "Mean requests per run", finished_mean("requests")))
        models.append({"key": mk, "label": (base or next(s for s in per if s))["model_label"], "hue": hue,
                       "series": [None if s is None else s["key"] for s in per], "rows": rows})

    # How big a gap has to be before it is more than luck, at the largest sample
    # size here (a best case: a suite still filling in has fewer).
    decided = [v["decided"] for m in models for v in m["rows"][0]["values"] if v and v["decided"]]
    noise = None
    if decided and max(decided) >= 2:
        n = max(decided)
        need = next((k for k in range(2, n + 1) if diff_interval(1, n, k, n)[0] > 0), None)
        noise = {"n": n, "need": need}

    data = {
        "suite": name,
        "config": {},
        "max_floor": shape["max_floor"],
        "act_count": shape["act_count"],
        "max_attempts": shape["max_attempts"],
        "pass_k_unit": "attempt" if shape["by_attempt"] else "seed",
        "act_starts": shape["act_starts"],
        "series": series,
        "runs": [{k: v for k, v in r.items() if k != "tokens"} for r in runs],
        "character": runs[0]["character"],
        "ascension": runs[0]["ascension"],
        "game_version": ", ".join(_distinct(r["game_version"] for r in runs)),
        "prompt_fingerprints": _distinct(r["prompt_fingerprint"] for r in runs),
        "protocols": protocols_of(runs),
        "harness_commits": _distinct(r["harness_commit"] for r in runs),
        "seeds": _distinct(r["seed"] for r in runs),
        "compare": {"name": name, "suites": suite_info, "configs": configs, "models": models, "noise": noise},
    }
    return write_page(out_dir, data, name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the benchmark report for a suite, or compare several suites.")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--suite", help="build bench/<suite>/report.html")
    what.add_argument("--compare", help="comma-separated suites to compare, earliest (the baseline) first")
    parser.add_argument("--name", help="comparison folder under bench/_compare (default: the suites joined with -vs-)")
    parser.add_argument("--labels", help="comma-separated display names for the compared suites (default: the suite names)")
    parser.add_argument("--open", action="store_true", help="open the report in the default browser")
    args = parser.parse_args()
    if args.compare:
        suites = [s.strip() for s in args.compare.split(",") if s.strip()]
        labels = [s.strip() for s in args.labels.split(",")] if args.labels else None
        out = build_compare(suites, args.name, labels)
    else:
        if args.name or args.labels:
            parser.error("--name and --labels go with --compare")
        out = build(args.suite)
    print(out)
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
