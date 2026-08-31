"""Minimal LLM provider abstraction (brief §6).

Only MockProvider is implemented in M3 so that all tests run without
credentials. OpenRouterProvider / OllamaProvider are added with the
baseline (M4). No routing, fallback, or streaming abstractions.

Provider transport errors are surfaced as ProviderError with the HTTP status
and the provider's own error message/code where available. A run that fails
must say why it failed; it must never be silently recorded as an empty result,
and no provider here retries or falls back to another model.
"""

import hashlib
import json


class ProviderError(RuntimeError):
    """A provider call failed or returned an unusable response.

    Carries the HTTP status and the provider's error code/message when the
    API supplied them, so a failed baseline run is diagnosable from the
    exception text alone.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None, provider: str | None = None):
        self.status = status
        self.code = code
        self.provider = provider
        parts = []
        if provider:
            parts.append(provider)
        if status is not None:
            parts.append(f"HTTP {status}")
        if code:
            parts.append(f"code={code}")
        prefix = " ".join(parts)
        super().__init__(f"{prefix}: {message}" if prefix else message)


class LLMProvider:
    name = "base"

    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError


class MockProvider(LLMProvider):
    """Deterministic canned-response provider for tests.

    Responses are registered by key; unmatched prompts return a stable,
    schema-shaped empty result so pipelines never need credentials to run.
    """

    name = "mock"

    def __init__(self, responses: dict[str, str] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        for key, resp in sorted(self.responses.items()):
            if key in user:
                return resp
        digest = hashlib.sha256(user.encode()).hexdigest()[:8]
        return json.dumps({"breaks": [], "matches": [], "mock_digest": digest})


class OpenRouterProvider(LLMProvider):
    """Minimal OpenRouter chat-completions client (stdlib urllib, no streaming)."""

    name = "openrouter"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self.last_usage: dict = {}

    def complete(self, system: str, user: str) -> str:
        """Return the reply text, or raise ProviderError explaining the failure.

        Request shape (endpoint, model, temperature, messages) and the return
        value for valid responses are unchanged; only the failure paths that
        previously surfaced as a bare KeyError are now diagnosable.
        """
        import urllib.error
        import urllib.request
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                status = getattr(resp, "status", None)
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            # HTTPError is itself a response: its body usually carries the
            # provider's explanation (rate-limit detail, invalid model, ...).
            # The body is a stream — read it once and reuse it.
            payload = _safe_json(_safe_read(e))
            detail = _error_detail(payload)
            raise ProviderError(detail or (e.reason or "HTTP error"),
                                status=e.code, code=detail_code(payload),
                                provider=self.name) from e
        except urllib.error.URLError as e:
            raise ProviderError(f"network failure: {e.reason}", provider=self.name) from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProviderError(
                f"response was not JSON ({e}); first 200 chars: {raw[:200]!r}",
                status=status, provider=self.name) from e
        if not isinstance(data, dict):
            raise ProviderError(f"expected a JSON object, got {type(data).__name__}",
                                status=status, provider=self.name)

        # A 200 response can still carry an error object instead of choices.
        if data.get("error"):
            raise ProviderError(_error_detail(data) or "provider returned an error",
                                status=status or _error_status(data),
                                code=detail_code(data), provider=self.name)

        content = _extract_content(data)
        # Usage is recorded only for a response we accepted, exactly as before.
        self.last_usage = data.get("usage", {}) or {}
        return content


class OllamaProvider(LLMProvider):
    """Minimal Ollama /api/chat client (stdlib urllib, non-streaming)."""

    name = "ollama"

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.last_usage: dict = {}

    def complete(self, system: str, user: str) -> str:
        import urllib.request
        body = json.dumps({
            "model": self.model, "stream": False,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(f"{self.base_url}/api/chat", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode())
        self.last_usage = {"prompt_tokens": data.get("prompt_eval_count"),
                           "completion_tokens": data.get("eval_count")}
        return data["message"]["content"]


def _safe_read(e) -> str:
    """Body of an HTTPError, if it can still be read."""
    try:
        return e.read().decode(errors="replace")
    except Exception:
        return ""


def _safe_json(raw: str):
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def _err_obj(data) -> dict:
    if not isinstance(data, dict):
        return {}
    err = data.get("error")
    return err if isinstance(err, dict) else ({"message": err} if err else {})


def _error_detail(data) -> str:
    """Human-readable provider error text, with nested metadata when present."""
    err = _err_obj(data)
    if not err:
        return ""
    msg = str(err.get("message") or "").strip()
    meta = err.get("metadata")
    if isinstance(meta, dict):
        extra = meta.get("raw") or meta.get("provider_name")
        if extra:
            msg = f"{msg} ({extra})" if msg else str(extra)
    return msg


def detail_code(data) -> str | None:
    err = _err_obj(data)
    code = err.get("code") or err.get("type")
    return str(code) if code not in (None, "") else None


def _error_status(data) -> int | None:
    code = _err_obj(data).get("code")
    return code if isinstance(code, int) else None


def _extract_content(data: dict) -> str:
    """Pull the reply text out of a chat.completion, or explain what was wrong.

    Guards each hop that previously assumed success:
    choices present -> non-empty list -> dict entry -> message -> string content.
    """
    if "choices" not in data:
        raise ProviderError(
            "response contained no 'choices' "
            f"(keys: {sorted(data.keys())!r})", provider=OpenRouterProvider.name)
    choices = data["choices"]
    if not isinstance(choices, list) or not choices:
        raise ProviderError(f"'choices' was empty or not a list: {choices!r}",
                            provider=OpenRouterProvider.name)
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError(f"choices[0] was not an object: {first!r}",
                            provider=OpenRouterProvider.name)
    message = first.get("message")
    if not isinstance(message, dict):
        # Some gateways return a bare 'text' field instead of a message object.
        text = first.get("text")
        if isinstance(text, str) and text != "":
            return text
        raise ProviderError(
            f"choices[0] had no usable 'message' (finish_reason="
            f"{first.get('finish_reason')!r}, keys={sorted(first.keys())!r})",
            provider=OpenRouterProvider.name)
    content = message.get("content")
    if isinstance(content, list):
        # Content-parts form: concatenate the text parts.
        content = "".join(part.get("text", "") for part in content
                          if isinstance(part, dict))
    if not isinstance(content, str) or content == "":
        raise ProviderError(
            f"choices[0].message.content was empty or not a string "
            f"(finish_reason={first.get('finish_reason')!r}, got {content!r})",
            provider=OpenRouterProvider.name)
    return content


def provider_from_env() -> LLMProvider:
    """Select provider from environment (LLM_PROVIDER: openrouter|ollama|mock)."""
    import os
    kind = os.environ.get("LLM_PROVIDER", "mock").lower()
    if kind == "openrouter":
        key, model = os.environ.get("OPENROUTER_API_KEY"), os.environ.get("OPENROUTER_MODEL")
        if not key or not model:
            raise SystemExit("OPENROUTER_API_KEY and OPENROUTER_MODEL required for LLM_PROVIDER=openrouter")
        return OpenRouterProvider(key, model)
    if kind == "ollama":
        model = os.environ.get("OLLAMA_MODEL")
        if not model:
            raise SystemExit("OLLAMA_MODEL required for LLM_PROVIDER=ollama")
        return OllamaProvider(os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"), model)
    return MockProvider()
