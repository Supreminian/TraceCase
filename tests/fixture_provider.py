"""Isolated regression transport; never imported by the application."""
import hashlib
import json
from pathlib import Path
from sdlc.domain import normalize_text
from sdlc.providers import ModelResponse, ProviderError
SAMPLE_DIR = Path(__file__).parent / "fixtures" / "generation"

def list_samples() -> list[dict]:
    manifest = json.loads((SAMPLE_DIR / "manifest.json").read_text(encoding="utf-8"))
    return [{"id": item["id"], "label": item["label"],
             "text": (SAMPLE_DIR / item["input"]).read_text(encoding="utf-8")}
            for item in manifest]


class FixtureProvider:
    config = {"provider": "fixture", "model": "authored-fixtures-v1", "model_revision": "1",
              "parameters": {}, "real_model_call": False, "endpoint": "https://unit-test.invalid/"}

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
        raise FixtureError("测试样本未匹配。")


class FixtureError(ProviderError):
    pass
