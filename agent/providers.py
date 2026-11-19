"""Model providers for the agent: one small interface, one implementation per API family.

The agent loop (spire_agent.Session) only ever talks to a Provider. Every provider is handed the
same things, so every model plays under the same rules:

  - the system prompt text (identical for every model),
  - the tools in their neutral form (spire_agent.TOOLS: name, description, input_schema, strict),
  - the message history, kept in the provider's own format,
  - the effort setting and the output-token cap.

It returns a Reply: the model's text, its reasoning summary if the API exposes one, its tool calls
(id, name, parsed input, or a parse error), a normalised stop reason, normalised token usage
(input, output, cache_read, cache_write) and the model id that served the request.

Model specs
-----------
    claude-sonnet-5-5                       Anthropic (a bare claude-... name)
    anthropic:claude-sonnet-5-5             the same, spelled out
    openai:gpt-5.1                          OpenAI
    openrouter:meta-llama/llama-4-maverick  OpenRouter (ids contain '/')
    local:qwen3:32b                         a local OpenAI-compatible server (ids may contain ':')

Effort is appended after '@', which never occurs in these ids:
    openai:gpt-5.1@high    local:qwen3:32b@low    claude-opus-5-5@medium

`@default` sends no effort setting at all (the endpoint's own default). For back-compatibility a
Claude spec may still carry its effort after a colon (`claude-haiku-5-5:low`).

The decision protocol is a second '@' word (see agent/system1.py). `tools` is the default and what
every spec without one means; `text` is the System 1 protocol (one request per decision, commands
parsed from a plain-text reply, no tools, thinking off or minimal):
    openrouter:meta-llama/llama-3.1-8b-instruct@text    claude-haiku-5-5@low@text

Non-LLM policies use the `policy` provider and always play the text protocol (agent/policy.py):
    policy:random_legal    policy:greedy    policy:mypkg.agent:act    policy:http://127.0.0.1:9000/act

Providers
---------
PROVIDERS below is the table. Each entry can be adjusted from the environment without code:
    SPIRE_<NAME>_BASE_URL       e.g. SPIRE_LOCAL_BASE_URL=http://127.0.0.1:1234/v1 (LM Studio)
    SPIRE_<NAME>_API_KEY_ENV    name of the env var holding the key
    SPIRE_<NAME>_REASONING      none | reasoning_effort | reasoning.effort
    SPIRE_<NAME>_STRICT_TOOLS   1 / 0
More providers can be added with SPIRE_PROVIDERS=<path to a JSON file>:
    {"deepinfra": {"base_url": "https://api.deepinfra.com/v1/openai", "api_key_env": "DEEPINFRA_API_KEY"}}
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

EFFORT_SEP = "@"
# Decision protocols. "tools": the act/info/set_notes tool conversation (spire_agent.Session).
# "text": one request per decision, commands parsed from plain text (system1.TextSession).
PROTOCOLS = ("tools", "text", "decisions")
DEFAULT_PROTOCOL = "tools"
# Effort values the Anthropic API takes in output_config.
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Effort applied when a spec does not name one, for providers that take an effort setting.
DEFAULT_EFFORT = "low"
# "Send no effort setting": what is recorded for providers that do not take one.
NO_EFFORT = "default"

# Anthropic models that accept the server-side refusal fallback parameter.
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"}


class ProviderError(Exception):
    """A request failed in a way retrying will not fix. Ends the run as api_error."""


class RateLimited(ProviderError):
    def __init__(self, message: str, retry_after: float = 20):
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(ProviderError):
    """Connection trouble or a server-side (5xx) error: worth retrying after a pause."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- provider table


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    # "anthropic" (Messages API), "openai" (Chat Completions with function tools) or "policy" (no model:
    # a Python callable or an HTTP endpoint that maps a state to commands; see agent/policy.py)
    kind: str
    base_url: str | None = None  # None: the SDK's default (honours ANTHROPIC_BASE_URL / OPENAI_BASE_URL)
    api_key_env: str | None = None  # None: no key needed (local servers)
    # How effort reaches the model: None (not sent), "reasoning_effort" (top-level Chat Completions
    # parameter), "reasoning.effort" (OpenRouter's unified reasoning object), "output_config.effort" (Anthropic).
    reasoning: str | None = None
    max_tokens_param: str = "max_tokens"  # OpenAI's reasoning models only take max_completion_tokens
    strict_tools: bool = False  # send "strict": true on function tools (only where the endpoint accepts it)


PROVIDERS: dict[str, ProviderConfig] = {
    "decisions": ProviderConfig("decisions", "decisions", "https://openrouter.ai/api/alpha/decisions",
                                "OPENROUTER_API_KEY"),
    "anthropic": ProviderConfig("anthropic", "anthropic", None, "ANTHROPIC_API_KEY", "output_config.effort"),
    "openai": ProviderConfig("openai", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY",
                             "reasoning_effort", "max_completion_tokens", strict_tools=True),
    "openrouter": ProviderConfig("openrouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                                 "reasoning.effort"),
    "together": ProviderConfig("together", "openai", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "groq": ProviderConfig("groq", "openai", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    # Ollama's default; LM Studio is http://127.0.0.1:1234/v1, vLLM http://127.0.0.1:8000/v1.
    "local": ProviderConfig("local", "openai", "http://127.0.0.1:11434/v1", None),
    # Not a model API: `policy:<name>` names a reference policy, a module:function, or an http(s) URL.
    "policy": ProviderConfig("policy", "policy", None, None),
}

_REASONING_STYLES = {None, "reasoning_effort", "reasoning.effort"}


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def provider_table() -> dict[str, ProviderConfig]:
    """PROVIDERS plus SPIRE_PROVIDERS additions, with SPIRE_<NAME>_* environment overrides applied."""
    table = dict(PROVIDERS)
    extra_path = os.environ.get("SPIRE_PROVIDERS", "").strip()
    if extra_path:
        with open(extra_path, encoding="utf-8") as f:
            extra = json.load(f)
        for name, entry in extra.items():
            name = name.strip().lower()
            base = table.get(name, ProviderConfig(name, "openai"))
            known = {k: v for k, v in entry.items() if k in ProviderConfig.__dataclass_fields__ and k != "name"}
            table[name] = replace(base, **known)
    for name, cfg in list(table.items()):
        env = "SPIRE_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_"
        changes: dict = {}
        if os.environ.get(env + "BASE_URL"):
            changes["base_url"] = os.environ[env + "BASE_URL"].strip()
        if os.environ.get(env + "API_KEY_ENV"):
            changes["api_key_env"] = os.environ[env + "API_KEY_ENV"].strip()
        if os.environ.get(env + "REASONING") and cfg.kind == "openai":
            style = os.environ[env + "REASONING"].strip()
            changes["reasoning"] = None if style.lower() in ("", "none", "off", "0") else style
        if os.environ.get(env + "STRICT_TOOLS"):
            changes["strict_tools"] = _truthy(os.environ[env + "STRICT_TOOLS"])
        if changes:
            table[name] = replace(cfg, **changes)
    for cfg in table.values():
        if cfg.kind == "policy" and cfg.name == "policy":
            continue
        if cfg.kind not in ("anthropic", "openai", "decisions"):
            raise ValueError(f"provider {cfg.name}: kind must be 'anthropic' or 'openai', not {cfg.kind!r}")
        if cfg.kind == "openai" and cfg.reasoning not in _REASONING_STYLES:
            raise ValueError(f"provider {cfg.name}: reasoning must be one of none, reasoning_effort, reasoning.effort")
    return table


# ---------------------------------------------------------------- model specs


@dataclass(frozen=True)
class ModelSpec:
    spec: str  # exactly as given, effort included
    provider: ProviderConfig
    model_id: str  # what is sent to the API (for a policy: its name, module:function or URL)
    effort: str | None  # named in the spec, or None
    protocol: str = DEFAULT_PROTOCOL  # "tools" or "text"; not part of the name (recorded separately)

    @property
    def name(self) -> str:
        """The model's name in recordings and reports: the Claude id alone, otherwise provider:model_id."""
        if self.provider.name == "anthropic":
            return self.model_id
        return f"{self.provider.name}:{self.model_id}"

    @property
    def is_policy(self) -> bool:
        return self.provider.kind == "policy"


def parse_model_spec(spec: str) -> ModelSpec:
    """`[provider:]model[@effort][@protocol]`, plus the legacy `claude-...:effort`. Raises ValueError if it makes no sense.

    After the model come up to two '@' words, in either order: an effort (low, high, default, ...) and a
    protocol (tools, text). `policy:...` specs take no effort and always play the text protocol.
    """
    raw = spec.strip()
    if not raw:
        raise ValueError("empty model spec")
    body, effort, protocol = raw, None, None
    words = []
    while EFFORT_SEP in body and len(words) < 2:
        body, _, word = body.rpartition(EFFORT_SEP)
        words.append(word.strip().lower())
    for word in words:
        if not re.fullmatch(r"[a-z]+", word or ""):
            raise ValueError(f"model spec {spec!r}: each word after '{EFFORT_SEP}' must be an effort like low or "
                             f"high, or a protocol ({', '.join(PROTOCOLS)})")
        if word in PROTOCOLS:
            if protocol is not None:
                raise ValueError(f"model spec {spec!r}: names a protocol twice")
            protocol = word
        else:
            if effort is not None:
                raise ValueError(f"model spec {spec!r}: names an effort twice")
            effort = word
    if EFFORT_SEP in body:
        raise ValueError(f"model spec {spec!r}: at most one effort and one protocol may follow the model")
    table = provider_table()
    prefix, colon, rest = body.partition(":")
    if colon and prefix.lower() in table:
        provider, model_id = table[prefix.lower()], rest
    elif body.startswith("claude-"):
        provider, model_id = table["anthropic"], body
    elif colon:
        raise ValueError(f"model spec {spec!r}: unknown provider {prefix!r} (known: {', '.join(sorted(table))})")
    else:
        raise ValueError(f"model spec {spec!r}: name a provider, e.g. openai:{body} or local:{body} "
                         "(bare names are taken as Claude models only when they start with claude-)")
    if provider.kind == "anthropic" and ":" in model_id:
        # Legacy `claude-haiku-5-5:low`.
        head, _, tail = model_id.rpartition(":")
        if tail in ANTHROPIC_EFFORTS and effort is None:
            model_id, effort = head, tail
    if not model_id.strip():
        raise ValueError(f"model spec {spec!r}: no model id")
    if provider.kind == "policy":
        if effort is not None and effort != NO_EFFORT:
            raise ValueError(f"model spec {spec!r}: a policy takes no effort")
        if protocol not in (None, "text"):
            raise ValueError(f"model spec {spec!r}: policies play the text protocol only")
        protocol = "text"
    if provider.kind == "decisions":
        if effort not in (None, NO_EFFORT):
            raise ValueError(f"model spec {spec!r}: Decisions API takes no reasoning effort")
        if protocol not in (None, "decisions"):
            raise ValueError(f"model spec {spec!r}: Decisions API requires the decisions protocol")
        protocol = "decisions"
    elif protocol == "decisions":
        raise ValueError(f"model spec {spec!r}: use the decisions: provider for the decisions protocol")
    return ModelSpec(raw, provider, model_id.strip(), effort, protocol or DEFAULT_PROTOCOL)


def resolve_effort(spec: ModelSpec, default: str | None = None) -> tuple[str, str | None]:
    """The effort a run is played at, and a warning if a requested default could not be applied.

    The spec's own effort wins, then `default`, then DEFAULT_EFFORT. A provider that takes no effort
    setting plays at NO_EFFORT ("default"); naming an effort for it in the spec is an error.
    """
    cfg = spec.provider
    if cfg.kind == "policy":
        return NO_EFFORT, None
    if cfg.kind == "anthropic":
        effort = spec.effort or default or DEFAULT_EFFORT
        if effort not in ANTHROPIC_EFFORTS:
            raise ValueError(f"{spec.spec}: Claude effort must be one of {', '.join(ANTHROPIC_EFFORTS)}")
        return effort, None
    if cfg.reasoning is None:
        if spec.effort and spec.effort != NO_EFFORT:
            env = "SPIRE_" + re.sub(r"[^A-Z0-9]", "_", cfg.name.upper()) + "_REASONING"
            raise ValueError(f"{spec.spec}: provider {cfg.name} is configured not to send an effort setting; "
                             f"drop '@{spec.effort}' or set {env}=reasoning_effort")
        warning = None
        if default and default != NO_EFFORT:
            warning = f"{spec.spec}: provider {cfg.name} takes no effort setting; playing at the endpoint default"
        return NO_EFFORT, warning
    return spec.effort or default or DEFAULT_EFFORT, None


def safe_filename(name: str) -> str:
    """A form of a model name that is a valid file name everywhere (Windows included).

    Names that are already safe (every Claude id) are unchanged, so existing suites resume; anything
    else has its unsafe characters replaced and a short hash of the original added, so two models
    never share a file.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    safe = safe.replace("__", "_-")  # "__" separates fields in bench file names
    if safe == name:
        return name
    return f"{safe[:80]}-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:6]}"


# ---------------------------------------------------------------- replies


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict  # parsed arguments; {"_unparsed": raw} when they could not be parsed
    error: str | None = None  # set when the arguments were unusable: sent back to the model as an error result
    repaired: str | None = None  # how arguments in the wrong shape were recovered, when they were


def recover_arguments(raw: str) -> tuple[object, str | None]:
    """Parse tool-call arguments, recovering the two malformations whose meaning is unambiguous.

    Weaker models often get the call format slightly wrong while meaning something clear:
    several objects glued together ('{"commands": "end"}{"commands": "end"}': the first one is
    used) or the object sent as a JSON string ('"{\\"commands\\": \\"end\\"}"': it is unwrapped).
    Recovering these keeps the benchmark measuring play rather than API-format compliance; each
    recovery is recorded, so format reliability stays visible as its own number. Anything else
    raises json.JSONDecodeError as before.
    """
    text = raw.strip()
    if not text:
        return {}, None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value, end = json.JSONDecoder().raw_decode(text)  # raises if even the start is not JSON
        rest = text[end:].strip()
        if not isinstance(value, dict) or not rest.startswith("{"):
            raise
        return value, "first of several glued JSON objects"
    if isinstance(value, str):
        try:
            inner = json.loads(value)
        except json.JSONDecodeError:
            return value, None
        if isinstance(inner, dict):
            return inner, "JSON object wrapped in a string"
    return value, None


@dataclass
class Reply:
    text: list[str]
    thinking: list[str]
    tool_calls: list[ToolCall]
    stop_reason: str  # normalised: end_turn, tool_use, max_tokens, refusal, or the raw value
    raw_stop_reason: str | None
    usage: dict  # input, output, cache_read, cache_write (input excludes cached tokens)
    served_by: str
    assistant_message: dict  # appended to the history as is
    usage_reported: bool = True
    reasoning_tokens: int | None = None
    extra: dict = field(default_factory=dict)


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(str(url))
    host = parsed.hostname or ""
    if not host:
        return None
    # host[:port] only: never a path, query or user:password@.
    return f"{host}:{parsed.port}" if parsed.port else host


def tools_fingerprint(rendered) -> str:
    return hashlib.sha256(json.dumps(rendered, sort_keys=True).encode("utf-8")).hexdigest()[:12]


class Provider:
    """What Session needs from a model API."""

    config: ProviderConfig
    model_id: str

    def request(self, system: str, tools: list[dict], messages: list, effort: str, max_tokens: int) -> Reply:
        raise NotImplementedError

    def tool_result_messages(self, results: list[tuple[str, str, bool]]) -> list[dict]:
        """(tool call id, result text, is_error) for every call of one reply, as messages to append."""
        raise NotImplementedError

    def describe(self, tools: list[dict], effort: str) -> dict:
        """What run_start records about how this provider is driven (the per-provider fairness record)."""
        raise NotImplementedError

    # ---- the text (System 1) protocol: no tools, one request per decision

    def complete(self, system: str, messages: list, effort: str, max_tokens: int) -> Reply:
        """One plain-text request: no tools, thinking off or as low as the model allows."""
        raise NotImplementedError

    def describe_text(self, effort: str) -> dict:
        """run_start's fairness record for the text protocol."""
        raise NotImplementedError

    def key_problem(self) -> str | None:
        """Why requests cannot be authenticated, or None."""
        return None


# ---------------------------------------------------------------- Anthropic


class AnthropicProvider(Provider):
    """The Claude Messages API, exactly as the harness always called it."""

    def __init__(self, config: ProviderConfig, model_id: str, client=None):
        self.config = config
        self.model_id = model_id
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def build_request(self, system: str, tools: list[dict], messages: list, effort: str, max_tokens: int):
        """(create function name, kwargs). Kept byte-for-byte what the harness sent before providers existed."""
        kwargs = dict(
            model=self.model_id,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            # "summarized" returns a readable summary of the reasoning for the replay; billing is unchanged.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
            # Caches the growing conversation prefix so each step re-reads it at the cache price.
            cache_control={"type": "ephemeral"},
        )
        if self.model_id in FALLBACK_MODELS:
            # If a safety classifier declines a request, let the API retry it on a fallback model.
            return "beta", dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        return "messages", kwargs

    # How the text protocol keeps thinking to a minimum, per model (from the API's per-model rules):
    # "disabled" sends thinking={"type": "disabled"}; "between_tools" is Sonnet 5.5's thinking-off mode;
    # "always_on" models cannot turn thinking off, so they run adaptive at the lowest effort asked for.
    TEXT_THINKING = {
        "claude-sonnet-5-5": "between_tools",
        "claude-opus-5-5": "always_on",
        "claude-fable-5-2": "always_on",
        "claude-fable-5-1": "always_on",
        "claude-fable-5": "always_on",
    }

    def text_thinking_mode(self) -> str:
        return self.TEXT_THINKING.get(self.model_id, "disabled")

    def build_text_request(self, system: str, messages: list, effort: str, max_tokens: int):
        """(create function name, kwargs) for one text-protocol decision: no tools, thinking minimal."""
        kwargs = dict(
            model=self.model_id,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        mode = self.text_thinking_mode()
        if mode == "always_on":
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        elif mode == "between_tools":
            kwargs["thinking"] = {"type": "between_tools"}
        else:
            kwargs["thinking"] = {"type": "disabled"}
        if effort and effort != NO_EFFORT:
            kwargs["output_config"] = {"effort": effort}
        if self.model_id in FALLBACK_MODELS:
            return "beta", dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        return "messages", kwargs

    def complete(self, system, messages, effort, max_tokens) -> Reply:
        which, kwargs = self.build_text_request(system, messages, effort, max_tokens)
        return self._send(which, kwargs)

    def describe_text(self, effort) -> dict:
        base_url = os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        mode = self.text_thinking_mode()
        control = {"disabled": "thinking=disabled", "between_tools": "thinking=between_tools (thinking off)",
                   "always_on": "thinking=adaptive (cannot be disabled on this model)"}[mode]
        if effort and effort != NO_EFFORT:
            control += " + output_config.effort"
        return {
            "provider": self.config.name,
            "api": "anthropic-messages",
            "model_id": self.model_id,
            "base_url_host": _host_of(base_url),
            "reasoning": {"sent": mode == "always_on" or bool(effort and effort != NO_EFFORT),
                          "control": control, "effort": effort, "thinking": mode,
                          "summary_visible": mode == "always_on"},
            "caching": {"mode": "explicit", "detail": "cache_control on the system prompt only (requests are stateless)"},
            "tools_format": None,
            "refusal_fallback": self.model_id in FALLBACK_MODELS,
        }

    def request(self, system, tools, messages, effort, max_tokens) -> Reply:
        which, kwargs = self.build_request(system, tools, messages, effort, max_tokens)
        return self._send(which, kwargs)

    def _send(self, which: str, kwargs: dict) -> Reply:
        import anthropic

        try:
            if which == "beta":
                response = self.client.beta.messages.create(**kwargs)
            else:
                response = self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise RateLimited(str(e), float(e.response.headers.get("retry-after", "20"))) from e
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            status = getattr(e, "status_code", None)
            if status is not None and status < 500:
                raise ProviderError(str(e)) from e
            raise TransientError(str(e), status) from e
        except anthropic.APIError as e:
            raise ProviderError(str(e)) from e

        thinking, text, calls = [], [], []
        for block in response.content:
            if block.type == "thinking" and getattr(block, "thinking", ""):
                thinking.append(block.thinking)
            elif block.type == "text" and block.text.strip():
                text.append(block.text.strip())
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, block.input))
        u = response.usage
        return Reply(
            text=text,
            thinking=thinking,
            tool_calls=calls,
            stop_reason=response.stop_reason,
            raw_stop_reason=response.stop_reason,
            usage={
                "input": u.input_tokens or 0,
                "output": u.output_tokens or 0,
                "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
                "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
            },
            served_by=getattr(response, "model", self.model_id),
            # Keep the full content (thinking blocks included) so the next request is valid.
            assistant_message={"role": "assistant", "content": response.content},
        )

    def tool_result_messages(self, results):
        return [{
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": result, "is_error": is_error}
                for call_id, result, is_error in results
            ],
        }]

    def describe(self, tools, effort) -> dict:
        base_url = os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        return {
            "provider": self.config.name,
            "api": "anthropic-messages",
            "model_id": self.model_id,
            "base_url_host": _host_of(base_url),
            "reasoning": {
                "sent": True,
                "control": "thinking=adaptive (summarized display) + output_config.effort",
                "effort": effort,
                "summary_visible": True,
            },
            "caching": {
                "mode": "explicit",
                "detail": "cache_control on the system prompt and on the conversation prefix",
            },
            "tools_format": "anthropic tools (strict)",
            "tools_fingerprint": tools_fingerprint(tools),
            "refusal_fallback": self.model_id in FALLBACK_MODELS,
        }

    def key_problem(self) -> str | None:
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return None
        return "no API key. Set ANTHROPIC_API_KEY or put ANTHROPIC_API_KEY=... in .env"


# ---------------------------------------------------------------- OpenAI-compatible

_THINK_RE = re.compile(r"<think>(.*?)(?:</think>|$)", re.DOTALL)


def openai_tools(tools: list[dict], strict: bool) -> list[dict]:
    """The neutral tool definitions as Chat Completions function tools: same names, descriptions and schemas."""
    out = []
    for tool in tools:
        function = {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}
        if strict and tool.get("strict"):
            function["strict"] = True
        out.append({"type": "function", "function": function})
    return out


def _extra(obj, key):
    """A field the openai SDK does not model (provider extensions such as reasoning_content)."""
    if obj is None:
        return None
    value = getattr(obj, key, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(key)
    return value


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class OpenAICompatProvider(Provider):
    """Any OpenAI-compatible Chat Completions endpoint with function tools."""

    FINISH = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use",
              "length": "max_tokens", "content_filter": "refusal"}

    def __init__(self, config: ProviderConfig, model_id: str, client=None):
        self.config = config
        self.model_id = model_id
        self._client = client

    def api_key(self) -> str | None:
        if not self.config.api_key_env:
            return "not-needed"  # the SDK insists on some value; local servers ignore it
        return os.environ.get(self.config.api_key_env) or None

    @property
    def client(self):
        if self._client is None:
            import openai

            self._client = openai.OpenAI(base_url=self.config.base_url, api_key=self.api_key())
        return self._client

    def build_request(self, system, tools, messages, effort, max_tokens) -> dict:
        params: dict = {
            "model": self.model_id,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": openai_tools(tools, self.config.strict_tools),
            self.config.max_tokens_param: max_tokens,
        }
        if effort and effort != NO_EFFORT:
            if self.config.reasoning == "reasoning_effort":
                params["reasoning_effort"] = effort
            elif self.config.reasoning == "reasoning.effort":
                params["extra_body"] = {"reasoning": {"effort": effort}}
        return params

    def build_text_request(self, system, messages, effort, max_tokens) -> dict:
        """One text-protocol decision: no tools. Effort is sent only as the spec asks (e.g. `@minimal`)."""
        params: dict = {
            "model": self.model_id,
            "messages": [{"role": "system", "content": system}, *messages],
            self.config.max_tokens_param: max_tokens,
        }
        if effort and effort != NO_EFFORT:
            if self.config.reasoning == "reasoning_effort":
                params["reasoning_effort"] = effort
            elif self.config.reasoning == "reasoning.effort":
                params["extra_body"] = {"reasoning": {"effort": effort}}
        return params

    def complete(self, system, messages, effort, max_tokens) -> Reply:
        return self._send(self.build_text_request(system, messages, effort, max_tokens))

    def describe_text(self, effort) -> dict:
        cfg = self.config
        sent = effort != NO_EFFORT and cfg.reasoning is not None
        return {
            "provider": cfg.name,
            "api": "openai-chat-completions",
            "model_id": self.model_id,
            "base_url_host": _host_of(cfg.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"),
            "reasoning": {
                "sent": sent,
                "control": cfg.reasoning if sent else None,
                "effort": effort,
                "summary_visible": "only if the endpoint returns reasoning text (reasoning_content, reasoning or <think>)",
            },
            "caching": {"mode": "automatic-or-none",
                        "detail": "no cache control is sent; any prefix caching is the endpoint's own"},
            "tools_format": None,
            "max_tokens_param": cfg.max_tokens_param,
        }

    def request(self, system, tools, messages, effort, max_tokens) -> Reply:
        return self._send(self.build_request(system, tools, messages, effort, max_tokens))

    def _send(self, params: dict) -> Reply:
        import openai

        try:
            response = self.client.chat.completions.create(**params)
        except openai.RateLimitError as e:
            retry = 20.0
            try:
                retry = float(e.response.headers.get("retry-after", "20"))
            except (TypeError, ValueError, AttributeError):
                pass
            raise RateLimited(str(e), retry) from e
        except openai.APIConnectionError as e:
            raise TransientError(str(e)) from e
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientError(str(e), e.status_code) from e
            raise ProviderError(str(e)) from e
        except openai.APIError as e:
            raise ProviderError(str(e)) from e

        choices = getattr(response, "choices", None) or []
        if not choices:
            # OpenRouter reports upstream failures as a 200 with an error object and no choices.
            raise TransientError(f"no choices in response: {_extra(response, 'error') or response}")
        choice = choices[0]
        message = choice.message

        content = message.content or ""
        thinking = []
        for key in ("reasoning_content", "reasoning"):  # vLLM/DeepSeek/Ollama, OpenRouter
            value = _extra(message, key)
            if isinstance(value, str) and value.strip():
                thinking.append(value.strip())
                break
        # Models served without a reasoning parser leave <think> blocks in the text.
        for match in _THINK_RE.finditer(content):
            if match.group(1).strip():
                thinking.append(match.group(1).strip())
        visible = _THINK_RE.sub("", content).strip()

        calls: list[ToolCall] = []
        history_calls: list[dict] = []
        for i, tc in enumerate(message.tool_calls or []):
            function = getattr(tc, "function", None)
            name = (getattr(function, "name", None) or "").strip()
            raw = getattr(function, "arguments", None)
            raw = raw if isinstance(raw, str) else json.dumps(raw if raw is not None else {})
            call_id = getattr(tc, "id", None) or f"call_{i}"
            error, parsed, repaired = None, None, None
            try:
                parsed, repaired = recover_arguments(raw)
            except json.JSONDecodeError as e:
                error = (f"The arguments of this {name or 'tool'} call were not valid JSON ({e.msg} at "
                         f"position {e.pos}): {raw[:300]!r}. Send the call again with a JSON object, "
                         'e.g. {"commands": "end"}.')
            if error is None and not isinstance(parsed, dict):
                error = f"The arguments of this {name or 'tool'} call must be a JSON object, not {raw[:200]!r}."
            if error is None and not name:
                error = "This tool call had no tool name; use act, info or set_notes."
            calls.append(ToolCall(call_id, name or "?", parsed if error is None else {"_unparsed": raw}, error,
                                  repaired if error is None else None))
            # Malformed arguments go back as "{}": several servers (Ollama among them) reject a history
            # whose tool-call arguments are not JSON. Recovered ones go back in their clean form.
            # The recording keeps what the model actually sent.
            if error is not None:
                history_args = "{}"
            elif repaired:
                history_args = json.dumps(parsed, ensure_ascii=False)
            else:
                history_args = raw
            history_calls.append({"id": call_id, "type": "function",
                                  "function": {"name": name or "?", "arguments": history_args}})

        assistant: dict = {"role": "assistant", "content": visible or None}
        if history_calls:
            assistant["tool_calls"] = history_calls
        elif assistant["content"] is None:
            assistant["content"] = ""
        details = _extra(message, "reasoning_details")
        if details:  # OpenRouter: hands reasoning back so models that need it keep their chain across tool calls
            assistant["reasoning_details"] = details

        usage = getattr(response, "usage", None)
        prompt = _as_int(getattr(usage, "prompt_tokens", 0))
        completion = _as_int(getattr(usage, "completion_tokens", 0))
        pdetails = getattr(usage, "prompt_tokens_details", None)
        cache_read = _as_int(getattr(pdetails, "cached_tokens", 0)) or _as_int(_extra(usage, "prompt_cache_hit_tokens"))
        cache_write = _as_int(_extra(pdetails, "cache_write_tokens"))
        cdetails = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(cdetails, "reasoning_tokens", None)

        raw_finish = getattr(choice, "finish_reason", None)
        stop = self.FINISH.get(raw_finish or "", raw_finish or "end_turn")
        if stop == "end_turn" and calls:
            stop = "tool_use"  # several servers say "stop" even when they return tool calls
        return Reply(
            text=[visible] if visible else [],
            thinking=thinking,
            tool_calls=calls,
            stop_reason=stop,
            raw_stop_reason=raw_finish,
            usage={
                # Chat Completions counts cached tokens inside prompt_tokens; the harness (like the
                # Anthropic API) counts them separately.
                "input": max(prompt - cache_read - cache_write, 0),
                "output": completion,  # includes reasoning tokens, as Anthropic output includes thinking
                "cache_read": cache_read,
                "cache_write": cache_write,
            },
            served_by=getattr(response, "model", None) or self.model_id,
            assistant_message=assistant,
            usage_reported=usage is not None,
            reasoning_tokens=_as_int(reasoning_tokens) if reasoning_tokens is not None else None,
        )

    def tool_result_messages(self, results):
        # Chat Completions has no is_error flag; errors are worded as errors in the text itself.
        return [{"role": "tool", "tool_call_id": call_id, "content": result} for call_id, result, _ in results]

    def describe(self, tools, effort) -> dict:
        cfg = self.config
        sent = effort != NO_EFFORT and cfg.reasoning is not None
        rendered = openai_tools(tools, cfg.strict_tools)
        return {
            "provider": cfg.name,
            "api": "openai-chat-completions",
            "model_id": self.model_id,
            "base_url_host": _host_of(cfg.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"),
            "reasoning": {
                "sent": sent,
                "control": cfg.reasoning if sent else None,
                "effort": effort,
                # Whether the model honoured it shows in each step's reasoning_tokens, where the endpoint reports them.
                "summary_visible": "only if the endpoint returns reasoning text (reasoning_content, reasoning or <think>)",
            },
            "caching": {
                "mode": "automatic-or-none",
                "detail": "no cache control is sent; any prefix caching is the endpoint's own and shows as cache_read",
            },
            "tools_format": f"openai function tools (strict={'true' if cfg.strict_tools else 'false'})",
            "tools_fingerprint": tools_fingerprint(rendered),
            "max_tokens_param": cfg.max_tokens_param,
        }

    def key_problem(self) -> str | None:
        if self.api_key():
            return None
        return (f"no API key for provider {self.config.name}. Set {self.config.api_key_env} "
                f"or put {self.config.api_key_env}=... in .env")


def make_provider(spec: ModelSpec, client=None) -> Provider:
    if spec.provider.kind == "decisions":
        from decisions import DecisionsProvider

        return DecisionsProvider(spec.provider, spec.model_id)
    if spec.provider.kind == "policy":
        from policy import PolicyProvider  # agent/policy.py; imported here so providers.py stays standalone

        return PolicyProvider(spec.provider, spec.model_id)
    if spec.provider.kind == "anthropic":
        return AnthropicProvider(spec.provider, spec.model_id, client)
    return OpenAICompatProvider(spec.provider, spec.model_id, client)
