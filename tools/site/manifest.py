"""Pick the runs the public benchmark page shows and reduce them to one manifest.

The manifest is the contract between the site builders:
  page.py   reads it to write index.html (blog post + benchmark) and annex/index.html
  video.py  reads it to render annex/videos/<id>.mp4 per run and rollup.mp4 (8x8)

Rules (stated on the page too):
  * Only the standard benchmark: Ironclad, Ascension 0, seeds BENCH01-04, from the suites in SUITES.
  * Each model is scored on the newest harness version it has runs on; older-version runs of that
    model are left out, because older harnesses misread some models' replies (see the diagnosis).
    Scripted policies do not depend on the harness, so all their runs count.
  * Only runs that ended in the game count: a win or a death. Runs still playing, runs lost to
    infrastructure (API billing errors, bridge errors) and runs cut short (stalled, hit a cap) are
    left out, after the harness version is picked. A model with no such run is left out.

Game footage (optional): tools/render/ re-plays a run in the real game and films it into
renders/<suite>/<stem>/ (game.mp4, poster.jpg, render.json, fights/floor_NN.mcr). Only renders whose
render.json says "verified" are published; the rest are only counted (manifest["renders"]). Fight
replays (.mcr) come from bench/<suite>/runs/<stem>.fights/ (written by bench.py), else from a
verified render's fights/ folder. The renders folder is --renders, else $STS2_RENDERS, else <repo>/renders.

    python tools/site/manifest.py [--out site_build/manifest.json] [--renders DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
from rundata import INFRASTRUCTURE_FAILURES, read_events, summarize  # noqa: E402

BENCH = ROOT / "bench"
RENDER_STATUSES = ("verified", "diverged", "failed")


def renders_root(renders: str | Path | None = None) -> Path:
    return Path(renders or os.environ.get("STS2_RENDERS") or ROOT / "renders")
SEEDS = ["BENCH01", "BENCH02", "BENCH03", "BENCH04"]
SUITES = [
    "ladder-ironclad-a0", "models3-ironclad-a0", "openmodels-ironclad-a0", "jev-ironclad-a0",
    "ladder2-ironclad-a0", "ladder2-openai-ironclad-a0",
    "diag-base-ironclad-a0", "diag-fix-ironclad-a0", "diag-fix042-ironclad-a0",
]
ACT_COUNT = 3
LOGO_CDN = "https://cdn.jsdelivr.net/npm/@lobehub/icons-static-svg@1.98.0/icons/"

# model id -> short name, maker, logo file (None: drawn locally), open weights?
MODELS = {
    "claude-opus-5-5": ("Opus 5.5", "Anthropic", "claude-color.svg", False),
    "claude-sonnet-5-5": ("Sonnet 5.5", "Anthropic", "claude-color.svg", False),
    "claude-haiku-5-5": ("Haiku 5.5", "Anthropic", "claude-color.svg", False),
    "openrouter:openai/gpt-6.1-sol": ("GPT-6.1 Sol", "OpenAI", "openai.svg", False),
    "openrouter:openai/gpt-6.1-terra": ("GPT-6.1 Terra", "OpenAI", "openai.svg", False),
    "openrouter:openai/gpt-6-luna": ("GPT-6 Luna", "OpenAI", "openai.svg", False),
    "openrouter:openai/gpt-6-astra": ("GPT-6 Astra", "OpenAI", "openai.svg", False),
    "openrouter:openai/gpt-oss-120b": ("gpt-oss 120B", "OpenAI", "openai.svg", True),
    "openrouter:deepseek/deepseek-v4-flash": ("DeepSeek V4 Flash", "DeepSeek", "deepseek-color.svg", True),
    "openrouter:deepseek/deepseek-v4-pro-0813": ("DeepSeek V4 Pro", "DeepSeek", "deepseek-color.svg", True),
    "openrouter:deepseek/deepseek-v4.1-flash-1017": ("DeepSeek V4.1 Flash", "DeepSeek", "deepseek-color.svg", True),
    "openrouter:meta-llama/llama-3.1-8b-instruct": ("Llama 3.1 8B", "Meta", "meta-color.svg", True),
    "openrouter:minimax/minimax-m3": ("MiniMax M3", "MiniMax", "minimax-color.svg", True),
    "openrouter:mistralai/mistral-large-4-0": ("Mistral Large 4", "Mistral", "mistral-color.svg", True),
    "openrouter:mistralai/mistral-nemo": ("Mistral Nemo", "Mistral", "mistral-color.svg", True),
    "openrouter:moonshotai/kimi-k3.1-pro": ("Kimi K3.1 Pro", "Moonshot", "kimi-color.svg", True),
    "openrouter:nvidia/nemotron-3-ultra-550b-a55b": ("Nemotron 3 Ultra", "NVIDIA", "nvidia-color.svg", True),
    "openrouter:qwen/qwen3-235b-a22b-2507": ("Qwen3 235B", "Qwen", "qwen-color.svg", True),
    "openrouter:qwen/qwen3-30b-a3b-instruct-2507": ("Qwen3 30B", "Qwen", "qwen-color.svg", True),
    "openrouter:qwen/qwen3.8-2.4t-a95b": ("Qwen3.8", "Qwen", "qwen-color.svg", True),
    "openrouter:xiaomi/mimo-v2.6-pro": ("MiMo V2.6 Pro", "Xiaomi", "xiaomimimo.svg", True),
    "openrouter:z-ai/glm-5.3": ("GLM-5.3", "Z.ai", "zai.svg", True),
    "policy:greedy": ("Greedy bot", "Scripted", None, None),
    "policy:random_legal": ("Random bot", "Scripted", None, None),
    "decisions:typesafe/jev-1.14": ("Jev 1.14", "OpenRouter", "openrouter.svg", None),
}


ENDED_IN_GAME = ("victory", "defeat")


def ended_in_game(r: dict) -> bool:
    """A run the board counts: it ended in the game (a win or a death), not cut short."""
    return r["outcome"] in ENDED_IN_GAME


def is_scripted(model: str) -> bool:
    return model.startswith("policy:")


def pools_versions(model: str) -> bool:
    """Scripted policies and the Decisions API do not read model replies, so the harness version doesn't matter."""
    return model.startswith(("policy:", "decisions:"))


def version_key(v: str | None) -> tuple:
    return tuple(int(p) for p in (v or "0").split(".") if p.isdigit())


def run_record(suite: str, path: Path) -> dict | None:
    s = summarize(path)
    if s is None:
        return None
    events = read_events(path)
    start = next(e for e in events if e["type"] == "run_start")
    end = next((e for e in reversed(events) if e["type"] == "run_end"), None)
    if s["seed"] not in SEEDS or (s["character"] or "").upper() != "IRONCLAD" or s["ascension"] != 0:
        return None
    if s["outcome"] == "running" or s["outcome"] in INFRASTRUCTURE_FAILURES:
        return None
    rid = f"{suite}--{path.stem}"
    cost = end.get("cost") if end and end.get("cost") is not None else s["cost"]
    return {
        "id": rid,
        "suite": suite,
        "path": str(path),
        "model": s["model"],
        "effort": s["effort"],
        "protocol": s["protocol"],
        "seed": s["seed"],
        "attempt": s["attempt"],
        "harness": start.get("harness_version"),
        "outcome": s["outcome"],
        "final_floor": s["final_floor"],
        "final_act": s["final_act"],
        "max_act": s["max_act"],
        "hp_end": s["hp_end"],
        "cost": round(cost, 4) if cost is not None else None,
        "cost_source": (end or {}).get("cost_source") or "estimated",
        "requests": s["requests"],
        "duration_s": s["duration_s"],
        "started_at": s["started_at"],
        "video": f"annex/videos/{rid}.mp4",
        "poster": f"annex/videos/{rid}.jpg",
        "replay": f"annex/replays/{rid}.html",
        "raw": f"annex/raw/{rid}.jsonl.gz",
    }


def mcr_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.mcr")) if folder.is_dir() else []


def add_footage(r: dict, renders: Path, counts: dict) -> None:
    """Add the game footage and fight replay fields to a run record, where they exist.

    `game_dir` and `fights_dir` are source folders for build_site.py; the pages only read the
    annex/ links."""
    path = Path(r["path"])
    folder = renders / r["suite"] / path.stem
    verified = False
    info_path = folder / "render.json"
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            status = info.get("status")
        except (OSError, ValueError):
            info, status = {}, "failed"
        if status not in RENDER_STATUSES:
            status = "failed"
        if status == "verified" and not (folder / "game.mp4").is_file():
            status = "failed"  # nothing to show
        counts[status] += 1
        if status == "verified":
            verified = True
            rid = r["id"]
            r["game_dir"] = str(folder)
            r["game_video"] = f"annex/game/{rid}.mp4"
            if (folder / "poster.jpg").is_file():
                r["game_poster"] = f"annex/game/{rid}.jpg"
            chapters = [{"t": float(c["t"]), "floor": c.get("floor"), "label": str(c.get("label") or f"Floor {c.get('floor')}")}
                        for c in info.get("chapters") or [] if c.get("t") is not None]
            r["game_render"] = {
                "status": status,
                "speed": info.get("speed"),
                "duration_s": info.get("duration_s"),
                "chapters": sorted(chapters, key=lambda c: c["t"]),
                "steps": info.get("steps"),
                "steps_matched": info.get("steps_matched"),
            }
    # Fights recorded during the benchmark run itself are preferred; a render's own copies are only
    # trusted when the render matched the recording step for step.
    for fights in (path.parent / f"{path.stem}.fights", folder / "fights" if verified else None):
        if fights is not None and mcr_files(fights):
            r["fights_dir"] = str(fights)
            r["fights_zip"] = f"annex/fights/{r['id']}.zip"
            break


def featured_game(runs: list[dict]) -> str | None:
    """The model's best run with verified footage: furthest floor, then a win, then the lowest seed."""
    filmed = [r for r in runs if r.get("game_video")]
    if not filmed:
        return None
    best = min(filmed, key=lambda r: (-r["final_floor"], r["outcome"] != "victory", r["seed"], r["attempt"], r["suite"]))
    return best["id"]


def model_stats(runs: list[dict]) -> dict:
    """Stats over a model's runs, all of which ended in the game (see ended_in_game)."""
    floors = [r["final_floor"] for r in runs]
    wins = sum(r["outcome"] == "victory" for r in runs)

    def cleared(act: int) -> dict:
        ok = sum(r["max_act"] > act or r["outcome"] == "victory" for r in runs)
        return {"cleared": ok, "decided": len(runs), "rate": round(ok / len(runs), 4)}

    costs = [r["cost"] for r in runs if r["cost"] is not None]
    return {
        "runs": len(runs),
        "wins": wins,
        "win_rate": round(wins / len(runs), 4),
        "act1": cleared(1),
        "act2": cleared(2),
        "mean_floor": round(statistics.mean(floors), 1),
        "median_floor": statistics.median(floors),
        "best_floor": max(floors),
        "cost_per_run": round(statistics.mean(costs), 4) if costs else None,
        "cost_total": round(sum(costs), 4) if costs else None,
        "cost_source": "billed" if any(r["cost_source"] == "reported" for r in runs) else "estimated",
        "minutes_per_run": round(statistics.mean(r["duration_s"] or 0 for r in runs) / 60, 1),
        "requests_per_run": round(statistics.mean(r["requests"] for r in runs)),
    }


def build(renders: str | Path | None = None) -> dict:
    renders = renders_root(renders)
    counts = dict.fromkeys(RENDER_STATUSES, 0)
    by_model: dict[str, list[dict]] = {}
    for suite in SUITES:
        runs_dir = BENCH / suite / "runs"
        if not runs_dir.is_dir():
            continue
        for path in sorted(runs_dir.glob("*.jsonl")):
            r = run_record(suite, path)
            if r and r["model"] in MODELS:
                by_model.setdefault(r["model"], []).append(r)

    models, runs = [], []
    for model, mine in by_model.items():
        if not pools_versions(model):
            newest = max((r["harness"] for r in mine), key=version_key)
            mine = [r for r in mine if r["harness"] == newest]
        mine = [r for r in mine if ended_in_game(r)]
        if not mine:
            continue
        mine.sort(key=lambda r: (r["seed"], r["attempt"], r["suite"]))
        for r in mine:
            add_footage(r, renders, counts)
        short, maker, logo, open_weights = MODELS[model]
        efforts = sorted({r["effort"] for r in mine})
        models.append({
            "model": model,
            "name": short,
            "maker": maker,
            "logo": LOGO_CDN + logo if logo else None,
            "open_weights": open_weights,
            "scripted": is_scripted(model),
            "effort": efforts[0] if len(efforts) == 1 else efforts,
            "harness": sorted({r["harness"] for r in mine}, key=version_key),
            "suites": sorted({r["suite"] for r in mine}),
            "stats": model_stats(mine),
            "runs": [r["id"] for r in mine],
            "featured_game": featured_game(mine),
        })
        for r in mine:
            r["name"] = short
        runs.extend(mine)

    # Rank: win rate, then Act 2 and Act 1 clear rates, then mean floor.
    def rank_key(m: dict) -> tuple:
        s = m["stats"]
        return (-(s["win_rate"] or 0), -(s["act2"]["rate"] or 0), -(s["act1"]["rate"] or 0), -s["mean_floor"])
    models.sort(key=rank_key)
    for i, m in enumerate(models, 1):
        m["rank"] = i

    return {
        "generated_from": [s for s in SUITES if (BENCH / s).is_dir()],
        "seeds": SEEDS,
        "character": "Ironclad",
        "ascension": 0,
        "act_count": ACT_COUNT,
        "max_floor": 48,
        "act_floors": [17, 33, 48],  # boss floor of each act
        "models": models,
        "runs": runs,
        "rollup": rollup(models, {r["id"]: r for r in runs}),
        "game_grid": game_grid(models, {r["id"]: r for r in runs}),
        "renders": counts,  # renders of the runs above; only verified ones are published
    }


def rollup(models: list[dict], runs: dict) -> dict:
    """8 rows x 8 runs for the 8x8 video: the 7 best-ranked models with at least 8 runs, plus the
    lowest-ranked one as the floor."""
    eligible = [m for m in models if len(m["runs"]) >= 8]
    if len(eligible) > 8:
        eligible = eligible[:7] + eligible[-1:]
    rows = []
    for m in eligible:
        ids = sorted(m["runs"], key=lambda i: (runs[i]["seed"], runs[i]["attempt"]))[:8]
        rows.append({"model": m["model"], "name": m["name"], "runs": ids})
    return {"rows": rows, "columns": [f"{s} #{a}" for s in SEEDS for a in (1, 2)]}


# The game-footage grid on the benchmark page: the strongest models of the labs people ask about,
# two typical runs each. Order = grid order.
GRID_MODELS = ["Opus 5.5", "GPT-6.1 Sol", "Sonnet 5.5", "GPT-6.1 Terra",
               "Kimi K3.1 Pro", "GLM-5.3", "DeepSeek V4.1 Flash", "DeepSeek V4 Pro"]
GRID_RUNS_PER_MODEL = 2


def representative(mine: list[dict], k: int) -> list[dict]:
    """k runs spread evenly through a model's results, so the grid wins and dies about as often as
    the model did: runs ordered worst to best (a win beats any loss, then by floor reached), then
    the ones at 1/(k+1), 2/(k+1), ... of the way up. Between runs with the same result, a seed not
    already shown is preferred."""
    seed_order = {s: i for i, s in enumerate(SEEDS)}
    result = lambda r: (r["outcome"] == "victory", r.get("final_floor") or 0)  # noqa: E731
    ordered = sorted(mine, key=lambda r: (result(r), seed_order.get(r["seed"], 9), r["attempt"]))
    picked: list[dict] = []
    for j in range(1, k + 1):
        target = ordered[round(j / (k + 1) * (len(ordered) - 1))]
        same = [r for r in ordered if result(r) == result(target) and r not in picked]
        same.sort(key=lambda r: r.get("game_render") is None)  # stable: filmed runs first, order kept
        fresh = [r for r in same if all(p["seed"] != r["seed"] for p in picked)]
        picked.append((fresh or same or [target])[0])
    return picked


def game_grid(models: list[dict], runs: dict) -> dict | None:
    """Rows of the real-footage grid, or None until every chosen run has verified footage."""
    rows = []
    for name in GRID_MODELS:
        m = next((m for m in models if m["name"] == name), None)
        if m is None or len(m["runs"]) < GRID_RUNS_PER_MODEL:
            return None
        picked = representative([runs[i] for i in m["runs"]], GRID_RUNS_PER_MODEL)
        if not all(r.get("game_render") for r in picked):
            return None
        rows.append({"model": m["model"], "name": m["name"], "maker": m["maker"], "runs": [r["id"] for r in picked]})
    return {"rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "site_build" / "manifest.json"))
    ap.add_argument("--renders", default=None, help="game renders folder (default: $STS2_RENDERS or <repo>/renders)")
    args = ap.parse_args()
    data = build(args.renders)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    for m in data["models"]:
        s = m["stats"]
        print(f"{m['rank']:>2} {m['name']:<20} v{'/'.join(m['harness']):<6} runs {s['runs']:>2}  wins {s['wins']}  "
              f"act1 {s['act1']['cleared']}/{s['act1']['decided']}  act2 {s['act2']['cleared']}/{s['act2']['decided']}  "
              f"floor {s['mean_floor']:>4}  ${s['cost_per_run']}")
    print("rollup rows:", [r["name"] for r in data["rollup"]["rows"]])
    print("renders:", data["renders"])
    print(out, f"{len(data['runs'])} runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
