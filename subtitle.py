"""字幕/文稿数据模型与导出。

统一所有转录引擎的输出：一段文字 + 可选的起止时间（毫秒）。
有时间戳的引擎（bcut / jianying）可导出 SRT/JSON/TXT；
无时间戳的引擎（siliconflow）只能导出 TXT/JSON（纯文稿）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional


class Segment:
    """单个转录片段。start_ms/end_ms 为 -1 表示无时间戳（纯文稿）。"""

    __slots__ = ("text", "start_ms", "end_ms")

    def __init__(self, text: str, start_ms: int = -1, end_ms: int = -1):
        self.text = (text or "").strip()
        self.start_ms = start_ms
        self.end_ms = end_ms

    @property
    def has_timing(self) -> bool:
        return self.start_ms >= 0 and self.end_ms >= 0

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }

    @staticmethod
    def _ms_to_srt(ms: int) -> str:
        ms = max(0, int(ms))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def to_srt_block(self, index: int) -> str:
        if not self.has_timing:
            raise ValueError("segment has no timing, cannot render SRT")
        return (
            f"{index}\n"
            f"{self._ms_to_srt(self.start_ms)} --> {self._ms_to_srt(self.end_ms)}\n"
            f"{self.text}\n"
        )


class Transcript:
    """一段完整的转录结果。"""

    def __init__(self, segments: List[Segment], source: str = ""):
        self.segments = [s for s in segments if s.text]
        self.source = source

    @property
    def has_timing(self) -> bool:
        return bool(self.segments) and all(s.has_timing for s in self.segments)

    @property
    def full_text(self) -> str:
        """整段文稿（按片段顺序拼接）。"""
        return "\n".join(s.text for s in self.segments)

    def to_srt(self) -> str:
        if not self.has_timing:
            raise ValueError("该引擎无时间戳，无法导出 SRT（请用 --format txt/json）")
        blocks = [s.to_srt_block(i) for i, s in enumerate(self.segments, 1)]
        return "\n".join(blocks) + "\n"

    def to_txt(self) -> str:
        return self.full_text + "\n"

    def to_json(self) -> str:
        data = {
            "source": self.source,
            "has_timing": self.has_timing,
            "text": self.full_text,
            "segments": [s.to_dict() for s in self.segments],
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    def save(self, output_path: str) -> str:
        """按扩展名导出 srt / json / txt。返回实际写出的路径。"""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()

        if suffix == ".srt":
            content = self.to_srt()
        elif suffix == ".json":
            content = self.to_json()
        elif suffix == ".txt":
            content = self.to_txt()
        else:
            raise ValueError(f"不支持的输出格式: {suffix}（支持 .srt .json .txt）")

        path.write_text(content, encoding="utf-8")
        return str(path)
