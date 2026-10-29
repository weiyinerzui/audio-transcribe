"""模型管理：本地转录模型的下载、列出、删除。

统一存放目录：~/.audio-transcribe/models/<model_id>/
下载源：HuggingFace 官方或其镜像（默认 hf-mirror.com，国内可直连）

设计要点：
  - 模型目录是纯文件，删掉即卸载，不依赖 pip 包管理
  - 下载用 huggingface_hub 的 snapshot_download，自带断点续传
  - 每个模型声明平台支持，不支持的平台在 UI 上禁用并说明原因
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HOME = Path(os.environ.get("AUDIO_TRANSCRIBE_HOME", Path.home() / ".audio-transcribe"))
MODELS_DIR = HOME / "models"

# 默认走镜像；用户可设 HF_ENDPOINT 覆盖
DEFAULT_HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")


@dataclass
class ModelSpec:
    id: str
    name: str
    repo: str
    backend: str                 # faster-whisper / mlx
    size_mb: int
    platforms: tuple[str, ...]   # darwin / linux / win32
    arch: tuple[str, ...]        # arm64 / x86_64 / AMD64
    desc: str
    lang: str = "多语种"


# 本地模型目录：平台过滤在 available_models() 里做
MODEL_CATALOG: list[ModelSpec] = [
    ModelSpec(
        id="fw-large-v3-turbo",
        name="Whisper large-v3-turbo（faster-whisper）",
        repo="Systran/faster-whisper-large-v3-turbo",
        backend="faster-whisper",
        size_mb=1620,
        platforms=("darwin", "linux", "win32"),
        arch=("arm64", "x86_64", "AMD64"),
        desc="跨平台首选。turbo 版比 large-v3 快数倍，准确率接近。CPU 可跑。",
    ),
    ModelSpec(
        id="fw-large-v3",
        name="Whisper large-v3（faster-whisper）",
        repo="Systran/faster-whisper-large-v3",
        backend="faster-whisper",
        size_mb=3090,
        platforms=("darwin", "linux", "win32"),
        arch=("arm64", "x86_64", "AMD64"),
        desc="准确率最高，速度较慢。适合对质量要求极高的场景。",
    ),
    ModelSpec(
        id="fw-medium",
        name="Whisper medium（faster-whisper）",
        repo="Systran/faster-whisper-medium",
        backend="faster-whisper",
        size_mb=1530,
        platforms=("darwin", "linux", "win32"),
        arch=("arm64", "x86_64", "AMD64"),
        desc="体积小、速度快，适合低配机器或对准确率要求不高的场景。",
    ),
    ModelSpec(
        id="mlx-large-v3-turbo",
        name="Whisper large-v3-turbo（MLX）",
        repo="mlx-community/whisper-large-v3-turbo",
        backend="mlx",
        size_mb=1620,
        platforms=("darwin",),
        arch=("arm64",),
        desc="Apple Silicon 专用，速度最快。需要 M1 及以上芯片。",
    ),
    ModelSpec(
        id="mlx-qwen3-asr-0.6b",
        name="Qwen3-ASR 0.6B（MLX）",
        repo="mlx-community/Qwen3-ASR-0.6B-4bit",
        backend="mlx",
        size_mb=340,
        platforms=("darwin",),
        arch=("arm64",),
        desc="体积最小的本地模型，中文效果好。需要 M1 及以上芯片。",
    ),
]

BY_ID = {m.id: m for m in MODEL_CATALOG}


def _platform_ok(spec: ModelSpec) -> tuple[bool, str]:
    import platform as _p
    import sys
    sysname = sys.platform
    machine = _p.machine()
    if sysname not in spec.platforms:
        need = " / ".join(spec.platforms)
        return False, f"仅支持 {need}，当前是 {sysname}"
    if machine not in spec.arch:
        need = " / ".join(spec.arch)
        return False, f"仅支持 {need} 架构，当前是 {machine}"
    return True, ""


def model_path(model_id: str) -> Path:
    return MODELS_DIR / model_id


def is_downloaded(model_id: str) -> bool:
    """判断模型是否已完整下载：目录存在且有可用权重。"""
    d = model_path(model_id)
    if not d.is_dir():
        return False
    # 有 config.json 且至少一个权重文件才算完整（避免半截下载被误判）
    has_cfg = (d / "config.json").exists() or (d / "model.bin").exists()
    has_weight = any(
        f.suffix in (".bin", ".safetensors", ".npz") and f.stat().st_size > 1024 * 1024
        for f in d.rglob("*") if f.is_file()
    )
    return has_cfg and has_weight


def dir_size_mb(model_id: str) -> int:
    d = model_path(model_id)
    if not d.is_dir():
        return 0
    total = 0
    for f in d.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return round(total / (1024 * 1024))


def list_models() -> list[dict]:
    """列出全部模型及本机状态，供 Web 界面渲染。"""
    out = []
    for spec in MODEL_CATALOG:
        ok, why = _platform_ok(spec)
        out.append({
            "id": spec.id,
            "name": spec.name,
            "repo": spec.repo,
            "backend": spec.backend,
            "size_mb": spec.size_mb,
            "desc": spec.desc,
            "lang": spec.lang,
            "platform_ok": ok,
            "platform_reason": why,
            "downloaded": is_downloaded(spec.id),
            "actual_size_mb": dir_size_mb(spec.id),
            "path": str(model_path(spec.id)),
        })
    return out


def delete_model(model_id: str) -> bool:
    d = model_path(model_id)
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


# ---------------------------------------------------------------- 下载


class DownloadTask:
    """一个下载任务的运行时状态（内存中，不落盘）。"""

    def __init__(self, model_id: str):
        self.model_id = model_id
        self.status = "pending"        # pending / running / done / error / cancelled
        self.downloaded_mb = 0
        self.total_mb = BY_ID[model_id].size_mb if model_id in BY_ID else 0
        self.error = ""
        self.started = time.time()
        self.cancel = threading.Event()


_tasks: dict[str, DownloadTask] = {}
_lock = threading.Lock()


def get_task(model_id: str) -> DownloadTask | None:
    with _lock:
        return _tasks.get(model_id)


def cancel_download(model_id: str) -> bool:
    with _lock:
        t = _tasks.get(model_id)
    if t and t.status == "running":
        t.cancel.set()
        return True
    return False


def start_download(model_id: str,
                   on_event: Callable[[dict], None] | None = None) -> DownloadTask:
    """后台线程下载模型。同一模型重复调用返回既有任务。"""
    if model_id not in BY_ID:
        raise ValueError(f"未知模型: {model_id}")

    spec = BY_ID[model_id]
    ok, why = _platform_ok(spec)
    if not ok:
        raise ValueError(f"当前平台不支持该模型：{why}")

    with _lock:
        existing = _tasks.get(model_id)
        if existing and existing.status == "running":
            return existing
        task = DownloadTask(model_id)
        _tasks[model_id] = task

    def _run():
        task.status = "running"
        emit = on_event or (lambda ev: None)
        try:
            from huggingface_hub import snapshot_download

            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            target = model_path(model_id)

            emit({"stage": "model", "msg": f"开始下载 {spec.name}…",
                  "downloaded_mb": 0, "total_mb": spec.size_mb})

            # snapshot_download 自带断点续传；local_dir 直接落到目标目录
            snapshot_download(
                repo_id=spec.repo,
                local_dir=str(target),
                local_dir_use_symlinks=False,
                endpoint=DEFAULT_HF_ENDPOINT,
                max_workers=4,
                resume_download=True,
            )

            if task.cancel.is_set():
                task.status = "cancelled"
                emit({"stage": "model", "msg": "下载已取消", "status": "cancelled"})
                return

            task.downloaded_mb = dir_size_mb(model_id)
            task.status = "done"
            emit({"stage": "model", "msg": f"{spec.name} 下载完成",
                  "status": "done", "downloaded_mb": task.downloaded_mb})

        except Exception as exc:
            if task.cancel.is_set():
                task.status = "cancelled"
                emit({"stage": "model", "msg": "下载已取消", "status": "cancelled"})
                return
            task.status = "error"
            task.error = str(exc)[:500]
            emit({"stage": "model", "msg": f"下载失败：{task.error}",
                  "status": "error", "error": task.error})

    threading.Thread(target=_run, daemon=True).start()
    return task


def endpoint() -> str:
    return DEFAULT_HF_ENDPOINT


def set_endpoint(url: str) -> None:
    """切换下载源（huggingface.co 直连不稳时用镜像）。"""
    global DEFAULT_HF_ENDPOINT
    DEFAULT_HF_ENDPOINT = (url or "").strip().rstrip("/")
    os.environ["HF_ENDPOINT"] = DEFAULT_HF_ENDPOINT
