---
name: audio-transcribe
description: 音频/视频转录为字幕或纯文稿，并自动清理口误、语气词、重复和语音识别错误。集成免费接口 bcut（必剪）、jianying（剪映）和硅基流动 Qwen3-ASR，支持接入任意 OpenAI 兼容网关做 LLM 文稿清理，未配置时自动降级为本地正则清理。支持本地文件与 B站/微博/YouTube/抖音/小红书/快手 链接。Use when the user wants to transcribe audio/video into subtitles (SRT), get a cleaned transcript (TXT/JSON), extract speech from a recording, or remove filler words and ASR errors from a transcription. Supports Chinese and English.
license: MIT
compatibility: 需要 python3 (3.10+)、ffmpeg/ffprobe，链接输入另需 yt-dlp；适用于 Claude Code / Codex / OpenCode / pi 等支持 Agent Skills 的 agent，也可直接当 Python 脚本运行。
metadata:
  version: "1.2.0"
  language: zh-CN
  category: media
  repository: https://github.com/weiyinerzui/audio-transcribe
---

# 音频转录（audio-transcribe）

把**本地音视频**或**网页链接**转成字幕（SRT）或纯文稿（TXT/JSON），并清理口误、语气词、
重复卡顿和语音识别错字。转录协议移植自 [VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner)。

## 核心用法

只有一条命令。`<skill-dir>` 是本文件所在目录，**必须用绝对路径**（脚本自己处理 import，可在任意 cwd 运行）：

```bash
python3 <skill-dir>/transcribe.py <输入文件或链接> --engine bcut -o 输出.srt
```

常用变体：

| 目的 | 命令 |
|---|---|
| 带时间戳字幕（最常用） | `--engine bcut -o 字幕.srt` |
| 纯文稿 | `--engine bcut --format txt -o 文稿.txt` |
| 完全离线，不调任何 LLM | `--clean rules` |
| 要原始识别结果 | `--clean none` |

三个清理模式的取舍见 [references/cleaning.md](references/cleaning.md)。

**默认 `--clean llm`**：调用任意 OpenAI 兼容网关润色。未配置网关时自动降级为本地正则清理，
不会把文稿发往第三方。详见 [references/cleaning.md](references/cleaning.md)。

首次使用先跑 `python3 <skill-dir>/transcribe.py --help` 确认参数。
依赖缺失时 `pip install -r <skill-dir>/requirements.txt`。

## 引擎选择

| 引擎 | 鉴权 | 时间戳 | 何时用 |
|---|---|---|---|
| `bcut` | 无需 key | ✅ | **默认首选**，免费稳定，走 B 站必剪接口 |
| `jianying` | 无需 key | ✅ | bcut 失败时备选；依赖第三方签名服务，**可能随时失效** |
| `siliconflow` | `SILICONFLOW_API_KEY` | ❌ | 只要纯文稿、不要时间轴时 |

需要 SRT 就必须用带时间戳的引擎。`siliconflow` 请求 SRT 会被自动降级为 txt 并提示。

## 输出

格式由 `-o` 的扩展名决定（`.srt` / `.json` / `.txt`），也可用 `--format` 显式指定。

- `.srt` — 字幕，需要引擎提供时间戳
- `.json` — `{source, has_timing, text, segments[{text,start_ms,end_ms}]}`
- `.txt` — 纯文稿

请求无时间戳组合（如 `siliconflow` + `.srt`）时会自动降级为 `txt` 并提示。

**`stdout` 只输出最终文件路径**，进度与警告全部走 stderr —— 便于脚本串联，你可以直接读取
stdout 拿到产物路径。

## 关键约束

这几条必须遵守，否则会出错或造成损失：

1. **不要删除用户的原始输入文件。** 脚本只清理自己产生的中间产物，用户给的音视频文件永远保留。
2. **Cookie 与密钥只走环境变量。** 不写入文件、不打印到日志、不写进回复正文。
   Cookie 等同于登录态，绝不能提交进 git 或展示给他人。
3. **不要假设 cwd。** 必须用 `<skill-dir>` 下的绝对路径调用 `transcribe.py`。
4. **不要因为清理失败就判定转录失败。** `--clean llm` 失败会自动降级 `rules`，
   产物依然有效；只有转录本身失败才算失败。
5. **长时间任务要如实告知。** 1 小时音频全程（下载+转录+清理）约需 30–45 分钟，
   不要中途误判为卡死。

## 按需查阅

以下内容不在主文件里展开，遇到对应场景再读：

| 场景 | 文件 |
|---|---|
| 平台链接支持、Cookie 配置、分P、视频号等 | [references/platforms.md](references/platforms.md) |
| 清理模式细节、LLM 网关配置、长音频稳定性 | [references/cleaning.md](references/cleaning.md) |
| 报错排查、已知限制、失败降级路径 | [references/troubleshooting.md](references/troubleshooting.md) |

## 结果交付

转录完成后本 skill 只做一件事：**把输出文件路径打到 stdout**。不主动发送、不上传、
不归档到任何外部服务。

需要把结果推给用户时，按你自己 agent 的环境实现即可：读取 stdout 的路径，再走自身的
文件发送能力（飞书 / Slack / Telegram 等），或把正文写进你的文档系统。

中间产物由脚本自己清干净（下载的原始音视频、转码出的临时 mp3），你只需保留 `-o` 指定的
产物。skill 目录下的代码与配置永远不要删。
