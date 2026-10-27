"""音频预处理工具：视频抽音轨、转码为 mp3/wav。

依赖 ffmpeg/ffprobe，二者在系统 PATH 上即可。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v"}


def has_tool(name: str) -> bool:
    try:
        subprocess.run(
            [name, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return True
    except Exception:
        return False


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            f"命令执行失败: {' '.join(cmd)}\n{proc.stderr.decode('utf-8', 'ignore')[-800:]}"
        )


def is_audio_file(path: str) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTENSIONS


def is_video_file(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def prepare_audio(input_path: str, target_format: str = "mp3") -> tuple[str, bool]:
    """把任意音/视频输入准备为指定格式的音频文件。

    返回 (path, is_temp)。仅当 is_temp=True 时 path 是本函数创建的临时文件，
    调用方才需要清理；is_temp=False 表示直接复用了用户原始输入，绝不能删除。
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"文件不存在: {input_path}")

    if not has_tool("ffmpeg"):
        raise RuntimeError("未找到 ffmpeg，请先安装（apt install ffmpeg 或 brew install ffmpeg）")

    src = Path(input_path)
    suffix = src.suffix.lower()

    if suffix == f".{target_format}" and suffix in AUDIO_EXTENSIONS:
        return str(src.resolve()), False

    fd, out = tempfile.mkstemp(suffix=f".{target_format}")
    os.close(fd)

    if suffix in VIDEO_EXTENSIONS:
        # 视频：只抽音轨
        _run(
            [
                "ffmpeg", "-y", "-i", str(src.resolve()),
                "-vn", "-ac", "1", "-ar", "16000",
                "-f", target_format, out,
            ]
        )
    else:
        # 其他音频格式：转码为单声道 16k（ASR 友好）
        _run(
            [
                "ffmpeg", "-y", "-i", str(src.resolve()),
                "-ac", "1", "-ar", "16000",
                "-f", target_format, out,
            ]
        )
    return out, True


def cleanup_temp(path: str) -> None:
    """删除临时文件（静默失败）。"""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass
