"""通用 OpenAI Whisper 兼容 ASR 引擎。

支持任何实现了 `POST {base_url}/audio/transcriptions` 的服务：
  - OpenAI 官方（whisper-1 / gpt-4o-transcribe）
  - Groq（whisper-large-v3 / whisper-large-v3-turbo，速度快）
  - 硅基流动（FunAudioLLM/SenseVoiceSmall）
  - 自建 faster-whisper-server / whisper.cpp server / vLLM
  - Azure OpenAI、Cloudflare Workers AI 等兼容实现

与内置 siliconflow 引擎的区别：那个写死了 URL 和模型，这个完全可配。

优先级：显式参数 > 环境变量。
环境变量：OPENAI_ASR_BASE_URL / OPENAI_ASR_API_KEY / OPENAI_ASR_MODEL
"""

from __future__ import annotations

import os

import requests

from .base import BaseEngine
from subtitle import Segment, Transcript

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "whisper-1"

# 这些模型返回 verbose_json 时带 segment 级时间戳
_TIMED_MODEL_HINTS = ("whisper", "transcribe")


class OpenAIASREngine(BaseEngine):
    name = "openai-asr"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 base_url: str | None = None, language: str | None = None,
                 timeout: int = 600):
        self.model = (model or os.environ.get("OPENAI_ASR_MODEL", DEFAULT_MODEL)).strip()
        self.api_key = (api_key or os.environ.get("OPENAI_ASR_API_KEY", "")).strip()
        self.base_url = (base_url or os.environ.get("OPENAI_ASR_BASE_URL",
                                                    DEFAULT_BASE_URL)).strip()
        self.language = language
        self.timeout = timeout
        if not self.api_key:
            raise ValueError(
                "未配置 ASR 服务的 API key：请填写 api_key，"
                "或设置 OPENAI_ASR_API_KEY 环境变量"
            )

    def _endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        # 容忍用户填了带 /audio/transcriptions 的完整地址
        if base.endswith("/audio/transcriptions"):
            return base
        return f"{base}/audio/transcriptions"

    def transcribe(self, audio_path: str) -> Transcript:
        data = {"model": self.model, "response_format": "verbose_json"}
        if self.language:
            data["language"] = self.language

        with open(audio_path, "rb") as f:
            r = requests.post(
                self._endpoint(),
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                data=data,
                timeout=(30, self.timeout),
            )

        # 有些服务不支持 verbose_json，退回 json 再试一次
        if r.status_code >= 400 and "response_format" in r.text.lower():
            data.pop("response_format", None)
            with open(audio_path, "rb") as f:
                r = requests.post(
                    self._endpoint(),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                    data=data,
                    timeout=(30, self.timeout),
                )

        r.encoding = "utf-8"
        if r.status_code != 200:
            raise RuntimeError(
                f"ASR 服务转录失败（{r.status_code}）：{r.text[:400]}"
            )

        try:
            payload = r.json()
        except ValueError as exc:
            raise RuntimeError(f"ASR 服务返回的不是 JSON：{r.text[:300]}") from exc

        return self._to_transcript(payload)

    def _to_transcript(self, payload: dict) -> Transcript:
        """把 verbose_json / json 响应统一成 Transcript。"""
        # 1) 有 segments：逐段带时间戳
        raw_segments = payload.get("segments") or []
        segments: list[Segment] = []
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            start = item.get("start")
            end = item.get("end")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                segments.append(Segment(text, int(start * 1000), int(end * 1000)))
            else:
                segments.append(Segment(text))

        if segments:
            return Transcript(segments, source=self.name)

        # 2) 只有整段 text：无时间戳
        text = (payload.get("text") or "").strip()
        if not text:
            raise RuntimeError("ASR 服务返回空文本")
        return Transcript([Segment(text)], source=self.name)
