"""Film many recorded runs in the real game, several game instances at once.

    python tools/render/render_batch.py --select featured --instances 2,3
    python tools/render/render_batch.py --select all --instances 2,3,4
    python tools/render/render_batch.py --ids <run id> [<run id> ...]

Runs come from the site manifest (site_build/manifest.json, written by tools/site/build_site.py), so
only runs the benchmark counts are filmed. "featured" is each model's furthest run (then a win, then
the lowest seed); "all" is every run. A run whose render.json is already "verified" is skipped
unless --force. Each instance films one run at a time (render_run.py); see that file for what a
render checks and writes.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import render_run  # noqa: E402

SEED_ORDER = {"BENCH01": 1, "BENCH02": 2, "BENCH03": 3, "BENCH04": 4}


def featured(runs: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for r in runs:
        key = (r.get("final_floor") or 0, r.get("outcome") == "victory", -SEED_ORDER.get(r.get("seed"), 9), -(r.get("attempt") or 1))
        if r["model"] not in best or key > best[r["model"]][0]:
            best[r["model"]] = (key, r)
    return [r for _, r in best.values()]


def done(out_root: Path, run: dict) -> bool:
    path = Path(run["path"])
    report = out_root / path.parent.parent.name / path.stem / "render.json"
    try:
        return json.loads(report.read_text(encoding="utf-8")).get("status") == "verified"
    except (OSError, ValueError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", type=Path, default=ROOT / "site_build" / "manifest.json")
    ap.add_argument("--select", choices=("featured", "all"), default="featured")
    ap.add_argument("--ids", nargs="*", default=None, help="run ids from the manifest instead of --select")
    ap.add_argument("--instances", default="2,3", help="game instances to film on (1 or more each)")
    ap.add_argument("--out", type=Path, default=ROOT / "renders")
    ap.add_argument("--speed", type=float, default=3.0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="film at most this many runs")
    args = ap.parse_args()

    runs = json.loads(args.manifest.read_text(encoding="utf-8"))["runs"]
    if args.ids:
        wanted = set(args.ids)
        chosen = [r for r in runs if r["id"] in wanted]
        missing = wanted - {r["id"] for r in chosen}
        if missing:
            print("not in the manifest:", ", ".join(sorted(missing)))
    else:
        chosen = featured(runs) if args.select == "featured" else list(runs)
    todo = [r for r in chosen if args.force or not done(args.out, r)]
    # Longest first, so the slow ones don't end up last on one instance.
    todo.sort(key=lambda r: -(r.get("final_floor") or 0))
    if args.limit:
        todo = todo[: args.limit]
    instances = [int(n) for n in args.instances.split(",") if n.strip()]
    print(f"{len(chosen)} runs selected, {len(chosen) - len(todo)} already filmed, {len(todo)} to film on instances {instances}")

    jobs: queue.Queue = queue.Queue()
    for r in todo:
        jobs.put(r)
    results: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(instance: int) -> None:
        while True:
            try:
                run = jobs.get_nowait()
            except queue.Empty:
                return
            report = render_run.render(Path(run["path"]), instance, args.out, args.speed, 30, "1280x720", 960, 30, False)
            if report.get("status") != "verified":
                # Rarely, timing on a busy GPU makes a filmed replay miss a click; one more try settles it.
                print(f"-- {run['id']}: {report.get('status')}, filming once more", flush=True)
                report = render_run.render(Path(run["path"]), instance, args.out, args.speed, 30, "1280x720", 960, 30, False)
            with lock:
                results.append((run["id"], report.get("status", "?")))
                print(f"== {len(results)}/{len(todo)} {run['id']}: {report.get('status')}", flush=True)

    started = time.time()
    threads = []
    for i, n in enumerate(instances):
        t = threading.Thread(target=worker, args=(n,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(20 if i + 1 < len(instances) else 0)  # stagger startups: launching is the heaviest moment
    for t in threads:
        t.join()
    counts: dict[str, int] = {}
    for _, status in results:
        counts[status] = counts.get(status, 0) + 1
    print(f"done in {(time.time() - started) / 60:.0f} min: {counts}")
    return 0 if all(s == "verified" for _, s in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
