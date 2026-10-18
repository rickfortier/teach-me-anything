"""Ordered summary providers. Keys stay in Python and never reach Electron."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass

import requests

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openrouter": "openai/gpt-4.1-mini",
    "openai": "gpt-4.1-mini",
    "gemini": "gemini-3.8-flash",
    "ollama": "auto",               # the best model installed locally (see ollama.py)
}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LOCAL = "local"                     # stands in for a key: Ollama needs none


def env_key(provider):
    """The provider's key from the environment; blank or the .env.example placeholder counts as none.
    Ollama's "key" is LOCAL when its server is running."""
    if provider == "ollama":
        import ollama
        return LOCAL if ollama.installed() else ""
    key = os.getenv(f"{provider.upper()}_API_KEY", "").strip()
    return "" if not key or "..." in key or key.lower() in ("none", "your-key-here") else key


def env_model(provider):
    """A model chosen in .env (ANTHROPIC_MODEL=..., etc.), or ''."""
    return os.getenv(f"{provider.upper()}_MODEL", "").strip()


logger = logging.getLogger("summarize")


class ProviderError(Exception):
    def __init__(self, message, status_code=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass
class Summary:
    text: str
    provider: str
    model: str


@dataclass
class Structured:
    data: dict
    provider: str
    model: str


def permanent_error(exc):
    message = str(exc).lower()
    return (getattr(exc, "status_code", None) in (401, 402, 403)
            or getattr(exc, "code", None) in (401, 402, 403, "insufficient_quota")
            or any(term in message for term in ("credit balance is too low", "insufficient credits", "insufficient_quota",
                                            "no credits remaining", "api key not valid")))


def _request(provider, model, system, prompt, max_output_tokens):
    """(url, payload) for a plain text completion on the given provider."""
    if provider == "ollama":
        import ollama
        return f"{ollama.url()}/api/chat", {
            "model": ollama.pick_model(model), "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            # Room for a long lesson; Ollama's default context is much smaller.
            "options": {"num_ctx": 16_384, "num_predict": max_output_tokens}}
    if provider == "gemini":
        return GEMINI_URL.format(model=model), {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output_tokens}}
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1/chat/completions", {
            "model": model, "max_tokens": max_output_tokens, "messages": [
                {"role": "system", "content": system}, {"role": "user", "content": prompt},
            ]}
    return "https://api.openai.com/v1/responses", {
        "model": model, "instructions": system, "input": prompt,
        "max_output_tokens": max_output_tokens, "store": False}


def _post(url, key, payload):
    # Gemini takes its key in a header of its own; Ollama needs none; the others use a bearer token.
    if url.startswith("https://generativelanguage.googleapis.com/"):
        headers = {"x-goog-api-key": key}
    else:
        headers = {} if key == LOCAL else {"Authorization": f"Bearer {key}"}
    # A local model on a CPU can take many minutes for one long lesson.
    timeout = (10, 1800) if key == LOCAL else (10, 300)
    with requests.post(url, headers=headers, json=payload, timeout=timeout) as response:
        try:
            data = response.json()
        except ValueError:
            raise ProviderError(f"HTTP {response.status_code}: unreadable provider response", response.status_code)
        error = data.get("error")
        if response.status_code >= 400 or error:
            error = error if isinstance(error, dict) else {"message": error} if isinstance(error, str) else {}
            raise ProviderError(error.get("message") or f"HTTP {response.status_code}",
                                response.status_code, error.get("code"))
    return data


def _text(provider, data, what="summary"):
    if provider == "ollama":
        if data.get("done_reason") not in (None, "stop"):
            raise ProviderError(f"Provider did not return a complete {what} ({data.get('done_reason')})")
        return (data.get("message") or {}).get("content") or ""
    if provider == "gemini":
        candidate = (data.get("candidates") or [{}])[0]
        if candidate.get("finishReason") != "STOP":
            raise ProviderError(f"Provider did not return a complete {what} ({candidate.get('finishReason')})")
        return "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", [])
                       if not part.get("thought"))
    if provider == "openrouter":
        choice = data.get("choices", [{}])[0]
        if choice.get("finish_reason") in ("length", "content_filter"):
            raise ProviderError(f"Provider did not return a complete {what}")
        return choice.get("message", {}).get("content") or ""
    if data.get("status") != "completed":
        raise ProviderError(f"Provider did not return a complete {what}")
    return " ".join(part.get("text", "") for item in data.get("output", [])
                    if item.get("type") == "message" for part in item.get("content", [])
                    if part.get("type") == "output_text")


def http_summary(provider, model, key, system, prompt, max_output_tokens=600):
    url, payload = _request(provider, model, system, prompt, max_output_tokens)
    return _text(provider, _post(url, key, payload))


def structured_http(provider, model, key, system, prompt, schema, max_output_tokens=2000, on_usage=None,
                    effort=None):
    """Ask OpenRouter, OpenAI, Gemini or a local Ollama model for JSON matching `schema`; returns the raw text.
    `on_usage(data)` receives the raw response, for token and cost accounting.
    `effort` is the reasoning level (OpenAI/OpenRouter: none..high; Gemini 3: minimal..high)."""
    url, payload = _request(provider, model, system, prompt, max_output_tokens)
    if provider == "ollama":
        payload["format"] = schema               # Ollama constrains the output to the schema
        if effort:
            # gpt-oss takes a level; other thinking models (qwen3, ...) just on/off.
            payload["think"] = False if effort in ("none", "off") else (
                effort if payload["model"].startswith("gpt-oss") else True)
    elif provider == "gemini":
        payload["generationConfig"] |= {"responseMimeType": "application/json", "responseJsonSchema": schema}
        if effort:
            payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": effort.upper()}
    elif provider == "openrouter":
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "result", "strict": True, "schema": schema}}
        if effort:
            payload["reasoning"] = {"effort": effort}
    else:
        payload["text"] = {"format": {"type": "json_schema", "name": "result",
                                      "strict": True, "schema": schema}}
        if effort:
            payload["reasoning"] = {"effort": effort}
    data = _post(url, key, payload)
    if on_usage:
        on_usage(data)
    return _text(provider, data, "response")


def parse_structured(text, schema):
    """Parse model output as a JSON object and check the schema's required keys."""
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("The provider returned no text")
    text = re.sub(r"^```[\w-]*\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProviderError(f"The provider returned invalid JSON: {exc}")
    if not isinstance(data, dict):
        raise ProviderError("The provider returned JSON that is not an object")
    missing = [key for key in schema.get("required", []) if key not in data]
    if missing:
        raise ProviderError("The provider response is missing required keys: " + ", ".join(missing))
    return data


def _print_progress(message):
    print(message, flush=True)


def _default_safe_error(exc):
    return f"{type(exc).__name__}: {exc}"


class SummaryProviders:
    def __init__(self, system, anthropic_call=None, safe_error=None, max_output_tokens=600,
                 max_input_chars=60_000, progress=None, order=None, models=None):
        """`order` (comma string) and `models` ({provider: model}) override the
        SUMMARY_PROVIDERS / *_SUMMARY_MODEL environment defaults."""
        order = order or os.getenv("SUMMARY_PROVIDERS", "openrouter,anthropic,openai")
        order = [p.strip().lower() for p in order.split(",") if p.strip()]
        if not order or any(p not in DEFAULT_MODELS for p in order):
            raise ValueError("Provider order must contain anthropic, openrouter, openai, gemini or ollama "
                             "in the desired order")
        self.providers = list(dict.fromkeys(order))
        self.keys = {p: env_key(p) for p in self.providers}
        models = models or {}
        # A model named in .env (ANTHROPIC_MODEL=...) wins over config.yaml.
        self.models = {p: env_model(p) or models.get(p) or os.getenv(f"{p.upper()}_SUMMARY_MODEL", "").strip()
                       or DEFAULT_MODELS[p] for p in self.providers}
        self.system = system
        self.anthropic_call = anthropic_call
        self.safe_error = safe_error or _default_safe_error
        self.progress = progress or _print_progress
        self.max_output_tokens = max_output_tokens
        self.max_input_chars = max_input_chars
        self.disabled = {}
        self.lock = threading.Lock()

    def unavailable_reason(self):
        with self.lock:
            available = any(self.keys[p] and p not in self.disabled for p in self.providers)
            failures = [f"{p}: {self.disabled[p]}" for p in self.providers if p in self.disabled]
        if available:
            return None
        if not failures:
            return ("No AI model is available. Add a free GEMINI_API_KEY (https://aistudio.google.com/apikey) "
                    "or another key to .env, or install Ollama (https://ollama.com) and run "
                    "\"ollama pull qwen3:8b\" to use a free local model. See README.md.")
        return ("No summary provider is available. " + "; ".join(failures)
                + " Add credits or configure ANTHROPIC_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY in .env, then Retry.")

    def _attempts(self, prefer=None):
        """Yield (provider, model) for every provider that has a key and is not disabled;
        `prefer` = (provider, model) goes first, even if it isn't in the fallback order."""
        order = self.providers
        if prefer:
            if prefer[0] not in DEFAULT_MODELS:
                raise ProviderError(f"Unknown provider {prefer[0]!r}")
            with self.lock:
                if prefer[0] not in self.keys:
                    self.keys[prefer[0]] = env_key(prefer[0])
            order = [prefer[0]] + [p for p in self.providers if p != prefer[0]]
        for provider in order:
            with self.lock:
                disabled = self.disabled.get(provider)
            if not self.keys[provider] or disabled:
                continue
            yield provider, prefer[1] if prefer and provider == prefer[0] else self.models[provider]

    def _failed(self, label, provider, exc, errors, subject="video"):
        detail = self.safe_error(exc)
        logger.warning("%s=%s provider=%s error=%s", subject, label, provider, detail)
        errors.append(f"{provider}: {detail}")
        if permanent_error(exc):
            with self.lock:
                self.disabled[provider] = detail
        self.progress(f"{provider} failed for {label}; checking configured fallbacks...")

    def summarize(self, video_id, title, transcript):
        if len(transcript) > self.max_input_chars:
            transcript = transcript[:self.max_input_chars] + "\n\n[...transcript truncated...]"
        prompt = f"Video title: {title}\n\nTranscript:\n{transcript}"
        errors = []
        for provider, model in self._attempts():
            logger.info("video=%s stage=summary provider=%s model=%s", video_id, provider, model)
            self.progress(f"Summarizing {video_id} with {provider} ({model})...")
            try:
                if provider == "anthropic":
                    if self.anthropic_call is None:
                        raise ProviderError("No Anthropic summary call is configured")
                    text = self.anthropic_call(title, transcript, model)
                else:
                    text = http_summary(provider, model, self.keys[provider], self.system, prompt, self.max_output_tokens)
                if not isinstance(text, str) or not text.strip():
                    raise ProviderError("The summary service returned no text")
                logger.info("video=%s stage=summary success provider=%s model=%s", video_id, provider, model)
                return Summary(text.strip(), provider, model)
            except Exception as exc:
                self._failed(video_id, provider, exc, errors)
        raise ProviderError(self.unavailable_reason() or "All configured summary providers failed. " + "; ".join(errors))

    def structured(self, label, system, prompt, schema, anthropic_call, max_output_tokens=2000,
                   models=None, on_usage=None, prefer=None, effort=None, fallback=True, retries=1):
        """Ask the first working provider for a JSON object matching `schema`.

        `anthropic_call(system, prompt, schema, model)` returns the raw text for
        Anthropic; other providers go through `structured_http`. Unparseable
        output or a missing required key counts as a provider failure.
        `models` overrides the model per provider for this call; `on_usage(model,
        data)` gets each HTTP provider response.

        `prefer` = (provider, model) is the task's own model: tried first, and
        `retries` more times after a non-permanent failure (bad JSON, timeout),
        before the fallbacks. `effort` applies to that model only (Anthropic's
        effort is set inside `anthropic_call`). With `fallback=False` nothing
        else is tried.
        """
        errors = []
        if prefer and not env_key(prefer[0]):
            errors.append(f"{prefer[0]}: no {prefer[0].upper()}_API_KEY in .env" if prefer[0] != "ollama"
                          else "ollama: not running")
        for provider, model in self._attempts(prefer):
            preferred = bool(prefer) and provider == prefer[0]
            if not preferred:
                if prefer and not fallback:
                    break
                model = (models or {}).get(provider) or model
            for attempt in range(1 + (retries if preferred else 0)):
                logger.info("task=%s stage=structured provider=%s model=%s attempt=%s", label, provider, model,
                            attempt + 1)
                self.progress(f"Running {label} with {provider} ({model})...")
                try:
                    if provider == "anthropic":
                        text = anthropic_call(system, prompt, schema, model)
                    else:
                        usage = (lambda data, m=model: on_usage(m, data)) if on_usage else None
                        text = structured_http(provider, model, self.keys[provider], system, prompt, schema,
                                               max_output_tokens, on_usage=usage,
                                               effort=effort if preferred else None)
                    data = parse_structured(text, schema)
                    logger.info("task=%s stage=structured success provider=%s model=%s", label, provider, model)
                    return Structured(data, provider, model)
                except Exception as exc:
                    self._failed(label, provider, exc, errors, subject="task")
                    if permanent_error(exc):
                        break
        raise ProviderError(self.unavailable_reason() or f"All configured providers failed for {label}. " + "; ".join(errors))
