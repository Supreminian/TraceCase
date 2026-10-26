"""Provider seam. Fixture responses are authored examples, never model evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sdlc.domain import normalize_text

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples"


@dataclass(frozen=True)
class ModelResponse:
    raw_output: str
    metadata: dict
    usage: dict | None = None


class ModelProvider(Protocol):
    """Adapters own network timeouts and read secrets only from environment.

    config/metadata must contain only nonsecret, reproducibility information.
    Latency is measured around generate() by the application, including failures.
    """

    @property
    def config(self) -> dict: ...

    def generate(self, request: dict) -> ModelResponse: ...


def list_samples() -> list[dict]:
    manifest = json.loads((SAMPLE_DIR / "manifest.json").read_text(encoding="utf-8"))
    return [{"id": item["id"], "label": item["label"],
             "text": (SAMPLE_DIR / item["input"]).read_text(encoding="utf-8")}
            for item in manifest]


class FixtureProvider:
    config = {"provider": "fixture", "model": "authored-fixtures-v1", "model_revision": "1",
              "parameters": {}, "real_model_call": False}

    def generate(self, request: dict) -> ModelResponse:
        normalized = normalize_text(request["normalized_text"])
        actual_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if request.get("input_hash") != actual_hash:
            raise ValueError("请求输入与快照哈希不一致。")
        manifest = json.loads((SAMPLE_DIR / "manifest.json").read_text(encoding="utf-8"))
        for item in manifest:
            sample = normalize_text((SAMPLE_DIR / item["input"]).read_text(encoding="utf-8"))
            if hashlib.sha256(sample.encode("utf-8")).hexdigest() == actual_hash:
                raw = (SAMPLE_DIR / item["output"]).read_text(encoding="utf-8")
                return ModelResponse(raw, {**self.config, "fixture_id": item["id"],
                                          "notice": "固定样例，未调用模型"}, None)
        raise ValueError("固定样例未匹配：请选择预置 PRD；自定义内容需要接入实际模型后生成。")


class UnconfiguredModelProvider:
    """Explicit disabled slot until service, model, budget and timeout are selected."""

    config = {"provider": "unconfigured", "model": None, "parameters": {},
              "real_model_call": False}

    def generate(self, request: dict) -> ModelResponse:
        raise RuntimeError("实际模型尚未配置。请先确定服务商、模型、预算、超时和环境变量密钥。")


class ProviderError(RuntimeError):
    """Sanitized transport error safe to store in the run ledger."""


class CompatibleProvider:
    """Non-streaming Chat Completions adapter, configured only from environment."""

    def __init__(self, *, endpoint=None, model=None, key=None):
        import os
        from urllib.parse import urlsplit

        endpoint = (os.environ.get('SDLC_MODEL_URL', '') if endpoint is None else endpoint).strip()
        model = (os.environ.get('SDLC_MODEL_NAME', '') if model is None else model).strip()
        parts = urlsplit(endpoint)
        local = parts.hostname in {'127.0.0.1', 'localhost', '::1'}
        if (parts.scheme not in {'https', 'http'} or not parts.hostname
                or (parts.scheme == 'http' and not local)
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError('SDLC_MODEL_URL 须为完整 HTTPS 接口地址；仅本机允许 HTTP，不允许凭据或查询参数。')
        if not model:
            raise ValueError('请设置 SDLC_MODEL_NAME。')
        key = (os.environ.get('SDLC_MODEL_API_KEY', '') if key is None else key).strip()
        if not local and not key:
            raise ValueError('远端模型需要环境变量 SDLC_MODEL_API_KEY。')
        if '\r' in key or '\n' in key:
            raise ValueError('密钥格式无效。')
        try:
            timeout = int(os.environ.get('SDLC_MODEL_TIMEOUT', '60'))
            limit = int(os.environ.get('SDLC_MODEL_MAX_TOKENS', '4096'))
        except ValueError as exc:
            raise ValueError('超时和输出 Token 上限必须是整数。') from exc
        if not 1 <= timeout <= 300 or not 128 <= limit <= 32768:
            raise ValueError('超时须为 1–300 秒，输出 Token 上限须为 128–32768。')
        self._endpoint, self._key = endpoint, key
        self.config = {'provider': 'openai-compatible', 'model': model,
                       'endpoint': endpoint, 'timeout_seconds': timeout,
                       'parameters': {'max_tokens': limit}, 'real_model_call': True}

    def generate(self, request: dict) -> ModelResponse:
        import socket
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, HTTPRedirectHandler, build_opener

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        payload = {'model': self.config['model'], 'stream': False,
                   'max_tokens': self.config['parameters']['max_tokens'],
                   'messages': [{'role': 'system', 'content': request['prompt']['rendered']}]}
        if self.config['provider'] == 'google-gemini':
            payload['response_format'] = {'type': 'json_object'}
        headers = {'Content-Type': 'application/json'}
        if self._key:
            headers['Authorization'] = 'Bearer ' + self._key
        try:
            req = Request(self._endpoint, data=json.dumps(payload).encode(), headers=headers)
            with build_opener(NoRedirect()).open(req, timeout=self.config['timeout_seconds']) as response:
                body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ProviderError('模型响应超过 4 MiB，已拒绝处理。')
            envelope = json.loads(body)
            choice = envelope['choices'][0]
            content = choice['message']['content']
            if not isinstance(content, str) or not content.strip():
                raise ProviderError('模型未返回文本内容，可能拒绝回答或响应格式不兼容。')
            usage = envelope.get('usage')
            if isinstance(usage, dict):
                usage = {'input_tokens': usage.get('prompt_tokens'),
                         'output_tokens': usage.get('completion_tokens')}
                if any(v is not None and (type(v) is not int or v < 0) for v in usage.values()):
                    usage = None
            else:
                usage = None
            return ModelResponse(content, {**self.config,
                'model_revision': envelope.get('model'), 'finish_reason': choice.get('finish_reason')}, usage)
        except HTTPError as exc:
            raise ProviderError(f'模型服务返回 HTTP {exc.code}；请检查地址、权限、限额和请求参数。') from None
        except (TimeoutError, socket.timeout):
            raise ProviderError('模型请求超时；未自动重试。') from None
        except URLError:
            raise ProviderError('无法连接模型服务；请检查地址、网络和证书。') from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError('模型响应不是兼容的 Chat Completions JSON。') from None


class GeminiProvider(CompatibleProvider):
    """Google's documented compatibility endpoint; no paid fallback or model substitution."""

    def __init__(self):
        import os
        import re

        model = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite').strip()
        if not re.fullmatch(r'gemini-[A-Za-z0-9._-]{1,100}', model):
            raise ValueError('GEMINI_MODEL 不是有效的 Gemini 模型 ID。')
        key = os.environ.get('GEMINI_API_KEY', '').strip() or os.environ.get('GOOGLE_API_KEY', '').strip()
        if not key:
            raise ValueError('Google Gemini 尚未配置密钥。请通过 scripts/run_google.py 复用桌宠钥匙串，或设置 GEMINI_API_KEY 后重启。')
        super().__init__(endpoint='https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
                         model=model, key=key)
        self.config['provider'] = 'google-gemini'
