"""流程编排：CLI 与 Web 共用的唯一转录流水线。

把 transcribe.py 里原先内联的「取音频 → 转录 → 清理 → 导出」抽成
可调用函数，两侧共用同一份实现，避免 CLI 和 Web 行为漂移。

设计约束：
  - 不 print 到 stdout（stdout 只留给最终路径，由 CLI 自己打）
  - 所有进度通过 on_event 回调上报，默认实现写 stderr（保持 CLI 原样）
  - 支持协作式取消：should_cancel() 在每个可中断边界被调用
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from engines import create_engine
import audio
import cleaner
import downloader
from subtitle import Transcript

EventFn = Callable[[dict], None]
CancelFn = Callable[[], bool]


class Cancelled(RuntimeError):
    """用户主动取消。"""


# ---------------------------------------------------------------- 事件


class EventBus:
    """把进度事件统一送出去。

    CLI 用默认实现（写 stderr，保持与旧版一致的文案）；
    Web 传自定义 on_event 转成 SSE。
    """

    def __init__(self, on_event: EventFn | None = None):
        self._on_event = on_event or self._stderr

    @staticmethod
    def _stderr(ev: dict) -> None:
        msg = ev.get("msg")
        if msg:
            print(msg, file=sys.stderr)

    def emit(self, stage: str, msg: str = "", **extra: Any) -> None:
        self._on_event({"stage": stage, "msg": msg, **extra})


# ---------------------------------------------------------------- 配置


@dataclass
class PipelineConfig:
    """一次转录任务的全部输入参数。"""

    source: str                      # 本地文件路径 或 网页链接
    engine: str                      # bcut / jianying / siliconflow / openai-asr / faster-whisper / mlx
    output: str | None = None
    fmt: str | None = None           # srt / json / txt，None 表示按扩展名推断
    clean: str = "llm"               # llm / rules / none
    asr_model: str | None = None
    clean_model: str | None = None
    clean_base_url: str | None = None
    clean_api_key: str | None = None
    # 引擎级额外参数（本地模型路径、openai-asr 的 base_url/key 等）
    engine_kwargs: dict = field(default_factory=dict)
    # cookies（仅内存传递，绝不落盘）
    cookies: dict = field(default_factory=dict)


@dataclass
class PipelineResult:
    transcript: Transcript
    output_path: str
    has_timing: bool
    degraded: bool = False           # 清理是否降级为 rules
    degrade_reason: str = ""
    elapsed: float = 0.0
    used_engine: str = ""
    download_platform: str = ""


# ---------------------------------------------------------------- 工具


def is_link(source: str) -> bool:
    return downloader.is_url(source)


def resolve_output(cfg: PipelineConfig) -> tuple[str, str]:
    """决定输出路径与格式，规则与 CLI 完全一致。"""
    fmt = cfg.fmt
    src = cfg.source

    if cfg.output:
        out = cfg.output
        if fmt is None:
            suffix = os.path.splitext(out)[1].lower().lstrip(".")
            if suffix in ("srt", "json", "txt"):
                fmt = suffix
            else:
                fmt = "srt"
                out = out + ".srt"
        return out, fmt

    # 无输出路径：链接输入用「平台_时间」，本地文件用原文件名
    if is_link(src):
        plat = downloader.detect_platform(src)
        stem = f"{plat}_{time.strftime('%Y%m%d_%H%M%S')}"
    else:
        stem = os.path.splitext(os.path.basename(src))[0]

    if fmt is None:
        fmt = "srt"
    return f"{stem}.{fmt}", fmt


def engine_has_timing(engine: str) -> bool:
    """siliconflow 与本地/通用 ASR 引擎是否有时间戳。

    bcut / jianying 逐句给时间；siliconflow 只给整段文本。
    openai-asr 视 response_format 而定，默认 verbose_json 有时间戳；
    faster-whisper / mlx 都有。
    """
    return engine != "siliconflow"


# ---------------------------------------------------------------- 主流程


def run(cfg: PipelineConfig,
        on_event: EventFn | None = None,
        should_cancel: CancelFn | None = None) -> PipelineResult:
    """执行一次完整转录。异常向上抛，由调用方决定如何呈现。"""
    bus = EventBus(on_event)
    cancel = should_cancel or (lambda: False)
    t0 = time.time()

    def checkpoint():
        if cancel():
            raise Cancelled("任务已取消")

    src = cfg.source
    link = is_link(src)
    if not link and not os.path.exists(src):
        raise FileNotFoundError(f"文件不存在: {src}")

    output_path, fmt = resolve_output(cfg)

    # siliconflow 无时间戳，不能出 srt
    if not engine_has_timing(cfg.engine) and fmt == "srt":
        bus.emit("warn", "提示：该引擎无时间戳，已改用 txt 输出")
        fmt = "txt"
        output_path = os.path.splitext(output_path)[0] + ".txt"

    work_audio = None
    is_temp = False
    dl_file = None
    platform = ""

    try:
        # ---- 1) 取音频 ----
        checkpoint()
        if link:
            platform = downloader.detect_platform(src)
            bus.emit("download", f"[1/3] 下载音频: {src}", platform=platform)
            # Cookie 以环境变量方式临时注入（downloader 只认环境变量）
            _apply_cookies(cfg.cookies)
            dl_file = downloader.download_audio(src)
            checkpoint()
            bus.emit("audio", "[1/3] 抽取音轨…")
            work_audio, is_temp = audio.prepare_audio(dl_file, target_format="mp3")
        else:
            bus.emit("audio", f"[1/3] 准备音频: {src}")
            work_audio, is_temp = audio.prepare_audio(src, target_format="mp3")

        checkpoint()

        # ---- 2) 转录 ----
        bus.emit("transcribe", f"[2/3] 转录中 (engine={cfg.engine}) …", engine=cfg.engine)
        kwargs = dict(cfg.engine_kwargs)
        if cfg.asr_model:
            kwargs["model"] = cfg.asr_model
        engine = create_engine(cfg.engine, **kwargs)
        transcript = engine.transcribe(work_audio)

        if not transcript.segments:
            raise RuntimeError("转录结果为空")

        checkpoint()

        # ---- 3) 清理 ----
        degraded = False
        degrade_reason = ""
        if cfg.clean == "none":
            bus.emit("clean_skip", "[3/3] 跳过清理")
        else:
            bus.emit("clean", f"[3/3] 清理文稿 (mode={cfg.clean}) …", mode=cfg.clean)
            clean_kwargs: dict[str, Any] = {}
            if cfg.clean_model:
                clean_kwargs["model"] = cfg.clean_model
            if cfg.clean_base_url:
                clean_kwargs["base_url"] = cfg.clean_base_url
            if cfg.clean_api_key:
                clean_kwargs["api_key"] = cfg.clean_api_key

            # 清理进度：把 chunk 级回调接进 bus
            def _clean_progress(current: int, total: int, label: str):
                checkpoint()
                bus.emit("clean_progress", f"清理第 {current}/{total} {label}",
                         current=current, total=total)

            try:
                transcript = cleaner.clean_transcript(
                    transcript, mode=cfg.clean,
                    fulltext=(fmt in ("txt", "json") and cfg.clean == "llm"),
                    on_progress=_clean_progress,
                    **clean_kwargs,
                )
            except Cancelled:
                raise
            except Exception as exc:  # 兜底：绝不因清理失败丢掉转录结果
                degraded = True
                degrade_reason = str(exc)
                bus.emit("warn", f"清理失败（{exc}），已降级为本地规则清理")
                transcript = cleaner.clean_transcript(transcript, mode="rules")

        checkpoint()

        # ---- 4) 导出 ----
        bus.emit("save", "正在写出结果…")
        transcript.save(output_path)
        elapsed = time.time() - t0
        bus.emit("done", f"完成：{output_path}", output=output_path,
                 elapsed=round(elapsed, 1), degraded=degraded)

        return PipelineResult(
            transcript=transcript,
            output_path=output_path,
            has_timing=transcript.has_timing,
            degraded=degraded,
            degrade_reason=degrade_reason,
            elapsed=round(elapsed, 1),
            used_engine=cfg.engine,
            download_platform=platform,
        )

    finally:
        if work_audio and is_temp:
            audio.cleanup_temp(work_audio)
        if dl_file:
            shutil.rmtree(os.path.dirname(dl_file), ignore_errors=True)
        _restore_cookies(cfg.cookies)


# ---------------------------------------------------------------- Cookie

_COOKIE_ENV = {"xiaohongshu": "XHS_COOKIE", "douyin": "DOUYIN_COOKIE",
               "kuaishou": "KUAISHOU_COOKIE"}
_cookie_backup: dict[str, str | None] = {}


def _apply_cookies(cookies: dict) -> None:
    """把调用方传入的 Cookie 临时放进环境变量。

    downloader 只从环境变量读 Cookie（这是它的安全设计，避免落盘）。
    Web 端在内存里持有，用完立刻还原，绝不写入任何文件。
    """
    _cookie_backup.clear()
    for plat, value in (cookies or {}).items():
        env = _COOKIE_ENV.get(plat)
        if not env or not value:
            continue
        _cookie_backup[env] = os.environ.get(env)
        os.environ[env] = value


def _restore_cookies(cookies: dict) -> None:
    for env, old in _cookie_backup.items():
        if old is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = old
    _cookie_backup.clear()
