# 排查与已知限制

## 失败降级路径

先理解这个分层，能避免大部分误判：

```
转录失败 ─────────────────────▶ 整个任务失败（报错退出）
清理失败（网关不可达/未配置）──▶ 自动降级 rules，产物依然有效
单块清理失败 ─────────────────▶ 该块保留原文，其他块正常
```

**关键**：`--clean llm` 出问题不代表转录失败。产物（SRT/TXT）照常生成，只是少了 LLM 润色。
看到「已降级为本地规则清理」的警告是**正常兜底**，不是错误。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 清理没生效，输出还有语气词 | agent 进程读不到 `NINEROUTER_*` | 写进 shell 启动文件或 agent env，重开会话；或改用 `--clean-base-url` / `--clean-model` 显式传 |
| `siliconflow` 出不了 SRT | 该接口只返回整段文本，无时间戳 | 换 `--engine bcut`；或接受 `--format txt` |
| `jianying` 报签名错误 | 依赖第三方签名服务 `asrtools-update.bkfeng.top`，会失效 | 换 `--engine bcut` |
| 抖音报 "Fresh cookies are needed" | 需要新鲜 Cookie | 重新登录复制 `DOUYIN_COOKIE` |
| 小红书下载失败 | 强依赖登录 Cookie，且仅笔记含视频时可下载 | 更新 `XHS_COOKIE`；确认该笔记是视频笔记 |
| 各平台突然解析失败 | 平台签名算法更新 | 先试 `yt-dlp -U` 升级 yt-dlp |
| 长音频中途「像卡住了」 | 正常。1 小时音频全程约 30–45 分钟 | 看 stderr 的「清理第 N/M 块完成」进度，不要中断 |
| 中文输出变成乱码 | 已修复（v1.1.0 起显式按 UTF-8 解码） | 升级到最新版 |

## 已知限制

- `siliconflow`（Qwen3-ASR）只返回整段文本，无 segments/words 时间戳，无法产出 SRT
- `jianying` 依赖第三方签名服务，不可控，失败会给出可读报错
- bcut / jianying 为免费共享接口，主要支持中文，超长音频建议分段处理
- 已放弃 Groq：其 Cloudflare 边缘会拦截部分区域出口 IP，与 API key 无关
- 小红书/快手强依赖登录 Cookie，Cookie 失效即失败；小红书仅笔记含视频时可下载
- 抖音需要「新鲜 Cookie」，无 Cookie 时部分视频无法解析
- 微信视频号 yt-dlp 不支持，需先用抓包工具（如 res-downloader）拿到直链再传给本工具
- B站音频流必须带 Referer 才能下载（脚本已处理）

## 依赖

`requests` + 系统 `ffmpeg`/`ffprobe` + `yt-dlp`；B站链接另需 `bilibili-api-python`。

```bash
pip install -r <skill-dir>/requirements.txt
```

`ffmpeg`/`ffprobe` 是系统工具，不走 pip：

```bash
# macOS
brew install ffmpeg
# Debian/Ubuntu
sudo apt install ffmpeg
# Arch
sudo pacman -S ffmpeg
# Windows
winget install Gyan.FFmpeg
```
