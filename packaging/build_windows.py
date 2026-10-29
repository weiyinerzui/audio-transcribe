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
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

# Windows 控制台默认 cp1252/cp936，直接 print 中文会抛
# UnicodeEncodeError: 'charmap' codec can't encode ...
# 必须在任何输出之前把标准流切成 UTF-8。
# 这是 CI 在 windows-latest 上失败的真实原因（不是路径问题）。
if os.name == "nt":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

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

# ffmpeg 静态版（gyan.dev）单个 exe 就上百 MB，打出来包体 139MB。
# BtbN 的 shared 构建把编解码器拆进 DLL，压缩包 83MB，
# 代价是 bin/ 下必须有那些 DLL，所以启动脚本要把 bin/ 放进 PATH。
# 只取 lGPL 版：不含 GPL 组件，分发限制更少，本项目用不到那些编码器。
FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-lgpl-shared.zip"
)

# Windows 长路径前缀（\\?\）。写成常量而不是内联字符串，
# 免得反斜杠在源码里被转义成一排问号。
LONG_PREFIX = "\\\\?\\"

PIP_PACKAGES = [
    "requests>=2.28",
    "yt-dlp>=2024.1.0",
    "bilibili-api-python>=16.0.0",
    "curl_cffi>=0.7",        # bilibili-api 17.x 起不自带请求库，缺了 B 站功能是坏的
    "fastapi>=0.110",
    "uvicorn>=0.27",
    "python-multipart>=0.0.9",
]


def log(msg: str):
    """打印日志。即使 reconfigure 没生效也不让编码问题中断构建。"""
    text = f"[build] {msg}"
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", "ascii") or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"), flush=True)


def long_path(p: Path) -> str:
    """Windows 上给路径加 ``\\\\?\\`` 前缀，绕开 260 字符 MAX_PATH 限制。

    python-build-standalone 的 site-packages 里 pip/_vendor 一类深路径，
    再加上 dist/audio-transcribe-win64/ 前缀，很容易超过 260。
    超了会抛 FileNotFoundError [WinError 3]，且报错信息是「找不到文件」，
    完全看不出是路径太长。
    """
    s = str(p.resolve())
    if os.name == "nt" and not s.startswith(LONG_PREFIX):
        return LONG_PREFIX + s
    return s


def walk_files(root: Path):
    """遍历所有文件，Windows 下不受 MAX_PATH 限制，且不跟随 symlink。"""
    base = long_path(root)
    for dirpath, _dirnames, filenames in os.walk(base):
        for fn in filenames:
            yield Path(dirpath) / fn


def rmtree_force(p: Path) -> None:
    """删目录，遇只读文件先改权限再删。

    Windows 上 PBS 解出来的文件带只读位，git archive 导出的也可能只读，
    直接 rmtree 会 PermissionError [WinError 5]。
    """
    if not p.exists():
        return

    def _onerr(func, path, _exc):
        try:
            os.chmod(path, 0o666)
            func(path)
        except Exception:
            pass

    try:
        shutil.rmtree(p, onexc=_onerr) if sys.version_info >= (3, 12) else shutil.rmtree(p, onerror=_onerr)
    except TypeError:
        shutil.rmtree(p, onerror=_onerr)


def safe_extract(tar, dest, filter_name: str) -> None:
    """tarfile 解压。3.12 起显式给 filter，避免 3.14 默认值变化导致行为漂移。"""
    if sys.version_info >= (3, 12):
        tar.extractall(dest, filter=filter_name)
    else:
        tar.extractall(dest)


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


def install_deps(pyexe: Path, staging: Path) -> None:
    """把依赖装进包内的 site-packages，用户侧不跑 pip。"""
    log("安装依赖（这一步要几分钟）")
    site = staging / "python" / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)

    # 国内镜像优先，失败回官方源。CI 在海外，官方源其实更快，
    # 所以顺序是：官方 → 清华 → 阿里。
    indexes = [None, "https://pypi.tuna.tsinghua.edu.cn/simple",
               "https://mirrors.aliyun.com/pypi/simple"]
    last = ""
    for idx in indexes:
        cmd = [str(pyexe), "-m", "pip", "install", "--no-warn-script-location",
               "--no-cache-dir", "--disable-pip-version-check",
               "--target", str(site), *PIP_PACKAGES]
        if idx:
            cmd += ["-i", idx]
        log(f"  pip install（源：{idx or '默认 PyPI'}）")
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            log("  依赖装好了")
            return
        last = (r.stdout or "")[-1500:] + "\n" + (r.stderr or "")[-1500:]
        log(f"  失败，换下一个源")
    log(last)
    raise SystemExit("pip 安装失败（三个源都试过了）")


def slim_python(staging: Path) -> None:
    """砍掉包里用不到的 Python 标准库和构建残留。

    python-build-standalone 是「完整」发行版，自带 test 套件、
    idlelib、ensurepip、pip 的 vendored 依赖等，加起来几十 MB，
    用户永远用不到。绿色包体积敏感，全删。
    """
    log("精简 Python 运行时")
    before = sum(f.stat().st_size for f in walk_files(staging) if f.is_file())

    lib = staging / "python" / "Lib"
    drop_dirs = [
        "test",              # 标准库测试套件，最大的一块
        "idlelib",           # IDLE，没有界面
        "ensurepip",         # 用户侧不跑 pip
        "lib2to3",           # Python 2 语法转换，早废弃
        "turtledemo",
        "ensurepip/_bundled",  # 里面是 pip/setuptools 的 wheel 文件，每个几 MB
        "site-packages/pip", # 用户侧装不了东西，pip 本身不需要
        "site-packages/setuptools",
        "site-packages/pkg_resources",
        "site-packages/wheel",
    ]
    for rel in drop_dirs:
        d = lib / rel
        if d.is_dir():
            rmtree_force(d)

    # tkinter 相关（有 GUI 的包里才有，删了省 10MB+）
    for rel in ("tkinter", "turtle.py", "turtledemo"):
        d = lib / rel
        if d.is_dir():
            rmtree_force(d)
        elif d.is_file():
            d.unlink(missing_ok=True)
    dll = staging / "python" / "DLLs"
    if dll.is_dir():
        for pat in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll"):
            for f in dll.glob(pat):
                f.unlink(missing_ok=True)

    # 测试与缓存
    for pat in ("**/tests", "**/test"):
        for d in staging.glob(pat):
            if d.is_dir() and d.name in ("tests", "test"):
                rmtree_force(d)

    after = sum(f.stat().st_size for f in walk_files(staging) if f.is_file())
    log(f"  {before / 1024 / 1024:.1f} MB → {after / 1024 / 1024:.1f} MB"
        f"（省 {(before - after) / 1024 / 1024:.1f} MB）")


def report_sizes(staging: Path) -> None:
    """按顶层目录报告体积，压缩前先看清到底是哪一块大。"""
    log("各部分体积：")
    rows = []
    for d in sorted(staging.iterdir()):
        if d.is_dir():
            sz = sum(f.stat().st_size for f in walk_files(d) if f.is_file())
        else:
            sz = d.stat().st_size
        rows.append((sz, d.name, True))
        # bin/ 底下是大头，单列出来看
        if d.is_dir() and d.name == "bin":
            for f in sorted(d.iterdir(), key=lambda x: -x.stat().st_size)[:10]:
                if f.is_file():
                    rows.append((f.stat().st_size, f"   └ {f.name}", False))
    total = sum(r[0] for r in rows if r[2])
    for sz, name, _ in sorted(rows, key=lambda r: -r[0]):
        log(f"  {sz / 1024 / 1024:8.1f} MB  {name}")
    log(f"  {total / 1024 / 1024:8.1f} MB  合计（解压后）")


def verify_deps(pyexe: Path, staging: Path) -> None:
    """真跑一次导入，别等用户双击了才发现缺包。"""
    log("自检：导入关键模块")
    code = (
        "import sys;"
        "import fastapi, uvicorn, requests, multipart, yt_dlp;"
        "import bilibili_api;"
        "print('OK', fastapi.__version__, uvicorn.__version__)"
    )
    r = subprocess.run([str(pyexe), "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(staging),
                       env={**os.environ, "PYTHONPATH": str(staging / "python" / "Lib" / "site-packages")})
    if r.returncode != 0:
        log(r.stdout[-1500:])
        log(r.stderr[-1500:])
        raise SystemExit("自检失败：包内依赖不完整")
    log(f"  {r.stdout.strip()}")


def build(clean: bool = True, skip_pip: bool = False):
    cache = ROOT / "packaging" / ".cache"
    staging = OUT / NAME

    if clean and staging.exists():
        log(f"清理旧产物 {staging}")
        rmtree_force(staging)
    staging.mkdir(parents=True, exist_ok=True)

    # ---- 1. Python 运行时
    global PBS_URL
    if not PBS_URL:
        PBS_URL = resolve_python_url()
    py_zip = download(PBS_URL, cache / "python-win64.tar.gz", _PBS_EXPECT[0])
    log("解压 Python 运行时")
    with tarfile.open(py_zip) as t:
        # filter="tar"：PBS 包里可能有符号链接，data 模式会拒绝
        safe_extract(t, staging, "tar")
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
        if os.name != "nt":
            log("⚠ 当前不是 Windows，跳过 pip 安装。")
            log("  跨平台装出来的 wheel 是错的，留到 Windows 上装。")
        else:
            install_deps(pyexe, staging)

    # ---- 2.5 自检：确认关键模块真的在里面（必须在精简之前跑）
    if not skip_pip and os.name == "nt":
        verify_deps(pyexe, staging)

    # ---- 2.6 精简运行时（验证过能 import 了才敢删）
    slim_python(staging)

    # ---- 3. ffmpeg
    log("处理 ffmpeg")
    ff_zip = download(FFMPEG_URL, cache / "ffmpeg-win64.zip")
    bin_dir = staging / "bin"
    bin_dir.mkdir(exist_ok=True)

    with zipfile.ZipFile(ff_zip) as z:
        names = z.namelist()
        # 只取 /bin/ 下的 exe 和 dll。shared 构建缺了 dll 就跑不起来，
        # 所以这里必须整目录搬，不能只挑两个 exe。
        wanted = [
            n for n in names
            if "/bin/" in n and n.lower().endswith((".exe", ".dll"))
        ]
        if not wanted:
            raise SystemExit("ffmpeg 包里 /bin/ 下没有 exe/dll，压缩包结构变了？")

        got = set()
        for n in wanted:
            fname = n.rsplit("/", 1)[-1]
            if fname.lower() in ("ffmpeg.exe", "ffprobe.exe") or fname.lower().endswith(".dll"):
                with z.open(n) as src, open(bin_dir / fname, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                got.add(fname)

        for need in ("ffmpeg.exe", "ffprobe.exe"):
            if need not in got:
                raise SystemExit(f"ffmpeg 包里没找到 {need}")
        n_dll = len([g for g in got if g.lower().endswith(".dll")])
        log(f"  ffmpeg.exe / ffprobe.exe + {n_dll} 个 DLL → {bin_dir}")
        if n_dll == 0:
            log("  ⚠ 没有 DLL，若下载到的是静态版则正常")

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
            safe_extract(t, app, "data")
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

    # ---- 5.5 瘦身：pip 留下的 Scripts/*.exe、测试缓存、__pycache__ 都不需要
    scripts = staging / "python" / "Scripts"
    if scripts.is_dir():
        for f in list(scripts.iterdir()):
            if f.suffix.lower() in (".exe", ".exe.manifest"):
                f.unlink(missing_ok=True)
    n_pyc = 0
    for d in staging.rglob("__pycache__"):
        n_pyc += len(list(d.glob("*.pyc")))
        shutil.rmtree(d, ignore_errors=True)
    if n_pyc:
        log(f"清理 {n_pyc} 个 .pyc")

    # ---- 6. 打包
    report_sizes(staging)
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
        root_long = long_path(staging)
        for p in sorted(walk_files(staging)):
            arc = Path(p).relative_to(root_long)
            if p.is_symlink():
                continue   # Windows 包里不放软链
            zi = zipfile.ZipInfo(str(arc).replace(os.sep, "/"), date_time=date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            # bit 0 = 0x01 表示用 unix 权限位；全给 644 / exe 给 755，
            # 但 Windows 不认这个，用户看到的是文件本身，不影响双击运行
            mode = 0o755 if p.suffix.lower() in (".exe", ".bat", ".cmd", ".sh") else 0o644
            zi.external_attr = mode << 16
            with open(long_path(p), "rb") as fh:
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
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        log("")
        log("=" * 60)
        log("构建失败。排查用上下文：")
        log(f"  平台      : {sys.platform} / {platform.machine()}")
        log(f"  Python    : {sys.version.split()[0]}")
        log(f"  工作目录  : {Path.cwd()}")
        log(f"  产物目录  : {OUT}")
        log(f"  缓存目录  : {ROOT / 'packaging' / '.cache'}")
        log(f"  PBS_URL   : {PBS_URL or '(未解析)'}")
        for d in (OUT, ROOT / "packaging" / ".cache"):
            if d.exists():
                for f in sorted(d.rglob("*"))[:20]:
                    if f.is_file():
                        log(f"    {f.relative_to(d.parent)}  {f.stat().st_size} B")
        log("=" * 60)
        raise SystemExit(1)
