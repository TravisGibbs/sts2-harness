"""Build one page that shows everything benchmarked so far: bench/index.html.

    python agent/index.py
    python agent/index.py --open
    python agent/index.py --include-smoke

Reads every suite under bench/ (bench/<suite>/runs/*.jsonl, through the same
rundata.summarize the reports use) and every comparison under bench/_compare/,
and writes bench/index.html with:

- a leaderboard across suites, one row per (model, effort, protocol, prompt
  fingerprint), since results played under different prompts or decision
  protocols (tools vs the System 1 text protocol) are not comparable;
- a chart of how often each row cleared Act 1, Act 2 and won;
- the suites, the comparisons, and the latest runs, linking to their reports
  and replays.

Only reads bench/; the one file it writes is bench/index.html. Suites whose name
starts with "smoke" are test data and are left out unless --include-smoke is
given; other folders starting with "_" (besides _compare) are always ignored.
Every link is relative, so the page works opened from disk and served from bench/.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report import config_order, cost_audit, run_shape, series_entry  # noqa: E402
from rundata import summarize  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH = REPO_ROOT / "bench"
TEMPLATE = Path(__file__).resolve().parent / "templates" / "index.html"
PRELIMINARY_BELOW = 10   # rows with fewer runs than this are marked preliminary


def link(*parts: str) -> str:
    """A relative link from bench/index.html, safe for any folder or file name."""
    return "/".join(urllib.parse.quote(p) for p in parts)


def utc(iso: str | None) -> str | None:
    """'2026-11-16T01:33:11+00:00' -> '2026-11-16 01:33' (UTC)."""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M")


def distinct(values) -> list:
    return sorted({v for v in values if v is not None and v != ""}, key=str)


def is_smoke(name: str) -> bool:
    return name.lower().startswith("smoke")


def spend(runs: list[dict]) -> tuple[float, int]:
    """Total known cost (running runs count what they have spent so far), and how many runs had no cost recorded."""
    return round(sum(r["cost"] for r in runs if r["cost"] is not None), 4), sum(r["cost"] is None for r in runs)


def read_suite(suite_dir: Path) -> dict | None:
    """One suite folder: its config and its runs. None if it has no usable recordings
    (or vanished while being read: another process may create and delete folders)."""
    runs_dir = suite_dir / "runs"
    try:
        config = json.loads((suite_dir / "suite.json").read_text(encoding="utf-8")) if (suite_dir / "suite.json").exists() else {}
        paths = sorted(runs_dir.glob("*.jsonl"))
        runs, newest = [], 0.0
        for path in paths:
            run = summarize(path)
            if run is None:
                continue
            newest = max(newest, path.stat().st_mtime)
            run["suite"] = suite_dir.name    # the folder it was read from
            replay = suite_dir / "replays" / (path.stem + ".html")
            run["replay"] = link(suite_dir.name, "replays", path.stem + ".html") if replay.exists() else None
            run["replay_stale"] = bool(replay.exists() and replay.stat().st_mtime < path.stat().st_mtime)
            runs.append(run)
    except (OSError, json.JSONDecodeError):
        return None
    if not runs:
        return None
    return {"dir": suite_dir, "config": config, "runs": runs, "newest": newest}


def suite_row(s: dict) -> dict:
    name, config, runs = s["dir"].name, s["config"], s["runs"]
    cells: dict[tuple, int] = {}
    for r in runs:
        cells[(r["key"], r["seed"])] = cells.get((r["key"], r["seed"]), 0) + 1
    planned_attempts = config.get("attempts") or 1
    planned = len(config.get("models") or []) * len(config.get("seeds") or []) * planned_attempts if config else None
    report = s["dir"] / "report.html"
    try:
        report_stale = report.exists() and report.stat().st_mtime < s["newest"]
    except OSError:
        report_stale = False
    total, unknown = spend(runs)
    audit = cost_audit(s["dir"])
    order = config_order(config)
    keys = sorted({r["key"] for r in runs}, key=lambda k: (order.index(k) if k in order else len(order), k))
    starts = sorted(r["started_at"] for r in runs if r["started_at"])
    return {
        "name": name,
        "smoke": is_smoke(name),
        "first": utc(starts[0]) if starts else None,
        "last": utc(starts[-1]) if starts else None,
        "models": [next(r["series"] for r in runs if r["key"] == k) for k in keys],
        "protocols": distinct(r.get("protocol") for r in runs),
        "seeds": distinct(r["seed"] for r in runs),
        "attempts": max(cells.values()),
        "planned_attempts": config.get("attempts"),
        "runs": len(runs),
        "planned": planned,
        "running": sum(r["outcome"] == "running" for r in runs),
        "wins": sum(r["outcome"] == "victory" for r in runs),
        "spend": audit["total_recorded_usd"],
        "cost_audit": audit,
        "spend_unknown": unknown,
        "prompt_fingerprints": distinct(r["prompt_fingerprint"] for r in runs),
        "harness_commits": distinct(r["harness_commit"] for r in runs),
        "character": ", ".join(c.title() for c in distinct(r["character"] for r in runs)) or None,
        "ascensions": distinct(r["ascension"] for r in runs),
        "limits": config.get("limits"),
        "report": link(name, "report.html") if report.exists() else None,
        "report_stale": report_stale,
        "folder": link(name) + "/",
    }


def leaderboard(runs: list[dict]) -> tuple[list[dict], list[dict]]:
    """One row per (model, effort, protocol, prompt fingerprint), built with the suite report's own series_entry.

    The protocol is in the key as well as in the fingerprint (each protocol hashes its own prompt), so a
    text-protocol run can never share a row with a tool-protocol run even if a fingerprint collided."""
    if not runs:
        return [], []
    def group(r):
        # A prompt hash does not cover game difficulty, code or run caps.
        conditions = {k: r.get(k) for k in ("character", "ascension", "game_version", "mod_version",
                                           "harness_commit", "limits", "context_policy", "max_output_tokens")}
        return (r["model"], r["effort"], r.get("protocol") or "tools", r["prompt_fingerprint"] or "?",
                json.dumps(conditions, sort_keys=True))
    shape = run_shape(runs, lambda r: group(r) + (r["seed"],))
    rows = []
    for g in sorted({group(r) for r in runs}):
        mine = sorted((r for r in runs if group(r) == g), key=lambda r: (r["suite"], str(r["seed"]), r["attempt"], r["id"]))
        s = series_entry("|".join(g), mine, shape)
        suites = distinct(r["suite"] for r in mine)
        rows.append({
            "key": s["key"],
            "label": s["label"],
            "model": s["model"],
            "effort": s["effort"],
            "protocol": g[2],
            "fingerprint": g[3],
            "conditions": json.loads(g[4]),
            "runs": s["runs"],
            "wins": s["wins"],
            "deaths": s["deaths"],
            "cut_short": s["cut_short"],
            "infrastructure_failures": s["infrastructure_failures"],
            "behavioral_failures": s["behavioral_failures"],
            "action_calls": s["action_calls"],
            "rejected_action_calls": s["rejected_action_calls"],
            "rejected_action_rate": s["rejected_action_rate"],
            "running": sum(r["outcome"] == "running" for r in mine),
            "stages": s["stages"],
            "median_floor": s["median_floor"],
            "best_floor": s["best_floor"],
            "mean_cost": s["mean_cost"],
            "seeds": distinct(r["seed"] for r in mine),
            "suites": [{"name": n, "runs": sum(r["suite"] == n for r in mine)} for n in suites],
            "preliminary": min(st["decided"] for st in s["stages"]) < PRELIMINARY_BELOW,
        })

    # Best first: win rate, then the furthest act cleared, then the earlier acts; more runs breaks ties.
    def rank(row):
        rates = [st["rate"] if st["rate"] is not None else -1 for st in row["stages"]]
        return tuple(-v for v in reversed(rates)) + (-row["runs"], row["label"], row["fingerprint"])
    rows.sort(key=rank)
    stage_names = [{"stage": st["stage"], "short": st["short"]} for st in rows[0]["stages"]]
    return rows, stage_names


def read_compares(known: set[str]) -> list[dict]:
    out = []
    root = BENCH / "_compare"
    if not root.is_dir():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        page = d / "report.html"
        if not page.exists():
            continue
        entry = {"name": d.name, "report": link("_compare", d.name, "report.html"), "suites": [], "runs": None, "running": 0,
                 "built": utc(datetime.fromtimestamp(page.stat().st_mtime, timezone.utc).isoformat())}
        try:
            c = json.loads((d / "summary.json").read_text(encoding="utf-8")).get("compare") or {}
            entry["suites"] = [{"name": s.get("name"), "label": s.get("label"), "runs": s.get("runs"),
                                "prompt_fingerprints": s.get("prompt_fingerprints") or [],
                                "report": link(s["name"], "report.html") if s.get("name") in known else None}
                               for s in c.get("suites", [])]
            entry["runs"] = sum(s.get("runs") or 0 for s in c.get("suites", []))
            entry["running"] = sum(s.get("running") or 0 for s in c.get("suites", []))
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
        out.append(entry)
    return out


def build(include_smoke: bool = False, recent: int = 25) -> Path:
    suites, skipped = [], []
    for d in sorted(p for p in BENCH.iterdir() if p.is_dir()) if BENCH.is_dir() else []:
        if d.name.startswith("_"):
            continue                      # _compare is read separately; other "_" folders are scratch
        if is_smoke(d.name) and not include_smoke:
            skipped.append(d.name)
            continue
        s = read_suite(d)
        if s is not None:
            suites.append(s)
    runs = [r for s in suites for r in s["runs"]]
    if not runs:
        raise SystemExit(f"no usable recordings under {BENCH}")

    rows, stage_names = leaderboard(runs)
    suite_rows = sorted((suite_row(s) for s in suites), key=lambda s: s["first"] or "")
    starts = sorted(r["started_at"] for r in runs if r["started_at"])
    total, unknown = spend(runs)
    total = round(sum(s["cost_audit"]["total_recorded_usd"] for s in suite_rows), 6)
    latest = sorted((r for r in runs if r["started_at"]),
                    key=lambda r: (utc(r["started_at"]), r["started_at"], r["id"]),
                    reverse=True)[:recent]
    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "include_smoke": include_smoke,
        "skipped": skipped,
        "preliminary_below": PRELIMINARY_BELOW,
        "totals": {
            "runs": len(runs),
            "suites": len(suites),
            "spend": total,
            "spend_unknown": unknown,
            "running": sum(r["outcome"] == "running" for r in runs),
            "first": utc(starts[0]) if starts else None,
            "last": utc(starts[-1]) if starts else None,
            "game_versions": distinct(r["game_version"] for r in runs),
            "characters": [c.title() for c in distinct(r["character"] for r in runs)],
            "ascensions": distinct(r["ascension"] for r in runs),
            "models": distinct(r["series"] for r in runs),
            "fingerprints": distinct(r["prompt_fingerprint"] for r in runs),
            "protocols": distinct(r.get("protocol") for r in runs),
            "mean_run_cost": round(statistics.mean(r["cost"] for r in runs if r["cost"] is not None and r["outcome"] != "running"), 4)
                             if any(r["cost"] is not None and r["outcome"] != "running" for r in runs) else None,
        },
        "stage_names": stage_names,
        "leaderboard": rows,
        "suites": suite_rows,
        "compares": read_compares({s["dir"].name for s in suites}),
        "recent": [{
            "started": utc(r["started_at"]),
            "suite": r["suite"],
            "label": r["series"],
            "fingerprint": r["prompt_fingerprint"],
            "protocol": r.get("protocol"),
            "seed": r["seed"],
            "attempt": r["attempt"],
            "outcome": r["outcome"],
            "final_floor": r["final_floor"],
            "final_act": r["final_act"],
            "cost": r["cost"],
            "requests": r["requests"],
            "replay": r["replay"],
            "replay_stale": r["replay_stale"],
        } for r in latest],
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out = BENCH / "index.html"
    out.write_text(TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Build bench/index.html: every suite, comparison and run in one place.")
    parser.add_argument("--include-smoke", action="store_true", help="also include suites whose name starts with 'smoke'")
    parser.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = parser.parse_args()
    out = build(include_smoke=args.include_smoke)
    print(out)
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
