"""Film a recorded run in the real game.

    python tools/render/render_run.py bench/<suite>/runs/<stem>.jsonl [--instance 2]

The run is played again in an actual game instance from its recorded commands: the same character,
ascension and seed, then every `act` the model sent, in order. After each recorded step the game's
state (floor, HP, gold, deck, relics, potions, screen) is compared with the snapshot recorded at that
step; the first difference stops the replay and marks the render "diverged". Meanwhile Godot's Movie
Maker records every frame and the game's audio (`launch.ps1 -Render -Movie`), on a separate Windows
desktop that is never shown, so nothing appears on screen or takes focus.

Output, in renders/<suite>/<stem>/:
    game.mp4      the run from the first decision to the end, sped up (--speed) and sized for the web
    poster.jpg    one frame of it
    render.json   status (verified | diverged | failed), steps checked, chapters (one per floor), versions
    fights/       the game's own replay of each fight (floor_NN.mcr), saved by the mod as it played

The Movie Maker clock is fixed (--fps frames per second of game time, however long a frame takes to
draw), so the footage plays smoothly whatever the machine's speed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
from bridge import Bridge, BridgeError, screen_kind  # noqa: E402

COMPARED = ("floor", "act", "hp", "max_hp", "gold", "deck", "relics", "potions", "screen")
WAIT_LIMIT = 40          # 'wait' calls while the game reports BUSY after a command
LEAD_IN_FRAMES = 15      # footage kept before the first decision
TAIL_SECONDS = 4.0       # wall-clock time the end screen is left to play out
CATCH_UP_SECONDS = 30.0  # how long a state may lag the recording before it counts as a divergence
SCREEN_WAIT_SECONDS = 10.0  # how long to wait for the screen the model saw before sending its commands anyway


def load(path: Path) -> tuple[dict, list[dict], dict | None]:
    rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    start = next((r for r in rows if r["type"] == "run_start"), None)
    if start is None:
        raise ValueError(f"{path} has no run_start")
    end = next((r for r in rows if r["type"] == "run_end"), None)
    steps, episode_state = [], None
    for r in rows:
        if r["type"] == "episode":
            episode_state = r.get("state")
        elif r["type"] == "step":
            if episode_state is not None:
                r = {**r, "_episode_state": episode_state}  # the state a fresh conversation began from
                episode_state = None
            steps.append(r)
    return start, steps, end


def acts_of(step: dict) -> list[tuple[str, str]]:
    """The command batches of one step that reached the game, in order, each with the state it returned."""
    out = []
    for call in step.get("calls", []):
        if call.get("tool") != "act":
            continue
        commands = str((call.get("input") or {}).get("commands", "")).strip()
        if commands:
            out.append((commands, str(call.get("result") or "")))
    return out


def state_part(text: str) -> str:
    """A reply minus what comes before its state header (`!` notes, the text of `info` lookups in the batch)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if screen_kind(line) != "UNKNOWN":
            return "\n".join(lines[i:])
    return ""


def screen_key(state: str) -> tuple:
    """What a command batch depends on: the screen kind and its numbered options (start of each line)."""
    options = tuple(line[:48] for line in state.splitlines() if re.match(r"^\d+ ", line))
    return screen_kind(state), options


DIALOGUE_LINE = ("EVENT", ("0 Continue",))
DIALOGUE_DONE = ("EVENT", ("0 Proceed",))


def wait_for_screen(bridge: Bridge, seen: str | None, patience_s: float = CATCH_UP_SECONDS) -> bool | str:
    """Before sending a batch, wait until the game shows the screen the model was looking at.

    A drawing game can still be playing an animation (a line of dialogue, an attack) that a headless
    game finished at once; a command sent then lands on an older screen and is lost. The comparison
    uses an observer's read (/state?peek=1), which changes nothing in the game. Returns False if the
    screen never matched, in which case the batch is sent anyway and the step check decides, and
    "skip" when the recorded batch only clicked through a line of dialogue this profile doesn't have.
    """
    if not seen or screen_kind(seen) == "UNKNOWN":
        return True
    want = screen_key(seen)
    deadline = time.time() + patience_s
    while True:
        try:
            live = bridge._request("/state?peek=1", timeout=30)
        except BridgeError:
            live = ""
        if screen_key(live) == want:
            return True
        # Some story dialogue (the Architect's, at the end of a won run) depends on the profile's
        # history, not on the run: this profile may have an extra line to click through. 'Continue'
        # only advances the conversation, so take it until the recorded screen appears.
        if screen_key(live) == DIALOGUE_LINE and want != DIALOGUE_LINE:
            bridge.act("choose 0")
            continue
        # The other way round: the model clicked through a line this profile skips. Leave it out.
        if want == DIALOGUE_LINE and screen_key(live) == DIALOGUE_DONE:
            return "skip"
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def differences(got: dict, want: dict) -> dict:
    return {k: (got.get(k), want.get(k)) for k in COMPARED if k in want and got.get(k) != want.get(k)}


def settled_differences(bridge: Bridge, want: dict, patience_s: float = CATCH_UP_SECONDS) -> tuple[dict, dict]:
    """Compare the live state with a recorded snapshot, giving the game time to catch up first.

    A drawing game plays some transitions in more game time than a headless one (the ending of a
    won run reaches GAME_OVER only after its cinematic), so a state can briefly lag the recording.
    Only a difference that is still there after `patience_s` counts; the match must still be exact.
    """
    deadline = time.time() + patience_s
    while True:
        got = bridge.snapshot()
        diff = differences(got, want)
        if not diff or time.time() >= deadline:
            return diff, got
        time.sleep(0.5)


def label_for(snap: dict) -> str:
    room = snap.get("room_type") or ""
    room = {"Monster": "Fight", "Elite": "Elite", "Boss": "Boss", "RestSite": "Rest", "Shop": "Shop",
            "Treasure": "Treasure", "Event": "Event"}.get(room, room)
    text = f"Floor {snap.get('floor')}"
    if room:
        text += f" · {room}"
    enemies = [e.get("name") for e in (snap.get("combat") or {}).get("enemies", []) if e.get("name")]
    if enemies:
        names = list(dict.fromkeys(enemies))
        text += ": " + ", ".join(names[:2]) + (" +" if len(names) > 2 else "")
    return text


def launch(*args: str, timeout: float = 240) -> str:
    done = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "launch.ps1"), *args],
        capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f"launch.ps1 {' '.join(args)} failed: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout.strip()


def tracked_pid(instance: int) -> int | None:
    pid_file = ROOT / ".instances" / str(instance) / "pid"
    try:
        return int(pid_file.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def stop_instance(instance: int) -> None:
    """Close the game through the bridge so Movie Maker finishes the file; launch.ps1 stops it by PID if it hangs."""
    pid = tracked_pid(instance)
    launch("-Stop", "-Instance", str(instance), timeout=120)
    deadline = time.time() + 60
    while process_alive(pid) and time.time() < deadline:
        time.sleep(0.5)


def atempo_chain(speed: float) -> str:
    parts, left = [], speed
    while left > 2.0:
        parts.append("atempo=2.0")
        left /= 2.0
    parts.append(f"atempo={left:.4f}")
    return ",".join(parts)


def encode(avi: Path, out: Path, start_s: float, end_s: float, speed: float, width: int, crf: int) -> float:
    """Cut [start_s, end_s] of the capture, speed it up and write a web-ready MP4. Returns its duration."""
    vf = f"trim=start={start_s:.3f}:end={end_s:.3f},setpts=(PTS-STARTPTS)/{speed},scale={width}:-2,fps=30"
    af = f"atrim=start={start_s:.3f}:end={end_s:.3f},asetpts=PTS-STARTPTS,{atempo_chain(speed)}"
    tmp = out.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(avi), "-filter_complex",
           f"[0:v]{vf}[v];[0:a]{af}[a]", "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "64k", "-ac", "2", "-movflags", "+faststart", str(tmp)]
    subprocess.run(cmd, check=True)
    tmp.replace(out)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True)
    return float(probe.stdout.strip())


def poster(video: Path, out: Path, at_s: float) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at_s:.2f}", "-i", str(video), "-frames:v", "1",
                    "-q:v", "4", str(out)], check=True)


def replay(bridge: Bridge, start: dict, steps: list[dict], end: dict | None, log) -> dict:
    """Play the recorded commands; returns status, counts, chapter marks (in capture frames) and frames."""
    state = bridge.start_run(start["character"], start["ascension"], start["seed"])
    first = bridge.snapshot()
    result = {"status": "verified", "steps": len(steps), "steps_matched": 0, "first_divergence": None,
              "acts": 0, "chapters": [], "start_frame": first.get("render_frame"), "end_frame": None,
              "fight_replays": first.get("fight_replays")}
    want0 = start.get("snapshot") or {}
    diff = differences(first, want0)
    if diff:
        result.update(status="diverged", first_divergence={"step": 0, "diff": diff})
        log(f"run start differs from the recording: {diff}")
        return result
    last_floor = None

    def mark(snap: dict) -> None:
        nonlocal last_floor
        if snap.get("floor") != last_floor and snap.get("render_frame") is not None:
            last_floor = snap.get("floor")
            result["chapters"].append({"frame": snap["render_frame"], "floor": last_floor, "label": label_for(snap)})

    mark(first)
    if screen_kind(state) == "BUSY":
        bridge.act("wait")
    seen: str | None = state  # the screen the model last saw, as recorded
    last_reply = state
    for index, step in enumerate(steps):
        if step.get("_episode_state"):
            seen = step["_episode_state"]
        for commands, recorded_reply in acts_of(step):
            seen_state = state_part(seen or "")
            matched = True
            if screen_kind(seen_state) not in ("BUSY", "UNKNOWN"):
                matched = wait_for_screen(bridge, seen_state, SCREEN_WAIT_SECONDS)
            if matched == "skip":
                result["dialogue_skipped"] = result.get("dialogue_skipped", 0) + 1
                seen = recorded_reply
                continue
            if not matched:
                result["screen_waits_timed_out"] = result.get("screen_waits_timed_out", 0) + 1
                log(f"step {step.get('n')}: screen never matched what the model saw before '{commands[:40]}'")
                log("  model saw: " + " | ".join(seen_state.splitlines()[:6]))
                log("  game shows: " + " | ".join(bridge._request("/state?peek=1", timeout=30).splitlines()[:6]))
            seen = recorded_reply
            reply = bridge.act(commands)
            last_reply = reply
            for _ in range(WAIT_LIMIT):
                if screen_kind(reply) != "BUSY":
                    break
                reply = bridge.act("wait")
            result["acts"] += 1
            snap = bridge.snapshot()
            mark(snap)
        diff, got = settled_differences(bridge, step.get("snapshot") or {})
        if diff and not acts_of(step) and index + 1 < len(steps) \
                and not differences(got, steps[index + 1].get("snapshot") or {}):
            # The recording's snapshot caught the game partway through something it does on its own
            # (Lord's Parasol buying out a shop, say), and this step sent no commands: the replay is
            # already where the recording is one step later. Anything else is still a divergence.
            result["ahead_of_recording"] = result.get("ahead_of_recording", 0) + 1
            log(f"step {step.get('n')}: already at the next recorded state (recording caught mid-change)")
            diff = {}
        if diff:
            result.update(status="diverged", first_divergence={"step": step.get("n"), "diff": diff})
            log(f"step {step.get('n')}: differs from the recording: {diff}")
            log("  last reply: " + " | ".join(last_reply.splitlines()[:6]))
            break
        result["steps_matched"] += 1
        # Name the fight once the enemies are known (the chapter mark is taken on arriving at the floor).
        newest = result["chapters"][-1] if result["chapters"] else None
        if newest and newest["floor"] == got.get("floor") and got.get("in_combat") and ":" not in newest["label"]:
            newest["label"] = label_for(got)
    if result["status"] == "verified" and end and end.get("snapshot"):
        diff, _ = settled_differences(bridge, end["snapshot"])
        if diff:
            result.update(status="diverged", first_divergence={"step": "end", "diff": diff})
            log(f"end of run differs from the recording: {diff}")
    time.sleep(TAIL_SECONDS)  # let the last screen (death, victory, reward) play out on film
    result["end_frame"] = bridge.snapshot().get("render_frame")
    return result


def render(recording: Path, instance: int, out_root: Path, speed: float, fps: int, size: str, width: int,
           crf: int, keep_avi: bool) -> dict:
    recording = recording.resolve()
    suite = recording.parent.parent.name
    out = out_root / suite / recording.stem
    out.mkdir(parents=True, exist_ok=True)
    tmp = out_root / "_capture"
    tmp.mkdir(parents=True, exist_ok=True)
    avi = tmp / f"{suite}--{recording.stem}.avi"
    log_lines: list[str] = []

    def log(text: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {recording.stem}: {text}"
        log_lines.append(line)
        print(line, flush=True)

    start, steps, end = load(recording)
    info = {"recording": str(recording.relative_to(ROOT)) if recording.is_relative_to(ROOT) else str(recording),
            "seed": start.get("seed"), "character": start.get("character"), "ascension": start.get("ascension"),
            "recorded_game_version": start.get("game_version"), "recorded_mod_version": start.get("mod_version"),
            "speed": speed, "fps": fps, "size": size, "instance": instance}
    started = time.time()
    bridge = Bridge.for_instance(instance)
    try:
        if avi.exists():
            avi.unlink()
        launch("-Instance", str(instance), "-Render", "-Movie", str(avi), "-Fps", str(fps), "-RenderSize", size)
        health = bridge.health()
        game_version = health.split(" game ")[1].split()[0] if " game " in health else None
        info.update(game_version=game_version, mod_version=health.split()[2] if len(health.split()) > 2 else None)
        if start.get("game_version") and game_version and start["game_version"] != game_version:
            raise RuntimeError(f"recorded on game {start['game_version']}, this game is {game_version}")
        log(f"replaying {len(steps)} steps on instance {instance}")
        result = replay(bridge, start, steps, end, log)
        fights = Path(result.pop("fight_replays") or "")
        if fights.is_dir():
            dest = out / "fights"
            shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir()
            for f in sorted(fights.glob("*.mcr")):
                shutil.copy2(f, dest / f.name)
    except Exception as e:  # noqa: BLE001 - any failure becomes a 'failed' render, recorded
        log(f"failed: {type(e).__name__}: {e}")
        result = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            stop_instance(instance)
        except Exception as e:  # noqa: BLE001
            log(f"could not stop instance {instance}: {e}")

    info["wall_s"] = round(time.time() - started, 1)
    if result.get("start_frame") is not None and result.get("end_frame") and avi.exists():
        start_s = max(0.0, (result["start_frame"] - LEAD_IN_FRAMES) / fps)
        end_s = result["end_frame"] / fps
        video = out / "game.mp4"
        duration = encode(avi, video, start_s, end_s, speed, width, crf)
        result["chapters"] = [{"t": round(max(0.0, (c["frame"] / fps - start_s) / speed), 2),
                               "floor": c["floor"], "label": c["label"]} for c in result["chapters"]]
        result["duration_s"] = round(duration, 2)
        poster(video, out / "poster.jpg", min(duration * 0.35, max(0.0, duration - 1)))
        log(f"{result['status']}: {result['steps_matched']}/{result['steps']} steps, "
            f"{duration / 60:.1f} min video ({video.stat().st_size / 1e6:.0f} MB), {info['wall_s'] / 60:.1f} min to film")
    if avi.exists() and not keep_avi:
        avi.unlink()
    report = {**result, **info, "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (out / "render.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    with (out / "render.log").open("a", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("recording", type=Path)
    ap.add_argument("--instance", type=int, default=2)
    ap.add_argument("--out", type=Path, default=ROOT / "renders")
    ap.add_argument("--speed", type=float, default=3.0, help="playback speed-up of the published video")
    ap.add_argument("--fps", type=int, default=30, help="Movie Maker frames per second of game time")
    ap.add_argument("--size", default="1280x720", help="game window (capture) size")
    ap.add_argument("--width", type=int, default=960, help="published video width")
    ap.add_argument("--crf", type=int, default=30)
    ap.add_argument("--keep-avi", action="store_true")
    args = ap.parse_args()
    report = render(args.recording, args.instance, args.out, args.speed, args.fps, args.size, args.width,
                    args.crf, args.keep_avi)
    return 0 if report.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
