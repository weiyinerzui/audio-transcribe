"""构建 Windows 绿色包（免安装，双击即用）。

产物结构：

    audio-transcribe-win64/
      ├── 启动.bat            ← 用户只需要双击这个
      ├── 停止.bat
      ├── 使用说明.txt
      ├── python/             ← python-build-standalone 3.12
      ├── bin/ffmpeg.exe
      ├── bin/ffprobe.exe
      └── app/                ← 本仓库代码

用法（在有网络的机器上跑，Linux/macOS/Windows 都行）：

    python packaging/build_windows.py

依赖：requests、py7zr（解压 .tar.zst 用）。只跑构建的人需要装，最终用户不需要。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "dist"
NAME = "audio-transcribe-win64"

# python-build-standalone：astral 维护的便携 Python，自带完整标准库
PBS_TAG = "20241016"
PBS_VERSION = "3.12.7"
PBS_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PBS_TAG}/cpython-{PBS_VERSION}+{PBS_TAG}-x86_64-pc-windows-msvc-install_only.tar.gz"
)

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

PIP_PACKAGES = [
    "requests>=2.28",
    "yt-dlp>=2024.1.0",
    "bilibili-api-python>=16.0.0",
    "fastapi>=0.110",
    "uvicorn>=0.27",
    "python-multipart>=0.0.9",
]


def log(msg: str):
    print(f"[build] {msg}", flush=True)


def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        log(f"已缓存 {dest.name}（{dest.stat().st_size // 1024 // 1024} MB）")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"下载 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "audio-transcribe-build"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        total = int(r.headers.get("content-length") or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and done % (1024 * 1024 * 8) < 1024 * 256:
                log(f"  {done * 100 // total}%")
    log(f"完成 {dest.name}（{dest.stat().st_size // 1024 // 1024} MB）")
    return dest


def build(clean: bool = True, skip_pip: bool = False):
    cache = ROOT / "packaging" / ".cache"
    staging = OUT / NAME

    if clean and staging.exists():
        log(f"清理旧产物 {staging}")
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    # ---- 1. Python 运行时
    py_zip = download(PBS_URL, cache / "python-win64.tar.gz")
    log("解压 Python 运行时")
    with tarfile.open(py_zip) as t:
        t.extractall(staging)
    # 包里是 python/ 目录，正好对口
    pyexe = staging / "python" / "python.exe"
    if not pyexe.exists():
        found = list(staging.rglob("python.exe"))
        if not found:
            raise SystemExit("没找到 python.exe，压缩包结构变了？")
        # 把内层目录挪成 python/
        inner = found[0].parent
        if inner != staging / "python":
            shutil.move(str(inner), str(staging / "python"))
    log(f"Python 就位：{pyexe}")

    # ---- 2. 依赖
    if not skip_pip:
        log("安装依赖（这一步要几分钟）")
        site = staging / "python" / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(pyexe), "-m", "pip", "install", "--no-warn-script-location",
            "--target", str(site), *PIP_PACKAGES,
            "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
        ]
        # 交叉构建时没法跑 Windows 的 python.exe，只能在本机 Windows 上跑
        if os.name != "nt":
            log("⚠ 当前不是 Windows，跳过 pip 安装。")
            log("  请在 Windows 上重跑，或用 GitHub Actions 构建。")
        else:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                log(r.stdout[-2000:])
                log(r.stderr[-2000:])
                raise SystemExit("pip 安装失败")
            log("依赖装好了")

    # ---- 3. ffmpeg
    log("处理 ffmpeg")
    ff_zip = download(FFMPEG_URL, cache / "ffmpeg-win64.zip")
    bin_dir = staging / "bin"
    bin_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(ff_zip) as z:
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            hit = [n for n in z.namelist() if n.endswith("/bin/" + name)]
            if not hit:
                raise SystemExit(f"ffmpeg 包里没找到 {name}")
            with z.open(hit[0]) as src, open(bin_dir / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
            log(f"  {name} → {bin_dir / name}")

    # ---- 4. 应用代码（用 git archive 拿干净副本，不含 .git）
    log("拷贝应用代码")
    app = staging / "app"
    if app.exists():
        shutil.rmtree(app)
    app.mkdir()
    try:
        subprocess.run(
            ["git", "archive", "HEAD"],
            cwd=ROOT, check=True, stdout=subprocess.PIPE,
        )
        data = subprocess.run(
            ["git", "archive", "HEAD"], cwd=ROOT, check=True, stdout=subprocess.PIPE
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(app)
        log("  代码已导出（git archive）")
    except (subprocess.CalledProcessError, FileNotFoundError):
        log("  git 不可用，改用文件拷贝")
        ignore = shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", "packaging", "docs", ".github", "dist")
        for item in ROOT.iterdir():
            if ignore(str(ROOT), [item.name]):
                continue
            dst = app / item.name
            if item.is_dir():
                shutil.copytree(item, dst, ignore=ignore)
            else:
                shutil.copy2(item, dst)

    # ---- 5. 启动脚本
    log("写入启动脚本")
    launcher = ROOT / "packaging" / "launcher" / "windows"
    for f in launcher.iterdir():
        shutil.copy2(f, staging / f.name)

    # ---- 6. 打包
    log("压缩")
    zip_path = OUT / f"{NAME}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in staging.rglob("*"):
            z.write(p, p.relative_to(staging).parent / p.name)

    size_mb = zip_path.stat().st_size / 1024 / 1024
    log(f"\n完成：{zip_path}（{size_mb:.1f} MB）")
    log(f"解压后：{staging}")
    return zip_path


def main():
    ap = argparse.ArgumentParser(description="构建 Windows 绿色包")
    ap.add_argument("--keep", action="store_true", help="保留旧产物，不清理")
    ap.add_argument("--skip-pip", action="store_true", help="跳过 pip（交叉构建调试用）")
    a = ap.parse_args()
    build(clean=not a.keep, skip_pip=a.skip_pip)


if __name__ == "__main__":
    main()
