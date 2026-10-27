"""转录引擎基类。"""

from __future__ import annotations

from subtitle import Transcript


class BaseEngine:
    """所有转录引擎的接口约定。

    子类实现 `transcribe(audio_path) -> Transcript`。
    """

    name = "base"

    def transcribe(self, audio_path: str) -> Transcript:
        raise NotImplementedError
