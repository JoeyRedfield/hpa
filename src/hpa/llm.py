"""LLM provider 抽象：默认 DeepSeek（OpenAI 兼容接口）。

原则（spec §3）：Python 管确定性流程，LLM 只负责「理解」三件事——
分诊、抽取、起草。所有调用都要求返回 JSON，解析失败即报错，不猜。
"""

from __future__ import annotations

import json
import os
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .mail import load_secrets

SECRETS_KEY = "HPA_LLM_API_KEY"


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, system: str, user: str) -> str:
        """返回模型原文；调用方负责 `parse_json`。"""
        ...


@dataclass
class DeepSeek:
    api_key: str
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    timeout: float = 90.0

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"DeepSeek 连接失败：{exc}") from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"DeepSeek 响应格式异常：{str(data)[:300]}") from exc


def parse_json(text: str) -> dict:
    """从模型输出里取第一个 JSON 对象；容忍 ```json 围栏与前后废话。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[: -3]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise LLMError(f"模型未返回 JSON：{text[:300]!r}")
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型 JSON 解析失败：{exc}；原文：{text[:300]!r}") from exc
    if not isinstance(value, dict):
        raise LLMError(f"模型返回的不是 JSON 对象：{type(value).__name__}")
    return value


def _llm_settings(cfg: Config) -> dict:
    path = cfg.project_root / "config.toml"
    if not path.exists():
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    return data.get("llm", {})


def from_config(cfg: Config) -> DeepSeek:
    settings = _llm_settings(cfg)
    secrets = load_secrets()
    api_key = os.environ.get(SECRETS_KEY) or secrets.get(SECRETS_KEY, "")
    if not api_key:
        raise LLMError(
            f"缺少 LLM 密钥：{SECRETS_KEY}。\n"
            "请写入 ~/.config/hpa/secrets.env（如 HPA_LLM_API_KEY=sk-...），"
            "或设置同名环境变量。"
        )
    return DeepSeek(
        api_key=api_key,
        model=os.environ.get("HPA_LLM_MODEL") or settings.get("model", "deepseek-chat"),
        base_url=os.environ.get("HPA_LLM_BASE_URL") or settings.get("base_url", "https://api.deepseek.com"),
    )


def load_prompt(cfg: Config, name: str) -> str:
    path = cfg.project_root / "prompts" / name
    if not path.exists():
        raise LLMError(f"缺少提示词文件：{path}")
    return path.read_text(encoding="utf-8")
