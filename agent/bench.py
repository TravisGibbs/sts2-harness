"""Benchmark runner: every model plays the same seeded runs under the same rules.

    python agent/bench.py --suite pilot \
        --models claude-haiku-5-5,claude-sonnet-5-5 \
        --seeds BENCH01,BENCH02,BENCH03 --attempts 2 --parallel 4

What is held fixed for every run in a suite, so results are comparable:
  - the run itself: (character, ascension, seed) with unlocks pinned to "everything",
    so the map, encounters, card offers and relics do not depend on the profile
  - the prompt, tools and context policy (recorded as a fingerprint in each run)
  - the per-run request cap (a run that hits it is "truncated", not a death)

A model entry is a spec `[provider:]model[@effort][@protocol]` (see agent/providers.py):
claude-opus-5-5@medium, openai:gpt-5.1@high, openrouter:meta-llama/llama-4-maverick,
local:qwen3:32b. A plain Claude entry may still give its effort as `model:effort`
(claude-opus-5-5:medium). What each provider forces to differ (effort controls,
caching) is recorded in every run's run_start.
`@text` plays the System 1 text protocol (agent/system1.py) instead of the tool protocol, and
`policy:random_legal`, `policy:greedy`, `policy:module:function` or `policy:http://host:port/act`
plays a non-LLM policy under it; the same model may be listed under both protocols. Text-protocol
recordings carry the protocol in their file name (`<model>__<effort>__text__<seed>__a01.jsonl`).
`--attempts N` plays every (model, seed) N times. `--parallel K` plays K runs at once,
each in its own game instance (`launch.ps1 -Instance 1..K`: own port, own user data);
`--first-instance N` uses instances N..N+K-1 instead (also with --parallel 1).
Runs are resumable: finished (model, seed, attempt) triples are skipped on the next invocation.
Recordings land in bench/<suite>/runs/, one JSONL per run.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import DEFAULT_BRIDGE, Bridge, BridgeError, BridgeUnreachable, instance_url  # noqa: E402
from spire_agent import (  # noqa: E402
    HARNESS_VERSION,
    REPO_ROOT,
    git_commit,
    key_problem,
    load_prices,
    parse_model_spec,
    price_for,
    prompt_fingerprint,
    resolve_effort,
    run_once,
    safe_filename,
)

# Outcomes that count as a finished attempt. Anything else (errors, Ctrl+C) is retried.
FINAL_OUTCOMES = {"victory", "defeat", "request_limit", "cost_limit", "floor_limit", "stalled", "refusal"}

# A job whose run could not be started (or whose game died under it) goes back on the queue this many times.
MAX_TRIES = 3
# A worker whose game fails this many jobs in a row stops taking jobs and leaves them to the others.
MAX_CONSECUTIVE_FAILURES = 3

_print_lock = threading.Lock()


def say(text: str) -> None:
    """One whole line at a time, so lines from different workers never interleave."""
    with _print_lock:
        print(text, flush=True)


class RunAborted(Exception):
    """Raised out of a bridge call to end a run that cannot finish. Deliberately not a
    BridgeError: the agent hands those to the model as tool errors and keeps spending."""


class SuiteCancelled(RunAborted):
    pass


class InstanceDown(RunAborted):
    pass


class GuardedBridge(Bridge):
    """A Bridge that stops a run once the suite is cancelled or its game is gone."""

    DOWN_AFTER = 3  # consecutive requests with no answer at all

    def __init__(self, base_url: str, cancelled: threading.Event):
        super().__init__(base_url)
        self._cancelled = cancelled
        self._unanswered = 0

    def _request(self, path: str, body: str | None = None, timeout: float = 240) -> str:
        if self._cancelled.is_set():
            raise SuiteCancelled("the suite was interrupted")
        try:
            result = super()._request(path, body, timeout)
        except BridgeUnreachable as e:
            self._unanswered += 1
            if self._unanswered >= self.DOWN_AFTER:
                self._unanswered = 0
                raise InstanceDown(f"the game at {self.base} stopped answering") from e
            raise
        self._unanswered = 0
        return result

    def reset(self) -> None:
        self._unanswered = 0


def fingerprint_for(spec, character: str) -> str:
    """The prompt fingerprint a model plays under: its protocol's prompt for this character."""
    if spec.protocol == "decisions":
        from decisions import prompt_fingerprint as decisions_fingerprint

        return decisions_fingerprint(character)
    if spec.protocol == "text":
        from system1 import text_prompt_fingerprint

        return text_prompt_fingerprint(character, policy=spec.is_policy)
    return prompt_fingerprint(character)


def read_end(path: Path) -> dict | None:
    """The run_end event of a recording, or None if the run never finished."""
    if not path.exists():
        return None
    end = None
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "run_end":
                end = event
    return end


def recorded_cost(path: Path) -> float:
    """What a recording's model requests cost so far; for runs that never reached a run_end."""
    total = 0.0
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "step":
                    total += event.get("cost") or 0.0
    except OSError:
        pass
    return total


def archive_recording(path: Path) -> Path | None:
    """Preserve failed/partial attempts outside the scored runs directory before a retry."""
    if not path.exists():
        return None
    archive = path.parent.parent / "archive"
    archive.mkdir(exist_ok=True)
    target = archive / f"{path.stem}__retry-{uuid.uuid4().hex[:12]}{path.suffix}"
    path.rename(target)
    fights = fights_dir(path)
    if fights.is_dir():  # its fight replays go with it, so they never pair up with the retry
        fights.rename(target.with_suffix(".fights"))
    return target


def fights_dir(recording: Path) -> Path:
    """Where a recording's fight replays are kept: bench/<suite>/runs/<stem>.fights/."""
    return recording.with_suffix(".fights")


def mod_replay_folder(instance: int | None) -> Path | None:
    """The SpireBridge mod's per-floor replay folder for a game instance, or None if it is not exactly
    one folder. Instance None/0 is the ordinary game (real %APPDATA%); n >= 1 uses .instances/<n>/appdata."""
    if instance:
        appdata = REPO_ROOT / ".instances" / str(instance) / "appdata"
    elif os.environ.get("APPDATA"):
        appdata = Path(os.environ["APPDATA"])
    else:
        return None
    found = [p for p in (appdata / "SlayTheSpire2").glob("steam/*/modded/profile*/replays/spirebridge") if p.is_dir()]
    return found[0] if len(found) == 1 else None


def keep_fight_replays(recording: Path, instance: int | None, since: float, label: str) -> None:
    """Copy the instance's floor_NN.mcr files (the mod empties its folder when a run starts) next to the
    recording. Only files written since the run started count. Never raises: a run is not failed over this."""
    try:
        folder = mod_replay_folder(instance)
        files = sorted(f for f in folder.glob("floor_*.mcr") if f.stat().st_mtime >= since - 5) if folder else []
        if not files:
            where = "no single replay folder" if folder is None else "no fight replays"
            say(f"{label}: fight replays not kept ({where} for {'the game' if not instance else f'instance {instance}'})")
            return
        dest = fights_dir(recording)
        dest.mkdir(exist_ok=True)
        for old in dest.glob("*.mcr"):
            old.unlink()
        for f in files:
            shutil.copy2(f, dest / f.name)
    except Exception as e:  # noqa: BLE001
        say(f"{label}: fight replays not kept: {type(e).__name__}: {e}")


def suite_recorded_cost(suite_dir: Path) -> float:
    """Count every API request, including abandoned attempts that are no longer scored."""
    total = sum(recorded_cost(p) for folder in ("runs", "archive")
                for p in (suite_dir / folder).glob("*.jsonl"))
    adjustments = suite_dir / "cost_adjustments.json"
    if adjustments.exists():
        for entry in json.loads(adjustments.read_text(encoding="utf-8")):
            amount = float(entry["amount_usd"])
            if not math.isfinite(amount) or amount < 0:
                raise ValueError("cost adjustments must be finite and nonnegative")
            total += amount
    return total


def validate_resume_config(previous: dict, current: dict) -> None:
    """Never silently label old results with a new experiment's settings."""
    changed = [key for key in ("character", "ascension", "seeds", "attempts", "limits", "prompt_fingerprint")
               if previous.get(key) != current.get(key)]
    # A harness version change alters behaviour the prompt fingerprint does not cover (guards, game
    # answers), so resuming across it would pool two experiments. Suites from before the version was
    # recorded have no value to compare.
    if previous.get("harness_version") and previous.get("harness_version") != current.get("harness_version"):
        changed.append("harness_version")
    def contestants(config):
        return sorted((m["model"], m["effort"], m.get("protocol", "tools"),
                       m.get("prompt_fingerprint", config.get("prompt_fingerprint")))
                      for m in config.get("models", []))
    if contestants(previous) != contestants(current):
        changed.append("models/protocols/prompts")
    if changed:
        raise ValueError("existing suite differs in " + ", ".join(changed) +
                         "; use a new --suite name to keep results comparable")


def launch_script(*extra: str, timeout: float = 240) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO_ROOT / "launch.ps1"), *extra],
        check=False,
        timeout=timeout,
        stdout=subprocess.DEVNULL,  # its progress lines would interleave with the suite's own
    )


def ensure_game(bridge: Bridge, instance: int | None = None) -> bool:
    """Start the game headless if the bridge is not answering. Returns True if it had to be launched.

    `instance` None is the ordinary game on whatever URL the bridge has; n >= 1 is an
    extra instance owned by the harness, which may also be force-restarted if it hangs.
    """
    try:
        bridge.health()
        return False
    except BridgeError:
        pass
    if sys.platform != "win32":
        raise BridgeError("the game is not running; start it with the SpireBridge mod first")
    who = "game" if instance is None else f"game instance {instance}"
    say(f"{who} not running; launching headless...")
    extra = [] if instance is None else ["-Instance", str(instance)]
    launch_script(*extra)
    try:
        bridge.health()
    except BridgeError:
        if not instance:
            raise
        # A harness-owned instance that is up but not answering: stop that one process and try once more.
        launch_script("-Stop", "-Instance", str(instance), timeout=90)
        launch_script(*extra)
        bridge.health()
    return True


@dataclass
class Job:
    index: int  # 1-based position in the whole plan
    attempt: int
    seed: str
    model: str  # recorded name (provider:model, or the bare Claude id)
    effort: str
    protocol: str
    spec: str  # the model spec as given on the command line
    path: Path
    stale: tuple[Path, ...]  # unfinished recordings of this job to clear before it is played
    tries: int = 0


class Suite:
    """What the workers share: the queue of runs still to play, the spend so far, and the stop flags."""

    def __init__(self, args: argparse.Namespace, total: int, spent: float):
        self.args = args
        self.total = total
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.lock = threading.Lock()
        self.spent = spent
        self.draining = threading.Event()  # take no new jobs; runs in flight finish
        self.cancelled = threading.Event()  # Ctrl+C: runs in flight are abandoned too
        self.launched: set[int] = set()  # extra instances this process started
        self.prices: dict = {}  # prices for models not built in (--prices / SPIRE_PRICES)

    def add_spend(self, amount: float) -> float:
        with self.lock:
            self.spent += amount
            return self.spent

    def budget_left(self) -> bool:
        """False, once, flips the suite to draining. Checked under the lock so every worker agrees."""
        budget = self.args.budget
        with self.lock:
            if budget is None or self.spent < budget:
                return True
            first = not self.draining.is_set()
            self.draining.set()
        if first:
            say(f"suite budget of ${budget:.2f} reached (${self.spent:.2f} spent); no further runs will start")
        return False

    def label(self, job: Job, instance: int | None) -> str:
        where = f" i{instance}" if instance else ""
        attempt = f" attempt {job.attempt}" if self.args.attempts > 1 else ""
        proto = "" if job.protocol == "tools" else f", {job.protocol} protocol"
        return f"[{job.index}/{self.total}{where}] {job.model} ({job.effort}{proto}) seed {job.seed}{attempt}"

    def play(self, job: Job, bridge: GuardedBridge, instance: int | None) -> str:
        """Play one job. Returns 'played', 'interrupted', or 'failed' (nothing usable was recorded)."""
        args = self.args
        label = self.label(job, instance)
        bridge.reset()
        try:
            if ensure_game(bridge, instance) and instance:
                with self.lock:
                    self.launched.add(instance)
            bridge.start_run(args.character, args.ascension, job.seed)
        except (BridgeError, RunAborted, subprocess.SubprocessError, OSError) as e:
            say(f"{label}: could not start the run: {e}")
            return "failed"

        for old in job.stale + (job.path,):
            archive_recording(old)

        say(f"{label}: playing...")
        started = time.time()
        try:
            end = run_once(
                bridge,
                job.spec,
                job.effort,
                job.path,
                prices=self.prices,
                max_cost=args.max_cost,
                max_requests=args.max_requests,
                max_floor=args.max_floor,
                verbose=args.verbose,
                extra_meta={"suite": args.suite, "attempt": job.attempt},
            )
        except RunAborted as e:
            # No run_end was written, so the recording counts as unfinished and is replayed.
            # What it cost still counts against the suite budget.
            total = self.add_spend(recorded_cost(job.path))
            say(f"{label}: aborted after {time.time() - started:.0f}s: {e} | suite total ${total:.2f}")
            return "interrupted" if isinstance(e, SuiteCancelled) else "failed"
        except Exception:
            # A parser/adapter bug can occur after paid requests. The worker may retry the job,
            # but those requests must reduce its budget immediately, not only after a restart.
            self.add_spend(recorded_cost(job.path))
            raise

        total = self.add_spend(end.get("cost") or 0.0)
        cost = "n/a" if end.get("cost") is None else "$0" if end["cost"] == 0 else f"${end['cost']:.3f}"
        say(f"{label}: {end['outcome']} on floor {end.get('floor')} (act {end.get('act')}), "
            f"{end['usage']['requests']} requests, {cost}, {time.time() - started:.0f}s "
            f"| suite total ${total:.2f}")
        # The run_end is written: keep the game's own replay of each fight next to the recording.
        keep_fight_replays(job.path, instance, started, label)

        if end["outcome"] == "interrupted":
            return "interrupted"
        # Leave the game at the main menu, whatever state the run stopped in.
        try:
            bridge.reach_menu()
        except (BridgeError, RunAborted) as e:
            say(f"warning: could not return to the menu: {e}")
        return "played"

    def work(self, instance: int | None, base_url: str) -> None:
        """Pull jobs until none are left. `instance` None plays on the ordinary game at base_url."""
        bridge = GuardedBridge(base_url, self.cancelled)
        failures = 0
        while not (self.draining.is_set() or self.cancelled.is_set()):
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                return
            if not self.budget_left():
                return
            try:
                status = self.play(job, bridge, instance)
            except Exception as e:  # noqa: BLE001 - one bad run must not take the worker, or the others, down
                say(f"{self.label(job, instance)}: failed: {type(e).__name__}: {e}")
                status = "failed"

            if status == "interrupted":
                self.cancelled.set()
                return
            if status == "played":
                failures = 0
                continue
            failures += 1
            job.tries += 1
            if job.tries < MAX_TRIES:
                self.jobs.put(job)
            else:
                say(f"{self.label(job, instance)}: giving up after {job.tries} tries; rerun the suite to retry it")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                who = "the game" if instance is None else f"instance {instance}"
                say(f"{who} failed {failures} runs in a row; this worker is stopping")
                return


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a seeded benchmark suite across models.")
    parser.add_argument("--suite", required=True, help="name of the suite; results go to bench/<suite>/")
    parser.add_argument("--models", required=True,
                        help="comma-separated model specs [provider:]model[@effort][@protocol], e.g. "
                             "claude-sonnet-5-5,openai:gpt-5.1@low,local:qwen3:32b@text,policy:greedy")
    parser.add_argument("--seeds", required=True, help="comma-separated seeds; every model plays every seed")
    parser.add_argument("--attempts", type=int, default=1, help="times each (model, seed) is played (default: 1)")
    parser.add_argument("--parallel", type=int, default=1,
                        help="runs to play at once; above 1, worker i uses its own game instance i on port 15527+i")
    parser.add_argument("--first-instance", type=int, default=None,
                        help="play on game instances N..N+parallel-1 (default: 1..parallel, or with --parallel 1 "
                             "the game at --bridge)")
    parser.add_argument("--keep-running", action="store_true",
                        help="leave the extra game instances this command launched running")
    parser.add_argument("--character", default="ironclad")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--effort", default=None,
                        help="effort for models that do not name their own (default: low for providers that take "
                             "one; providers configured without an effort control play at their default)")
    parser.add_argument("--prices", default=None,
                        help="JSON file of USD-per-million-token prices for models not built in (or SPIRE_PRICES)")
    parser.add_argument("--max-requests", type=int, default=600, help="per-run cap on model requests")
    parser.add_argument("--max-cost", type=float, default=None, help="optional per-run spend cap in USD")
    parser.add_argument("--max-floor", type=int, default=None, help="stop each run once it is past this floor")
    parser.add_argument("--budget", type=float, default=None,
                        help="stop starting runs once this many USD are spent (runs already in flight finish)")
    parser.add_argument("--bridge", default=os.environ.get("SPIREBRIDGE_URL", DEFAULT_BRIDGE),
                        help="game to use when --parallel is 1")
    parser.add_argument("--verbose", action="store_true", help="echo every game state while playing")
    args = parser.parse_args()
    if args.attempts < 1 or args.parallel < 1:
        parser.error("--attempts and --parallel must be at least 1")
    if args.max_requests < 1 or any(value is not None and (not math.isfinite(value) or value <= 0)
                                  for value in (args.max_cost, args.budget, args.max_floor)):
        parser.error("request, cost, budget and floor limits must be positive")
    if not args.suite or Path(args.suite).name != args.suite or args.suite in (".", ".."):
        parser.error("--suite must be a single directory name")
    if args.first_instance is not None and args.first_instance < 1:
        parser.error("--first-instance must be at least 1 (0 is the ordinary game: use --bridge)")

    # (spec as given, parsed spec, effort). Specs may contain ':' and '/', so the list splits on ',' only.
    models = []
    try:
        for entry in args.models.split(","):
            entry = entry.strip()
            if entry:
                spec = parse_model_spec(entry)
                effort, warning = resolve_effort(spec, args.effort)
                if warning:
                    print(f"note: {warning}", flush=True)
                models.append((entry, spec, effort))
        prices = load_prices(args.prices)
    except (ValueError, OSError) as e:
        parser.error(str(e))
    keys = [(spec.name, effort, spec.protocol) for _, spec, effort in models]
    if len(set(keys)) != len(keys):
        parser.error("the same model, effort and protocol is listed twice")
    if len({(safe_filename(name), effort, protocol) for name, effort, protocol in keys}) != len(keys):
        parser.error("two models map to the same recording file name")
    seeds = [s.strip().upper() for s in args.seeds.split(",") if s.strip()]
    if not models or not seeds:
        parser.error("at least one model and one seed are required")
    normalized_seeds = [s.replace("O", "0").replace("I", "1") for s in seeds]
    if len(set(normalized_seeds)) != len(normalized_seeds):
        parser.error("seeds must be distinct after the game's O/0 and I/1 normalization")

    suite_dir = REPO_ROOT / "bench" / args.suite
    runs_dir = suite_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "suite": args.suite,
        "character": args.character,
        "ascension": args.ascension,
        "seeds": seeds,
        "models": [{"model": spec.name, "effort": effort, "spec": entry, "provider": spec.provider.name,
                    "model_id": spec.model_id, "protocol": spec.protocol,
                    "prompt_fingerprint": fingerprint_for(spec, args.character),
                    "priced": price_for(spec, prices)[0] is not None}
                   for entry, spec, effort in models],
        "attempts": args.attempts,
        "parallel": args.parallel,
        "limits": {"max_requests": args.max_requests, "max_cost": args.max_cost, "max_floor": args.max_floor},
        "harness_version": HARNESS_VERSION,
        "harness_commit": git_commit(),
        # The tool protocol's; each model entry above carries the fingerprint of the protocol it plays.
        "prompt_fingerprint": prompt_fingerprint(args.character),
        "protocols": sorted({spec.protocol for _, spec, _ in models}),
    }
    config_path = suite_dir / "suite.json"
    if config_path.exists() and any(runs_dir.glob("*.jsonl")):
        try:
            validate_resume_config(json.loads(config_path.read_text(encoding="utf-8")), config)
        except (ValueError, OSError) as e:
            parser.error(str(e))

    for entry, spec, _ in models:
        if (problem := key_problem(spec)) is not None:
            print(f"error: {entry}: {problem}", file=sys.stderr)
            return 2
        if price_for(spec, prices)[0] is None:
            if args.max_cost is not None or args.budget is not None:
                parser.error(f"{spec.name}: budget limits require known prices; provide --prices")
            print(f"note: no price for {spec.name}; its cost is recorded as n/a and --max-cost/--budget "
                  "do not count it", flush=True)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Attempt-major, then seed, then model: after any interruption every model has played
    # the same seeds the same number of times (give or take the runs in flight).
    plan = [(attempt, seed, spec, effort)
            for attempt in range(1, args.attempts + 1) for seed in seeds for _, spec, effort in models]
    prompts = ", ".join(sorted({f"{m['protocol']} {m['prompt_fingerprint']}" for m in config["models"]}))
    print(f"suite '{args.suite}': {len(plan)} runs ({len(models)} models x {len(seeds)} seeds x "
          f"{args.attempts} attempts), parallel {args.parallel}, "
          f"prompt {prompts}, commit {config['harness_commit']}", flush=True)

    try:
        spent = suite_recorded_cost(suite_dir)
    except (ValueError, OSError, KeyError, TypeError) as e:
        parser.error(f"cannot read the suite's previous spend: {e}")
    pending: list[Job] = []
    for index, (attempt, seed, spec, effort) in enumerate(plan, 1):
        model = spec.name
        # Claude ids are already safe file names, so their files keep the names they always had.
        stem = safe_filename(model)
        # Tool-protocol files keep their old names; any other protocol gets its own field, so the same
        # model under two protocols never shares (or resumes from) a recording.
        variant = effort if spec.protocol == "tools" else f"{effort}__{spec.protocol}"
        path = runs_dir / f"{stem}__{variant}__{seed}__a{attempt:02d}.jsonl"
        # Suites recorded before --attempts existed have no suffix: that file is attempt 1.
        legacy = runs_dir / f"{stem}__{effort}__{seed}.jsonl"
        candidates = (path, legacy) if attempt == 1 and spec.protocol == "tools" else (path,)
        previous = next((end for end in map(read_end, candidates) if end and end.get("outcome") in FINAL_OUTCOMES), None)
        if previous:
            tag = f" attempt {attempt}" if args.attempts > 1 else ""
            proto = "" if spec.protocol == "tools" else f", {spec.protocol} protocol"
            print(f"[{index}/{len(plan)}] {model} ({effort}{proto}) seed {seed}{tag}: already done "
                  f"({previous['outcome']}, floor {previous.get('floor')})", flush=True)
            continue
        pending.append(Job(index, attempt, seed, model, effort, spec.protocol, spec.spec, path, stale=candidates[1:]))

    suite = Suite(args, len(plan), spent)
    suite.prices = prices
    for job in pending:
        suite.jobs.put(job)

    workers = min(args.parallel, len(pending))
    try:
        if args.parallel == 1 and args.first_instance is None:
            if pending:
                suite.work(None, args.bridge)
        elif workers:
            first = args.first_instance or 1
            threads = [
                threading.Thread(target=suite.work, args=(i, instance_url(i)), name=f"instance-{i}", daemon=True)
                for i in range(first, first + workers)
            ]
            for t in threads:
                t.start()
            try:
                while any(t.is_alive() for t in threads):
                    for t in threads:
                        t.join(0.5)
            except KeyboardInterrupt:
                say("interrupted: abandoning the runs in flight (they are replayed when the suite is rerun)...")
                suite.cancelled.set()
                deadline = time.time() + 120  # each worker notices at its next game call
                for t in threads:
                    t.join(max(0.0, deadline - time.time()))
    except KeyboardInterrupt:
        say("interrupted")
    finally:
        if suite.launched and not args.keep_running:
            say(f"stopping game instance(s) {', '.join(map(str, sorted(suite.launched)))}...")
            stoppers = [
                threading.Thread(target=launch_script, args=("-Stop", "-Instance", str(i)), kwargs={"timeout": 90})
                for i in sorted(suite.launched)
            ]
            for t in stoppers:
                t.start()
            for t in stoppers:
                t.join()

    print(f"done. report: python agent/report.py --suite {args.suite}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
