"""LLM access for Teach Me: a provider chain that returns schema-checked JSON,
with a model and effort per task and a running cost for the build."""
from __future__ import annotations

import logging
import os
import re
import threading

from summary_providers import (DEFAULT_MODELS, GEMINI_URL, ProviderError, SummaryProviders, _post, _text,
                               env_key, env_model, parse_structured, permanent_error)

MAX_TOKENS = 32_000
_SECRETS = [
    (re.compile(r"(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+"), r"\1…"),      # Anthropic / OpenAI / OpenRouter
    (re.compile(r"AIza[A-Za-z0-9_-]{10,}"), "AIza…"),                   # Google / YouTube
    (re.compile(r"([?&](?:key|api_key|access_token)=)[^&\s'\"]+", re.I), r"\1…"),
]

# List prices, $ per million tokens (input, output), matched by model-name prefix.
# Thinking is billed as output. OpenRouter reports its own charge; its ids are matched
# without the vendor prefix, for estimates.
PRICES = {
    "claude-fable-5-1": (10, 50), "claude-opus-5-5": (4, 20), "claude-opus-5": (5, 25),
    "claude-sonnet-5": (2, 10), "claude-haiku-4-5": (1, 5),
    "gpt-6-luna": (0.10, 0.50), "gpt-5-nano": (0.05, 0.40),
    "gemini-3.8-flash": (0.75, 3.75),            # doubles on 2027-01-01
    "gemini-2.5-flash-lite": (0.10, 0.40), "gemini-3.5-flash-lite": (0.30, 2.50),
    "qwen3.8-flash": (0.15, 0.47), "glm-5.3": (1.40, 4.40), "minimax-m3": (0.30, 1.20),
}
# Current Claude model ids. Anything else in config.yaml is almost certainly a typo
# (e.g. "claude-sonnet-5-5"), and every call to it would fail over to the next provider.
ANTHROPIC_MODELS = {"claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5"}
SEARCH_PRICE = 0.01          # $10 per 1,000 web searches

log = logging.getLogger("teachme")


def mask_secrets(text: str) -> str:
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def safe_error(exc) -> str:
    """Error text with API keys masked, short enough for a log line."""
    if getattr(exc, "friendly", False):          # already a clean message for the learner
        return str(exc)
    return mask_secrets(f"{type(exc).__name__}: {exc}")[:500]


def task_of(label: str) -> str:
    """'day 3 lesson' -> 'lesson', 'course plan' -> 'plan', 'day 2 video check' -> 'video_check'."""
    task = re.sub(r"^day \d+ ", "", label).strip()
    return {"course plan": "plan", "placement quiz": "placement", "course category": "category"}.get(
        task, task.replace(" ", "_"))


def price_of(model: str) -> tuple[float, float] | None:
    model = (model or "").rsplit("/", 1)[-1]           # "openai/gpt-6-luna" (OpenRouter) -> "gpt-6-luna"
    for name, price in sorted(PRICES.items(), key=lambda p: -len(p[0])):
        if model.startswith(name):
            return price
    return None


def check_models(config: dict) -> list[str]:
    """Warnings for Claude model ids in config.yaml that don't exist."""
    found = [((config.get("web_search_models") or {}).get("anthropic"), "web_search_models.anthropic")]
    for task, chain in task_chains(config).items():
        found += [(model, f"tasks.{task}") for provider, model, _ in chain if provider == "anthropic"]
    return [f"{where}: {model!r} is not a Claude model id (current ones: claude-sonnet-5, claude-opus-5, "
            f"claude-haiku-4-5, ...)" for model, where in found if model and model not in ANTHROPIC_MODELS]


class Meter:
    """Adds up what this process spends, logs every call, and keeps a list of the
    calls (task, provider, model, tokens, cost) for the course's build report."""
    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0.0
        self.by_model: dict[str, float] = {}
        self.calls: list[dict] = []

    def model_cost(self, model: str) -> float:
        with self.lock:
            return self.by_model.get(model, 0.0)

    def add(self, label: str, model: str, tokens_in: int, tokens_out: int, cost: float | None,
            searches: int = 0, provider: str = "") -> None:
        with self.lock:
            self.total += cost or 0.0
            self.by_model[model] = self.by_model.get(model, 0.0) + (cost or 0.0)
            total = self.total
            self.calls.append({"label": label, "task": task_of(label), "provider": provider, "model": model,
                               "tokens_in": tokens_in, "tokens_out": tokens_out, "searches": searches,
                               "cost": cost})
        shown = f"${cost:.3f}" if cost is not None else "unknown"
        log.info("usage task=%s model=%s in=%s out=%s searches=%s cost=%s total=$%.2f",
                 label, model, tokens_in, tokens_out, searches, shown, total)

    def anthropic(self, label: str, message) -> None:
        usage = message.usage
        tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        tokens_out = usage.output_tokens or 0
        searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
        price = price_of(message.model)
        cost = None if price is None else (tokens_in * price[0] + tokens_out * price[1]) / 1e6 + searches * SEARCH_PRICE
        self.add(label, message.model, tokens_in, tokens_out, cost, searches, "anthropic")

    def http(self, label: str, model: str, data: dict) -> None:
        if "eval_count" in data or "prompt_eval_count" in data:          # Ollama: on this computer, free
            self.add(label, model, data.get("prompt_eval_count", 0), data.get("eval_count", 0), 0.0,
                     provider="ollama")
            return
        provider = "gemini" if "usageMetadata" in data else "openrouter" if "/" in model else "openai"
        if "usageMetadata" in data:                      # Gemini; thinking is billed as output
            usage = data["usageMetadata"]
            tokens_in = usage.get("promptTokenCount", 0)
            tokens_out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
            cost = None
        else:
            usage = data.get("usage") or {}
            tokens_in = usage.get("prompt_tokens", usage.get("input_tokens", 0))
            tokens_out = usage.get("completion_tokens", usage.get("output_tokens", 0))
            cost = usage.get("cost")          # OpenRouter reports the real charge; OpenAI doesn't
        if cost is None:
            price = price_of(model)
            cost = None if price is None else (tokens_in * price[0] + tokens_out * price[1]) / 1e6
        self.add(label, model, tokens_in, tokens_out, float(cost) if cost is not None else None, provider=provider)


def _effort_config(model: str, effort: str) -> dict:
    # Haiku 4.5 has no effort setting.
    return {} if model.startswith("claude-haiku-4") else {"effort": effort}


# Models documented to take server-side refusal fallbacks; Sonnet and Haiku run without them.
FALLBACK_MODELS = ("claude-fable-5", "claude-opus-5", "claude-mythos-5")


def anthropic_call(effort: str, on_message=None):
    """Build the Anthropic structured-output call used by SummaryProviders.structured."""
    def call(system: str, prompt: str, schema: dict, model: str) -> str:
        import anthropic
        request = dict(model=model, max_tokens=MAX_TOKENS, system=[{"type": "text", "text": system}],
                       messages=[{"role": "user", "content": prompt}],
                       output_config={**_effort_config(model, effort),
                                      "format": {"type": "json_schema", "schema": schema}})
        with anthropic.Anthropic(timeout=600, max_retries=2) as api:
            # Streaming keeps long lessons (with SVG figures) clear of HTTP timeouts.
            if model.startswith(FALLBACK_MODELS):
                # Re-run a classifier refusal on Anthropic's recommended model.
                stream = api.beta.messages.stream(**request, betas=["server-side-fallback-2026-07-01"],
                                                  fallbacks="default")
            else:
                stream = api.messages.stream(**request)
            with stream as events:
                message = events.get_final_message()
        if on_message:
            on_message(message)
        if message.stop_reason == "refusal":
            raise ProviderError("The model declined this request")
        if message.stop_reason == "max_tokens":
            raise ProviderError("The response was cut off (max_tokens)")
        return "".join(b.text for b in message.content if b.type == "text")
    return call


# Tried in this order. OpenRouter + Exa comes first because it is far cheaper than
# Anthropic's own web search tool, which reads whole result pages.
WEB_SEARCH_MODELS = {"openrouter": "anthropic/claude-sonnet-5", "anthropic": "claude-sonnet-5"}
WEB_MAX_TOKENS = 16_000


def anthropic_web_call(system: str, prompt: str, model: str, searches: int, on_message=None) -> str:
    """Claude with Anthropic's server-side web search tool; returns the final text."""
    import anthropic
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": searches}]
    messages = [{"role": "user", "content": prompt}]
    with anthropic.Anthropic(timeout=600, max_retries=2) as api:
        for _ in range(4):
            message = api.messages.create(model=model, max_tokens=WEB_MAX_TOKENS, system=system,
                                          tools=tools, messages=messages)
            if on_message:
                on_message(message)
            if message.stop_reason != "pause_turn":
                break
            # The server paused a long search turn; send it back to continue.
            messages = [messages[0], {"role": "assistant", "content": message.content}]
        else:
            raise ProviderError("The web search did not finish")
    if message.stop_reason == "refusal":
        raise ProviderError("The model declined this request")
    return "".join(b.text for b in message.content if b.type == "text")


def openrouter_web_call(key: str, system: str, prompt: str, model: str, engine: str, results: int,
                        on_usage=None) -> str:
    """The search through OpenRouter. The "exa" engine hands the model a short list of
    results (about $0.02 a day); "native" lets it read whole pages (10-20x the cost)."""
    data = _post("https://openrouter.ai/api/v1/chat/completions", key, {
        "model": model, "max_tokens": WEB_MAX_TOKENS,
        "plugins": [{"id": "web", "engine": engine, "max_results": results}],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
    if on_usage:
        on_usage(data)
    return _text("openrouter", data, "search result")


def gemini_video_call(key: str, model: str, system: str, prompt: str, schema: dict, video_url: str,
                      effort: str | None = None, fps: float | None = None, resolution: str | None = None,
                      on_usage=None) -> str:
    """Gemini watches a public YouTube video (`video_url`) and answers in JSON."""
    video = {"fileData": {"fileUri": video_url}}
    if fps:
        video["videoMetadata"] = {"fps": fps}
    config = {"maxOutputTokens": 4_000, "responseMimeType": "application/json", "responseJsonSchema": schema}
    if resolution:                       # "low" sends far fewer tokens per frame
        config["mediaResolution"] = f"MEDIA_RESOLUTION_{resolution.upper()}"
    if effort:
        config["thinkingConfig"] = {"thinkingLevel": effort.upper()}
    data = _post(GEMINI_URL.format(model=model), key, {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [video, {"text": prompt}]}],   # video first, as Google advises
        "generationConfig": config})
    if on_usage:
        on_usage(data)
    return _text("gemini", data, "video verdict")


def openrouter_video_call(key: str, model: str, system: str, prompt: str, schema: dict, video_url: str,
                          effort: str | None = None, on_usage=None) -> str:
    """The same through OpenRouter. Only Google AI Studio's Gemini endpoints take YouTube
    links, so the request is pinned to that provider."""
    payload = {"model": model, "max_tokens": 4_000,
               "provider": {"only": ["google-ai-studio"], "allow_fallbacks": False},
               "response_format": {"type": "json_schema", "json_schema": {"name": "result", "strict": True,
                                                                          "schema": schema}},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": [{"type": "video_url", "video_url": {"url": video_url}},
                                                         {"type": "text", "text": prompt}]}]}
    if effort:
        payload["reasoning"] = {"effort": effort}
    data = _post("https://openrouter.ai/api/v1/chat/completions", key, payload)
    if on_usage:
        on_usage(data)
    return _text("openrouter", data, "video verdict")


def can_watch(route) -> bool:
    """Gemini itself, or Gemini through OpenRouter: the only models that can watch a YouTube link."""
    return route[0] == "gemini" or (route[0] == "openrouter" and route[1].startswith("google/gemini"))


def parse_loose(text: str, schema: dict) -> dict:
    """Search answers can't use strict JSON mode, so accept the JSON object inside the text."""
    try:
        return parse_structured(text, schema)
    except ProviderError:
        start, end = (text or "").find("{"), (text or "").rfind("}")
        if start < 0 or end <= start:
            raise
        return parse_structured(text[start:end + 1], schema)


# ------------------------------------------------------------ per-task models

# Every task lists its models in config.yaml, first choice first:
#   lesson: [openrouter/openai/gpt-6-luna@low, openrouter/qwen/qwen3.8-flash, openrouter/z-ai/glm-5.3]
# Each one is "provider/model", plus "@effort" if wanted. A model whose key is
# missing is skipped and the next one in the list is tried.
TASKS = ("plan", "lesson", "quiz", "placement", "walkthrough", "videos", "category", "video_judge")
# What the learner loses when a task has no usable model.
WITHOUT = {"plan": "no course can be built", "lesson": "no course can be built",
           "quiz": "mix courses get no quizzes", "placement": "the placement quiz won't work",
           "walkthrough": "video days get no animated walkthrough",
           "videos": "videos can't be chosen when Gemini can't watch them",
           "category": "courses aren't filed in a category",
           "video_judge": "videos are picked by title and description instead of being watched"}
KEY_CHECKS = {
    "openrouter": ("https://openrouter.ai/api/v1/key", lambda k: {"Authorization": f"Bearer {k}"}),
    "openai": ("https://api.openai.com/v1/models", lambda k: {"Authorization": f"Bearer {k}"}),
    "anthropic": ("https://api.anthropic.com/v1/models", lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models", lambda k: {"x-goog-api-key": k}),
}


def parse_route(entry) -> tuple[str, str, str | None]:
    """'openrouter/openai/gpt-6-luna@low' -> ('openrouter', 'openai/gpt-6-luna', 'low')."""
    if isinstance(entry, dict):                     # the older {provider, model, effort} form
        provider = entry.get("provider", "anthropic")
        return provider, entry.get("model") or DEFAULT_MODELS.get(provider, ""), entry.get("effort")
    text, _, effort = str(entry).strip().partition("@")
    provider, _, model = text.partition("/")
    provider, model = provider.strip().lower(), model.strip()
    if provider not in DEFAULT_MODELS or not model:
        raise ValueError(f"config.yaml: {entry!r} should be provider/model, e.g. openrouter/openai/gpt-6-luna "
                         f"(providers: {', '.join(DEFAULT_MODELS)})")
    return provider, model, effort.strip() or None


def route_name(route) -> str:
    return f"{route[0]}/{route[1]}" + (f"@{route[2]}" if route[2] else "")


def task_chains(config: dict) -> dict[str, list[tuple]]:
    chains = {}
    for task, entries in (config.get("tasks") or {}).items():
        entries = entries if isinstance(entries, list) else [entries]
        chains[task] = [parse_route(e) for e in entries if e]
    return chains


def missing_key(provider: str) -> str | None:
    """Why the provider can't be used (no key, Ollama not running), or None."""
    if env_key(provider):
        return None
    return "Ollama isn't running" if provider == "ollama" else f"no {provider.upper()}_API_KEY in .env"


def check_key(provider: str) -> str | None:
    """Like missing_key, but also asks the provider whether the key works (free)."""
    import requests
    problem = missing_key(provider)
    if problem or provider not in KEY_CHECKS:
        return problem
    url, headers = KEY_CHECKS[provider]
    try:
        resp = requests.get(url, headers=headers(env_key(provider)), timeout=8)
    except requests.RequestException as exc:
        return f"couldn't reach {provider} to check the key ({type(exc).__name__})"
    if resp.status_code in (400, 401, 403):
        return f"{provider.upper()}_API_KEY was rejected (wrong or revoked key)"
    if resp.status_code >= 400:
        return f"{provider} answered HTTP {resp.status_code} when checking the key"
    if provider == "openrouter":
        try:
            left = (resp.json().get("data") or {}).get("limit_remaining")
        except ValueError:
            left = None
        if left is not None and left <= 0:
            return "the OpenRouter key has no credit left"
    return None


def openrouter_models() -> set[str] | None:
    import requests
    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        return {m["id"] for m in resp.json()["data"]}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def task_status(config: dict, online: bool = True) -> dict[str, list[dict]]:
    """For each task, each model in its list: usable or not, and why. Never includes keys.
    `online` also checks each key with its provider, and OpenRouter model names."""
    chains = task_chains(config)
    problems: dict[str, str | None] = {}
    known = openrouter_models() if online and any(r[0] == "openrouter" for c in chains.values() for r in c) \
        else None
    status = {}
    for task in list(TASKS) + [t for t in chains if t not in TASKS]:
        rows = []
        for route in chains.get(task, []):
            provider, model = route[0], route[1]
            if provider not in problems:
                problems[provider] = check_key(provider) if online else missing_key(provider)
            why = problems[provider]
            if not why and task == "video_judge" and not can_watch(route):
                why = "only Gemini can watch YouTube videos (gemini/... or openrouter/google/gemini-...)"
            elif not why and provider == "openrouter" and known is not None and model not in known:
                why = f"OpenRouter has no model named {model}"
            elif not why and provider == "anthropic" and model not in ANTHROPIC_MODELS:
                why = f"{model} is not a Claude model id"
            rows.append({"model": route_name(route), "ok": not why, "problem": why})
        status[task] = rows
    return status


def task_problems(status: dict[str, list[dict]]) -> list[str]:
    """One line per task that has no usable model: what to fix, and what is lost."""
    lines = []
    for task, rows in status.items():
        if any(r["ok"] for r in rows):
            continue
        why = "; ".join(dict.fromkeys(r["problem"] for r in rows)) or "no models listed in config.yaml"
        lines.append(f"{task}: no usable model ({why}), so {WITHOUT.get(task, 'this step fails')}")
    return lines


def extra_problems(config: dict) -> list[str]:
    """Missing pieces outside the AI models: the YouTube key and the narration voice."""
    import narration
    lines = []
    if config.get("video_source", "youtube") == "youtube" and not os.getenv("YOUTUBE_API_KEY", "").strip():
        lines.append("videos: no YOUTUBE_API_KEY in .env, so video days get no YouTube videos "
                     "(only the animated walkthrough)")
    try:
        narrator = narration.Narrator(config)
        why = narrator.unavailable_reason() if narrator.enabled else None
    except ValueError as exc:
        why = str(exc)
    if why:
        lines.append(f"narration: {why}; walkthroughs use the computer's own voice meanwhile")
    return lines


def ignored_env_models() -> list[str]:
    """*_MODEL lines in .env: no longer used, since each task lists its own models."""
    return [f"{p.upper()}_MODEL" for p in ("anthropic", "openai", "openrouter", "gemini") if env_model(p)]


def provider_status(config: dict) -> dict:
    """What the app can use right now, for the setup popup. Never includes keys."""
    import ollama
    keys = {p: bool(env_key(p)) for p in ("anthropic", "openai", "openrouter", "gemini")}
    keys["youtube"] = bool(os.getenv("YOUTUBE_API_KEY", "").strip())
    local = ollama.status()
    try:
        tasks = task_status(config)
        problems = task_problems(tasks)
    except ValueError as exc:
        tasks, problems = {}, [str(exc)]
    problems += extra_problems(config)
    working = [r["model"] for rows in tasks.values() for r in rows if r["ok"]]

    def usable(task):
        return any(r["ok"] for r in tasks.get(task, []))
    return {"keys": keys, "ollama": local, "tasks": tasks, "problems": problems,
            "ignored_env_models": ignored_env_models(),
            "can_build": usable("plan") and usable("lesson"),
            "local_only": bool(working) and all(m.startswith("ollama/") for m in working)}


class LLM:
    def __init__(self, config: dict, progress=None):
        self.providers = SummaryProviders("", None, safe_error, max_output_tokens=MAX_TOKENS, progress=progress,
                                          order=",".join(DEFAULT_MODELS))
        self.effort = config.get("effort", "low")          # Claude's effort when a model has no @effort
        self.chains = task_chains(config)
        self.video_fps = config.get("video_fps")
        self.video_resolution = config.get("video_resolution", "low")
        self.web_models = config.get("web_search_models") or WEB_SEARCH_MODELS
        self.web_searches = max(1, int(config.get("web_searches_per_day", 3)))
        self.web_engine = config.get("web_search_engine", "exa")
        self.web_results = max(3, int(config.get("web_search_results", 10)))
        self.meter = Meter()
        for warning in check_models(config):
            log.warning("config: %s", warning)
            self.providers.progress(f"config.yaml: {warning}")
        for name in ignored_env_models():
            self.providers.progress(f".env: {name} is ignored; each task's models are listed in config.yaml")
        status = task_status(config, online=False)
        problems = task_problems(status)
        for line in problems:
            log.warning("setup: %s", line)
            self.providers.progress(f"Setup: {line}")
        if not any(r["ok"] for rows in status.values() for r in rows):
            raise ProviderError("No task has a usable model. " + " ".join(problems)
                                + " Add the missing keys to .env (see README.md), then Retry.")

    def spent(self) -> float:
        return self.meter.total

    def has_key(self, provider: str) -> bool:
        return bool(env_key(provider))

    def route(self, task: str) -> tuple[str, str, str | None] | None:
        """The first model in the task's list that has a key, or None."""
        return next((r for r in self.chains.get(task, []) if not missing_key(r[0])), None)

    def json(self, label: str, system: str, prompt: str, schema: dict, route: tuple | None = None,
             fallback: bool = True) -> dict:
        """JSON from the task's first model, then the next ones in its list if that fails.
        Each model gets one retry after a passing failure (bad JSON, timeout). `route` =
        (provider, model[, effort]) replaces the list for this call (the eval uses it to
        test one model alone; `fallback` is accepted for its calls)."""
        import summary_providers
        task = task_of(label)
        chain = [(route[0], route[1], route[2] if len(route) > 2 else None)] if route else self.chains.get(task)
        if not chain:
            raise ProviderError(f"config.yaml lists no models for the task '{task}'")
        errors = []
        for provider, model, effort in chain:
            with self.providers.lock:
                earlier = self.providers.disabled.get(provider)
            why = missing_key(provider) or (f"skipped, {provider} failed earlier: {earlier}" if earlier else None)
            if why:
                errors.append(f"{provider}/{model}: {why}")
                continue
            for _ in range(2):
                self.providers.progress(f"Running {label} with {provider}/{model}...")
                try:
                    if provider == "anthropic":
                        call = anthropic_call(effort or self.effort, lambda m: self.meter.anthropic(label, m))
                        text = call(system, prompt, schema, model)
                    else:
                        text = summary_providers.structured_http(
                            provider, model, env_key(provider), system, prompt, schema, MAX_TOKENS,
                            on_usage=lambda data, m=model: self.meter.http(label, m, data), effort=effort)
                    return parse_structured(text, schema)
                except Exception as exc:
                    # A permanent failure (bad key, no credit) also skips this provider from now on.
                    self.providers._failed(label, f"{provider}/{model}", exc, errors, subject="task")
                    if permanent_error(exc):
                        with self.providers.lock:
                            self.providers.disabled[provider] = safe_error(exc)
                        break
        raise ProviderError(f"No model could do the {task} step. " + "; ".join(errors))

    def judges(self) -> list[tuple]:
        """The video_judge models that can watch videos and have a key, in order."""
        return [r for r in self.chains.get("video_judge", []) if can_watch(r) and not missing_key(r[0])]

    def can_watch_videos(self) -> bool:
        return bool(self.judges())

    def video_json(self, label: str, system: str, prompt: str, schema: dict, video_url: str) -> dict:
        """Gemini watches the video and answers in JSON (task `video_judge`), directly or
        through OpenRouter; each model in the list gets one retry."""
        routes = self.judges()
        if not routes:
            raise ProviderError("No model can watch videos: tasks.video_judge in config.yaml needs gemini/... "
                                "(GEMINI_API_KEY) or openrouter/google/gemini-... (OPENROUTER_API_KEY)")
        error = None
        for provider, model, effort in routes:
            usage = lambda data, m=model: self.meter.http(label, m, data)
            for _ in range(2):
                try:
                    if provider == "gemini":
                        text = gemini_video_call(env_key("gemini"), model, system, prompt, schema, video_url, effort,
                                                 self.video_fps, self.video_resolution, usage)
                    else:
                        text = openrouter_video_call(env_key("openrouter"), model, system, prompt, schema,
                                                     video_url, effort, usage)
                    return parse_structured(text, schema)
                except Exception as exc:
                    error = exc
                    if permanent_error(exc) or getattr(exc, "status_code", None) in (400, 404):
                        break
        raise error

    def web_json(self, label: str, system: str, prompt: str, schema: dict) -> dict:
        """Ask Claude, with web search, for a JSON object matching `schema`."""
        providers, errors = self.providers, []
        for provider, model in self.web_models.items():
            if not providers.keys.get(provider) or provider in providers.disabled:
                continue
            providers.progress(f"Running {label} with {provider} web search ({model})...")
            try:
                if provider == "anthropic":
                    text = anthropic_web_call(system, prompt, model, self.web_searches,
                                              lambda m: self.meter.anthropic(label, m))
                elif provider == "openrouter":
                    text = openrouter_web_call(providers.keys[provider], system, prompt, model, self.web_engine,
                                               self.web_results, lambda data: self.meter.http(label, model, data))
                else:
                    continue
                return parse_loose(text, schema)
            except Exception as exc:
                providers._failed(label, provider, exc, errors, subject="task")
        raise ProviderError("Web search is unavailable. " + ("; ".join(errors) or
                            "It needs ANTHROPIC_API_KEY or OPENROUTER_API_KEY in .env."))
