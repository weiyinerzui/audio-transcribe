---
name: audio-transcribe
description: 音频/视频转录为字幕或纯文稿，并自动清理口误、语气词、重复和语音识别错误。集成免费接口 bcut（必剪）、jianying（剪映）和硅基流动 Qwen3-ASR，支持接入任意 OpenAI 兼容网关做 LLM 文稿清理，未配置时自动降级为本地正则清理。Use when the user wants to transcribe audio/video into subtitles (SRT), get a cleaned transcript (TXT/JSON), extract speech from a recording, or remove filler words and ASR errors from a transcription. Supports Chinese and English.
allowed-tools: Bash(python3 *transcribe.py *)
---

# 音频转录（audio-transcribe）

把音频或视频转成字幕（SRT）或纯文稿（TXT/JSON），并清理口误、语气词、重复和语音识别错误。

转录协议移植自 [VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner)。

## 输入

可以直接喂**本地音视频文件**，也可以喂**网页链接**，从链接直接产出文字稿。

| 平台 | 免登录 | 输出 | 说明 |
|------|--------|------|------|
| B站 bilibili.com / b23.tv | ✅ | srt/json/txt | 走 bilibili-api 取 DASH 音频流，支持分P |
| 微博 weibo.com | ✅ 通常 | srt/json/txt | 走 yt-dlp |
| YouTube 等 | ✅ 视连通性 | srt/json/txt | 走 yt-dlp |
| 抖音 douyin.com / v.douyin.com | ⚠️ 建议 Cookie | srt/json/txt | 需 `DOUYIN_COOKIE`（yt-dlp 报 "Fresh cookies are needed"） |
| 小红书 xiaohongshu.com / xhslink.com | ❌ 需 Cookie | srt/json/txt | 需 `XHS_COOKIE`，且仅笔记含视频时可下载 |
| 快手 kuaishou.com / v.kuaishou.com | ❌ 需 Cookie | srt/json/txt | 需 `KUAISHOU_COOKIE` |
| 微信视频号 | ❌ 不支持 | — | yt-dlp 无该平台支持，需抓包工具取得直链 |

```bash
# B站：开箱即用
python3 <skill-dir>/transcribe.py "https://www.bilibili.com/video/BV1xx411c7XX" --engine bcut -o 文稿.txt

# B站分P
python3 <skill-dir>/transcribe.py "https://www.bilibili.com/video/BV1xx411c7XX?p=3" -o p3.srt --engine bcut

# 抖音 / 小红书 / 快手：先在浏览器登录，复制完整 Cookie 到环境变量
export XHS_COOKIE="a1=...; web_session=..."
export DOUYIN_COOKIE="ttwid=...; odin_tt=..."
export KUAISHOU_COOKIE="kuaishou.la_gk=..."
python3 <skill-dir>/transcribe.py "https://www.xiaohongshu.com/explore/xxxx" --engine bcut -o 笔记.txt
```

链接输入时若不指定 `-o`，自动命名为 `<平台>_<时间戳>.srt`。

**Cookie 只通过环境变量传入，不写入文件、不打印到日志、不随结论输出。** Cookie 等同于登录态，请勿提交到 git 仓库。

## 运行方式

始终用 `python3` 加本 skill 目录下的绝对路径调用（脚本自身处理 import，可在任意 cwd 运行）：

```bash
python3 <skill-dir>/transcribe.py <输入文件> --engine bcut -o 输出.srt
```

**首次使用前**先跑 `--help` 确认参数。依赖：`requests` + 系统 `ffmpeg`/`ffprobe` + `yt-dlp`；B站链接另需 `bilibili-api-python`。

## 转录引擎

| 引擎 | 鉴权 | 时间戳 | 可输出 | 说明 |
|------|------|--------|--------|------|
| `bcut` | 无需 key（免费） | ✅ | srt/json/txt | B 站必剪接口，**默认首选** |
| `jianying` | 无需 key（免费） | ✅ | srt/json/txt | 剪映接口，**尽力而为**：依赖第三方签名服务 `asrtools-update.bkfeng.top`，可能随时失效 |
| `siliconflow` | `SILICONFLOW_API_KEY` | ❌ | txt/json | `Qwen/Qwen3-ASR-1.7B`，只返回整段文稿，接口不提供时间戳 |

`bcut` 免费且稳定、带时间戳，是默认推荐。请求时加 `SILICONFLOW_API_KEY` 才能用 `siliconflow`。

## 文稿清理（默认开启，`--clean llm`）

转录结果不会原样输出，默认经任意 OpenAI 兼容网关的 LLM 清理：
删除语气词（嗯/啊/那个/就是/对吧…）、重复卡顿（今天今天→今天）、明显识别错别字。

清理网关需自行配置（`NINEROUTER_BASE_URL` / `NINEROUTER_MODEL` / `NINEROUTER_API_KEY`），
可用 OpenAI、DeepSeek、本地 Ollama 等任意兼容端点。**未配置时自动降级为 `--clean rules`**，
不会把文稿发往任何第三方主机。

- **有时间戳**（bcut/jianying）：分批编号逐行清理，**完整保留 SRT 时间轴**，纯语气词行被丢弃后序号自动重排
- **无时间戳**（siliconflow）：整篇润色
- `--clean rules`：本地正则清理，免费不联网，同样保留时间轴
- `--clean none`：不清理，输出原始识别结果
- 清理网关不可达或未配置时**自动降级为 rules**并打印警告，不会让整次转录失败

## 常用命令

```bash
# 免费转录 + 清理 + 保留时间轴字幕（最常用）
python3 <skill-dir>/transcribe.py 视频.mp4 --engine bcut -o 字幕.srt

# 完全离线清理（不调用任何 LLM）
python3 <skill-dir>/transcribe.py 音频.mp3 --engine bcut --clean rules -o 字幕.srt

# 剪映（尽力而为，接口可能失效）
python3 <skill-dir>/transcribe.py 音频.mp3 --engine jianying -o 字幕.srt

# 换成文稿形态
python3 <skill-dir>/transcribe.py 音频.mp3 --engine bcut --format txt -o 文稿.txt

# 硅基流动（纯文稿，无时间戳）
export SILICONFLOW_API_KEY="sk-..."
python3 <skill-dir>/transcribe.py 音频.mp3 --engine siliconflow -o 文稿.txt
```

## 输出格式

- `.srt` — 字幕，需要引擎提供时间戳
- `.json` — `{source, has_timing, text, segments[{text,start_ms,end_ms}]}`
- `.txt` — 纯文稿

格式由 `-o` 的扩展名决定，也可用 `--format` 显式指定。请求无时间戳组合时自动降级为 `txt` 并提示。
`stdout` 只输出最终文件路径（进度与警告走 stderr），便于脚本串联。

## 交付（可选，按需）

转录完成后默认只做一件事：**把输出文件路径打到 `stdout`**，不主动发送、不上传、归档到任何外部服务。

如果需要把结果推给用户（例如在 chat bot / agent 里运行），按你自己的通道实现即可，常见两种：

1. **直接返回文件**：调用方读取 `stdout` 的路径，再走自身的文件发送能力（飞书、Slack、Telegram 等）。
2. **写进知识库/文档**：把 `.srt` / `.txt` 内容写入你的笔记系统，正文长时用文档链接代替聊天消息。

### 临时文件清理

`transcribe.py` 自己会清干净中间产物，调用方只需注意：

| 文件 | 谁清理 |
|---|---|
| 链接下载的原始音视频（`audiotr_dl_*` 临时目录） | 脚本在 `finally` 里删除 |
| 转码出的 16k 单声道 mp3 | 脚本在 `finally` 里删除（用户原始输入**绝不删除**） |
| 你指定的 `-o` 输出文件 | 保留，这是产物 |

skill 目录下的代码与配置永远不要删。

## 环境变量

| 变量 | 用途 | 必需性 |
|------|------|--------|
| `SILICONFLOW_API_KEY` | 硅基流动转录 | 仅 `--engine siliconflow` 需要 |
| `NINEROUTER_BASE_URL` | 清理网关 base URL（任意 OpenAI 兼容端点） | 想用 LLM 清理时必填 |
| `NINEROUTER_MODEL` | 清理模型名 | 想用 LLM 清理时必填 |
| `NINEROUTER_API_KEY` | 清理网关密钥（本地 Ollama 可留空时用占位值） | 想用 LLM 清理时必填 |
| `XHS_COOKIE` | 小红书登录 Cookie | 小红书链接需要 |
| `DOUYIN_COOKIE` | 抖音登录 Cookie | 抖音链接建议提供 |
| `KUAISHOU_COOKIE` | 快手登录 Cookie | 快手链接需要 |

`bcut` 与 `jianying` 完全免费，不需要任何 key。

`--clean-model` / `--clean-base-url` 优先级高于环境变量。

示例（DeepSeek 官方 API）：

```bash
export NINEROUTER_BASE_URL="https://api.deepseek.com/v1"
export NINEROUTER_MODEL="deepseek-chat"
export NINEROUTER_API_KEY="sk-..."
```

不想联网就用 `--clean rules`，完全不调任何 LLM。

## 已知限制

- `siliconflow`（Qwen3-ASR）接口只返回整段文本，无 segments/words 时间戳，因此无法产出带时间轴的 SRT
- `jianying` 依赖第三方签名服务，不可控，失败会给出可读报错
- bcut / jianying 为免费共享接口，主要支持中文，长音频建议分段处理
- 已放弃 Groq：其 Cloudflare 边缘会拦截本机所在区域的出口 IP，与 API key 无关
- 小红书/快手强依赖登录 Cookie，Cookie 失效即失败；小红书仅笔记内含视频时可下载
- 抖音需要"新鲜 Cookie"（yt-dlp 原话），无 Cookie 时部分视频无法解析
- 各平台签名算法会随平台更新而失效，超时可先试 `yt-dlp -U` 升级
- 微信视频号 yt-dlp 不支持，需先用抓包工具（如 res-downloader）拿到直链，再把直链传给本工具
- B站音频流必须带 Referer 才能下载（脚本已处理）
