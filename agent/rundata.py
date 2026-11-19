"""Read run recordings (JSONL) into plain dicts for the replay viewer and the report."""

from __future__ import annotations

import json
import re
from pathlib import Path
from bridge import screen_kind

# Outcomes where the run did not die and did not win: it was cut short, so for
# survival statistics it is "censored" (known alive up to its last floor).
CENSORED = {"request_limit", "cost_limit", "floor_limit", "stalled", "refusal", "interrupted",
            "bridge_error", "api_error", "abandoned", "running", "no_run"}
INFRASTRUCTURE_FAILURES = {"bridge_error", "api_error", "no_run"}
BEHAVIORAL_FAILURES = {"stalled", "refusal"}


def call_diagnostics(call: dict) -> dict:
    """Interpret bridge notes without changing the recorded result or API error flag.

    A successful HTTP/tool exchange can still contain a rejected game command.
    Conversely, ! stopped after ... is normal batch control, not an invalid move.
    """
    result = call.get("result") or ""
    errors, notices, bridge_errors = [], [], []
    for line in result.splitlines():
        if not line.startswith("!"):
            continue
        if line.startswith(("! stopped after ", "! skipped ")):
            notices.append(line)
        elif line.startswith(("! could not render ", "! info ")):
            bridge_errors.append(line)
        else:
            errors.append(line)
    return {"game_errors": errors, "batch_notices": notices, "bridge_errors": bridge_errors,
            "game_rejected": call.get("tool") == "act" and bool(errors),
            "has_game_state": call.get("tool") == "act" and screen_kind(result) != "UNKNOWN"}


def action_quality(steps: list[dict]) -> dict:
    """Call-level observability; a batched act counts once regardless of command count."""
    calls = [c for step in steps for c in step.get("calls", [])]
    acts = [c for c in calls if c.get("tool") == "act"]
    rejected = sum(call_diagnostics(c)["game_rejected"] for c in acts)
    return {"action_calls": len(acts), "rejected_action_calls": rejected,
            "rejected_action_rate": round(rejected / len(acts), 4) if acts else None,
            "game_error_lines": sum(len(call_diagnostics(c)["game_errors"]) for c in acts),
            "tool_error_calls": sum(bool(c.get("is_error")) for c in calls),
            "bridge_error_calls": sum(bool(call_diagnostics(c)["bridge_errors"]) for c in calls),
            "batch_notice_calls": sum(bool(call_diagnostics(c)["batch_notices"]) for c in acts)}


def read_events(path: Path) -> list[dict]:
    events = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line still being written
    return events


def short_model(model: str) -> str:
    return model[len("claude-"):] if model.startswith("claude-") else model


# Recordings from before protocols existed were all played through tools.
DEFAULT_PROTOCOL = "tools"


def protocol_of(start: dict) -> str:
    return start.get("protocol") or DEFAULT_PROTOCOL


def series_key(model: str, effort: str, protocol: str | None = None) -> str:
    """What separates one contestant from another in a report: model, effort and protocol.

    Tool-protocol keys keep their old form (`model|effort`), so suite configs and colours recorded
    before protocols existed still line up; any other protocol is appended (`model|effort|text`).
    """
    key = f"{model}|{effort}"
    return key if (protocol or DEFAULT_PROTOCOL) == DEFAULT_PROTOCOL else f"{key}|{protocol}"


def series_label(model: str, effort: str, protocol: str | None = None) -> str:
    """`sonnet-5-5 · low` for the tool protocol; `haiku-5-5 · low · text protocol`; `policy:greedy · text protocol`."""
    parts = [short_model(model)]
    if effort and not model.startswith("policy:"):
        parts.append(effort)
    if (protocol or DEFAULT_PROTOCOL) != DEFAULT_PROTOCOL:
        parts.append(f"{protocol} protocol")
    return " · ".join(parts)


def attempt_of(start: dict, path: Path) -> int:
    """Which repeat of its (model, seed) a run is, counting from 1.

    Recordings made with repeated attempts carry it in run_start and in the
    file name ("..__a03.jsonl"); older ones have neither and are attempt 1.
    """
    value = start.get("attempt")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    match = re.search(r"__a(\d+)$", path.stem)
    if match and int(match.group(1)) >= 1:
        return int(match.group(1))
    return 1


def summarize(path: Path) -> dict | None:
    """One run reduced to what the report needs."""
    events = read_events(path)
    start = next((e for e in events if e["type"] == "run_start"), None)
    if start is None:
        return None
    end = next((e for e in reversed(events) if e["type"] == "run_end"), None)
    steps = [e for e in events if e["type"] == "step"]

    # Per floor: act, and the last HP seen there (the HP the run left that floor with).
    floors: dict[int, dict] = {}
    snapshots = [start.get("snapshot") or {}] + [s.get("snapshot") or {} for s in steps]
    if end:
        snapshots.append(end.get("snapshot") or {})
    max_act = 0
    for snap in snapshots:
        floor = snap.get("floor")
        if not isinstance(floor, int) or floor < 1 or not snap.get("run"):
            continue
        max_act = max(max_act, snap.get("act") or 0)
        # A floor belongs to the act it was entered in. After a boss the game
        # advances the act before the floor counter, so later snapshots on the
        # boss floor already report the next act.
        entered_in = floors[floor]["act"] if floor in floors else snap.get("act")
        floors[floor] = {
            "floor": floor,
            "act": entered_in,
            "hp": snap.get("hp"),
            "max_hp": snap.get("max_hp"),
            "gold": snap.get("gold"),
            "deck_size": snap.get("deck_size"),
            "relics": len(snap.get("relics") or []),
            "room": snap.get("room_type"),
        }

    usage = {"requests": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    cost = 0.0
    cost_known = True
    latency = 0.0
    for s in steps:
        for key in usage:
            usage[key] += (s.get("usage") or {}).get(key, 0)
        if s.get("cost") is None:
            cost_known = False
        else:
            cost += s["cost"]
        latency += s.get("latency_s") or 0.0

    last = snapshots[-1] if snapshots else {}
    final_floor = max(floors) if floors else 0
    outcome = end["outcome"] if end else "running"
    model = start.get("model", "?")
    effort = start.get("effort", "")
    protocol = protocol_of(start)
    # Per-decision counts of the text protocol (retries, fallbacks); from run_end, else counted from the steps.
    text_counts = None
    if protocol != DEFAULT_PROTOCOL:
        if end and "fallbacks" in end:
            text_counts = {k: end.get(k) for k in ("decisions", "invalid_replies", "retries", "fallbacks")}
        else:
            text_counts = {
                "decisions": len({s.get("decision") for s in steps if s.get("decision") is not None}),
                "invalid_replies": sum(1 for s in steps if s.get("invalid")),
                "retries": sum(1 for s in steps if s.get("invalid") and s.get("attempt") == 1),
                "fallbacks": sum(1 for s in steps if s.get("fallback")),
            }
    return {
        "id": path.stem,
        "file": path.name,
        "model": model,
        "effort": effort,
        "protocol": protocol,
        "key": series_key(model, effort, protocol),
        "series": series_label(model, effort, protocol),
        "provider": start.get("provider"),
        "text_protocol": text_counts,
        "action_quality": action_quality(steps),
        "seed": start.get("seed"),
        "attempt": attempt_of(start, path),
        "character": start.get("character"),
        "ascension": start.get("ascension"),
        "started_at": start.get("started_at"),
        "game_version": start.get("game_version"),
        "mod_version": start.get("mod_version"),
        "context_policy": start.get("context_policy"),
        "max_output_tokens": start.get("max_output_tokens"),
        "harness_commit": start.get("harness_commit"),
        "prompt_fingerprint": start.get("prompt_fingerprint"),
        # What a cross-suite comparison needs to show what differed between suites.
        "playbook": start.get("playbook"),
        "limits": start.get("limits"),
        "suite": start.get("suite"),
        "outcome": outcome,
        "censored": outcome in CENSORED,
        "infrastructure_failure": outcome in INFRASTRUCTURE_FAILURES,
        "behavioral_failure": outcome in BEHAVIORAL_FAILURES,
        "final_floor": final_floor,
        "final_act": floors[final_floor]["act"] if floors else None,
        "max_act": max_act,
        "act_count": last.get("act_count") or (start.get("snapshot") or {}).get("act_count"),
        "hp_end": last.get("hp"),
        "max_hp_end": last.get("max_hp"),
        "requests": usage["requests"],
        "tokens": usage,
        "cost": round(cost, 4) if cost_known else None,
        "duration_s": end.get("duration_s") if end else (steps[-1]["t"] if steps else 0),
        "model_seconds": round(latency, 1),
        "floors": [floors[k] for k in sorted(floors)],
    }
