"""
Exercises the provider-agnostic LLM layer (agent/llm/) with no network and no
API key: the OpenAI-compatible adapter runs against an in-process
httpx.MockTransport, and the router runs against fake providers.

Covers: neutral-history -> wire-format translation (tool calls, tool results,
screenshot placement), response parsing, error classification, retry with
backoff, sticky failover, and the all-providers-failed case.

Run: python3 tests/test_llm_providers.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from agent.llm import (  # noqa: E402
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMResponse,
    RetryableLLMError,
    TextBlock,
    ToolUseBlock,
)
from agent.llm.openai_compat import OpenAICompatProvider, to_openai_messages  # noqa: E402

HISTORY = [
    {"role": "user", "content": "URL: /members/search"},
    {"role": "assistant", "content": [
        TextBlock("Filling the member ID."),
        ToolUseBlock("call_1", "fill", {"index": 1, "value": "10001"}),
    ]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "Filled."}]},
]


def _provider(handler) -> OpenAICompatProvider:
    return OpenAICompatProvider("openrouter", "https://example.invalid/api/v1", "test-key", "some/model",
                                client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_history_translates_to_openai_messages():
    msgs = to_openai_messages("SYSTEM", HISTORY, image_bytes=b"\x89PNG", image_mime_type="image/png")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "user"], msgs
    call = msgs[2]["tool_calls"][0]
    assert call["id"] == "call_1" and call["function"]["name"] == "fill"
    assert json.loads(call["function"]["arguments"]) == {"index": 1, "value": "10001"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "Filled."}
    # A tool message can't carry an image, so the screenshot follows it in a user message.
    assert msgs[4]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    first_turn = to_openai_messages("SYSTEM", HISTORY[:1], image_bytes=b"\x89PNG")
    assert len(first_turn) == 2 and first_turn[1]["content"][0]["text"] == "URL: /members/search"
    print("PASS: neutral history -> OpenAI messages (tool call, tool result, screenshot placement)")


def test_openai_compat_round_trip():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "Submitting the search.",
            "tool_calls": [{"id": "abc", "type": "function",
                            "function": {"name": "click", "arguments": "{\"index\": 2}"}}],
        }}]})

    resp = _provider(handler).decide("SYSTEM", HISTORY)
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "some/model"
    assert {t["function"]["name"] for t in seen["body"]["tools"]} >= {"click", "fill", "finish_success"}
    assert (resp.provider, resp.model) == ("openrouter", "some/model")
    assert resp.content[0].text == "Submitting the search."
    assert (resp.content[1].name, resp.content[1].input, resp.content[1].id) == ("click", {"index": 2}, "abc")
    print("PASS: OpenAI-compatible request/response round trip")


def test_openai_compat_error_classification():
    def expect(handler, exc_type):
        try:
            _provider(handler).decide("SYSTEM", HISTORY)
        except exc_type as e:
            return e
        raise AssertionError(f"expected {exc_type.__name__}")

    e = expect(lambda r: httpx.Response(429, text="slow down", headers={"retry-after": "7"}), RetryableLLMError)
    assert e.retry_after_s == 7.0
    expect(lambda r: httpx.Response(503, text="overloaded"), RetryableLLMError)
    # OpenRouter reports some upstream failures as HTTP 200 + an error body.
    expect(lambda r: httpx.Response(200, json={"error": {"code": 502, "message": "upstream"}}), RetryableLLMError)

    def boom(request):
        raise httpx.ConnectTimeout("timed out")
    expect(boom, RetryableLLMError)

    e = expect(lambda r: httpx.Response(401, text="bad key"), LLMError)
    assert not isinstance(e, RetryableLLMError), "an auth failure must not be retried"
    print("PASS: error classification (429/503/timeout retryable, 401 not)")


def test_malformed_tool_arguments_become_text():
    resp = _provider(lambda r: httpx.Response(200, json={"choices": [{"message": {
        "content": None, "tool_calls": [{"id": "x", "function": {"name": "click", "arguments": "{not json"}}],
    }}]})).decide("SYSTEM", HISTORY)
    assert [b.type for b in resp.content] == ["text"], resp.content
    print("PASS: malformed tool arguments surface as text (loop re-prompts)")


class FakeProvider:
    """Raises the queued exceptions in order, then answers."""

    def __init__(self, name, failures=()):
        self.name, self.model = name, f"{name}-model"
        self.failures = list(failures)
        self.calls = 0

    def decide(self, system_prompt, messages, image_bytes=None, image_mime_type="image/png"):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return LLMResponse([ToolUseBlock("id1", "click", {"index": 1}, provider=self.name),
                            ToolUseBlock("id2", "click", {"index": 2}, provider=self.name)],
                           provider=self.name, model=self.model)


def _client(providers, **kw):
    events, sleeps = [], []
    client = LLMClient(providers, on_event=events.append, sleep=sleeps.append, **kw)
    return client, events, sleeps


def test_retry_then_success():
    flaky = FakeProvider("gemini", [RetryableLLMError("503"), RetryableLLMError("503")])
    client, events, sleeps = _client([flaky, FakeProvider("openrouter")])
    resp = client.decide("S", HISTORY)
    assert resp.provider == "gemini" and flaky.calls == 3
    assert sleeps == [1.0, 2.0], sleeps
    assert [e["event"] for e in events] == ["llm_retry", "llm_retry"]
    assert len([b for b in resp.content if b.type == "tool_use"]) == 1, "parallel tool calls must be trimmed to one"
    print("PASS: transient failure retried with backoff on the same provider")


def test_failover_is_sticky():
    down = FakeProvider("gemini", [RetryableLLMError("503")] * 10)
    backup = FakeProvider("openrouter")
    client, events, sleeps = _client([down, backup], max_retries=2)
    assert client.decide("S", HISTORY).provider == "openrouter"
    assert down.calls == 3, "1 attempt + 2 retries before failing over"
    assert events[-1]["event"] == "llm_failover" and events[-1]["to_provider"] == "openrouter"
    assert (client.provider, client.model) == ("openrouter", "openrouter-model")
    client.decide("S", HISTORY)
    assert down.calls == 3, "an abandoned provider is not tried again this run"
    print("PASS: failover after retries are exhausted, and it sticks")


def test_non_retryable_fails_over_immediately():
    bad_key = FakeProvider("gemini", [LLMError("401 bad key")])
    client, events, sleeps = _client([bad_key, FakeProvider("openrouter")])
    assert client.decide("S", HISTORY).provider == "openrouter"
    assert bad_key.calls == 1 and sleeps == []
    print("PASS: non-retryable failure skips straight to the next provider")


def test_all_providers_failed():
    client, events, sleeps = _client([FakeProvider("gemini", [LLMError("401")]),
                                      FakeProvider("openrouter", [LLMError("404 no such model")])])
    try:
        client.decide("S", HISTORY)
    except LLMError as e:
        assert "401" in str(e) and "404" in str(e), e
    else:
        raise AssertionError("expected LLMError")
    print("PASS: every provider failing raises one error naming each cause")


def test_from_env(monkey_env):
    monkey_env({"LLM_PROVIDERS": "openrouter", "OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "vendor/model"})
    client = LLMClient.from_env()
    assert client.describe() == "openrouter:vendor/model", client.describe()

    for env, needle in (({"LLM_PROVIDERS": "openrouter"}, "OPENROUTER_API_KEY"),
                        ({"LLM_PROVIDERS": "nope"}, "Unknown LLM provider"),
                        ({}, "No LLM provider is configured")):
        monkey_env(env)
        try:
            LLMClient.from_env()
        except LLMConfigError as e:
            assert needle in str(e), e
        else:
            raise AssertionError(f"expected LLMConfigError for {env}")
    print("PASS: provider selection from env, and clear errors when misconfigured")


def test_gemini_history_after_failover():
    """A tool call made by another provider has no thought_signature Gemini
    would accept, so it must be replayed to Gemini as text, while Gemini's
    own calls stay real function_call parts with their signature."""
    from agent.llm.gemini import GeminiProvider
    gemini = GeminiProvider(api_key="fake-key-no-call-is-made")
    history = [
        {"role": "user", "content": "URL: /members/search"},
        {"role": "assistant", "content": [ToolUseBlock("g1", "fill", {"index": 1}, provider="gemini",
                                                       provider_data=b"sig")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "g1", "content": "Filled."}]},
        {"role": "assistant", "content": [ToolUseBlock("o1", "click", {"index": 2}, provider="openrouter")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "o1", "content": "Clicked."}]},
    ]
    contents = gemini._to_contents(history, image_bytes=b"\x89PNG", image_mime_type="image/png")
    native_call, native_result = contents[1].parts[0], contents[2].parts[0]
    assert native_call.function_call.name == "fill" and native_call.thought_signature == b"sig"
    assert native_result.function_response.name == "fill"
    foreign_call, foreign_result = contents[3].parts[0], contents[4].parts[0]
    assert foreign_call.function_call is None and "click" in foreign_call.text
    assert foreign_result.function_response is None and "Clicked." in foreign_result.text
    assert contents[4].parts[-1].inline_data is not None, "screenshot goes on the latest user turn"
    print("PASS: Gemini replays its own tool calls natively and another provider's as text")


def _monkey_env(values):
    import os
    for key in list(os.environ):
        if key.startswith(("LLM_", "GEMINI_", "GOOGLE_API", "OPENROUTER_", "OPENAI_", "GROQ_")):
            del os.environ[key]
    os.environ.update(values)


if __name__ == "__main__":
    test_history_translates_to_openai_messages()
    test_openai_compat_round_trip()
    test_openai_compat_error_classification()
    test_malformed_tool_arguments_become_text()
    test_retry_then_success()
    test_failover_is_sticky()
    test_non_retryable_fails_over_immediately()
    test_all_providers_failed()
    test_gemini_history_after_failover()
    test_from_env(_monkey_env)
    print("\nALL LLM PROVIDER TESTS PASSED")
