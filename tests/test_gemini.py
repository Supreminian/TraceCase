import io
import json

import pytest

from sdlc.providers import GeminiProvider


def test_gemini_configuration_uses_pet_model_and_does_not_expose_key(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-private-key')
    monkeypatch.delenv('GEMINI_MODEL', raising=False)
    monkeypatch.setenv('SDLC_MODEL_URL', 'https://wrong.example.invalid/')
    provider = GeminiProvider()
    assert provider.config['provider'] == 'google-gemini'
    assert provider.config['model'] == 'gemini-3.5-flash-lite'
    assert provider.config['endpoint'] == 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    assert 'test-private-key' not in str(provider.config)


def test_gemini_requires_credential_and_valid_model(monkeypatch):
    for name in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match='密钥'):
        GeminiProvider()
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.setenv('GEMINI_MODEL', '../../bad')
    with pytest.raises(ValueError, match='模型 ID'):
        GeminiProvider()


def test_google_json_request_and_usage_mapping(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-private-key')
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return io.BytesIO(json.dumps({'model': 'gemini-3.5-flash-lite',
                'choices': [{'message': {'content': '{"schema_version":"1.0"}'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 20, 'completion_tokens': 30}}).encode())
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    result = GeminiProvider().generate({'prompt': {'rendered': '虚构需求'}})
    assert len(calls) == 1
    payload = json.loads(calls[0].data)
    assert payload['response_format'] == {'type': 'json_object'}
    assert calls[0].get_header('Authorization') == 'Bearer test-private-key'
    assert 'test-private-key' not in calls[0].full_url
    assert result.usage == {'input_tokens': 20, 'output_tokens': 30}
    assert 'test-private-key' not in str(result)
