"""环境检测与自动修复：python / ffmpeg / pip 包 / yt-dlp。

分级策略：
  L1 检测（只读，随时可跑）：python 版本、ffmpeg/ffprobe、依赖包、yt-dlp 新旧
  L2 自动修（用户点一下）：pip 装依赖、升级 yt-dlp、下载 ffmpeg 静态二进制
  L3 引导修（只提示命令）：系统级安装（apt/brew/winget），不擅自 sudo

安全约束：
  - 绝不执行 sudo
  - 绝不修改系统 python（PEP 668 环境下用 venv 或 --user）
  - 所有外部命令带超时
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

HOME = Path(os.environ.get("AUDIO_TRANSCRIBE_HOME", Path.home() / ".audio-transcribe"))
BIN_DIR = HOME / "bin"

# 核心依赖：name -> (pip 包名, 是否必需)
CORE_PACKAGES = {
    "requests": ("requests>=2.28", True),
    "yt_dlp": ("yt-dlp>=2024.1.0", True),
    "bilibili_api": ("bilibili-api-python>=16.0.0", False),
}

WEB_PACKAGES = {
    "fastapi": ("fastapi>=0.110", True),
    "uvicorn": ("uvicorn>=0.27", True),
    "multipart": ("python-multipart>=0.0.9", True),
}

# bilibili-api-python 17.x 起不再自带请求库，缺了会在取流时报
# ArgsException「尚未安装第三方请求库」。优先 curl_cffi（自带 TLS 指纹，最不易被风控）
BILI_CLIENT_PACKAGES = {
    "curl_cffi": "curl_cffi>=0.7",
    "httpx": "httpx>=0.27",
    "aiohttp": "aiohttp>=3.9",
}

OPTIONAL_PACKAGES = {
    "faster_whisper": ("faster-whisper>=1.0", "本地转录（跨平台）"),
    "mlx_whisper": ("mlx-whisper", "本地转录（Apple Silicon）"),
    "huggingface_hub": ("huggingface_hub>=0.23", "模型下载"),
}


@dataclass
class CheckItem:
    key: str
    label: str
    ok: bool
    level: str = "ok"        # ok / warn / error
    detail: str = ""
    fixable: bool = False    # 是否可由本工具自动修复
    fix_action: str = ""     # 修复动作标识
    manual_hint: str = ""    # 手动修复命令


def _which(name: str) -> str | None:
    # 优先用本工具下载的静态二进制
    local = BIN_DIR / (name + (".exe" if sys.platform == "win32" else ""))
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name)


def _run_quick(cmd: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
        return p.returncode == 0, (p.stdout or p.stderr or "").strip()
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------- L1 检测


def check_all() -> list[dict]:
    """全量检测，返回可 JSON 序列化的列表。"""
    items: list[CheckItem] = []

    # 1. Python 版本
    v = sys.version_info
    py_ok = v >= (3, 10)
    items.append(CheckItem(
        key="python", label="Python 版本", ok=py_ok,
        level="ok" if py_ok else "error",
        detail=f"{v.major}.{v.minor}.{v.micro}",
        manual_hint="" if py_ok else "需要 Python 3.10 或更新版本",
    ))

    # 2. ffmpeg
    ff = _which("ffmpeg")
    if ff:
        ok, out = _run_quick([ff, "-version"])
        ver = out.split("\n")[0][:60] if out else ""
        items.append(CheckItem("ffmpeg", "ffmpeg", ok, detail=ver or ff))
    else:
        items.append(CheckItem(
            "ffmpeg", "ffmpeg", False, level="error",
            detail="未找到（用于抽取音轨、转码）",
            fixable=True, fix_action="install_ffmpeg",
            manual_hint=_ffmpeg_hint(),
        ))

    # 3. ffprobe
    fp = _which("ffprobe")
    if fp:
        items.append(CheckItem("ffprobe", "ffprobe", True, detail=fp))
    else:
        items.append(CheckItem(
            "ffprobe", "ffprobe", False, level="error",
            detail="未找到（用于读取时长）",
            fixable=True, fix_action="install_ffmpeg",
            manual_hint=_ffmpeg_hint(),
        ))

    # 4. 核心 pip 包
    for mod, (pkg, required) in CORE_PACKAGES.items():
        ok = _importable(mod)
        if ok:
            items.append(CheckItem(mod, pkg.split(">=")[0], True, detail="已安装"))
        else:
            items.append(CheckItem(
                mod, pkg.split(">=")[0], False,
                level="error" if required else "warn",
                detail="未安装" + ("" if required else "（仅 B站链接需要）"),
                fixable=True, fix_action="install_deps",
            ))

    # 4b. bilibili-api 的请求库：17.x 起必须单独装一个
    if _importable("bilibili_api"):
        has_client = next((m for m in BILI_CLIENT_PACKAGES if _importable(m)), None)
        if has_client:
            items.append(CheckItem(
                "bili_client", "B站请求库", True, detail=f"已安装 {has_client}"))
        else:
            items.append(CheckItem(
                "bili_client", "B站请求库", False, level="warn",
                detail="缺少 curl_cffi / httpx / aiohttp，B站链接会取流失败",
                fixable=True, fix_action="install_bili_client",
                manual_hint="pip install curl_cffi",
            ))

    # 5. yt-dlp 是否过旧（B站/抖音等平台频繁变更，旧版会突然失效）
    if _importable("yt_dlp"):
        try:
            import yt_dlp
            ver = yt_dlp.version.__version__
            # 版本号形如 2024.10.22；超过 180 天视为过旧
            age_days = _ytdlp_age_days(ver)
            old = age_days is not None and age_days > 180
            items.append(CheckItem(
                "ytdlp_version", "yt-dlp 版本", not old,
                level="warn" if old else "ok",
                detail=f"{ver}" + (f"（已 {age_days} 天未更新，建议升级）" if old else ""),
                fixable=old, fix_action="upgrade_ytdlp" if old else "",
            ))
        except Exception:
            pass

    # 6. 可选：本地转录后端
    for mod, (pkg, desc) in OPTIONAL_PACKAGES.items():
        ok = _importable(mod)
        note = desc
        if mod == "mlx_whisper" and sys.platform != "darwin":
            note = "仅 macOS 可用，当前平台不需要"
        items.append(CheckItem(
            mod, pkg, ok,
            level="ok" if ok else "warn",
            detail=note if ok else f"未安装（{note}）",
            fixable=(not ok) and not (mod == "mlx_whisper" and sys.platform != "darwin"),
            fix_action="install_optional",
            manual_hint="" if ok else f"pip install {pkg}",
        ))

    # 7. 磁盘空间（模型动辄 1-3 GB）
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(str(HOME))
        free_gb = usage.free / (1024 ** 3)
        items.append(CheckItem(
            "disk", "磁盘剩余空间", free_gb > 5,
            level="ok" if free_gb > 5 else "warn",
            detail=f"{free_gb:.1f} GB 可用（本地模型建议保留 5 GB 以上）",
        ))
    except Exception:
        pass

    return [it.__dict__ for it in items]


def _importable(mod: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _ytdlp_age_days(ver: str) -> int | None:
    import datetime
    try:
        parts = ver.split(".")
        d = datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
        return (datetime.date.today() - d).days
    except Exception:
        return None


def _ffmpeg_hint() -> str:
    if sys.platform == "darwin":
        return "brew install ffmpeg   （或点下方按钮自动下载静态版）"
    if sys.platform == "win32":
        return "winget install Gyan.FFmpeg   （或点下方按钮自动下载静态版）"
    return "sudo apt install ffmpeg   （或点下方按钮自动下载静态版，无需 root）"


# ---------------------------------------------------------------- L2 修复


def install_python_deps(which: str = "core",
                        on_log: Callable[[str], None] | None = None) -> dict:
    """pip 安装依赖。which: core / web / optional / all。"""
    log = on_log or (lambda s: None)
    pkgs: list[str] = []
    if which in ("core", "all"):
        pkgs += [pkg for pkg, _ in CORE_PACKAGES.values()]
        pkgs += list(BILI_CLIENT_PACKAGES.values())
    if which in ("web", "all"):
        pkgs += [pkg for pkg, _ in WEB_PACKAGES.values()]
    if which in ("optional", "all"):
        pkgs += [pkg for pkg, _ in OPTIONAL_PACKAGES.values()]
    # 去重并保持顺序
    seen, uniq = set(), []
    for p in pkgs:
        base = p.split(">=")[0].split("==")[0]
        if base not in seen:
            seen.add(base)
            uniq.append(p)
    pkgs = uniq

    if not pkgs:
        return {"ok": False, "log": "没有需要安装的包"}

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + pkgs
    log(f"$ {' '.join(cmd)}")
    res = _exec_stream(cmd, log, timeout=900)

    # bilibili-api-python 的旧版会锁死老依赖（lxml 4.6.5 / aiohttp 3.7.4），
    # 在新 Python 上必然编译失败。失败时改试不带版本约束的最新版。
    if not res.get("ok") and which in ("core", "all") and "bilibili_api" not in _installed():
        log("")
        log("依赖存在版本冲突，改为只装最新版 bilibili-api-python 再试…")
        cmd2 = [sys.executable, "-m", "pip", "install", "--upgrade", "--no-cache-dir",
                "bilibili-api-python"]
        log(f"$ {' '.join(cmd2)}")
        res2 = _exec_stream(cmd2, log, timeout=900)
        if res2.get("ok"):
            res = res2
    return res


def _installed() -> set[str]:
    import importlib.util
    return {m for m in list(CORE_PACKAGES) + list(WEB_PACKAGES) + list(OPTIONAL_PACKAGES)
            if importlib.util.find_spec(m) is not None}


def install_bili_client(on_log: Callable[[str], None] | None = None) -> dict:
    """给 bilibili-api 补一个请求库。"""
    log = on_log or (lambda s: None)
    pkgs = [pkg for pkg in BILI_CLIENT_PACKAGES.values()]
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--no-cache-dir"] + pkgs
    log(f"$ {' '.join(cmd)}")
    return _exec_stream(cmd, log, timeout=600)

def upgrade_ytdlp(on_log: Callable[[str], None] | None = None) -> dict:
    log = on_log or (lambda s: None)
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
    log(f"$ {' '.join(cmd)}")
    return _exec_stream(cmd, log, timeout=300)


def _exec_stream(cmd: list[str], log: Callable[[str], None],
                 timeout: int = 600) -> dict:
    """执行命令并流式回传输出。"""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    except Exception as exc:
        return {"ok": False, "log": str(exc)}

    import time
    start = time.time()
    tail: list[str] = []
    try:
        for line in p.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if line:
                log(line)
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
            if time.time() - start > timeout:
                p.kill()
                return {"ok": False, "log": "执行超时，已终止"}
        p.wait(timeout=10)
    except Exception as exc:
        return {"ok": False, "log": str(exc)}
    return {"ok": p.returncode == 0, "code": p.returncode, "log": "\n".join(tail)}


# ---------------------------------------------------------------- ffmpeg 静态版


def ffmpeg_download_url() -> str | None:
    """各平台官方静态构建地址。"""
    sysname, machine = sys.platform, platform.machine().lower()
    if sysname == "linux":
        arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
        return f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz"
    if sysname == "darwin":
        # macOS 官方不提供静态二进制，走 evermeet 的通用构建
        return "https://evermeet.cx/ffmpeg/getrelease/zip"
    if sysname == "win32":
        return "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    return None


def install_ffmpeg(on_log: Callable[[str], None] | None = None) -> dict:
    """下载 ffmpeg 静态二进制到 ~/.audio-transcribe/bin/，不需要 root。"""
    log = on_log or (lambda s: None)
    url = ffmpeg_download_url()
    if not url:
        return {"ok": False, "log": f"当前平台 {sys.platform} 没有自动下载方案，请手动安装"}

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    log(f"下载 ffmpeg: {url}")

    try:
        import requests
        with requests.get(url, stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            tmp = tempfile.mktemp()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        log(f"  已下载 {done * 100 // total}%")
        log("下载完成，开始解压…")
    except Exception as exc:
        return {"ok": False, "log": f"下载失败：{exc}"}

    try:
        if url.endswith(".zip"):
            with zipfile.ZipFile(tmp) as z:
                z.extractall(tempfile.mkdtemp())
                base = Path(tmp).parent
        else:
            with tarfile.open(tmp) as t:
                t.extractall(str(BIN_DIR / "_tmp"))
            # tar.xz 里是 ffmpeg-release-amd64-static/ffmpeg
            for name in ("ffmpeg", "ffprobe"):
                found = list((BIN_DIR / "_tmp").rglob(name))
                if found:
                    dest = BIN_DIR / name
                    shutil.copy2(found[0], dest)
                    dest.chmod(0o755)
            shutil.rmtree(BIN_DIR / "_tmp", ignore_errors=True)
    except Exception as exc:
        return {"ok": False, "log": f"解压失败：{exc}"}

    ok = _which("ffmpeg") is not None and _which("ffprobe") is not None
    log("ffmpeg 已就绪" if ok else "安装后仍找不到 ffmpeg，请手动安装")
    return {"ok": ok, "path": str(BIN_DIR)}


def env_summary() -> dict:
    """给 UI 显示的环境概览。"""
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": sys.platform,
        "machine": platform.machine(),
        "executable": sys.executable,
        "home": str(HOME),
        "bin": str(BIN_DIR),
        "ffmpeg": _which("ffmpeg") or "",
        "ffprobe": _which("ffprobe") or "",
        "in_venv": sys.prefix != sys.base_prefix,
    }
