"""绿色包冒烟测试：验证打出来的 Windows 包真的能跑。

前面 release.yml 的「校验产物完整」只证明文件都在，
这个脚本负责证明东西能用：包里自带的 python.exe 能不能启动服务，
依赖有没有装全，ffmpeg 能不能执行。

用法（在仓库根目录，Windows 上）：

    python packaging/smoke_test.py

CI 里两个版本各跑一次，本地也能直接跑。退出码非 0 即有失败。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows 控制台 cp1252，打印中文/符号会炸
if os.name == "nt":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "packaging" / "dist"
PORT = 8899

# 必须能导入的模块。bilibili-api 跑 B 站要用，curl_cffi 是 17.x 起
# 不自带请求库的补丁，缺了 B 站功能就是坏的，所以都算必需。
MODULES = ["fastapi", "uvicorn", "multipart", "requests", "yt_dlp", "bilibili_api", "curl_cffi"]

failures: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        log(f"  OK   {name}" + (f"  ({detail})" if detail else ""))
    else:
        log(f"  FAIL {name}" + (f"  ({detail})" if detail else ""))
        failures.append(f"{name}: {detail}")


def http_json(url: str, timeout: float = 3) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def wait_health(proc: subprocess.Popen, seconds: float = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False          # 进程已经死了，别等了
        if http_json(f"http://127.0.0.1:{PORT}/api/health"):
            return True
        time.sleep(0.5)
    return False


def test_variant(variant: str) -> None:
    root = DIST / variant
    log(f"\n===== {variant} =====")

    if not root.is_dir():
        check(f"{variant} 目录存在", False, str(root))
        return

    pyexe = root / "python" / ("python.exe" if os.name == "nt" else "bin/python3")
    check("包内 Python 存在", pyexe.is_file(), str(pyexe))
    if not pyexe.is_file():
        return

    # 1. 包内 Python 能跑
    try:
        r = subprocess.run([str(pyexe), "-c", "import sys;print(sys.version.split()[0])"],
                           capture_output=True, text=True, timeout=60)
        check("Python 可执行", r.returncode == 0, (r.stdout or r.stderr).strip()[:80])
    except Exception as exc:
        check("Python 可执行", False, str(exc))
        return

    # 2. 依赖装全了（最容易静默失败的一步）
    code = (
        "import importlib\n"
        f"mods = {MODULES!r}\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m); print('OK ' + m)\n"
        "    except Exception as e:\n"
        "        print('FAIL ' + m + ': ' + str(e))\n"
    )
    try:
        r = subprocess.run([str(pyexe), "-c", code], capture_output=True, text=True,
                           timeout=180, encoding="utf-8", errors="replace")
        lines = (r.stdout or "").splitlines() + (r.stderr or "").splitlines()
        # 每个模块只认自己那一行，别把整段输出贴进详情
        result = {}
        for ln in lines:
            if ln.startswith("OK "):
                result[ln[3:].strip()] = (True, "")
            elif ln.startswith("FAIL "):
                mod, _, why = ln[5:].partition(": ")
                result[mod.strip()] = (False, why.strip())
        for m in MODULES:
            ok, why = result.get(m, (False, "没有输出（进程可能崩了）"))
            check(f"import {m}", ok, why[:150])
    except Exception as exc:
        check("依赖自检", False, str(exc))

    # 3. ffmpeg 能执行（shared 构建缺 DLL 会在这里暴露）
    ff = root / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if ff.is_file():
        try:
            r = subprocess.run([str(ff), "-version"], capture_output=True, text=True, timeout=60)
            check("ffmpeg 可执行", r.returncode == 0, (r.stdout or r.stderr).split("\n")[0][:70])
        except Exception as exc:
            check("ffmpeg 可执行", False, str(exc))
        fp = root / "bin" / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if fp.is_file():
            try:
                r = subprocess.run([str(fp), "-version"], capture_output=True, text=True, timeout=60)
                check("ffprobe 可执行", r.returncode == 0)
            except Exception as exc:
                check("ffprobe 可执行", False, str(exc))
    else:
        log("  --   bin/ 无 ffmpeg（轻量版正常）")

    # 4. 真启动服务
    data_dir = ROOT / f".smoke-{variant}"
    env = dict(os.environ)
    env["AUDIO_TRANSCRIBE_HOME"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"

    server = root / "app" / "web" / "server.py"
    proc = None
    try:
        proc = subprocess.Popen(
            [str(pyexe), str(server), "--no-browser", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=str(root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace",
        )
        if not wait_health(proc):
            tail = ""
            try:
                proc.kill()
                tail = (proc.stdout.read() or "")[-800:]
            except Exception:
                pass
            check("服务启动", False, tail)
            return
        check("服务启动", True, f"端口 {PORT}")

        # 首页
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=10) as r:
                body = r.read()
            check("首页可访问", r.status == 200 and len(body) > 5000, f"{len(body)} 字节")
        except Exception as exc:
            check("首页可访问", False, str(exc))

        # 环境检测接口（会真去 which ffmpeg，能反映包内 bin 是否生效）
        env_data = http_json(f"http://127.0.0.1:{PORT}/api/env", timeout=20)
        if env_data and env_data.get("checks"):
            checks = env_data["checks"]
            check("/api/env 返回检测项", True, f"{len(checks)} 项")
            ff_item = next((c for c in checks if c.get("key") == "ffmpeg"), None)
            if ff.is_file() and ff_item:
                check("包内 ffmpeg 被识别", bool(ff_item.get("ok")),
                      (ff_item.get("detail") or "")[:60])
        else:
            check("/api/env 返回检测项", False, "无响应")

        # 静态资源
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/tasks", timeout=10) as r:
                tasks = json.loads(r.read().decode("utf-8"))
            check("/api/tasks 可访问", isinstance(tasks, (list, dict)))
        except urllib.error.HTTPError as exc:
            # 401 也算通过：说明鉴权中间件正常工作
            check("/api/tasks 可访问", exc.code in (401, 403), f"HTTP {exc.code}")
        except Exception as exc:
            check("/api/tasks 可访问", False, str(exc))

    finally:
        if proc and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        # 清理测试数据
        if data_dir.exists():
            import shutil
            shutil.rmtree(data_dir, ignore_errors=True)


def main() -> int:
    variants = [d.name for d in sorted(DIST.glob("audio-transcribe-win64*")) if d.is_dir()]
    if not variants:
        log(f"没有找到产物，先跑 python packaging/build_windows.py（{DIST}）")
        return 1

    for v in variants:
        test_variant(v)

    log("")
    if failures:
        log(f"冒烟测试失败 {len(failures)} 项：")
        for f in failures:
            log(f"  - {f}")
        return 1
    log("冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
