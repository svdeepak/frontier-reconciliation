"""OpenRouterProvider error-handling tests (mocked urllib, no network).

Prompted by a real live-provider failure: the first baseline attempt hit an
HTTP 429 and the retry raised a bare `KeyError: 'choices'`, which said nothing
about what the API had actually returned. These tests pin the diagnostics.

No test here makes a network call: urllib.request.urlopen is monkeypatched.
"""

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.providers import OpenRouterProvider, ProviderError  # noqa: E402

MODEL = "minimax/minimax-m2.7:free"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


@pytest.fixture()
def provider():
    return OpenRouterProvider("test-key", MODEL)


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload, status=200):
        raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        super().__init__(raw.encode() if isinstance(raw, str) else raw)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def patch_urlopen(monkeypatch, result, capture=None):
    """Route urlopen to `result` (a response, or an exception to raise)."""
    def fake_urlopen(req, timeout=None):
        if capture is not None:
            capture["req"] = req
            capture["timeout"] = timeout
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def http_error(status, payload=None, reason="Too Many Requests"):
    body = io.BytesIO(json.dumps(payload).encode() if payload is not None else b"")
    return urllib.error.HTTPError(ENDPOINT, status, reason, {}, body)


VALID = {
    "id": "gen-1",
    "choices": [{"finish_reason": "stop",
                 "message": {"role": "assistant", "content": '{"breaks": []}'}}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540},
}


# ------------------------------------------------------- preserved behaviour


def test_valid_response_returns_content_and_records_usage(provider, monkeypatch):
    patch_urlopen(monkeypatch, FakeResponse(VALID))
    assert provider.complete("sys", "user") == '{"breaks": []}'
    assert provider.last_usage == VALID["usage"]


def test_request_shape_is_unchanged(provider, monkeypatch):
    """Endpoint, model, temperature=0 and the two messages must not drift."""
    cap = {}
    patch_urlopen(monkeypatch, FakeResponse(VALID), capture=cap)
    provider.complete("SYS", "USER")
    req = cap["req"]
    assert req.full_url == ENDPOINT
    assert req.method == "POST"
    assert req.get_header("Authorization") == "Bearer test-key"
    body = json.loads(req.data.decode())
    assert body["model"] == MODEL
    assert body["temperature"] == 0
    assert body["messages"] == [{"role": "system", "content": "SYS"},
                                {"role": "user", "content": "USER"}]
    assert "response_format" not in body and "stream" not in body
    assert cap["timeout"] == 180


def test_missing_usage_leaves_empty_dict(provider, monkeypatch):
    payload = {"choices": [{"message": {"content": "hi"}}]}
    patch_urlopen(monkeypatch, FakeResponse(payload))
    assert provider.complete("s", "u") == "hi"
    assert provider.last_usage == {}


def test_content_parts_list_is_concatenated(provider, monkeypatch):
    payload = {"choices": [{"message": {"content": [
        {"type": "text", "text": '{"a":'}, {"type": "text", "text": " 1}"}]}}]}
    patch_urlopen(monkeypatch, FakeResponse(payload))
    assert provider.complete("s", "u") == '{"a": 1}'


# ---------------------------------------------------------------- HTTP errors


def test_http_429_raises_provider_error_with_status_and_message(provider, monkeypatch):
    """The failure that started this: rate limiting must be named as such."""
    patch_urlopen(monkeypatch, http_error(429, {
        "error": {"code": 429,
                  "message": "Rate limit exceeded: free-models-per-day"}}))
    with pytest.raises(ProviderError) as ei:
        provider.complete("s", "u")
    assert ei.value.status == 429
    msg = str(ei.value)
    assert "429" in msg
    assert "Rate limit exceeded: free-models-per-day" in msg
    assert "openrouter" in msg


def test_http_error_body_is_read_only_once(provider, monkeypatch):
    """The body is a stream; reading it twice would silently drop the code."""
    patch_urlopen(monkeypatch, http_error(429, {
        "error": {"code": "rate_limit_exceeded", "message": "slow down"}}))
    with pytest.raises(ProviderError) as ei:
        provider.complete("s", "u")
    assert ei.value.code == "rate_limit_exceeded"
    assert "code=rate_limit_exceeded" in str(ei.value)


def test_http_401_surfaces_auth_failure(provider, monkeypatch):
    patch_urlopen(monkeypatch, http_error(401, {
        "error": {"code": 401, "message": "No auth credentials found"}},
        reason="Unauthorized"))
    with pytest.raises(ProviderError) as ei:
        provider.complete("s", "u")
    assert ei.value.status == 401
    assert "No auth credentials found" in str(ei.value)


def test_http_error_with_unparseable_body_falls_back_to_reason(provider, monkeypatch):
    err = urllib.error.HTTPError(ENDPOINT, 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))
    patch_urlopen(monkeypatch, err)
    with pytest.raises(ProviderError) as ei:
        provider.complete("s", "u")
    assert ei.value.status == 502
    assert "Bad Gateway" in str(ei.value)


def test_network_failure_is_reported_not_swallowed(provider, monkeypatch):
    patch_urlopen(monkeypatch, urllib.error.URLError("connection refused"))
    with pytest.raises(ProviderError, match="network failure"):
        provider.complete("s", "u")


# ------------------------------------------------- error object in a 200 body


def test_error_object_in_successful_response_raises(provider, monkeypatch):
    """HTTP 200 with an error payload must not be treated as a reply."""
    patch_urlopen(monkeypatch, FakeResponse({
        "error": {"code": 429, "message": "Provider returned error",
                  "metadata": {"provider_name": "Minimax",
                               "raw": "quota exhausted"}}}))
    with pytest.raises(ProviderError) as ei:
        provider.complete("s", "u")
    msg = str(ei.value)
    assert "Provider returned error" in msg
    assert "quota exhausted" in msg
    assert ei.value.code == "429"


def test_error_object_does_not_record_usage(provider, monkeypatch):
    patch_urlopen(monkeypatch, FakeResponse({
        "error": {"message": "boom"}, "usage": {"prompt_tokens": 9}}))
    with pytest.raises(ProviderError):
        provider.complete("s", "u")
    assert provider.last_usage == {}


def test_string_error_field_is_handled(provider, monkeypatch):
    patch_urlopen(monkeypatch, FakeResponse({"error": "upstream exploded"}))
    with pytest.raises(ProviderError, match="upstream exploded"):
        provider.complete("s", "u")


# ---------------------------------------------------------- missing choices


def test_missing_choices_names_the_keys_present(provider, monkeypatch):
    """The original KeyError: 'choices' — now says what actually came back."""
    patch_urlopen(monkeypatch, FakeResponse({"id": "gen-2", "object": "chat.completion"}))
    with pytest.raises(ProviderError) as ei:
        provider.complete("s", "u")
    msg = str(ei.value)
    assert "no 'choices'" in msg
    assert "'id'" in msg and "'object'" in msg


def test_missing_choices_is_not_a_key_error(provider, monkeypatch):
    patch_urlopen(monkeypatch, FakeResponse({"id": "x"}))
    with pytest.raises(ProviderError):
        provider.complete("s", "u")
    # A bare KeyError would not be a ProviderError; assert the type is right.
    assert not issubclass(ProviderError, KeyError)


# ------------------------------------------------ malformed / empty choices


@pytest.mark.parametrize("payload,expected", [
    ({"choices": []}, "empty or not a list"),
    ({"choices": {}}, "empty or not a list"),
    ({"choices": ["oops"]}, "not an object"),
    ({"choices": [{"finish_reason": "error"}]}, "no usable 'message'"),
    ({"choices": [{"message": {"content": ""}}]}, "empty or not a string"),
    ({"choices": [{"message": {"content": None}}]}, "empty or not a string"),
    ({"choices": [{"message": {}}]}, "empty or not a string"),
])
def test_malformed_choices_raise_actionable_errors(provider, monkeypatch, payload, expected):
    patch_urlopen(monkeypatch, FakeResponse(payload))
    with pytest.raises(ProviderError, match=expected):
        provider.complete("s", "u")


def test_empty_content_reports_finish_reason(provider, monkeypatch):
    """finish_reason is the clue when a model returns nothing (e.g. length)."""
    patch_urlopen(monkeypatch, FakeResponse({
        "choices": [{"finish_reason": "length", "message": {"content": ""}}]}))
    with pytest.raises(ProviderError) as ei:
        provider.complete("s", "u")
    assert "length" in str(ei.value)


def test_bare_text_field_is_accepted_as_content(provider, monkeypatch):
    patch_urlopen(monkeypatch, FakeResponse({"choices": [{"text": "plain reply"}]}))
    assert provider.complete("s", "u") == "plain reply"


# ----------------------------------------------------- non-JSON / non-object


def test_non_json_response_shows_a_snippet(provider, monkeypatch):
    patch_urlopen(monkeypatch, FakeResponse("<html>502 Bad Gateway</html>"))
    with pytest.raises(ProviderError) as ei:
        provider.complete("s", "u")
    assert "not JSON" in str(ei.value)
    assert "502 Bad Gateway" in str(ei.value)


def test_json_array_response_is_rejected(provider, monkeypatch):
    patch_urlopen(monkeypatch, FakeResponse("[1, 2, 3]"))
    with pytest.raises(ProviderError, match="expected a JSON object"):
        provider.complete("s", "u")


def test_provider_error_is_a_runtime_error(provider):
    assert issubclass(ProviderError, RuntimeError)
