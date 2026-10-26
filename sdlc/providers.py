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
