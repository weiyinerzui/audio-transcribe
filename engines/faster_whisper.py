"""本地转录引擎：faster-whisper（CTranslate2 后端）。

跨平台可用（Linux / macOS / Windows），CPU 与 NVIDIA GPU 都能跑。
模型由 model_manager 下载到本地目录，本引擎只负责加载与推理。

为什么单独写而不直接调 whisper：
  - 需要模型目录路径可控（统一放在 ~/.audio-transcribe/models/）
  - 需要把 segment 时间戳转成统一的 Transcript
  - 需要支持取消与进度上报

依赖：pip install faster-whisper（可选依赖，不装不影响其他引擎）
"""

from __future__ import annotations

import os
from typing import Callable

from .base import BaseEngine
from subtitle import Segment, Transcript


class FasterWhisperEngine(BaseEngine):
    name = "faster-whisper"

    def __init__(self, model: str | None = None, model_dir: str | None = None,
                 device: str | None = None, compute_type: str | None = None,
                 language: str | None = None,
                 on_progress: Callable[[float], None] | None = None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ValueError(
                "未安装 faster-whisper。请执行："
                "pip install faster-whisper\n"
                "（或在 Web 界面的「依赖检测」里一键安装）"
            ) from exc

        self._WhisperModel = WhisperModel
        self.model_ref = model_dir or model or "large-v3-turbo"
        self.language = language
        self.on_progress = on_progress

        # 默认设备：有 CUDA 用 CUDA，否则 CPU；M 系列 Mac 用 CPU 更稳
        self.device = device or ("cuda" if _has_cuda() else "cpu")
        # float16 在 CPU 上很慢，int8 快很多且质量损失可接受
        self.compute_type = compute_type or ("float16" if self.device == "cuda" else "int8")

        self._model = None

    def _load(self):
        if self._model is None:
            self._model = self._WhisperModel(
                self.model_ref,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def transcribe(self, audio_path: str) -> Transcript:
        model = self._load()
        segments_iter, info = model.transcribe(
            audio_path,
            language=self.language,
            vad_filter=True,          # 去掉静音段，长音频更快更准
            vad_parameters={"min_silence_duration_ms": 500},
            beam_size=5,
        )

        total = getattr(info, "duration", 0) or 0
        out: list[Segment] = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            out.append(Segment(text, int(seg.start * 1000), int(seg.end * 1000)))
            # 用已处理时长占总时长的比例上报进度
            if self.on_progress and total > 0:
                self.on_progress(min(1.0, seg.end / total))

        if not out:
            raise RuntimeError("faster-whisper 未识别到任何语音内容")
        return Transcript(out, source=self.name)


def _has_cuda() -> bool:
    """探测是否有可用的 CUDA 设备。"""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False
