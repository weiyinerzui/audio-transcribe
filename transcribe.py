#!/usr/bin/env python3
"""统一转录入口：audio-transcribe。

用法：
  python transcribe.py <输入音/视频> --engine bcut|jianying|siliconflow \
      [-o 输出文件] [--format srt|json|txt] [--clean llm|rules|none] \
      [--model <ASR模型>] [--clean-model <清理模型>] [--clean-base-url <网关>]

示例：
  python transcribe.py 视频.mp4 --engine bcut -o 字幕.srt
  python transcribe.py 音频.mp3 --engine siliconflow --clean llm -o 文稿.txt

流程编排已抽到 pipeline.py，与 Web 端共用同一份实现。
本文件只负责命令行参数解析与退出码。
"""

from __future__ import annotations

import argparse
import os
import sys

# 保证本目录及 engines 子目录可被 import（以任意 cwd 调用时）
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pipeline import PipelineConfig, run

ENGINE_CHOICES = ["bcut", "jianying", "siliconflow", "openai-asr",
                  "faster-whisper", "mlx-whisper"]


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="audio-transcribe", description="多引擎音频/视频转录")
    p.add_argument("input", help="输入音/视频文件路径，或 B站/小红书等网页链接")
    p.add_argument("--engine", required=True, choices=ENGINE_CHOICES,
                   help="转录引擎（bcut/jianying 免 key；siliconflow 需 key；"
                        "openai-asr 需 OpenAI Whisper 兼容服务；"
                        "faster-whisper/mlx-whisper 为本地模型）")
    p.add_argument("-o", "--output", help="输出文件路径（默认：输入名 + 格式后缀）")
    p.add_argument("--format", choices=["srt", "json", "txt"], default=None,
                   help="输出格式（默认：有输出文件时按扩展名推断，否则 srt）")
    p.add_argument("--clean", choices=["llm", "rules", "none"], default="llm",
                   help="文稿清理方式（默认 llm，用 OpenAI 兼容网关润色；未配置则自动降级为 rules）")
    p.add_argument("--model", default=None,
                   help="ASR 模型名 / 本地模型路径")
    p.add_argument("--clean-model", default=None,
                   help="清理用 LLM 模型（也可用环境变量 NINEROUTER_MODEL）")
    p.add_argument("--clean-base-url", default=None,
                   help="清理用 OpenAI 兼容网关 base URL（也可用环境变量 NINEROUTER_BASE_URL）")
    p.add_argument("--clean-api-key", default=None,
                   help="清理用 LLM 的 API key（也可用 NINEROUTER_API_KEY / OPENAI_API_KEY）")
    p.add_argument("--asr-base-url", default=None,
                   help="openai-asr 引擎的 base URL")
    p.add_argument("--asr-api-key", default=None,
                   help="openai-asr 引擎的 API key")
    p.add_argument("--local-model", default=None,
                   help="本地模型目录（faster-whisper / mlx-whisper）")
    p.add_argument("--device", default=None, help="本地模型运行设备（cpu/cuda/mps，可选）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    engine_kwargs = {}
    if args.asr_base_url:
        engine_kwargs["base_url"] = args.asr_base_url
    if args.asr_api_key:
        engine_kwargs["api_key"] = args.asr_api_key
    if args.local_model:
        engine_kwargs["model_dir"] = args.local_model
    if args.device:
        engine_kwargs["device"] = args.device

    cfg = PipelineConfig(
        source=args.input,
        engine=args.engine,
        output=args.output,
        fmt=args.format,
        clean=args.clean,
        asr_model=args.model,
        clean_model=args.clean_model,
        clean_base_url=args.clean_base_url,
        clean_api_key=args.clean_api_key,
        engine_kwargs=engine_kwargs,
    )

    try:
        result = run(cfg)
    except FileNotFoundError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return 5

    # stdout 只输出最终路径，方便脚本取值
    print(result.output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
