"""A tiny OpenAI-compatible Chat Completions server that plays the game with a fixed policy.

It exists to test the harness's OpenAI-compatible provider path end to end without any API key:

    python tools/mock_model_server.py --port 0        # prints the base URL it listens on
    set SPIRE_LOCAL_BASE_URL=http://127.0.0.1:<port>/v1
    python agent/bench.py --suite _mock --models local:mock-policy --seeds MOCK1 --first-instance 6 ...

Policy, read from the latest game state in the request (the last tool result or user message that has a
`Commands:` line):
  - COMBAT: play the first playable hand card (at the first enemy if it targets one), then `end`.
    If the previous batch reported an error, just `end`.
  - CARD_SELECT: `select 0` (`select none` after an error, where allowed).
  - anywhere else: the first of `choose 0`, `open`, `proceed`, `skip`, `wait` the Commands line allows;
    after an error it skips `choose 0` and `open`.
On a fixed schedule it also exercises the harness's edge cases: an `info boss` lookup, a call whose
arguments are not valid JSON, a reply with two tool calls, and one reply with no tool call at all.

It also checks every request the way a strict server would (system message first, the three tools present,
every assistant tool call answered by a tool message with its id) and answers 400 when one is not.
GET /stats returns counters, so a test can see what was exercised.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATS = {"requests": 0, "rejected": 0, "malformed_sent": 0, "info_sent": 0, "multi_sent": 0, "no_tool_sent": 0,
         "error_results_seen": 0, "acts_sent": 0}
_lock = threading.Lock()


# ---------------------------------------------------------------- policy


def latest_state(messages: list[dict]) -> str | None:
    """The newest message content that is a game state (it ends with a Commands: line)."""
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, list):  # content parts
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or "Commands:" not in content:
            continue
        if message.get("role") == "user" and "Current state:\n" in content:
            return content.split("Current state:\n", 1)[1]
        if message.get("role") in ("tool", "user"):
            return content
    return None


def choose_commands(state: str | None) -> str:
    if not state:
        return "wait"
    lines = state.splitlines()
    had_error = any(line.startswith("!") for line in lines)
    body = [line for line in lines if not line.startswith("!")]
    kind = body[0].split(" ", 1)[0] if body else ""
    commands_line = next((line for line in reversed(body) if line.startswith("Commands:")), "Commands: wait")
    options = [part.strip() for part in commands_line[len("Commands:"):].split("|")]
    verbs = {option.split(" ", 1)[0] for option in options if option}

    if kind == "COMBAT":
        if had_error:
            return "end"
        enemy = next((m.group(1) for line in body if (m := re.match(r"^e(\d+) ", line))), None)
        in_hand = False
        for line in body:
            if line == "Hand:":
                in_hand = True
                continue
            if in_hand:
                match = re.match(r"^(\d+) ", line)
                if not match:
                    break
                if "[unplayable" in line:
                    continue
                target = f" e{enemy}" if ">enemy" in line and enemy is not None else ""
                return f"play {match.group(1)}{target}; end"
        return "end"

    if "select" in verbs:
        if had_error and any(option == "select none" for option in options):
            return "select none"
        return "select 0"

    order = ["proceed", "skip", "wait"] if had_error else ["choose", "open", "proceed", "skip", "wait"]
    for verb in order:
        if verb in verbs:
            return "choose 0" if verb == "choose" else verb
    return "wait"


# ---------------------------------------------------------------- request checks


def problems_with(request: dict) -> str | None:
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty list"
    if messages[0].get("role") != "system" or not messages[0].get("content"):
        return "the first message must be the system prompt"
    names = sorted(t.get("function", {}).get("name", "") for t in request.get("tools") or [])
    if names != ["act", "info", "set_notes"]:
        return f"expected the tools act, info, set_notes; got {names}"
    for tool in request["tools"]:
        params = tool["function"].get("parameters") or {}
        if params.get("type") != "object" or not params.get("required"):
            return f"tool {tool['function'].get('name')} has no object schema"
    open_ids: list[str] = []
    for i, message in enumerate(messages[1:], 1):
        role = message.get("role")
        if role == "tool":
            if message.get("tool_call_id") not in open_ids:
                return f"message {i}: tool result for unknown or already answered id {message.get('tool_call_id')}"
            open_ids.remove(message["tool_call_id"])
            continue
        if open_ids:
            return f"message {i}: tool calls {open_ids} were not answered before a {role} message"
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                try:
                    json.loads(call["function"]["arguments"])
                except (KeyError, TypeError, json.JSONDecodeError):
                    return f"message {i}: assistant tool call arguments in the history are not JSON"
                open_ids.append(call["id"])
        elif role != "user":
            return f"message {i}: unexpected role {role}"
    if open_ids:
        return f"tool calls {open_ids} were never answered"
    if messages[-1].get("role") == "assistant":
        return "the last message must not be the assistant's"
    return None


# ---------------------------------------------------------------- server


def reply_for(request: dict, n: int) -> dict:
    messages = request["messages"]
    last = messages[-1]
    if last.get("role") == "tool" and str(last.get("content", "")).lower().startswith("the arguments of this"):
        with _lock:
            STATS["error_results_seen"] += 1
    state = latest_state(messages)
    commands = choose_commands(state)
    calls: list[tuple[str, str]] = []
    content = None
    cycle = n % 10
    if n == 5:
        content = "Thinking about the run."  # no tool call: the harness should nudge
        with _lock:
            STATS["no_tool_sent"] += 1
    elif cycle == 3:
        calls = [("info", json.dumps({"query": "boss"}))]
        with _lock:
            STATS["info_sent"] += 1
    elif cycle == 6:
        calls = [("act", '{"commands": "' + commands + '"')]  # missing closing brace
        with _lock:
            STATS["malformed_sent"] += 1
    elif cycle == 8:
        calls = [("info", json.dumps({"query": "deck"})), ("act", json.dumps({"commands": commands}))]
        with _lock:
            STATS["multi_sent"] += 1
            STATS["acts_sent"] += 1
    else:
        calls = [("act", json.dumps({"commands": commands}))]
        with _lock:
            STATS["acts_sent"] += 1

    message: dict = {"role": "assistant", "content": content,
                     "reasoning_content": f"mock policy step {n}: {commands}"}
    if calls:
        message["tool_calls"] = [
            {"id": f"call_{n}_{k}", "type": "function", "function": {"name": name, "arguments": args}}
            for k, (name, args) in enumerate(calls)
        ]
    text = json.dumps(messages)
    prompt_tokens = max(len(text) // 4, 1)
    system_tokens = len(messages[0].get("content", "")) // 4
    cached = system_tokens if n > 1 else 0
    return {
        "id": f"chatcmpl-mock-{n}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.get("model", "mock-policy"),
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 20 + 10 * len(calls),
            "total_tokens": prompt_tokens + 20 + 10 * len(calls),
            "prompt_tokens_details": {"cached_tokens": min(cached, prompt_tokens)},
            "completion_tokens_details": {"reasoning_tokens": 8},
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MockModel/1.0"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/stats"):
            with _lock:
                self._send(200, dict(STATS))
        elif self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": "mock-policy", "object": "model"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "body is not JSON", "type": "invalid_request_error"}})
            return
        with _lock:
            STATS["requests"] += 1
            n = STATS["requests"]
        problem = problems_with(request)
        if problem:
            with _lock:
                STATS["rejected"] += 1
            print(f"[mock] request {n} rejected: {problem}", file=sys.stderr, flush=True)
            self._send(400, {"error": {"message": problem, "type": "invalid_request_error"}})
            return
        reply = reply_for(request, n)
        calls = reply["choices"][0]["message"].get("tool_calls") or []
        print(f"[mock] request {n}: {len(request['messages'])} messages -> "
              + (", ".join(f"{c['function']['name']}({c['function']['arguments']})" for c in calls) or "no tool call"),
              file=sys.stderr, flush=True)
        self._send(200, reply)

    def log_message(self, format, *args) -> None:  # noqa: A002 - quiet: one line per request is printed above
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI-compatible mock model that plays with a fixed policy.")
    parser.add_argument("--port", type=int, default=0, help="port to listen on (0: any free port)")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock model server listening on http://127.0.0.1:{server.server_address[1]}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
