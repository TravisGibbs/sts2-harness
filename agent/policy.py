"""Non-LLM policies as benchmark contestants: `policy:<target>` model specs.

A policy is anything that maps the current game state (the same text a model sees) to a command
string in one step. This is how a trained RL agent, a world-model-based controller or a scripted
baseline is benchmarked on the same seeded runs as the language models. Policies always play the
text protocol (agent/system1.py), so they get the same retry-once-then-fallback handling and their
runs are recorded and reported like any other, with a cost of $0.

Targets
-------
    policy:random_legal              a reference policy in agent/policies/ (also: greedy)
    policy:mypkg.mymodule:act        any importable module and function
    policy:C:/path/to/bot.py:act     a Python file and a function in it
    policy:http://127.0.0.1:9000/act an HTTP endpoint

A Python policy is a function `act(state: str) -> str` or `act(state: str, context: dict) -> str`.
It may return a string (`"play 0 e0; end"`, several lines are fine), a list of commands, or
{"commands": "..."}. `context` holds:
    summary   the run summary (act, floor, boss, HP, gold, deck, relics, potions), or ""
    seed, attempt, character, ascension
    step      the decision number in this run, from 1
    retry     True when the previous answer was unusable; `state` then starts with `!` lines saying why
    rng       a random.Random owned by this run, seeded from (seed, attempt): use it for reproducibility
    memory    a dict that lives for the whole run (recurrent state, caches)
    run_id    an id unique to this run
Exceptions raised by the policy end the run as api_error (bench retries it), so bugs are not hidden.

An HTTP policy receives a POST with the state text as the body (text/plain; charset=utf-8) and the
headers X-Spire-Run, X-Spire-Seed, X-Spire-Attempt, X-Spire-Step and X-Spire-Retry (0/1). It answers
200 with the commands as plain text, or JSON {"commands": "..."}. Connection failures and 5xx
answers are retried like a model API's; other errors end the run as api_error.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from providers import Provider, ProviderConfig, ProviderError, Reply, TransientError, _host_of

# Reference policies shipped with the harness (agent/policies/).
REFERENCE_POLICIES = {
    "random_legal": "policies.random_legal:act",
    "greedy": "policies.greedy:act",
}

HTTP_TIMEOUT_S = 120


def _load_callable(target: str):
    """`module:function`, `path/to/file.py:function`, or a reference policy name -> the function."""
    target = REFERENCE_POLICIES.get(target, target)
    module_name, sep, attr = target.rpartition(":")
    if not sep or not module_name or not attr:
        raise ProviderError(f"policy {target!r}: name a reference policy ({', '.join(REFERENCE_POLICIES)}), "
                            "module:function, path/to/file.py:function, or an http(s) URL")
    if module_name.endswith(".py"):
        path = Path(module_name).expanduser().resolve()
        if not path.is_file():
            raise ProviderError(f"policy {target!r}: no file {path}")
        spec = importlib.util.spec_from_file_location(f"spire_policy_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            if os.getcwd() in sys.path:
                raise
            sys.path.append(os.getcwd())  # a module next to where the command was run
            module = importlib.import_module(module_name)
    fn = getattr(module, attr, None)
    if not callable(fn):
        raise ProviderError(f"policy {target!r}: {module_name} has no function {attr}")
    return fn


def _takes_context(fn) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2 or any(p.kind == p.VAR_POSITIONAL for p in params)


def _as_text(out) -> str:
    if out is None:
        return ""
    if isinstance(out, dict):
        out = out.get("commands", "")
    if isinstance(out, (list, tuple)):
        return "; ".join(str(c).strip() for c in out if str(c).strip())
    return str(out)


class PolicyProvider(Provider):
    """A policy behind the Provider interface. Only the text protocol's `decide` is used."""

    def __init__(self, config: ProviderConfig, target: str):
        self.config = config
        self.model_id = target
        self.is_http = target.lower().startswith(("http://", "https://"))
        self._fn = None
        self._with_context = False
        # Loopback endpoints bypass system proxies, as the bridge does.
        host = (_host_of(target) or "").split(":")[0]
        loopback = host in ("127.0.0.1", "localhost", "::1") or host.endswith(".localhost")
        self._opener = (urllib.request.build_opener(urllib.request.ProxyHandler({})) if loopback
                        else urllib.request.build_opener())

    @property
    def kind(self) -> str:
        if self.is_http:
            return "http"
        return "reference" if self.model_id in REFERENCE_POLICIES else "python"

    def load(self):
        if self._fn is None and not self.is_http:
            fn = _load_callable(self.model_id)
            self._with_context = _takes_context(fn)
            self._fn = fn
        return self._fn

    def key_problem(self) -> str | None:
        if self.is_http:
            return None
        try:
            self.load()
        except (ProviderError, ImportError, OSError, SyntaxError) as e:
            return f"cannot load policy {self.model_id}: {e}"
        return None

    def decide(self, state: str, context: dict) -> Reply:
        """The policy's answer to one state, as a Reply with no tokens."""
        if self.is_http:
            text = self._post(state, context)
        else:
            fn = self.load()
            try:
                out = fn(state, context) if self._with_context else fn(state)
            except Exception as e:  # noqa: BLE001 - surfaced as the run's error
                raise ProviderError(f"policy {self.model_id} raised {type(e).__name__}: {e}") from e
            text = _as_text(out)
        return Reply(text=[text.strip()] if text.strip() else [], thinking=[], tool_calls=[], stop_reason="end_turn",
                     raw_stop_reason=None, usage={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                     served_by=f"policy:{self.model_id}", assistant_message={})

    def _post(self, state: str, context: dict) -> str:
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Accept": "text/plain, application/json",
            "X-Spire-Run": str(context.get("run_id", "")),
            "X-Spire-Seed": str(context.get("seed") or ""),
            "X-Spire-Attempt": str(context.get("attempt") or 1),
            "X-Spire-Step": str(context.get("step", 0)),
            "X-Spire-Retry": "1" if context.get("retry") else "0",
        }
        req = urllib.request.Request(self.model_id, data=state.encode("utf-8"), headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=HTTP_TIMEOUT_S) as resp:
                body = resp.read().decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code >= 500:
                raise TransientError(f"policy endpoint returned {e.code}: {detail}", e.code) from e
            raise ProviderError(f"policy endpoint returned {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise TransientError(f"cannot reach the policy endpoint ({e})") from e
        if "json" in ctype or body.lstrip().startswith("{"):
            try:
                return _as_text(json.loads(body))
            except json.JSONDecodeError:
                pass
        return body

    def describe_text(self, effort) -> dict:
        return {
            "provider": "policy",
            "api": "http" if self.is_http else "python-callable",
            "model_id": self.model_id,
            "base_url_host": _host_of(self.model_id) if self.is_http else None,
            "policy": {"kind": self.kind,
                       "target": REFERENCE_POLICIES.get(self.model_id, self.model_id) if not self.is_http else None,
                       "context": None if self.is_http else self._with_context},
            "reasoning": {"sent": False, "control": None, "effort": effort, "summary_visible": False},
            "caching": None,
            "tools_format": None,
        }
