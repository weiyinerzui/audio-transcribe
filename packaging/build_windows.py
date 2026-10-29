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

用法：

    python packaging/build_windows.py          # Windows 上跑，产完整可用包
    python packaging/build_windows.py --skip-pip  # 非 Windows 上只想验证骨架

pip 那一步必须在 Windows 上跑：要装的是 Windows 版 wheel，
在 Linux/macOS 上装出来的是错平台的，所以脚本会跳过并提示。
正式产物由 GitHub Actions（.github/workflows/release.yml）在
windows-latest 上构建，打 tag 时自动附到 Release。

只用标准库，构建者不用额外装任何东西。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "dist"
NAME = "audio-transcribe-win64"

# python-build-standalone：astral 维护的便携 Python，自带完整标准库。
# 用 build() 里解析出来的最新 release，避免手写版本号过期（第一版就踩了这个坑，
# 写死的 20241016 版本资产已经 404）。
PBS_RELEASE_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
PBS_PY_SERIES = "3.12"   # 固定 3.12：生态 wheel 最全，且 FastAPI 全系支持
PBS_URL = os.environ.get("PBS_URL", "").strip()  # 可手动指定，绕过 API 查询
_PBS_EXPECT = [0]   # resolve_python_url() 顺带记下期望字节数，供 download 校验

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


def resolve_python_url(series: str = PBS_PY_SERIES) -> str:
    """查最新 release，挑出指定小版本的 win-x64 便携包。

    动态解析而不是写死，是因为 python-build-standalone 只在
    releases/latest 下保留近期资产，旧 tag 的下载链接会失效。
    """
    log("查询 python-build-standalone 最新版本")
    # 这个 release 的 JSON 有 1.7MB，网络差时会被截断，所以带重试。
    rel = None
    last_err = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                PBS_RELEASE_API, headers={"User-Agent": "audio-transcribe-build",
                                          "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                rel = json.loads(r.read())
            break
        except Exception as e:   # IncompleteRead 继承 HTTPException，不是 OSError，必须抓宽的
            last_err = e
            log(f"  第 {attempt + 1} 次查询失败：{type(e).__name__}，重试")
            time.sleep(2 * (attempt + 1))
    if rel is None:
        raise SystemExit(
            f"查询 python-build-standalone 失败：{last_err}\n"
            f"可以直接指定完整 URL 绕过：\n"
            f"  PBS_URL=https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"<tag>/cpython-3.12.x+<tag>-x86_64-pc-windows-msvc-install_only.tar.gz \\\n"
            f"  python packaging/build_windows.py")
    tag = rel["tag_name"]
    cands = [
        a for a in rel["assets"]
        if a["name"].startswith(f"cpython-{series}.")
        and "x86_64-pc-windows-msvc-install_only" in a["name"]
        and a["name"].endswith(".tar.gz")
        and "stripped" not in a["name"]
    ]
    if not cands:
        raise SystemExit(f"没找到 Python {series} 的 Windows x64 便携包（release {tag}）")
    # 同一 series 有多个补丁版时取版本号最大的
    def patch(a):
        try:
            return int(a["name"].split("cpython-")[1].split(".")[2].split("+")[0])
        except (IndexError, ValueError):
            return 0
    pick = max(cands, key=patch)
    log(f"  {tag} → {pick['name']}（{pick['size'] / 1024 / 1024:.1f} MB）")
    _PBS_EXPECT[0] = pick["size"]
    return pick["browser_download_url"]


def download(url: str, dest: Path, expect_size: int = 0) -> Path:
    """下载到 dest，并校验大小；不完整就重下。

    expect_size 来自 GitHub API 给的字节数。必须校验：
    第一版没校验，结果把 9MB 的截断文件当缓存，
    下一轮解压直接 EOFError，排查半天才发现文件是半截的。
    """
    if dest.exists() and dest.stat().st_size > 0:
        size = dest.stat().st_size
        if not expect_size or size == expect_size:
            log(f"已缓存 {dest.name}（{size / 1024 / 1024:.1f} MB）")
            return dest
        log(f"缓存 {dest.name} 大小不对（{size} != {expect_size}），重新下载")
        dest.unlink(missing_ok=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(4):
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            log(f"下载 {url}" + (f"（第 {attempt + 1} 次）" if attempt else ""))
            req = urllib.request.Request(url, headers={"User-Agent": "audio-transcribe-build"})
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("content-length") or expect_size or 0)
                done = 0
                last_pct = -1
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        if pct >= last_pct + 10:
                            log(f"  {pct}%")
                            last_pct = pct
            got = tmp.stat().st_size
            want = expect_size or total
            if want and got != want:
                raise IOError(f"大小不符：下载 {got} 字节，应为 {want}")
            tmp.replace(dest)
            log(f"完成 {dest.name}（{got / 1024 / 1024:.1f} MB）")
            return dest
        except Exception as e:
            last_err = e
            log(f"  下载失败：{type(e).__name__}: {e}")
            tmp.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
    raise SystemExit(f"下载 {url} 失败（已重试 4 次）：{last_err}")


def build(clean: bool = True, skip_pip: bool = False):
    cache = ROOT / "packaging" / ".cache"
    staging = OUT / NAME

    if clean and staging.exists():
        log(f"清理旧产物 {staging}")
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    # ---- 1. Python 运行时
    global PBS_URL
    if not PBS_URL:
        PBS_URL = resolve_python_url()
    py_zip = download(PBS_URL, cache / "python-win64.tar.gz", _PBS_EXPECT[0])
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
        # 教程/构建脚本不进包：docs 有 3MB+ 的截图和视频，packaging 用户用不上
        for drop in ("docs", "packaging", ".github"):
            p_drop = app / drop
            if p_drop.is_dir():
                shutil.rmtree(p_drop)
        log("  代码已导出（git archive），已剔除 docs/packaging/.github")
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
    # git archive 出来的文件 mtime 取自 commit 时间，可能早于 1980，
    # 而 ZIP 格式不支持 1980 年前的日期，直接 write 会抛 ValueError。
    # 统一把打包时间设成「现在」，顺便让产物可复现。
    now = time.localtime()
    if now.tm_year < 1980:
        now = time.struct_time((1980, 1, 1, 0, 0, 0, 1, 1, 0))
    date_time = (now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(staging.rglob("*")):
            arc = p.relative_to(staging)
            if p.is_dir():
                continue
            zi = zipfile.ZipInfo(str(arc).replace(os.sep, "/"), date_time=date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            # bit 0 = 0x01 表示用 unix 权限位；全给 644 / exe 给 755，
            # 但 Windows 不认这个，用户看到的是文件本身，不影响双击运行
            mode = 0o755 if p.suffix.lower() in (".exe", ".bat", ".cmd", ".sh") else 0o644
            zi.external_attr = mode << 16
            with open(p, "rb") as fh:
                z.writestr(zi, fh.read())

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
