"""多平台链接 → 本地音频。

分层策略（详见 SKILL.md）：
  - B站：走 bilibili-api 取 DASH 音频流，免登录、稳定
  - 小红书 / 抖音 / 快手 / 微博 / 其他：走 yt-dlp（社区持续维护各平台签名），
    受限平台通过环境变量提供 Cookie
  - 微信视频号：yt-dlp 不支持，明确报错并给出替代方案

Cookie 统一通过环境变量注入，不写入任何文件，也不落盘到日志。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
BILI_REFERER = "https://www.bilibili.com"


class DownloadError(RuntimeError):
    """下载失败，message 面向用户可读。"""


# ---------------------------------------------------------------- 平台路由

# hosts: 命中即选该平台；cookie_env: 该平台可用的 Cookie 环境变量
PLATFORMS = {
    "bilibili": {
        "hosts": ("bilibili.com", "b23.tv"),
        "cookie_env": None,          # 免登录
        "referer": "https://www.bilibili.com/",
        "note": "B站免登录即可下载",
    },
    "xiaohongshu": {
        "hosts": ("xiaohongshu.com", "xhslink.com"),
        "cookie_env": "XHS_COOKIE",
        "referer": "https://www.xiaohongshu.com/",
        "note": "小红书需登录 Cookie，且仅笔记内含视频时可下载",
    },
    "douyin": {
        "hosts": ("douyin.com", "iesdouyin.com"),
        "cookie_env": "DOUYIN_COOKIE",
        "referer": "https://www.douyin.com/",
        "note": "抖音建议提供 Cookie，部分公开视频可免登录",
    },
    "kuaishou": {
        "hosts": ("kuaishou.com", "chenzhongtech.com", "gifshow.com"),
        "cookie_env": "KUAISHOU_COOKIE",
        "referer": "https://www.kuaishou.com/",
        "note": "快手需登录 Cookie",
    },
    "weibo": {
        "hosts": ("weibo.com", "weibo.cn", "weibo.tv"),
        "cookie_env": "WEIBO_COOKIE",
        "referer": "https://weibo.com/",
        "note": "微博公开视频通常免登录",
    },
    "wechat_channels": {
        "hosts": ("channels.weixin.qq.com", "mp.weixin.qq.com"),
        "cookie_env": "WECHAT_COOKIE",
        "referer": "https://channels.weixin.qq.com/",
        "note": "微信视频号 yt-dlp 不支持，需用代理抓包方式（见 SKILL.md）",
        "unsupported": True,
    },
}

DEFAULT_PLATFORM = "generic"


def is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def detect_platform(url: str) -> str:
    low = url.lower()
    for name, cfg in PLATFORMS.items():
        if any(host in low for host in cfg["hosts"]):
            return name
    return DEFAULT_PLATFORM


def _resolve_short_url(url: str) -> str:
    """跟随短链跳转拿真实 URL（b23.tv / xhslink / v.douyin.com 等）。"""
    try:
        r = requests.get(
            url, headers={"User-Agent": MOBILE_UA}, allow_redirects=True, timeout=20
        )
        return r.url or url
    except requests.RequestException:
        return url  # 解析失败时交给下游再试


# ---------------------------------------------------------------- B 站

def _parse_bilibili(url: str) -> tuple[str, int]:
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        raise DownloadError(f"无法从链接识别 B 站 BV 号: {url}")
    pm = re.search(r"[?&]p=(\d+)", url)
    return m.group(1), max(0, int(pm.group(1)) - 1) if pm else 0


def _download_bilibili(url: str, workdir: str, cfg: dict) -> str:
    try:
        from bilibili_api import video as bili_video
    except ImportError:
        raise DownloadError(
            "B站下载需要依赖 bilibili-api-python（pip install bilibili-api-python）"
        )

    if "b23.tv" in url:
        url = _resolve_short_url(url)
    bvid, page_index = _parse_bilibili(url)

    async def _fetch():
        v = bili_video.Video(bvid=bvid)
        info = await v.get_info()
        pages = info.get("pages") or []
        if pages and page_index >= len(pages):
            raise DownloadError(f"该视频只有 {len(pages)} 个分P")
        data = await v.get_download_url(page_index)
        audios = (data.get("dash") or {}).get("audio") or []
        title = info.get("title", bvid) or bvid
        if pages:
            title = f"{title}_P{page_index + 1}"
        if audios:
            best = max(audios, key=lambda x: x.get("bandwidth", 0))
            return title, best["baseUrl"]
        durl = data.get("durl") or []
        if durl:
            return title, durl[0]["url"]
        raise DownloadError("未找到可用的音频流")

    try:
        title, stream_url = asyncio.run(_fetch())
    except DownloadError:
        raise
    except Exception as e:
        raise DownloadError(f"B站取流失败: {type(e).__name__}: {e}")

    safe = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", title)[:60] or bvid
    out = os.path.join(workdir, "bili_" + safe + ".m4a")
    try:
        with requests.get(
            stream_url,
            headers={"User-Agent": UA, "Referer": BILI_REFERER},
            stream=True,
            timeout=180,
        ) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        raise DownloadError(f"B站音频下载失败: {e}")

    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise DownloadError("B站音频下载为空")
    _log(f"B站《{title}》 {os.path.getsize(out) / 1048576:.1f} MB")
    return out


# ---------------------------------------------------------------- yt-dlp 通用

def _log(msg: str) -> None:
    print(f"[下载] {msg}", file=sys.stderr)


def _ytdlp_download(url: str, workdir: str, cfg: dict, platform: str) -> str:
    if shutil.which("yt-dlp") is None:
        raise DownloadError("未找到 yt-dlp，请先安装（pip install yt-dlp）")

    cookie_env = cfg.get("cookie_env")
    cookie = (os.environ.get(cookie_env, "") if cookie_env else "").strip()

    out_tpl = os.path.join(workdir, f"{platform}_%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "-f", "bestaudio/best",
        "--add-header", f"User-Agent:{UA}",
        "--add-header", f"Referer:{cfg.get('referer', url)}",
        "-o", out_tpl,
        url,
    ]
    if cookie:
        cmd[1:1] = ["--add-header", f"Cookie:{cookie}"]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout or "").strip().splitlines() or ["未知错误"])[-1]
        hint = ""
        if cookie_env and not cookie:
            hint = (
                f"\n提示：{platform} 建议提供登录 Cookie。请把浏览器里该站点的完整 Cookie "
                f"字符串设置到环境变量 {cookie_env} 后重试。"
            )
        elif cookie:
            hint = f"\n提示：{cookie_env} 可能已过期，请重新从浏览器复制。"
        raise DownloadError(
            f"{platform} 下载失败：{detail[:300]}{hint}\n({cfg.get('note', '')})"
        )

    files = sorted(
        Path(workdir).glob(f"{platform}_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise DownloadError(f"{platform} 下载未产出文件")
    _log(f"{platform} {files[0].name} {files[0].stat().st_size / 1048576:.1f} MB")
    return str(files[0])


# ---------------------------------------------------------------- 入口

def download_audio(url: str) -> str:
    """把链接下载为本地音频文件，返回路径。

    调用方负责清理返回文件所在的临时目录。
    """
    platform = detect_platform(url)
    cfg = PLATFORMS.get(platform, {})

    if cfg.get("unsupported"):
        raise DownloadError(
            f"暂不支持该平台链接（{platform}）：{cfg.get('note', '')}\n"
            "替代方案：用 res-downloader 之类的抓包工具在手机端嗅探到直链后再传入本工具。"
        )

    workdir = tempfile.mkdtemp(prefix="audiotr_dl_")
    if platform == "bilibili":
        return _download_bilibili(url, workdir, cfg)
    return _ytdlp_download(url, workdir, cfg, platform)
