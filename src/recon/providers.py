"""Minimal LLM provider abstraction (brief §6).

Only MockProvider is implemented in M3 so that all tests run without
credentials. OpenRouterProvider / OllamaProvider are added with the
baseline (M4). No routing, fallback, or streaming abstractions.
"""

import hashlib
import json


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
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
        self.last_usage = data.get("usage", {})
        return data["choices"][0]["message"]["content"]


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
