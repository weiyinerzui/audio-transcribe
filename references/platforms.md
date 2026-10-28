# 平台链接支持

把网页链接直接喂给 `transcribe.py`，从链接产出文字稿。本地文件不需要看这一页。

## 支持矩阵

| 平台 | 免登录 | 输出 | 说明 |
|------|--------|------|------|
| B站 bilibili.com / b23.tv | ✅ | srt/json/txt | 走 bilibili-api 取 DASH 音频流，支持分P |
| 微博 weibo.com | ✅ 通常 | srt/json/txt | 走 yt-dlp |
| YouTube 等 | ✅ 视连通性 | srt/json/txt | 走 yt-dlp |
| 抖音 douyin.com / v.douyin.com | ⚠️ 建议 Cookie | srt/json/txt | 需 `DOUYIN_COOKIE`（yt-dlp 报 "Fresh cookies are needed"） |
| 小红书 xiaohongshu.com / xhslink.com | ❌ 需 Cookie | srt/json/txt | 需 `XHS_COOKIE`，且仅笔记含视频时可下载 |
| 快手 kuaishou.com / v.kuaishou.com | ❌ 需 Cookie | srt/json/txt | 需 `KUAISHOU_COOKIE` |
| 微信视频号 | ❌ 不支持 | — | yt-dlp 无该平台支持，需抓包工具取得直链 |

## 命令示例

```bash
# B站：开箱即用
python3 <skill-dir>/transcribe.py "https://www.bilibili.com/video/BV1xx411c7XX" --engine bcut -o 文稿.txt

# B站分P
python3 <skill-dir>/transcribe.py "https://www.bilibili.com/video/BV1xx411c7XX?p=3" -o p3.srt --engine bcut

# 短视频平台：先在浏览器登录，复制完整 Cookie 到环境变量
export XHS_COOKIE="a1=...; web_session=..."
export DOUYIN_COOKIE="ttwid=...; odin_tt=..."
export KUAISHOU_COOKIE="kuaishou.la_gk=..."
python3 <skill-dir>/transcribe.py "https://www.xiaohongshu.com/explore/xxxx" --engine bcut -o 笔记.txt
```

链接输入时若不指定 `-o`，自动命名为 `<平台>_<时间戳>.srt`。

## Cookie 规矩

**Cookie 只通过环境变量传入**，不写入文件、不打印到日志、不随结论输出。
Cookie 等同于登录态，请勿提交到 git 仓库。

短链（如 `b23.tv`）会先 302 到完整地址再下载，脚本已处理，无需手动展开。
想要知道真实 BV 号时，可用 `curl -sI <短链>` 看 `location` 头。
