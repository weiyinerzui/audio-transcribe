# audio-transcribe

> 通用 **Agent Skill**：把**本地音视频**或**网页链接**转成带时间戳的字幕（SRT）或干净文稿（TXT/JSON），并自动清理口误、语气词、重复卡顿和语音识别错字。

遵循 [Agent Skills](https://agentskills.io) 开放标准，**Claude Code / Codex / OpenCode / pi** 等任意支持 skills 的 agent 都能直接用；不依赖 agent 也能当普通 Python 脚本跑。

**免费开箱即用**：默认走 B 站必剪（bcut）云转录接口，不需要任何 API key，本地 ASR 模型一个都不跑。

```
本地文件 / 网页链接
   │
   ▼
┌──────────────┐   ┌────────────────────────────┐   ┌─────────────────────┐
│  取音频       │──▶│  云转录（免费，毫秒级时间戳） │──▶│  文稿清理（可选）      │
│  ffmpeg 抽音轨 │   │  bcut / jianying / 硅基流动  │   │  LLM 或本地正则       │
│  B站直连 API  │   │  自动挑选可用通道            │   │  时间轴严格保留       │
│  yt-dlp 兜底  │   └────────────────────────────┘   └──────────┬──────────┘
└──────────────┘                                               │
                                                               ▼
                                        ┌──────────────────────────────────┐
                                        │  .srt 字幕 / .txt 文稿 / .json 时间轴 │
                                        └──────────────────────────────────┘
```

## 为什么用它

| 特性 | 说明 |
|---|---|
| **零配置可用** | 不配任何 key：bcut（B 站必剪自家接口，免登录）直接跑，中文识别准，时间戳精确到毫秒 |
| **三个引擎可换** | 免费 bcut / 免费 jianying（剪映，尽力而为）/ 硅基流动 Qwen3-ASR（要 key，纯文稿） |
| **链接直出文稿** | B站（含分P）、微博、YouTube 开箱即用；抖音/小红书/快手支持 Cookie 注入 |
| **默认清理文稿** | 自动删「嗯/啊/那个/就是」、折叠「今天今天→今天」、修同音错字，**SRT 时间轴原样保留** |
| **清理不联网也行** | `--clean rules` 纯本地正则，不调用任何 LLM；LLM 网关挂了也自动降级，不会让整次转录失败 |
| **不跑本地模型** | 转录全部走云端免费接口，无 GPU 的笔记本、NAS、树莓派都能处理长音频 |
| **跨 agent 通用** | 标准 SKILL.md frontmatter，装到各家 skills 目录即可识别，无需改代码 |
| **可脚本串联** | `stdout` 只输出最终文件路径，进度与警告走 `stderr`，方便塞进 pipeline |

## 环境要求

所有 agent 通用，先装好这些：

- Python **3.10+**
- 系统命令行工具：`ffmpeg`、`ffprobe`（必需）、`yt-dlp`（下链接时需要）
- Python 包：`requests`；B 站链接另需 `bilibili-api-python`

```bash
# 系统工具（按平台选一）
#   macOS:          brew install ffmpeg && pip install -U yt-dlp
#   Debian/Ubuntu:  sudo apt install ffmpeg && pip install -U yt-dlp
#   Arch:           sudo pacman -S ffmpeg && pip install -U yt-dlp
#   Windows:        winget install Gyan.FFmpeg && pip install -U yt-dlp
```

## 安装到各家 agent

skill 本质是一个含 `SKILL.md` 的目录，**不同 agent 只是「放在哪」不一样**。下面任选其一，
装好后重启 agent（或开新会话）即自动生效。

### Claude Code

个人级（所有项目可用）：

```bash
mkdir -p ~/.claude/skills
git clone https://github.com/weiyinerzui/audio-transcribe ~/.claude/skills/audio-transcribe
pip install -r ~/.claude/skills/audio-transcribe/requirements.txt
```

项目级（跟着仓库走，团队共享）：

```bash
mkdir -p .claude/skills
git clone https://github.com/weiyinerzui/audio-transcribe .claude/skills/audio-transcribe
pip install -r .claude/skills/audio-transcribe/requirements.txt
```

用法：直接把文件或链接丢给 Claude ——「把 `会议.mp4` 转成字幕」「转录这个 B 站视频：https://...」；
也可以显式 `/audio-transcribe <文件或链接>`。

> Claude Code 会把目录名作为命令名，所以**目录名必须叫 `audio-transcribe`**，别改成别的。

### Codex

```bash
# 个人级（Codex 读取 ~/.agents/skills 与 $CODEX_HOME/skills，默认 CODEX_HOME=~/.codex）
mkdir -p ~/.agents/skills
git clone https://github.com/weiyinerzui/audio-transcribe ~/.agents/skills/audio-transcribe
pip install -r ~/.agents/skills/audio-transcribe/requirements.txt
```

想装到 Codex 自己的 skills 目录也等价：

```bash
mkdir -p ~/.codex/skills
git clone https://github.com/weiyinerzui/audio-transcribe ~/.codex/skills/audio-transcribe
```

用法：`codex` 里直接说「用 audio-transcribe 转录 会议.mp4」，或让它 `$audio-transcribe`。

### OpenCode

个人级：

```bash
mkdir -p ~/.config/opencode/skills
git clone https://github.com/weiyinerzui/audio-transcribe ~/.config/opencode/skills/audio-transcribe
pip install -r ~/.config/opencode/skills/audio-transcribe/requirements.txt
```

项目级：

```bash
mkdir -p .opencode/skills
git clone https://github.com/weiyinerzui/audio-transcribe .opencode/skills/audio-transcribe
```

> OpenCode 也会读 Claude 兼容路径（`~/.claude/skills/`、`.claude/skills/`）和通用路径
> （`~/.agents/skills/`、`.agents/skills/`）。已经给 Claude Code 装过的话，OpenCode **可以直接复用，不用装第二份**。

### pi

```bash
git clone https://github.com/weiyinerzui/audio-transcribe ~/.pi/agent/skills/audio-transcribe
pip install -r ~/.pi/agent/skills/audio-transcribe/requirements.txt
```

### 其它 agent / 不对接 agent

只要它支持 Agent Skills 标准，把仓库 clone 到它的 `skills/` 目录即可。若路径不标准，直接软链过去：

```bash
ln -s /path/to/audio-transcribe ~/.your-agent/skills/audio-transcribe
```

**完全不接 agent 也完全可用** —— 这本来就是一套普通 Python 脚本：

```bash
git clone https://github.com/weiyinerzui/audio-transcribe && cd audio-transcribe
pip install -r requirements.txt
python3 transcribe.py 视频.mp4 --engine bcut -o 字幕.srt
```

### 一次装好，多 agent 共用

不想在多处重复 clone？装一份，其余软链过去：

```bash
git clone https://github.com/weiyinerzui/audio-transcribe ~/skills/audio-transcribe
pip install -r ~/skills/audio-transcribe/requirements.txt

mkdir -p ~/.claude/skills ~/.agents/skills ~/.config/opencode/skills ~/.pi/agent/skills
ln -s ~/skills/audio-transcribe ~/.claude/skills/audio-transcribe
ln -s ~/skills/audio-transcribe ~/.agents/skills/audio-transcribe          # Codex / OpenCode 通用路径
ln -s ~/skills/audio-transcribe ~/.config/opencode/skills/audio-transcribe
ln -s ~/skills/audio-transcribe ~/.pi/agent/skills/audio-transcribe
```

## 快速上手

不论用哪个 agent，底层都是同一个命令：

```bash
# 最常用：视频 → 带时间戳的中文字幕（免费，无需 key）
python3 transcribe.py 视频.mp4 --engine bcut -o 字幕.srt

# 音频 → 纯文稿
python3 transcribe.py 录音.mp3 --engine bcut -o 文稿.txt

# B站链接直接转录（支持 ?p=3 分P）
python3 transcribe.py "https://www.bilibili.com/video/BV1xx411c7XX" --engine bcut -o 文稿.txt

# 完全离线：不调用任何 LLM，只用本地正则清理
python3 transcribe.py 音频.mp3 --engine bcut --clean rules -o 字幕.srt

# 不清理，要原始识别结果
python3 transcribe.py 音频.mp3 --engine bcut --clean none -o 原始.srt
```

链接输入时不指定 `-o`，自动命名为 `<平台>_<时间戳>.srt`。

## 命令行参数

```
python3 transcribe.py <输入文件或链接> --engine {bcut,jianying,siliconflow}
        [-o 输出文件] [--format srt|json|txt] [--clean llm|rules|none]
        [--model ASR模型] [--clean-model 清理模型] [--clean-base-url 网关地址]
```

| 参数 | 说明 |
|---|---|
| `input` | 本地音视频文件路径，或网页链接 |
| `--engine` | **必填**：`bcut`（默认推荐）/ `jianying` / `siliconflow` |
| `-o, --output` | 输出路径，格式按扩展名自动推断 |
| `--format` | 显式指定 `srt` / `json` / `txt` |
| `--clean` | 清理方式：`llm`（默认）/ `rules`（本地正则，免费）/ `none` |
| `--model` | ASR 模型名（siliconflow 默认 `Qwen/Qwen3-ASR-1.7B`） |
| `--clean-model` | 清理用的 LLM 模型（覆盖环境变量） |
| `--clean-base-url` | 清理用的 OpenAI 兼容网关（覆盖环境变量） |

## 转录引擎

| 引擎 | 鉴权 | 时间戳 | 可输出 | 说明 |
|---|---|---|---|---|
| `bcut` | 无需 key（免费） | ✅ | srt / json / txt | B 站必剪接口，**默认首选**，稳定、中文准 |
| `jianying` | 无需 key（免费） | ✅ | srt / json / txt | 剪映接口，**尽力而为**：依赖第三方签名服务 `asrtools-update.bkfeng.top`，可能随时失效 |
| `siliconflow` | `SILICONFLOW_API_KEY` | ❌ | txt / json | `Qwen/Qwen3-ASR-1.7B`，接口只返回整段文稿，**出不了 SRT** |

> 无时间戳的组合会自动降级为 `txt` 并提示，不会静默产出坏字幕。

## 支持的链接平台

| 平台 | 免登录 | 说明 |
|---|---|---|
| B站 bilibili.com / b23.tv | ✅ | 走 bilibili-api 取 DASH 音频流，支持分P |
| 微博 weibo.com | ✅ 通常 | 走 yt-dlp |
| YouTube 等 | ✅ 视连通性 | 走 yt-dlp |
| 抖音 douyin.com / v.douyin.com | ⚠️ 建议 Cookie | 无 Cookie 时常报 "Fresh cookies are needed" |
| 小红书 xiaohongshu.com / xhslink.com | ❌ 需 Cookie | 仅笔记内含视频时可下载 |
| 快手 kuaishou.com / v.kuaishou.com | ❌ 需 Cookie | |
| 微信视频号 | ❌ 不支持 | yt-dlp 无该平台支持，需抓包拿直链 |

需要登录态的站点，把浏览器里该站点的完整 Cookie 塞进环境变量即可：

```bash
export XHS_COOKIE="a1=...; web_session=..."
export DOUYIN_COOKIE="ttwid=...; odin_tt=..."
export KUAISHOU_COOKIE="kuaishou.la_gk=..."
python3 transcribe.py "https://www.xiaohongshu.com/explore/xxxx" --engine bcut -o 笔记.txt
```

> **Cookie 等同于登录态。** 本项目只从环境变量读取，不写文件、不打日志。请勿把它提交进 git。

## 文稿清理

转录结果不会原样丢给你。默认 `--clean llm`，用任意 OpenAI 兼容的 LLM 做校对：

- 删除口误、口头禅、语气词（嗯 / 啊 / 呃 / 那个 / 就是 / 对吧…）
- 折叠无意义重复（「今天今天」→「今天」、「一下下」→「一下」）
- 修正明显的同音错别字
- **严格不改原意、不翻译、不总结**；有时间戳时逐行编号清理，时间轴 100% 保留，纯语气词行丢弃后序号自动重排

| 模式 | 联网 | 时间轴 | 适用 |
|---|---|---|---|
| `llm`（默认） | 是 | ✅ 保留 | 质量优先，跨句纠错更强 |
| `rules` | 否 | ✅ 保留 | 免费、离线、无隐私顾虑 |
| `none` | 否 | ✅ 保留 | 要原始识别结果做对比 |

**清理网关不配置也不会报错失败** —— 自动降级为 `rules` 并打一条警告。

### 配置清理网关（可选）

清理功能支持任何 OpenAI 兼容端点（OpenAI、DeepSeek、硅基流动、本地 Ollama、one-api 等）：

```bash
export NINEROUTER_BASE_URL="https://api.deepseek.com/v1"
export NINEROUTER_MODEL="deepseek-chat"
export NINEROUTER_API_KEY="sk-xxxxxxxx"
```

也可以用参数覆盖：`--clean-base-url` / `--clean-model`。

> 变量名沿用 `NINEROUTER_*` 是历史原因，语义就是「清理用的 LLM 网关」，与具体服务商无关。

**给 agent 用时**：环境变量要在 agent 进程的环境里生效（写进 shell 的 `~/.bashrc` / `~/.zshrc`，
或写进 agent 自己的配置/env 文件）。否则 agent 调用脚本时读不到，会退化成 `rules` 清理 —— 功能正常，只是没 LLM 润色。

## 输出格式

| 扩展名 | 内容 |
|---|---|
| `.srt` | 标准字幕，需要引擎提供时间戳，可直接挂进 VLC / PotPlayer / 剪辑软件 |
| `.txt` | 纯文稿 |
| `.json` | `{source, has_timing, text, segments[{text, start_ms, end_ms}]}` |

格式由 `-o` 扩展名决定，也可 `--format` 显式指定。`stdout` 只输出最终文件路径，进度和警告走 `stderr`：

```bash
python3 transcribe.py 视频.mp4 --engine bcut -o 字幕.srt 2>/dev/null   # 只拿路径
```

## 项目结构

```
audio-transcribe/
├── SKILL.md              # agent 行为说明书：路由表、各步骤规则、踩坑记录
├── README.md
├── LICENSE               # MIT
├── requirements.txt
├── transcribe.py         # 统一 CLI 入口（参数解析、流程编排、输出兜底）
├── audio.py              # ffmpeg 抽音轨 / 转码成 16k 单声道
├── downloader.py         # 多平台链接 → 本地音频（B站 API + yt-dlp 分层）
├── cleaner.py            # 文稿清理（LLM 逐段编号 / 本地正则 / 跳过）
├── subtitle.py           # Segment / Transcript 数据模型与 SRT/JSON/TXT 导出
└── engines/
    ├── base.py           # 引擎接口约定
    ├── bcut.py           # B 站必剪免费接口（移植 VideoCaptioner 的 BcutASR）
    ├── jianying.py       # 剪映接口（依赖第三方签名服务）
    └── siliconflow.py    # 硅基流动 Qwen3-ASR
```

想加新引擎？实现一个 `BaseEngine` 子类，提供 `transcribe(audio_path) -> Transcript`，在 `engines/__init__.py` 的 `ENGINES` 注册表里登记一行即可。

## 常见问题

**Q：agent 说找不到 / 不触发这个 skill？**
先确认目录名是 `audio-transcribe`（Claude Code 用它当命令名），再确认 `SKILL.md` 在目录**里面**而不是变成了 `SKILL.md.txt`。然后重启会话 —— skill 是启动时扫描的。

**Q：agent 里能转录，但清理没生效？**
agent 进程多半读不到你 shell 里 `export` 的 `NINEROUTER_*` 变量，于是降级成 `rules`。写进 shell 启动文件后重开会话，或改用 `--clean-base-url` / `--clean-model` 参数显式传。

**Q：bcut 提示额度触顶？**
B 站对免费接口有滑动窗口限流，等一会儿重置即可；或换 `--engine jianying`；或改用 `siliconflow`（要 key）。

**Q：剪映引擎报签名失败？**
`jianying` 依赖第三方签名服务，接口随时可能变。这是预期内的「尽力而为」通道，失败就回退 `bcut`。

**Q：转录里有错别字？**
同音字/专有名词是 ASR 通病。开 `--clean llm` 能修掉大部分；专有名词密集的内容建议转录后人工过一遍，或用 `--clean-model` 换更强的模型。

**Q：想同时要 SRT 和文稿？**
先出 `.json`（既有 `text` 又有 `segments`），再自己转任意格式；或者直接跑两次。

**Q：小红书/抖音下不动？**
这两个站强依赖登录 Cookie，且会过期。重新从浏览器复制一次完整 Cookie；小红书还要确认笔记里确实含视频。

**Q：Windows 能用吗？**
能。装好 `ffmpeg` 与 Python 依赖后 `python transcribe.py ...`；路径有空格记得加引号。Cookie/环境变量用 `set X=...`（cmd）或 `$env:X="..."`（PowerShell）。

**Q：yt-dlp 报签名错误？**
先 `pip install -U yt-dlp`。各平台签名算法会持续更新，升级通常能解决。

**Q：微信视频号支持吗？**
不支持，yt-dlp 没有该平台实现。需先用 res-downloader 之类的抓包工具拿到直链，再把直链当输入传进来。

## 隐私说明

- 音频会**上传到所选引擎的云接口**（bcut 为 B 站、siliconflow 为硅基流动）。涉及敏感内容请勿使用云引擎。
- `--clean rules` / `none` 不联网；`--clean llm` 只把**文本**发给你自己配置的网关。
- Cookie 与 API key 只从环境变量读取，代码里不含任何密钥，也不会把它们写进日志。

## 致谢

- 转录协议移植自 [VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner)：bcut / jianying 引擎的 HTTP 协议逻辑来自其 `BcutASR`、`JianYingASR` 实现
- 必剪 Bcut 与剪映的免费云转录接口
- [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)、[yt-dlp](https://github.com/yt-dlp/yt-dlp)
- 硅基流动 [Qwen3-ASR](https://siliconflow.cn)

## License

[MIT](LICENSE)
