"""本地转录引擎：MLX（Apple Silicon 专用）。

MLX 是 Apple 的机器学习框架，只在 macOS (Apple Silicon) 上能真正推理。
Linux 上的 mlx wheel 是开发用 CPU 后端，缺少 Metal，无法用于生产推理；
因此本引擎在非 macOS/arm64 平台会明确报错，而不是静默失败或卡死。

支持模型（mlx-community 转好的 4bit/8bit 版本）：
  - mlx-community/whisper-large-v3-turbo
  - mlx-community/whisper-large-v3-mlx
  - mlx-community/Qwen3-ASR-0.6B（如需，需 mlx-audio 后端）

依赖：pip install mlx-whisper（仅 macOS 用户需要）
"""

from __future__ import annotations

import platform
import sys
from typing import Callable

from .base import BaseEngine
from subtitle import Segment, Transcript


def mlx_supported() -> tuple[bool, str]:
    """MLX 是否可用，及不可用原因。"""
    if sys.platform != "darwin":
        return False, (
            f"MLX 仅在 macOS (Apple Silicon) 上可用，当前系统是 {sys.platform}。"
            "请改用 faster-whisper 引擎（跨平台）"
        )
    if platform.machine() != "arm64":
        return False, (
            f"MLX 需要 Apple Silicon (arm64)，当前架构是 {platform.machine()}。"
            "请改用 faster-whisper 引擎"
        )
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        return False, (
            "未安装 mlx-whisper。请执行：pip install mlx-whisper"
        )
    return True, ""


class MLXWhisperEngine(BaseEngine):
    name = "mlx-whisper"

    def __init__(self, model: str | None = None, model_dir: str | None = None,
                 language: str | None = None,
                 on_progress: Callable[[float], None] | None = None):
        ok, reason = mlx_supported()
        if not ok:
            raise ValueError(reason)

        import mlx_whisper
        self._mlx_whisper = mlx_whisper
        self.model_ref = model_dir or model or "mlx-community/whisper-large-v3-turbo"
        self.language = language
        self.on_progress = on_progress

    def transcribe(self, audio_path: str) -> Transcript:
        # mlx_whisper 的内存/速度权衡：默认 4bit 量化权重最省内存
        kwargs = {}
        if self.language:
            kwargs["language"] = self.language

        if self.on_progress:
            self.on_progress(0.05)

        result = self._mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=self.model_ref,
            **kwargs,
        )

        if self.on_progress:
            self.on_progress(0.95)

        segments: list[Segment] = []
        for item in (result.get("segments") or []):
            text = (item.get("text") or "").strip()
            if not text:
                continue
            start = item.get("start") or 0
            end = item.get("end") or 0
            segments.append(Segment(text, int(float(start) * 1000), int(float(end) * 1000)))

        if not segments:
            text = (result.get("text") or "").strip()
            if not text:
                raise RuntimeError("MLX Whisper 未识别到任何语音内容")
            segments = [Segment(text)]

        if self.on_progress:
            self.on_progress(1.0)

        return Transcript(segments, source=self.name)
