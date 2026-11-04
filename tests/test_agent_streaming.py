"""Native streaming delivers wire deltas before completion and validates final output."""

from __future__ import annotations

import json

import httpx
import pytest

from sdlc.agent.adapters.models import (
    AnthropicModelClient, GoogleModelClient, ModelAdapterError, OpenAIModelClient,
    RoutingModelClient,
)
from sdlc.agent.domain import AssistantMessage, Context, Decision, ModelSettings, ToolResult, UserMessage


PROVIDERS = [
    ("openai", OpenAIModelClient, "gpt-5"),
    ("anthropic", AnthropicModelClient, "claude-sonnet-4-5"),
    ("google", GoogleModelClient, "gemini-2.5-flash"),
]


def settings(provider, model):
    return ModelSettings(provider=provider, model_id=model, effort="default", max_tokens=2048, timeout_seconds=10)


def context(output_type=AssistantMessage):
    return Context("Trusted policy", [], [UserMessage(content="Help test my PRD")], output_type)


def frame(data, *, event=None):
    prefix = f"event: {event}\n" if event else ""
    return (prefix + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode()


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, *, before_chunk=None):
        self.chunks, self.before_chunk, self.closed = chunks, before_chunk, False

    def __iter__(self):
        for index, chunk in enumerate(self.chunks):
            if self.before_chunk:
                self.before_chunk(index)
            yield chunk

    def close(self):
        self.closed = True


def text_events(provider, text="先分析需求", remainder="，再规划覆盖。"):
    if provider == "openai":
        return [
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "message", "id": "message", "role": "assistant", "content": []}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": text},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
             "delta": remainder},
            {"type": "response.completed", "response": {"id": "response", "status": "completed",
             "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}, "output": [
                 {"type": "message", "content": [{"type": "output_text", "text": text + remainder}]}]}},
        ], 2
    if provider == "anthropic":
        return [
            {"type": "message_start", "message": {"id": "response", "usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": remainder}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ], 3
    return [
        {"responseId": "response", "candidates": [{"index": 0, "content": {"parts": [{"text": text}]}}]},
        {"candidates": [{"index": 0, "content": {"parts": [{"text": remainder}]}}]},
        {"candidates": [{"index": 0, "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "totalTokenCount": 8}},
    ], 1


@pytest.mark.parametrize("provider,client_class,model", PROVIDERS)
def test_native_content_delta_arrives_before_next_response_chunk(provider, client_class, model):
    deltas, captured = [], {}
    events, next_index = text_events(provider)

    def before_chunk(index):
        if index == next_index:
            assert deltas == ["先分析需求"], "callback must run before response completion"

    stream = Chunks([frame(event) for event in events], before_chunk=before_chunk)

    def handler(request):
        captured.update({"url": str(request.url), "payload": json.loads(request.content),
                         "headers": dict(request.headers)})
        return httpx.Response(200, headers={"content-type": "text/event-stream", "x-request-id": "req1"},
                              stream=stream)

    client = client_class(api_key="secret", transport=httpx.MockTransport(handler))
    output = client.generate_stream(context(), settings(provider, model), deltas.append)
    assert deltas == ["先分析需求", "，再规划覆盖。"]
    assert output.content == "".join(deltas)
    assert output.metadata["model_event"]["streamed"] is True
    assert client.last_metadata["status"] == "completed"
    assert client.last_metadata["request_id"] == "req1"
    assert client.last_usage == {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
    assert stream.closed
    assert "secret" not in json.dumps(client.last_metadata)
    if provider == "google":
        assert captured["url"].endswith(":streamGenerateContent?alt=sse")
        assert "stream" not in captured["payload"]
    else:
        assert captured["payload"]["stream"] is True
    assert captured["headers"]["accept"] == "text/event-stream"


def test_openai_partial_tool_arguments_and_encrypted_continuity_are_assembled_privately():
    deltas, requests = [], []
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {
            "type": "reasoning", "id": "rs1", "encrypted_content": "opaque-state",
            "summary": [{"text": "PRIVATE-THOUGHT"}]}},
        {"type": "response.reasoning_text.delta", "delta": "PRIVATE-THOUGHT"},
        {"type": "response.output_item.added", "output_index": 1, "item": {
            "type": "function_call", "id": "fc1", "call_id": "call1", "name": "read_task",
            "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"task'},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '_id":"one"}'},
        {"type": "response.completed", "response": {"status": "completed", "id": "response1",
         "usage": {"input_tokens": 3, "output_tokens": 2}}},
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Chunks([frame(event) for event in events]))

    client = OpenAIModelClient(api_key="secret", transport=httpx.MockTransport(handler))
    ctx = context()
    output = client.generate_stream(ctx, settings("openai", "gpt-5"), deltas.append)
    assert deltas == []
    assert output.tool_calls[0].arguments == {"task_id": "one"}
    assert output.tool_calls[0].id == "call1"
    assert output.metadata["provider_state"]["reasoning"] == [
        {"id": "rs1", "encrypted_content": "opaque-state"}]
    assert "PRIVATE-THOUGHT" not in json.dumps(output.model_dump())
    assert "PRIVATE-THOUGHT" not in json.dumps(client.last_public_output)
    ctx.messages += [output, ToolResult(tool_call_id="call1", content="task")]
    client.generate_stream(ctx, settings("openai", "gpt-5"), deltas.append)
    assert requests[-1]["input"][1]["encrypted_content"] == "opaque-state"


def test_anthropic_partial_tool_json_and_thought_blocks_never_reach_callback():
    events = [
        {"type": "message_start", "message": {"id": "m1", "usage": {
            "input_tokens": 5, "cache_read_input_tokens": 4}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "PRIVATE-THOUGHT"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "PRIVATE-THOUGHT"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "PRIVATE-SIGNATURE"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "call1", "name": "read_task", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"task_id":'}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '"one"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ]
    deltas = []
    client = AnthropicModelClient(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunks([frame(e) for e in events]))))
    output = client.generate_stream(context(), settings("anthropic", "claude-sonnet-4-5"), deltas.append)
    assert output.tool_calls[0].arguments == {"task_id": "one"}
    assert deltas == []
    assert client.last_usage == {"input_tokens": 9, "output_tokens": 3, "total_tokens": 12}
    assert "PRIVATE" not in json.dumps(output.model_dump())
    assert "PRIVATE" not in json.dumps(client.last_public_output)


def test_google_stream_preserves_signatures_and_complete_calls_with_no_thought_text():
    requests, deltas = [], []
    public_parts = [{"text": "我先读取需求", "thoughtSignature": "opaque-text"},
                    {"functionCall": {"id": "call1", "name": "read_task", "args": {"task_id": "one"}},
                     "thoughtSignature": "opaque-call"}]
    events = [
        {"candidates": [{"content": {"parts": [{"text": "PRIVATE-THOUGHT", "thought": True}]}}]},
        *[{"candidates": [{"content": {"parts": [part]}}]} for part in public_parts],
        {"candidates": [{"finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 5,
            "candidatesTokenCount": 3, "thoughtsTokenCount": 2, "totalTokenCount": 10}},
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Chunks([frame(e) for e in events]))

    client = GoogleModelClient(api_key="test", transport=httpx.MockTransport(handler))
    ctx = context()
    output = client.generate_stream(ctx, settings("google", "gemini-2.5-flash"), deltas.append)
    assert deltas == ["我先读取需求"]
    assert output.tool_calls[0].arguments == {"task_id": "one"}
    assert output.metadata["provider_state"]["parts"] == public_parts
    assert "PRIVATE-THOUGHT" not in json.dumps(output.model_dump())
    assert "opaque" not in json.dumps(client.last_public_output)
    assert client.last_usage == {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
    ctx.messages += [output, ToolResult(tool_call_id="call1", content="task")]
    client.generate_stream(ctx, settings("google", "gemini-2.5-flash"), deltas.append)
    assert requests[-1]["contents"][-2]["parts"] == public_parts


@pytest.mark.parametrize("provider,client_class,model", PROVIDERS)
def test_stream_without_terminal_response_is_not_a_success(provider, client_class, model):
    events, _ = text_events(provider)
    events.pop()
    deltas = []
    client = client_class(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunks([frame(e) for e in events]))))
    with pytest.raises(ModelAdapterError, match="did not complete"):
        client.generate_stream(context(), settings(provider, model), deltas.append)
    assert deltas
    assert client.last_metadata["status"] == "failed"


@pytest.mark.parametrize("provider,client_class,model,terminal", [
    (*PROVIDERS[0], {"type": "response.incomplete", "response": {
        "status": "incomplete", "usage": {"input_tokens": 2, "output_tokens": 1}}}),
    (*PROVIDERS[1], {"type": "error", "error": {"message": "SECRET UPSTREAM ERROR"}}),
    (*PROVIDERS[2], {"candidates": [{"finishReason": "MAX_TOKENS"}],
                     "usageMetadata": {"totalTokenCount": 4}}),
])
def test_upstream_failures_after_public_text_are_sanitized(provider, client_class, model, terminal):
    events, next_index = text_events(provider)
    events = events[:next_index] + [terminal]
    client = client_class(api_key="secret", transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunks([frame(e) for e in events]))))
    deltas = []
    with pytest.raises(ModelAdapterError) as error:
        client.generate_stream(context(), settings(provider, model), deltas.append)
    assert deltas == ["先分析需求"]
    assert "SECRET" not in str(error.value)
    assert "secret" not in json.dumps(client.last_metadata)
    assert client.last_metadata["status"] == "failed"
    if provider != "anthropic":
        assert client.last_usage["total_tokens"] > 0


@pytest.mark.parametrize("provider,client_class,model", PROVIDERS)
def test_structured_reviewer_stream_falls_back_without_text_delta(provider, client_class, model):
    captured, deltas = {}, []
    value = '{"should_allow":true,"reason":"Allowed by policy"}'

    def handler(request):
        captured.update(json.loads(request.content))
        assert ":streamGenerateContent" not in str(request.url)
        if provider == "openai":
            output = {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": value}]}]}
        elif provider == "anthropic":
            output = {"stop_reason": "end_turn", "content": [{"type": "text", "text": value}]}
        else:
            output = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": value}]}}]}
        return httpx.Response(200, json=output)

    client = client_class(api_key="test", transport=httpx.MockTransport(handler))
    result = client.generate_stream(context(Decision), settings(provider, model), deltas.append)
    assert result.should_allow is True
    assert deltas == []
    assert "stream" not in captured


def test_stream_sse_frames_can_span_bytes_and_multiline_data_without_delaying_content():
    events, _ = text_events("google", "猫", "")
    payload = frame(events[0])
    char_index = payload.index("猫".encode())
    chunks = [b": keepalive\r\n\r\n", payload[:char_index + 1], payload[char_index + 1:],
              b'data: {"candidates":\n' + b'data: [{"finishReason":"STOP"}]}\n\n']
    client = GoogleModelClient(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=Chunks(chunks))))
    deltas = []
    assert client.generate_stream(context(), settings("google", "gemini-2.5-flash"), deltas.append).content == "猫"
    assert deltas == ["猫"]


@pytest.mark.parametrize("bad_body", [b'data: {"candidates":', b"data: not-json\n\n", b"data: []\n\n"])
def test_malformed_stream_never_becomes_a_success(bad_body):
    client = GoogleModelClient(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunks([bad_body]))))
    with pytest.raises(ModelAdapterError):
        client.generate_stream(context(), settings("google", "gemini-2.5-flash"), lambda _: None)
    assert client.last_metadata["status"] == "failed"


def test_stream_resource_limits_redirects_and_network_failures_are_bounded():
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example"}, text="SECRET")

    client = OpenAIModelClient(api_key="test", transport=httpx.MockTransport(redirect))
    with pytest.raises(ModelAdapterError, match="HTTP 302"):
        client.generate_stream(context(), settings("openai", "gpt-5"), lambda _: None)
    assert len(calls) == 1
    client = OpenAIModelClient(api_key="test", max_response_bytes=1024,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={
            "content-type": "text/event-stream"}, stream=Chunks([b":" + b"x" * 1024]))))
    with pytest.raises(ModelAdapterError, match="resource limits"):
        client.generate_stream(context(), settings("openai", "gpt-5"), lambda _: None)

    def timeout(_):
        raise httpx.ReadTimeout("SECRET")

    client = OpenAIModelClient(api_key="test", transport=httpx.MockTransport(timeout))
    with pytest.raises(ModelAdapterError, match="timed out"):
        client.generate_stream(context(), settings("openai", "gpt-5"), lambda _: None)


@pytest.mark.parametrize("provider,client_class,model", PROVIDERS)
def test_stream_rejects_json_response_instead_of_fake_playback(provider, client_class, model):
    client = client_class(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"text": "not a native stream"})))
    with pytest.raises(ModelAdapterError, match="not an event stream"):
        client.generate_stream(context(), settings(provider, model), lambda _: None)


def test_router_forwards_real_stream_without_provider_fallback_and_records_metadata():
    events, _ = text_events("google")
    google = GoogleModelClient(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunks([frame(e) for e in events]))))

    class Unused:
        def generate_stream(self, *_):
            pytest.fail("must never fall back to a different paid provider")

    router = RoutingModelClient(clients={"google": google, "openai": Unused()})
    deltas = []
    output = router.generate_stream(context(), settings("google", "gemini-2.5-flash"), deltas.append)
    assert output.content == "".join(deltas)
    assert router.last_metadata == google.last_metadata
    assert router.last_public_output == google.last_public_output
    google._transport = httpx.MockTransport(lambda _: httpx.Response(429, text="SECRET"))
    with pytest.raises(ModelAdapterError, match="HTTP 429"):
        router.generate_stream(context(), settings("google", "gemini-2.5-flash"), deltas.append)
    assert router.last_metadata["status"] == "failed"
    assert router.last_public_output is None


@pytest.mark.parametrize("provider,client_class,model", PROVIDERS)
def test_stream_failure_resets_callback_before_regular_generate(provider, client_class, model):
    requests, deltas = [], []

    def handler(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(429, text="SECRET")
        payload = json.loads(request.content)
        assert "stream" not in payload
        assert ":streamGenerateContent" not in str(request.url)
        if provider == "openai":
            data = {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "regular"}]}]}
        elif provider == "anthropic":
            data = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "regular"}]}
        else:
            data = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "regular"}]}}]}
        return httpx.Response(200, json=data)

    client = client_class(api_key="test", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelAdapterError, match="HTTP 429"):
        client.generate_stream(context(), settings(provider, model), deltas.append)
    assert client.generate(context(), settings(provider, model)).content == "regular"
    assert deltas == []


@pytest.mark.parametrize("provider,client_class,model,events", [
    (*PROVIDERS[0], [
        {"type": "response.output_item.added", "output_index": 0, "item": {
            "type": "function_call", "id": "fc1", "call_id": "c1", "name": "read_task", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"broken":'},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]),
    (*PROVIDERS[1], [
        {"type": "message_start", "message": {"id": "m1", "usage": {}}},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "c1", "name": "read_task", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"broken":'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]),
    (*PROVIDERS[2], [
        {"candidates": [{"content": {"parts": [{"functionCall": {
            "id": "c1", "name": "read_task", "partialArgs": [], "willContinue": True}}]}}]},
        {"candidates": [{"finishReason": "STOP"}]},
    ]),
])
def test_unfinished_tool_arguments_are_never_returned_as_an_executable_call(
        provider, client_class, model, events):
    deltas = []
    client = client_class(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunks([frame(e) for e in events]))))
    with pytest.raises(ModelAdapterError):
        client.generate_stream(context(), settings(provider, model), deltas.append)
    assert deltas == []
    assert client.last_metadata["status"] == "failed"


@pytest.mark.parametrize("delimiter", [b"\r", b"\r\n"])
def test_sse_supports_cr_line_endings_split_across_chunks(delimiter):
    events, _ = text_events("google", "hello", "")
    payload = b"".join(frame(event).replace(b"\n", delimiter) for event in events)
    client = GoogleModelClient(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=Chunks([bytes([value]) for value in payload]))))
    deltas = []
    assert client.generate_stream(context(), settings("google", "gemini-2.5-flash"), deltas.append).content == "hello"
    assert deltas == ["hello"]
