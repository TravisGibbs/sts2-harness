"""Turn a run recording into a self-contained replay page.

    python agent/replay.py runs/20261115-120000-claude-sonnet-5-5.jsonl
    python agent/replay.py bench/pilot/runs/claude-sonnet-5-5__low__BENCH01.jsonl --open

The page needs no server: everything is embedded. Step through with the arrow
keys to see what the model saw, what it reasoned, what it sent, and what the
game answered.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rundata import action_quality, call_diagnostics, protocol_of, read_events, series_label  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent / "templates" / "replay.html"


def build_data(path: Path) -> dict:
    events = read_events(path)
    start = next((e for e in events if e["type"] == "run_start"), {})
    end = next((e for e in reversed(events) if e["type"] == "run_end"), None)

    frames = []
    pending_episode = None
    for event in events:
        if event["type"] == "episode":
            pending_episode = event
        elif event["type"] == "step":
            snap = event.get("snapshot") or {}
            frame = {
                "n": event.get("n"),
                "t": event.get("t"),
                "latency": event.get("latency_s"),
                "cost": event.get("cost"),
                "usage": event.get("usage"),
                "thinking": event.get("thinking") or "",
                "text": event.get("text") or "",
                "calls": [{**c, **call_diagnostics(c)} for c in event.get("calls") or []],
                # Text protocol: which attempt of a decision this was, and why an answer was rejected.
                "attempt": event.get("attempt"),
                "invalid": event.get("invalid"),
                "fallback": event.get("fallback"),
                # Typed Decisions responses contain a choice/distribution, not prose reasoning.
                "decision_response": event.get("decision_response"),
                "provider_cost": event.get("provider_cost"),
                "floor": snap.get("floor"),
                "act": snap.get("act"),
                "hp": snap.get("hp"),
                "max_hp": snap.get("max_hp"),
                "gold": snap.get("gold"),
                "screen": snap.get("screen"),
                "room": snap.get("room_type"),
                "deck": snap.get("deck") or [],
                "relics": snap.get("relics") or [],
                "potions": snap.get("potions") or [],
            }
            if pending_episode is not None:
                # This step opened a fresh context: record what the model was handed.
                frame["episode"] = {
                    "state": pending_episode.get("state", ""),
                    "summary": pending_episode.get("summary", ""),
                    "notes": pending_episode.get("notes", ""),
                }
                pending_episode = None
            frames.append(frame)

    meta = {
        "file": path.name,
        "model": start.get("model", "?"),
        "label": series_label(start.get("model", "?"), "", None),
        "effort": start.get("effort"),
        "protocol": protocol_of(start),
        "action_quality": action_quality([e for e in events if e.get("type") == "step"]),
        "policy": start.get("provider") == "policy",
        "text_protocol": ({k: end.get(k) for k in ("decisions", "retries", "fallbacks", "invalid_replies")}
                          if end and "fallbacks" in end else None),
        "seed": start.get("seed"),
        "character": start.get("character"),
        "ascension": start.get("ascension"),
        "started_at": start.get("started_at"),
        "game_version": start.get("game_version"),
        "harness_commit": start.get("harness_commit"),
        "prompt_fingerprint": start.get("prompt_fingerprint"),
        "outcome": end.get("outcome") if end else "running",
        "final_floor": end.get("floor") if end else (frames[-1]["floor"] if frames else None),
        "cost": end.get("cost") if end else None,
        "requests": (end.get("usage") or {}).get("requests") if end else len(frames),
        "duration_s": end.get("duration_s") if end else None,
        "final_notes": end.get("notes") if end else "",
    }
    return {"meta": meta, "frames": frames}


def build(path: Path, out: Path | None = None) -> Path:
    data = build_data(path)
    out = out or path.with_suffix(".html")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)
    tag = f" · {data['meta']['protocol']} protocol" if data["meta"]["protocol"] != "tools" else ""
    title = f"{data['meta']['label']}{tag} · seed {data['meta']['seed']}"
    html = html.replace("__TITLE__", title.replace("<", ""))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a replay page from a run recording.")
    parser.add_argument("recording", type=Path, help="a .jsonl run recording")
    parser.add_argument("--out", type=Path, default=None, help="output .html (default: next to the recording)")
    parser.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = parser.parse_args()

    if not args.recording.exists():
        print(f"error: {args.recording} does not exist", file=sys.stderr)
        return 2
    out = build(args.recording, args.out)
    print(out)
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
