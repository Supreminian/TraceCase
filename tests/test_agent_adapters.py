"""Adapter contracts: recovery/CAS, constrained diagnostics, native provider protocols."""

from __future__ import annotations

import json
import multiprocessing
import stat

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field

from sdlc.agent.adapters.bash import BashTool
from sdlc.agent.adapters.models import (
    AnthropicModelClient, GoogleModelClient, ModelAdapterError, OpenAIModelClient,
    RoutingModelClient,
)
from sdlc.agent.adapters.session_store import JsonlSessionStore, SessionStorageError
from sdlc.agent.application import SessionConflictError
from sdlc.agent.domain import (
    AssistantMessage, Context, Decision, ModelSettings, Session, ToolCall, ToolResult, UserMessage,
)


def _save_racer(root, ready, start, result):
    store = JsonlSessionStore(root, "workspace:project")
    session = store.get("session")
    ready.put(True)
    start.wait(10)
    session.append(UserMessage(content="concurrent update"))
    try:
        store.save(session, expected_revision=0)
        result.put("saved")
    except SessionConflictError:
        result.put("conflict")


def test_store_round_trip_tenant_isolation_and_permissions(tmp_path):
    first, second = JsonlSessionStore(tmp_path, "team:one"), JsonlSessionStore(tmp_path, "team:two")
    session = Session(id="session")
    session.append(UserMessage(content="private PRD"))
    session.append(AssistantMessage(tool_calls=[ToolCall(id="call1", name="read", arguments={})],
                                    metadata={"provider_state": {"opaque": "token"}}))
    session.append(ToolResult(tool_call_id="call1", content="evidence"))
    first.save(session)
    assert first.get("session") == session
    assert first.list_sessions() == [session]
    assert second.get("session") is None
    assert "team:one" not in str(first.directory)
    assert stat.S_IMODE((first.directory / "session.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE(first.directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("identifier", ["../session", "a/b", ".", "", "a.jsonl", "/tmp/escape"])
def test_store_rejects_path_traversal(tmp_path, identifier):
    with pytest.raises(ValueError):
        JsonlSessionStore(tmp_path, "scope").get(identifier)


def test_store_cas_cannot_replace_another_writers_snapshot(tmp_path):
    store = JsonlSessionStore(tmp_path, "scope")
    session = Session(id="session")
    store.save(session)
    stale = store.get(session.id)
    session.append(UserMessage(content="winner"))
    store.save(session, expected_revision=0)
    stale.append(UserMessage(content="loser"))
    with pytest.raises(SessionConflictError):
        store.save(stale, expected_revision=0)
    with pytest.raises(SessionConflictError):
        store.save(stale)
    assert store.get(session.id).messages[0].content == "winner"


def test_store_uses_cross_process_lock_for_compare_and_swap(tmp_path):
    JsonlSessionStore(tmp_path, "workspace:project").save(Session(id="session"))
    ctx = multiprocessing.get_context("spawn")
    ready, result, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [ctx.Process(target=_save_racer, args=(str(tmp_path), ready, start, result))
                 for _ in range(2)]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=20) and ready.get(timeout=20)
        start.set()
        assert sorted([result.get(timeout=20), result.get(timeout=20)]) == ["conflict", "saved"]
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.kill()


@pytest.mark.parametrize("tail", [b'{"format":', b"", b"\xe4\xb8"])
def test_store_recovers_crash_tail_without_losing_complete_snapshot(tmp_path, tail):
    store = JsonlSessionStore(tmp_path, "scope")
    session = Session(id="session")
    store.save(session)
    path = store.directory / "session.jsonl"
    if tail:
        with path.open("ab") as stream:
            stream.write(tail)
    else:
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    assert store.get(session.id) == session
    session.append(UserMessage(content="after recovery"))
    store.save(session, expected_revision=0)
    assert store.get(session.id) == session
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["revision"] for record in records] == [0, 1]


def test_store_complete_corruption_and_symlinks_fail_closed(tmp_path):
    store = JsonlSessionStore(tmp_path, "scope")
    store.save(Session(id="session"))
    path = store.directory / "session.jsonl"
    with path.open("ab") as stream:
        stream.write(b"invalid complete record\n")
    with pytest.raises(SessionStorageError):
        store.get("session")
    secret = tmp_path / "secret"
    secret.write_text("private")
    (store.directory / "other.jsonl").symlink_to(secret)
    with pytest.raises(OSError):
        store.get("other")
    assert secret.read_text() == "private"


def test_bash_default_disabled_and_rejects_non_diagnostic_operations(tmp_path):
    with pytest.raises(PermissionError):
        BashTool(tmp_path).execute({"command": ["pwd"]})
    tool = BashTool(tmp_path, enabled=True)
    for command in (["sh", "-c", "pwd"], ["curl", "https://example.com"],
                    ["git", "config", "--list"], ["git", "diff"],
                    ["cat", ".env"], ["ls", "-la"], ["ls", ".."], ["pwd; env"]):
        with pytest.raises((PermissionError, ValueError, FileNotFoundError)):
            tool.execute({"command": command})


def test_bash_directory_scope_and_output_limit(tmp_path):
    directory = tmp_path / "approved"
    directory.mkdir()
    (tmp_path / "unapproved").mkdir()
    (tmp_path / "linked").symlink_to(directory)
    tool = BashTool(tmp_path, enabled=True, allowed_directories=("approved",), max_output_bytes=128)
    for index in range(30):
        (directory / f"test-file-{index:03d}").touch()
    result = json.loads(tool.execute({"command": ["ls", "approved"]}))
    assert result["truncated"]
    assert len(result["output"].encode()) <= 128
    for path in ("unapproved", "linked", ".env"):
        with pytest.raises((ValueError, PermissionError, FileNotFoundError)):
            tool.execute({"command": ["ls", path]})


def test_bash_never_inherits_secrets_or_uses_a_shell(tmp_path, monkeypatch):
    import sdlc.agent.adapters.bash as module
    actual = module.subprocess.Popen
    captured = {}

    def wrapped(*args, **kwargs):
        captured.update(kwargs)
        return actual(*args, **kwargs)

    monkeypatch.setenv("GEMINI_API_KEY", "never-available-in-process")
    monkeypatch.setattr(module.subprocess, "Popen", wrapped)
    assert json.loads(BashTool(tmp_path, enabled=True).execute({"command": ["pwd"]}))["exit_code"] == 0
    assert "GEMINI_API_KEY" not in captured["env"]
    assert captured["shell"] is False


def test_bash_rejects_external_git_metadata(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /private/repository")
    with pytest.raises(PermissionError, match="local workspace metadata"):
        BashTool(tmp_path, enabled=True).execute({"command": ["git", "status", "--short"]})


def _context(output_type=AssistantMessage):
    return Context("Trusted policy", [BashTool(".")], [UserMessage(content="Inspect PRD")],
                   output_type, memory="project knowledge", agents_md="untrusted project text")


def _settings(provider, model_id, effort="default"):
    return ModelSettings(provider=provider, model_id=model_id, effort=effort,
                         max_tokens=4096, timeout_seconds=10)


def test_openai_native_calls_roles_usage_and_encrypted_continuity():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert request.url == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer secret-key"
        return httpx.Response(200, headers={"x-request-id": "request-1"}, json={
            "id": "response-1", "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 8},
            "output": [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque",
                        "summary": [{"text": "private reasoning must not persist"}]},
                       {"type": "function_call", "call_id": "call-1", "name": "bash",
                        "arguments": '{"command":["pwd"]}'}]})

    client = OpenAIModelClient(api_key="secret-key", transport=httpx.MockTransport(handler))
    context = _context()
    output = client.generate(context, _settings("openai", "gpt-5", "high"))
    assert output.tool_calls[0].arguments == {"command": ["pwd"]}
    assert client.last_usage == {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18}
    assert output.metadata["model_event"]["request_id"] == "request-1"
    assert "private reasoning" not in json.dumps(output.model_dump())
    context.messages.extend([output, ToolResult(tool_call_id="call-1", content="/workspace")])
    client.generate(context, _settings("openai", "gpt-5", "high"))
    inputs = requests[-1]["input"]
    assert any(item.get("type") == "reasoning" and item["encrypted_content"] == "opaque" for item in inputs)
    assert inputs[-1] == {"type": "function_call_output", "call_id": "call-1", "output": "/workspace"}
    assert requests[-1]["instructions"] == "Trusted policy"
    assert requests[-1]["store"] is False


@pytest.mark.parametrize("invalid", ["{bad", "[]", '{"x":1,"x":2}', '{"x":NaN}'])
def test_openai_invalid_tool_arguments_cannot_reach_tools(invalid):
    client = OpenAIModelClient(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={"status": "completed", "output": [{"type": "function_call", "call_id": "c",
              "name": "bash", "arguments": invalid}]})))
    with pytest.raises(ModelAdapterError):
        client.generate(_context(), _settings("openai", "gpt-5"))


def test_anthropic_native_tool_results_and_untrusted_references():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "msg1", "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 3},
            "content": [{"type": "thinking", "thinking": "never persist"},
                        {"type": "text", "text": "Completed"}]})

    context = _context()
    context.messages += [AssistantMessage(tool_calls=[ToolCall(id="c1", name="bash", arguments={})]),
                         ToolResult(tool_call_id="c1", content="denied", is_error=True)]
    result = AnthropicModelClient(api_key="test", transport=httpx.MockTransport(handler)).generate(
        context, _settings("anthropic", "claude-sonnet-4-5"))
    assert captured["system"] == "Trusted policy"
    assert captured["messages"][-1]["content"][0] == {
        "type": "tool_result", "tool_use_id": "c1", "content": "denied", "is_error": True}
    assert captured["tools"][0]["input_schema"] == context.tools[0].parameters
    assert result.content == "Completed"
    assert "never persist" not in json.dumps(result.model_dump())


def test_google_function_signatures_round_trip_and_tool_result_names():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"responseId": "g1", "usageMetadata": {"totalTokenCount": 20},
            "candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": "private chain", "thought": True},
                {"functionCall": {"id": "c1", "name": "bash", "args": {"command": ["pwd"]}},
                 "thoughtSignature": "opaque-signature"}]}}]})

    client = GoogleModelClient(api_key="test", transport=httpx.MockTransport(handler))
    context = _context()
    first = client.generate(context, _settings("google", "gemini-2.5-flash", "low"))
    assert first.content == ""
    context.messages += [first, ToolResult(tool_call_id="c1", content="success")]
    client.generate(context, _settings("google", "gemini-2.5-flash", "low"))
    parts = requests[-1]["contents"][-2]["parts"]
    assert parts[0]["thoughtSignature"] == "opaque-signature"
    assert requests[-1]["contents"][-1]["parts"][0]["functionResponse"] == {
        "id": "c1", "name": "bash", "response": {"result": "success"}}
    assert "private chain" not in json.dumps(first.model_dump())


def test_google_preserves_text_signatures_and_absent_wire_call_ids():
    requests = []
    original = [{"text": "Inspecting the requirement", "thoughtSignature": "text-token"},
                {"functionCall": {"name": "bash", "args": {"command": ["pwd"]}},
                 "thoughtSignature": "call-token"}]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
                                                       "content": {"parts": original}}]})

    client = GoogleModelClient(api_key="test", transport=httpx.MockTransport(handler))
    context = _context()
    result = client.generate(context, _settings("google", "gemini-2.5-flash"))
    context.messages += [result, ToolResult(tool_call_id=result.tool_calls[0].id, content="done")]
    client.generate(context, _settings("google", "gemini-2.5-flash"))
    assert requests[-1]["contents"][-2]["parts"] == original
    assert "id" not in requests[-1]["contents"][-1]["parts"][0]["functionResponse"]


@pytest.mark.parametrize("provider,client_class,model", [
    ("openai", OpenAIModelClient, "gpt-5"),
    ("anthropic", AnthropicModelClient, "claude-sonnet-4-5"),
    ("google", GoogleModelClient, "gemini-2.5-flash"),
])
def test_reviewer_strict_decision_and_no_unfinished_native_tool_call(provider, client_class, model):
    captured = {}
    decision = '{"should_allow":false,"reason":"not authorized"}'

    def handler(request):
        captured.update(json.loads(request.content))
        if provider == "openai":
            data = {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": decision}]}]}
        elif provider == "anthropic":
            data = {"stop_reason": "end_turn", "content": [{"type": "text", "text": decision}]}
        else:
            data = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": decision}]}}]}
        return httpx.Response(200, json=data)

    context = _context(Decision)
    context.messages += [AssistantMessage(tool_calls=[ToolCall(id="pending", name="bash", arguments={})])]
    result = client_class(api_key="test", transport=httpx.MockTransport(handler)).generate(
        context, _settings(provider, model))
    assert result.should_allow is False
    assert "tools" not in captured
    if provider == "openai":
        assert all(item.get("type") != "function_call" for item in captured["input"])


@pytest.mark.parametrize("decision", ['{"should_allow":"true","reason":"x"}',
    '{"should_allow":false,"reason":""}', '{"should_allow":true,"reason":"x","extra":1}'])
def test_reviewer_does_not_coerce_or_accept_unrecognized_fields(decision):
    client = AnthropicModelClient(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={"stop_reason": "end_turn", "content": [{"type": "text", "text": decision}]})))
    with pytest.raises(ModelAdapterError):
        client.generate(_context(Decision), _settings("anthropic", "claude-sonnet-4-5"))


def test_transport_redirects_limits_truncation_and_errors_are_sanitized():
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/private"},
                              text="SECRET PROVIDER BODY")

    client = GoogleModelClient(api_key="secret", transport=httpx.MockTransport(redirect))
    with pytest.raises(ModelAdapterError) as error:
        client.generate(_context(), _settings("google", "gemini-2.5-flash"))
    assert len(calls) == 1
    assert "SECRET" not in str(error.value) and "secret" not in json.dumps(client.last_event)
    client = OpenAIModelClient(api_key="test", max_response_bytes=1024,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="x" * 1025)))
    with pytest.raises(ModelAdapterError, match="resource limits"):
        client.generate(_context(), _settings("openai", "gpt-5"))
    client = GoogleModelClient(api_key="test", transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={"usageMetadata": {"totalTokenCount": 42},
              "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]})))
    with pytest.raises(ModelAdapterError, match="did not complete"):
        client.generate(_context(), _settings("google", "gemini-2.5-flash"))
    assert client.last_usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 42}
    assert client.last_metadata["raw_usage"] == {"totalTokenCount": 42}
    assert client.last_event["status"] == "failed"
    with pytest.raises(ValueError):
        client.generate(_context(), _settings("google", "gemini-2.0-flash", "high"))
    assert client.last_event == {}


@pytest.mark.parametrize("client_class,provider,model,effort", [
    (OpenAIModelClient, "openai", "gpt-4o", "high"),
    (OpenAIModelClient, "openai", "o3-mini", "none"),
    (AnthropicModelClient, "anthropic", "claude-sonnet-4-5", "high"),
    (GoogleModelClient, "google", "gemini-2.0-flash", "high"),
])
def test_unsupported_effort_fails_before_network(client_class, provider, model, effort):
    def unexpected(_):
        pytest.fail("unsupported configuration must not make a network request")
    with pytest.raises(ValueError):
        client_class(api_key="test", transport=httpx.MockTransport(unexpected)).generate(
            _context(), _settings(provider, model, effort))


def test_router_has_no_implicit_provider_fallback():
    class FailingClient:
        last_event = {"provider": "google", "status": "failed"}

        def generate(self, context, settings):
            raise ModelAdapterError("quota unavailable")

    class UnusedClient:
        def generate(self, context, settings):
            pytest.fail("paid fallback must never be implicit")

    router = RoutingModelClient(clients={"google": FailingClient(), "openai": UnusedClient()})
    with pytest.raises(ModelAdapterError, match="quota unavailable"):
        router.generate(_context(), _settings("google", "gemini-2.5-flash"))
    assert router.last_event["status"] == "failed"


class _TypedStage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    count: int = Field(ge=1)
    labels: list[str] = Field(min_length=1)
    default: str = "default field name must survive schema adaptation"


@pytest.mark.parametrize("provider,client_class,model", [
    ("openai", OpenAIModelClient, "gpt-5"),
    ("anthropic", AnthropicModelClient, "claude-sonnet-4-5"),
    ("google", GoogleModelClient, "gemini-2.5-flash"),
])
def test_generic_typed_stage_outputs_schema_and_local_validation(provider, client_class, model):
    output = {"count": 2, "labels": ["permissions"]}
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        text = json.dumps(output)
        if provider == "openai":
            response = {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": text}]}]}
        elif provider == "anthropic":
            response = {"stop_reason": "end_turn", "usage": {"input_tokens": 2,
                "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5, "output_tokens": 3},
                "content": [{"type": "text", "text": text}]}
        else:
            response = {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3,
                "thoughtsTokenCount": 2, "totalTokenCount": 10},
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]}
        return httpx.Response(200, json=response)

    client = client_class(api_key="test", transport=httpx.MockTransport(handler))
    context = _context(_TypedStage)
    result = client.generate(context, _settings(provider, model))
    assert isinstance(result, _TypedStage) and result.count == 2
    if provider == "openai":
        schema = requests[-1]["text"]["format"]["schema"]
    elif provider == "anthropic":
        schema = requests[-1]["output_config"]["format"]["schema"]
        assert client.last_usage == {"input_tokens": 17, "output_tokens": 3, "total_tokens": 20}
    else:
        schema = requests[-1]["generationConfig"]["responseJsonSchema"]
        assert client.last_usage == {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
    assert "default" in schema["properties"]
    output.update({"count": 0, "labels": []})
    with pytest.raises(ModelAdapterError, match="required schema"):
        client.generate(context, _settings(provider, model))
    output.update({"count": "2", "labels": ["permissions"]})
    with pytest.raises(ModelAdapterError, match="required schema"):
        client.generate(context, _settings(provider, model))
    assert client.last_event["status"] == "failed"


@pytest.mark.parametrize("provider,client_class,model,response", [
    ("openai", OpenAIModelClient, "gpt-5", {"status": "incomplete", "output": []}),
    ("anthropic", AnthropicModelClient, "claude-sonnet-4-5",
     {"stop_reason": "max_tokens", "content": []}),
    ("google", GoogleModelClient, "gemini-2.5-flash",
     {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}),
])
def test_truncated_typed_stage_is_never_accepted(provider, client_class, model, response):
    client = client_class(api_key="test", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=response)))
    with pytest.raises(ModelAdapterError, match="did not complete"):
        client.generate(_context(_TypedStage), _settings(provider, model))
    assert client.last_event["status"] == "failed"


def test_google_compiles_case_batch_grammar_but_keeps_full_local_validation():
    from sdlc.testing_agent.domain import CaseBatch
    from tests.test_testing_agent import batch
    output = batch()
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP',
            'content': {'parts': [{'text': json.dumps(output)}]}}]})
    client = GoogleModelClient(api_key='test', transport=httpx.MockTransport(handler))
    context = _context(CaseBatch)
    result = client.generate(context, _settings('google', 'gemini-2.5-flash'))
    assert len(result.cases) == 1
    def check(value):
        if isinstance(value, dict):
            assert not set(value) & {'minLength', 'maxLength', 'maxItems', 'minItems', 'default', 'title'}
            if '$ref' in value:
                assert all(key.startswith('$') for key in value)
            for key, child in value.items():
                if key in {'properties', '$defs'}:
                    for item in child.values():
                        check(item)
                else:
                    check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(requests[0]['generationConfig']['responseJsonSchema'])
    output['cases'] = []
    with pytest.raises(ModelAdapterError, match='required schema'):
        client.generate(context, _settings('google', 'gemini-2.5-flash'))
    assert client.last_public_output
    assert not client.last_metadata.get('public_output')
