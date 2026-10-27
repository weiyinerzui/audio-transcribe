"""硅基流动（SiliconFlow）ASR 接口。

Qwen/Qwen3-ASR-1.7B 只返回整段 text，不含时间戳（实测确认），因此本引擎
输出纯文稿，无法导出 SRT。鉴权：环境变量 SILICONFLOW_API_KEY。
"""

from __future__ import annotations

import os

import requests

from .base import BaseEngine
from subtitle import Segment, Transcript

API_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"


class SiliconFlowEngine(BaseEngine):
    name = "siliconflow"

    def __init__(self, model: str = "Qwen/Qwen3-ASR-1.7B", api_key: str | None = None):
        self.model = model
        self.api_key = (api_key or os.environ.get("SILICONFLOW_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError("未设置 SILICONFLOW_API_KEY 环境变量")

    def transcribe(self, audio_path: str) -> Transcript:
        with open(audio_path, "rb") as f:
            r = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                data={"model": self.model},
                timeout=300,
            )
        if r.status_code != 200:
            raise RuntimeError(f"硅基流动转录失败（{r.status_code}）: {r.text[:500]}")
        data = r.json()
        text = (data.get("text") or "").strip()
        if not text:
            raise RuntimeError("硅基流动返回空文本")
        return Transcript([Segment(text)], source="siliconflow")
