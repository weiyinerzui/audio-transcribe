#!/usr/bin/env python3
"""统一转录入口：audio-transcribe。

用法：
  python transcribe.py <输入音/视频> --engine bcut|jianying|siliconflow \
      [-o 输出文件] [--format srt|json|txt] [--clean llm|rules|none] \
      [--model <ASR模型>] [--clean-model <清理模型>] [--clean-base-url <网关>]

示例：
  python transcribe.py 视频.mp4 --engine bcut -o 字幕.srt
  python transcribe.py 音频.mp3 --engine siliconflow --clean llm -o 文稿.txt
"""

from __future__ import annotations

import argparse
import os
import sys

# 保证本目录及 engines 子目录可被 import（以任意 cwd 调用时）
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import audio
import cleaner
import downloader
from engines import create_engine


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="audio-transcribe", description="多引擎音频/视频转录")
    p.add_argument("input", help="输入音/视频文件路径，或 B站/小红书等网页链接")
    p.add_argument("--engine", required=True, choices=["bcut", "jianying", "siliconflow"],
                   help="转录引擎")
    p.add_argument("-o", "--output", help="输出文件路径（默认：输入名 + 格式后缀）")
    p.add_argument("--format", choices=["srt", "json", "txt"], default=None,
                   help="输出格式（默认：有输出文件时按扩展名推断，否则 srt）")
    p.add_argument("--clean", choices=["llm", "rules", "none"], default="llm",
                   help="文稿清理方式（默认 llm，用 OpenAI 兼容网关润色；未配置则自动降级为 rules）")
    p.add_argument("--model", default=None,
                   help="ASR 模型名（siliconflow 默认 Qwen/Qwen3-ASR-1.7B）")
    p.add_argument("--clean-model", default=None,
                   help="清理用 LLM 模型（也可用环境变量 NINEROUTER_MODEL）")
    p.add_argument("--clean-base-url", default=None,
                   help="清理用 OpenAI 兼容网关 base URL（也可用环境变量 NINEROUTER_BASE_URL）")
    return p.parse_args(argv)


def resolve_output(args) -> tuple[str, str]:
    fmt = args.format
    if args.output:
        out = args.output
        if fmt is None:
            suffix = os.path.splitext(out)[1].lower().lstrip(".")
            if suffix in ("srt", "json", "txt"):
                fmt = suffix
            else:
                fmt = "srt"
                out = out + ".srt"
        return out, fmt
    # 无输出：输入名 + 后缀
    stem = os.path.splitext(args.input)[0]
    if fmt is None:
        fmt = "srt"
    return f"{stem}.{fmt}", fmt


def main(argv=None) -> int:
    args = parse_args(argv)

    # 输入可以是本地文件，也可以是 B站/小红书等链接
    is_link = downloader.is_url(args.input)
    if not is_link and not os.path.exists(args.input):
        print(f"错误：文件不存在 {args.input}", file=sys.stderr)
        return 3

    # 确定输出（链接输入时无法从路径取名，默认用来源域名+时间）
    if is_link and not args.output:
        import time as _t
        plat = downloader.detect_platform(args.input)
        args.output = f"{plat}_{_t.strftime('%Y%m%d_%H%M%S')}.srt"
    output_path, fmt = resolve_output(args)

    # 兜底校验：siliconflow 无时间戳，不能出 srt
    if args.engine == "siliconflow" and fmt == "srt":
        print("提示：siliconflow 无时间戳，已改用 txt 输出", file=sys.stderr)
        fmt = "txt"
        output_path = os.path.splitext(output_path)[0] + ".txt"

    work_audio = None
    is_temp = False
    dl_file = None
    try:
        # 1) 取音频：链接先下载，本地文件直接转码
        if is_link:
            print(f"[1/3] 下载音频: {args.input}", file=sys.stderr)
            dl_file = downloader.download_audio(args.input)
            work_audio, is_temp = audio.prepare_audio(dl_file, target_format="mp3")
        else:
            print(f"[1/3] 准备音频: {args.input}", file=sys.stderr)
            work_audio, is_temp = audio.prepare_audio(args.input, target_format="mp3")

        # 转录
        print(f"[2/3] 转录中 (engine={args.engine}) ...", file=sys.stderr)
        engine_kwargs = {}
        if args.model:
            engine_kwargs["model"] = args.model
        engine = create_engine(args.engine, **engine_kwargs)
        transcript = engine.transcribe(work_audio)

        if not transcript.segments:
            print("错误：转录结果为空", file=sys.stderr)
            return 5

        # 清理：有时间戳时逐段保留时间轴，无时间戳时整篇润色
        if args.clean != "none":
            print(f"[3/3] 清理文稿 (mode={args.clean}) ...", file=sys.stderr)
            clean_kwargs = {}
            if args.clean_model:
                clean_kwargs["model"] = args.clean_model
            if args.clean_base_url:
                clean_kwargs["base_url"] = args.clean_base_url
            # 输出纯文稿（txt/json）时用整篇润色：跨句语义纠错更强
            transcript = cleaner.clean_transcript(
                transcript, mode=args.clean,
                fulltext=(fmt in ("txt", "json") and args.clean == "llm"),
                **clean_kwargs
            )
        else:
            print("[3/3] 跳过清理", file=sys.stderr)

        # 保存
        transcript.save(output_path)
        print(output_path)
        print(f"完成：{output_path}", file=sys.stderr)
        return 0

    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return 5
    finally:
        if work_audio and is_temp:
            audio.cleanup_temp(work_audio)
        if dl_file:
            import shutil
            shutil.rmtree(os.path.dirname(dl_file), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
