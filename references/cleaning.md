# 文稿清理

默认 `--clean llm`。转录结果不会原样输出，而是经 LLM 校对：
删除语气词（嗯/啊/那个/就是/对吧…）、折叠重复卡顿（今天今天→今天）、修正明显同音错字。

**严格不改原意、不翻译、不总结。**

## 三种模式

| 模式 | 联网 | 时间轴 | 适用 |
|---|---|---|---|
| `llm`（默认） | 是 | ✅ 保留 | 质量优先，跨句纠错更强 |
| `rules` | 否 | ✅ 保留 | 免费、离线、无隐私顾虑 |
| `none` | 否 | ✅ 保留 | 要原始识别结果做对比 |

- **有时间戳**（bcut/jianying）：分批编号逐行清理，**完整保留 SRT 时间轴**，
  纯语气词行被丢弃后序号自动重排
- **无时间戳**（siliconflow）：按段落分块整篇润色

## 配置清理网关（可选）

支持任何 OpenAI 兼容端点（OpenAI、DeepSeek、硅基流动、本地 Ollama、one-api 等）：

```bash
export NINEROUTER_BASE_URL="https://api.deepseek.com/v1"
export NINEROUTER_MODEL="deepseek-chat"
export NINEROUTER_API_KEY="sk-xxxxxxxx"
```

也可用参数覆盖：`--clean-base-url` / `--clean-model`（优先级高于环境变量）。

> 变量名沿用 `NINEROUTER_*` 是历史原因，语义就是「清理用的 LLM 网关」，与具体服务商无关。

**未配置时自动降级 `rules`**，不会报错失败，也不会把文稿发往任何第三方主机。

**agent 环境提醒**：环境变量必须在启动 agent 的进程里生效（写进 `~/.bashrc` / `~/.zshrc`
或 agent 自己的 env 配置）。仅在子 shell 里 export 会读不到，此时自动降级 `rules`。

## 长音频稳定性

1 小时以上音频的实测结论：不会因为模型慢而超时降级。

| 机制 | 说明 |
|---|---|
| **流式请求** | 超时判定的是「相邻数据块间隔」（默认 120s），不是整段生成总时长 |
| **小分块** | 默认 1200 字/块，在段落 → 行 → 句子边界切，不切断语义 |
| **分块级容错** | 单块失败只保留该块原文，其他块照常；**全部**失败才降级 `rules` |
| **预算递增** | 输出为空或被截断（`finish_reason=length`）时自动加大 `max_tokens` 重试 |

实测 51.7 MB / 约 1 小时视频（2.9 万字，切成 25 块）：全部成功、零降级、零乱码。

可调参数（一般不用动）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `NINEROUTER_CLEAN_CHUNK` | `1200` | 整篇润色每块字符上限，越小越稳 |
| `NINEROUTER_CLEAN_BATCH` | `30` | 带时间戳时每批字幕段数 |
| `NINEROUTER_CLEAN_READ_TIMEOUT` | `120` | 流式相邻数据块间隔超时（秒） |

## 环境变量总表

| 变量 | 用途 | 必需性 |
|------|------|--------|
| `SILICONFLOW_API_KEY` | 硅基流动转录 | 仅 `--engine siliconflow` |
| `NINEROUTER_BASE_URL` | 清理网关 base URL | 想用 LLM 清理时必填 |
| `NINEROUTER_MODEL` | 清理模型名 | 想用 LLM 清理时必填 |
| `NINEROUTER_API_KEY` | 清理网关密钥 | 想用 LLM 清理时必填 |
| `XHS_COOKIE` | 小红书登录 Cookie | 小红书链接 |
| `DOUYIN_COOKIE` | 抖音登录 Cookie | 抖音链接建议 |
| `KUAISHOU_COOKIE` | 快手登录 Cookie | 快手链接 |

`bcut` 与 `jianying` 完全免费，不需要任何 key。不想联网就用 `--clean rules`。
