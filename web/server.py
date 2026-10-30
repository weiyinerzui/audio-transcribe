"""音频转写 Web 服务端（FastAPI）。

只监听 127.0.0.1。所有 LLM / ASR 的 api_key 由浏览器在请求头里带过来，
服务端只透传、不保存、不写日志。

启动：
  python web/server.py            # http://127.0.0.1:8787
  python web/server.py --port 9000
"""

from __future__ import annotations

import json
import os
import queue as _queue
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import env_check
import model_manager
import pipeline
from web import auth
from subtitle import Segment, Transcript
from taskqueue import HOME, OUTPUT_DIR, UPLOAD_DIR, Queue, Task

# 绿色包运行时：包内 bin/ 放 ffmpeg/ffprobe，应优先于系统里已有的。
# 只有用户目录还没装过 ffmpeg 时才注入 PATH ——
# 用户在界面里点「安装 ffmpeg」装到数据目录的版本依然生效。
def _prepend_bundled_bin() -> None:
    """把绿色包自带的 bin/ 加进 PATH。

    包结构是  <包根>/app/web/server.py  和  <包根>/bin/ffmpeg.exe，
    所以 bin 在 server.py 往上三层，不是两层。
    （之前写成两层指向 app/bin，Windows 上直接找不到，
      只有在 Linux 上被系统的 /usr/bin/ffmpeg 兜住才没暴露。）
    """
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    here = Path(__file__).resolve()
    bundled = ""
    # 逐级往上找 bin/ffmpeg.exe，比硬算层级更耐改动
    for up in (3, 2, 1, 0):
        cand = here.parents[up] / "bin" if up else here.parent / "bin"
        if (cand / exe_name).is_file():
            bundled = str(cand)
            break
    if not bundled:
        return
    home = Path(os.environ.get("AUDIO_TRANSCRIBE_HOME") or str(HOME / ".audio-transcribe"))
    if os.path.isfile(home / "bin" / exe_name):
        return
    os.environ["PATH"] = bundled + os.pathsep + os.environ.get("PATH", "")
    print(f"[audio-transcribe] 优先使用包内 ffmpeg：{bundled}", flush=True)


_prepend_bundled_bin()

app = FastAPI(title="audio-transcribe", docs_url=None, redoc_url=None)

QUEUE = Queue(concurrency=int(os.environ.get("AT_CONCURRENCY", "1")))
STATIC = HERE / "static"

# ---------------------------------------------------------------- 访问控制
#
# 不设 AT_ACCESS_PASSWORD / AT_USERS_JSON 时完全开放（本地自用）。
# 设了就要登录，按账户记每日限额，admin 不限。


def _client_ip(request: Request) -> str:
    """反代后面取真实 IP，否则按直连 IP。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def current_user(request: Request) -> auth.User | None:
    """已登录返回 User，否则 None。未启用鉴权时返回 admin 语义的匿名用户。"""
    if not auth.enabled():
        return auth.User(name="local", role="admin")
    return auth.read_token(request.cookies.get(auth.COOKIE_NAME, ""))


def require_user(request: Request) -> auth.User:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "请先登录")
    return u


def require_quota(request: Request) -> auth.User:
    """提交转录前调用：既校验登录，也校验今日额度。"""
    u = require_user(request)
    ok, msg = auth.USAGE.check(u)
    if not ok:
        raise HTTPException(429, msg)
    return u


@app.middleware("http")
async def _guard(request: Request, call_next):
    """启用鉴权后，除登录页 / 登录接口 / 健康检查外一律要登录。"""
    if not auth.enabled():
        return await call_next(request)

    path = request.url.path
    public = (
        path in ("/", "/login", "/api/auth/login", "/api/auth/me", "/api/health")
        or path.startswith("/static")
        or path == "/favicon.ico"
    )
    if public:
        return await call_next(request)

    if not auth.read_token(request.cookies.get(auth.COOKIE_NAME, "")):
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    return await call_next(request)

# 允许的音频/视频扩展名：上传时校验，避免任意文件落盘
ALLOWED_EXT = {
    ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v",
}
MAX_UPLOAD_BYTES = int(os.environ.get("AT_MAX_UPLOAD_MB", "2048")) * 1024 * 1024


# ---------------------------------------------------------------- 任务执行


def _run_task(task: Task):
    """队列 worker 调用的执行体。"""
    task.status = "running"
    task.started = time.time()
    QUEUE.emit(task, {"stage": "start", "msg": "开始处理", "status": "running"})

    # 从任务里取回运行时参数（不落盘的敏感字段存在 _runtime 里）
    rt: dict = getattr(task, "_runtime", {}) or {}
    fmt = task.fmt
    cfg = pipeline.PipelineConfig(
        source=rt.get("source") or task.source,
        engine=task.engine,
        output=str(OUTPUT_DIR / f"{task.id}.{fmt}"),
        fmt=fmt,
        clean=task.clean,
        asr_model=rt.get("asr_model"),
        clean_model=rt.get("clean_model"),
        clean_base_url=rt.get("clean_base_url"),
        clean_api_key=rt.get("clean_api_key"),
        engine_kwargs=rt.get("engine_kwargs") or {},
        cookies=rt.get("cookies") or {},
    )

    def on_event(ev: dict):
        if task._cancel.is_set():
            raise pipeline.Cancelled("任务已取消")
        QUEUE.emit(task, ev)

    try:
        result = pipeline.run(
            cfg,
            on_event=on_event,
            should_cancel=lambda: task._cancel.is_set(),
        )
        task.status = "done"
        task.output_path = result.output_path
        task.degraded = result.degraded
        task.progress = 1.0
        # 按音频实际时长记用量，防「一次传 3 小时」刷爆演示站
        try:
            dur = (result.transcript.segments[-1].end_ms / 1000.0
                   if result.transcript.segments and result.transcript.has_timing else 0)
            if task.owner and dur > 0:
                auth.USAGE.add_seconds(task.owner, dur)
        except Exception:
            pass
        QUEUE.emit(task, {
            "stage": "done", "msg": f"完成：{Path(result.output_path).name}",
            "status": "done", "output": result.output_path,
            "degraded": result.degraded, "elapsed": result.elapsed,
        })
    except pipeline.Cancelled:
        task.status = "cancelled"
        QUEUE.emit(task, {"stage": "cancelled", "msg": "任务已取消", "status": "cancelled"})
    except Exception as exc:
        task.status = "error"
        task.error = str(exc)[:800]
        QUEUE.emit(task, {"stage": "error", "msg": f"失败：{task.error}",
                          "status": "error"})
    finally:
        # 除非用户要保留原始媒体（供播放器），否则清理上传的临时文件
        up = rt.get("upload_path")
        if up and os.path.exists(up) and not rt.get("keep_upload"):
            try:
                os.unlink(up)
            except OSError:
                pass
        task._runtime = {}


QUEUE.set_runner(_run_task)
QUEUE.start()


# ---------------------------------------------------------------- 页面


@app.get("/", response_class=HTMLResponse)
def index():
    f = STATIC / "index.html"
    if not f.exists():
        return HTMLResponse("<h1>缺少 web/static/index.html</h1>", status_code=500)
    return HTMLResponse(f.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 登录


class LoginReq(BaseModel):
    username: str = "guest"
    password: str = ""


@app.post("/api/auth/login")
def api_login(req: LoginReq, request: Request):
    if not auth.enabled():
        return {"ok": True, "enabled": False, "user": "local", "role": "admin"}
    user = auth.check_password(req.username or "guest", req.password or "")
    if not user:
        # 故意不区分「用户不存在」和「密码错」
        raise HTTPException(401, "用户名或密码不正确")
    token = auth.issue_token(user)
    resp = JSONResponse({
        "ok": True, "enabled": True,
        "user": user.name, "role": user.role,
        "quota": auth.USAGE.summary(user),
    })
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.TOKEN_TTL, httponly=True, samesite="lax", secure=secure, path="/",
    )
    return resp


@app.post("/api/auth/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/api/auth/me")
def api_me(request: Request):
    if not auth.enabled():
        return {"enabled": False, "user": "local", "role": "admin",
                "quota": {"role": "admin", "unlimited": True}}
    u = auth.read_token(request.cookies.get(auth.COOKIE_NAME, ""))
    # 未登录时回 200 + user:null，而不是 401：
    # 这是「查询登录状态」的正常结果，用 401 会让浏览器控制台报红、干扰排查。
    if not u:
        return {"enabled": True, "user": None, "role": None, "quota": None}
    return {"enabled": True, "user": u.name, "role": u.role,
            "quota": auth.USAGE.summary(u)}


# ---------------------------------------------------------------- 环境与模型


@app.get("/api/env")
def api_env():
    return {
        "summary": env_check.env_summary(),
        "checks": env_check.check_all(),
        "concurrency": QUEUE.concurrency,
    }


class InstallReq(BaseModel):
    action: str          # install_deps / upgrade_ytdlp / install_ffmpeg / install_optional
    which: str = "core"


@app.post("/api/env/install")
def api_env_install(req: InstallReq):
    """执行修复。同步返回完整日志（前端按行展示）。"""
    logs: list[str] = []

    def log(s: str):
        logs.append(s)

    fn = {
        "install_deps": lambda: env_check.install_python_deps(req.which, log),
        "upgrade_ytdlp": lambda: env_check.upgrade_ytdlp(log),
        "install_ffmpeg": lambda: env_check.install_ffmpeg(log),
        "install_optional": lambda: env_check.install_python_deps("optional", log),
        "install_bili_client": lambda: env_check.install_bili_client(log),
    }.get(req.action)

    if not fn:
        raise HTTPException(400, f"未知动作 {req.action}")

    try:
        res = fn()
    except Exception as exc:
        res = {"ok": False, "log": str(exc)}
    return {"result": res, "logs": logs, "checks": env_check.check_all()}


@app.get("/api/models")
def api_models():
    models = model_manager.list_models()
    for m in models:
        t = model_manager.get_task(m["id"])
        if t:
            m["task"] = {"status": t.status, "error": t.error,
                         "downloaded_mb": t.downloaded_mb}
    return {"models": models, "endpoint": model_manager.endpoint(),
            "dir": str(model_manager.MODELS_DIR)}


class DownloadReq(BaseModel):
    id: str


@app.post("/api/models/download")
def api_model_download(req: DownloadReq):
    try:
        model_manager.start_download(req.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.get("/api/models/{model_id}/progress")
def api_model_progress(model_id: str):
    def gen():
        last = ""
        for _ in range(3600):     # 最多 1 小时
            t = model_manager.get_task(model_id)
            if not t:
                yield "data: {\"status\":\"none\"}\n\n"
                return
            payload = {"status": t.status, "error": t.error,
                       "downloaded_mb": t.downloaded_mb, "total_mb": t.total_mb}
            cur = json.dumps(payload, ensure_ascii=False)
            if cur != last:
                yield f"data: {cur}\n\n"
                last = cur
            if t.status in ("done", "error", "cancelled"):
                return
            time.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.delete("/api/models/{model_id}")
def api_model_delete(model_id: str):
    model_manager.cancel_download(model_id)
    ok = model_manager.delete_model(model_id)
    return {"ok": ok}


class EndpointReq(BaseModel):
    url: str


@app.post("/api/models/endpoint")
def api_model_endpoint(req: EndpointReq):
    model_manager.set_endpoint(req.url)
    return {"endpoint": model_manager.endpoint()}


@app.get("/api/engines")
def api_engines():
    from engines import ENGINE_META
    return {
        "engines": [{"id": k, **v} for k, v in ENGINE_META.items()],
        "mlx": dict(zip(("ok", "reason"),
                        __import__("engines.mlx_whisper", fromlist=["x"]).mlx_supported())),
    }


# ---------------------------------------------------------------- 转录任务


def _mk_task(source_display: str, source_type: str, engine: str, fmt: str,
             clean: str, runtime: dict, tid: str | None = None) -> Task:
    t = Task(
        id=tid or uuid.uuid4().hex[:12],
        kind="transcribe",
        source=source_display,
        source_type=source_type,
        engine=engine,
        fmt=fmt,
        clean=clean,
        status="queued",
    )
    t._runtime = runtime
    return t


def _submit(t: Task, user: auth.User):
    """提交任务 + 记一次用量。额度在 require_quota 里已校验过。"""
    t.owner = user.name
    auth.USAGE.add_task(user.name)
    QUEUE.submit(t)


class UrlReq(BaseModel):
    url: str
    engine: str
    fmt: str = "srt"
    clean: str = "llm"
    asr_model: str | None = None
    clean_model: str | None = None
    clean_base_url: str | None = None
    clean_api_key: str | None = None
    asr_api_key: str | None = None
    asr_base_url: str | None = None
    local_model: str | None = None
    cookies: dict = {}


@app.post("/api/transcribe/url")
def api_transcribe_url(req: UrlReq, request: Request):
    user = require_quota(request)
    url = (req.url or "").strip()
    if not url or not pipeline.is_link(url):
        raise HTTPException(400, "请输入有效的网页链接")
    ek = {}
    if req.asr_api_key:
        ek["api_key"] = req.asr_api_key
    if req.asr_base_url:
        ek["base_url"] = req.asr_base_url
    if req.local_model:
        ek["model_dir"] = req.local_model

    t = _mk_task(url, "url", req.engine, req.fmt, req.clean, {
        "source": url,
        "asr_model": req.asr_model,
        "clean_model": req.clean_model,
        "clean_base_url": req.clean_base_url,
        "clean_api_key": req.clean_api_key,
        "engine_kwargs": ek,
        "cookies": req.cookies or {},
    })
    _submit(t, user)
    return {"id": t.id, "quota": auth.USAGE.summary(user)}


@app.post("/api/transcribe/file")
async def api_transcribe_file(
    file: UploadFile = File(...),
    engine: str = Form("bcut"),
    fmt: str = Form("srt"),
    clean: str = Form("llm"),
    asr_model: str = Form(""),
    clean_model: str = Form(""),
    clean_base_url: str = Form(""),
    clean_api_key: str = Form(""),
    asr_api_key: str = Form(""),
    asr_base_url: str = Form(""),
    local_model: str = Form(""),
    keep_media: bool = Form(True),
    request: Request = None,
):
    user = require_quota(request)
    name = file.filename or "upload"
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型 {ext}")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 用任务 id 命名上传文件：任务删除时可连带清理，播放器也能按 id 找回
    tid_early = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{tid_early}{ext}"

    size = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"保存上传文件失败：{exc}")

    ek = {}
    if asr_api_key:
        ek["api_key"] = asr_api_key
    if asr_base_url:
        ek["base_url"] = asr_base_url
    if local_model:
        ek["model_dir"] = local_model

    t = _mk_task(name, "file", engine, fmt, clean, {
        "source": str(dest),
        "upload_path": str(dest),
        "keep_upload": keep_media,
        "asr_model": asr_model or None,
        "clean_model": clean_model or None,
        "clean_base_url": clean_base_url or None,
        "clean_api_key": clean_api_key or None,
        "engine_kwargs": ek,
    }, tid=tid_early)
    _submit(t, user)
    return {"id": t.id, "size": size, "quota": auth.USAGE.summary(user)}


def _visible(user: auth.User, t: Task) -> bool:
    """admin 看全部；guest 只看自己的。

    老数据（owner 为空）在多账户模式下归给 admin，guest 看不到，
    避免历史任务泄露给访客。
    """
    if user.is_admin:
        return True
    return bool(t.owner) and t.owner == user.name


def _get_owned(tid: str, user: auth.User) -> Task:
    t = QUEUE.get(tid)
    if not t or not _visible(user, t):
        raise HTTPException(404, "任务不存在")
    return t


@app.get("/api/tasks")
def api_tasks(request: Request):
    u = require_user(request)
    return {"tasks": [t.public() for t in QUEUE.list() if _visible(u, t)]}


@app.get("/api/tasks/{tid}")
def api_task(tid: str, request: Request):
    return _get_owned(tid, require_user(request)).public()


@app.get("/api/tasks/{tid}/events")
def api_task_events(tid: str, request: Request):
    t = _get_owned(tid, require_user(request))
    q = QUEUE.subscribe(t)

    def gen():
        try:
            # 已结束的任务：把历史事件发完就关
            while True:
                try:
                    ev = q.get(timeout=1.0)
                except _queue.Empty:
                    if t.status in ("done", "error", "cancelled"):
                        break
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("stage") in ("done", "error", "cancelled"):
                    break
        finally:
            QUEUE.unsubscribe(t, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/tasks/{tid}/cancel")
def api_task_cancel(tid: str, request: Request):
    _get_owned(tid, require_user(request))
    return {"ok": QUEUE.cancel(tid)}


@app.delete("/api/tasks/{tid}")
def api_task_delete(tid: str, request: Request):
    _get_owned(tid, require_user(request))
    return {"ok": QUEUE.delete(tid)}


@app.get("/api/tasks/{tid}/content")
def api_task_content(tid: str, request: Request = None):
    """取转录结果全文 + 分段，供播放器/编辑器/对话使用。"""
    t = _get_owned(tid, require_user(request))
    if not t.output_path or not os.path.exists(t.output_path):
        raise HTTPException(404, "结果文件不存在")
    suffix = Path(t.output_path).suffix.lower()
    text = Path(t.output_path).read_text(encoding="utf-8", errors="ignore")
    segments = []
    if suffix == ".json":
        try:
            data = json.loads(text)
            segments = data.get("segments") or []
        except ValueError:
            pass
    elif suffix == ".srt":
        segments = _parse_srt(text)
    elif suffix == ".txt":
        # txt 无时间轴：按非空行切段，-1 表示无时间戳
        segments = [{"text": ln.strip(), "start_ms": -1, "end_ms": -1}
                    for ln in text.split("\n") if ln.strip()]
    return {"id": tid, "fmt": suffix.lstrip("."), "text": text,
            "segments": segments, "source": t.source}


def _parse_srt(text: str) -> list[dict]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = [l for l in block.split("\n") if l.strip()]
        if len(lines) < 3:
            continue
        try:
            times = lines[1].split("-->")
            out.append({
                "start_ms": _srt_ms(times[0].strip()),
                "end_ms": _srt_ms(times[1].strip()),
                "text": " ".join(lines[2:]),
            })
        except (ValueError, IndexError):
            continue
    return out


def _srt_ms(s: str) -> int:
    hms, _, ms = s.partition(",")
    h, m, sec = hms.split(":")
    return ((int(h) * 60 + int(m)) * 60 + int(sec)) * 1000 + int(ms or 0)


@app.get("/api/tasks/{tid}/download")
def api_task_download(tid: str, fmt: str = "", request: Request = None):
    t = _get_owned(tid, require_user(request))
    if not t.output_path or not os.path.exists(t.output_path):
        raise HTTPException(404, "结果文件不存在")
    path = Path(t.output_path)
    if fmt and fmt != path.suffix.lstrip("."):
        # 现场转换格式
        content = api_task_content(tid, request)
        segs = [Segment(s["text"], s["start_ms"], s["end_ms"]) for s in content["segments"]]
        if not segs:
            segs = [Segment(content["text"])]
        tr = Transcript(segs, source=t.source)
        tmp = OUTPUT_DIR / f"{tid}.{fmt}"
        tr.save(str(tmp))
        path = tmp
    return FileResponse(str(path), filename=f"{Path(t.source).stem or 'transcript'}{path.suffix}")


class EditReq(BaseModel):
    segments: list[dict]


@app.post("/api/tasks/{tid}/save")
def api_task_save(tid: str, req: EditReq, request: Request = None):
    """保存用户在线编辑后的分段。"""
    t = _get_owned(tid, require_user(request))
    segs = [Segment(s.get("text", ""), int(s.get("start_ms", -1)),
                    int(s.get("end_ms", -1))) for s in req.segments]
    tr = Transcript(segs, source=t.source)
    ext = Path(t.output_path).suffix if t.output_path else ".srt"
    target = OUTPUT_DIR / f"{tid}{ext}"
    try:
        tr.save(str(target))
    except ValueError:
        target = OUTPUT_DIR / f"{tid}.txt"
        tr.save(str(target))
    t.output_path = str(target)
    QUEUE._save()
    return {"ok": True, "output": str(target)}


# ---------------------------------------------------------------- LLM 派生功能


class LLMReq(BaseModel):
    tid: str
    base_url: str
    api_key: str
    model: str
    task: str = "summarize"      # summarize / polish / chat
    question: str = ""
    history: list[dict] = []
    intensity: str = "medium"
    style: str = "outline"


def _llm_call(base_url: str, api_key: str, model: str, messages: list[dict],
              max_tokens: int = 4096, temperature: float = 0.3) -> str:
    """统一的 LLM 调用（非流式）。"""
    import requests
    url = f"{base_url.rstrip('/')}/chat/completions"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages,
              "max_tokens": max_tokens, "temperature": temperature},
        timeout=(15, 300),
    )
    r.encoding = "utf-8"
    if r.status_code != 200:
        raise HTTPException(502, f"模型服务返回 {r.status_code}：{r.text[:300]}")
    try:
        data = r.json()
    except ValueError:
        raise HTTPException(502, "模型服务返回的不是 JSON")
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()


def _load_segments(tid: str) -> tuple[list[dict], str]:
    t = QUEUE.get(tid)
    if not t or not t.output_path or not os.path.exists(t.output_path):
        raise HTTPException(404, "结果文件不存在")
    c = api_task_content(tid)
    segs = c["segments"] or [{"text": c["text"], "start_ms": -1, "end_ms": -1}]
    return segs, t.source


def _ts(ms: int) -> str:
    if ms is None or ms < 0:
        return ""
    s, _ = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _chunks(segs: list[dict], size: int = 40) -> list[str]:
    out = []
    for i in range(0, len(segs), size):
        part = segs[i:i + size]
        lines = [f"[{_ts(s.get('start_ms', -1))}] {s['text']}" for s in part]
        out.append("\n".join(lines))
    return out


@app.post("/api/llm/summarize")
def api_llm_summarize(req: LLMReq):
    segs, src = _load_segments(req.tid)
    full = "\n".join(f"[{_ts(s.get('start_ms', -1))}] {s['text']}" for s in segs)
    if len(full) > 12000:
        segs_for_llm = segs[:]
        full = _reduce_long(segs_for_llm, req)
    prompt = (
        "下面是一份音视频转录稿。请输出结构化的中文总结，要求：\n"
        "1. 一句话概括（不超过 50 字）\n"
        "2. 核心要点（5-10 条，每条尽量带上 [时间戳]）\n"
        "3. 值得记录的原话（2-4 条，带 [时间戳]）\n"
        "4. 如果涉及操作步骤或待办事项，单列「行动项」\n"
        "不要编造原文没有的内容。\n\n"
        f"转录稿：\n{full}"
    )
    out = _llm_call(req.base_url, req.api_key, req.model,
                    [{"role": "user", "content": prompt}], max_tokens=3000)
    return {"result": out, "source": src}


@app.post("/api/llm/polish")
def api_llm_polish(req: LLMReq):
    segs, src = _load_segments(req.tid)
    rules = {
        "light": "只删除语气词（嗯、啊、那个、就是）、口吃重复，其余一字不改。",
        "medium": "删除语气词与口头禅，修正明显的同音错字和语病，保持原意和口语风格。",
        "heavy": "整理为通顺的书面表达，合并碎片句，可调整语序，但不得增删事实。",
    }
    rule = rules.get(req.intensity, rules["medium"])
    full = "\n".join(f"[{_ts(s.get('start_ms', -1))}] {s['text']}" for s in segs)
    if len(full) > 12000:
        full = _reduce_long(segs, req)
    prompt = (
        f"你是专业文稿校对员。{rule}\n"
        "保留每行开头的 [时间戳] 标记，行数与顺序必须完全一致。\n"
        "只输出处理后的文稿，不要解释。\n\n"
        f"原文：\n{full}"
    )
    out = _llm_call(req.base_url, req.api_key, req.model,
                    [{"role": "user", "content": prompt}], max_tokens=8000)
    return {"result": out, "source": src}


def _reduce_long(segs: list[dict], req: LLMReq, budget: int = 10000) -> str:
    """长文本：先分段摘要再拼接，控制发送量。"""
    parts = _chunks(segs, 60)
    summaries = []
    for i, p in enumerate(parts[:20], 1):
        s = _llm_call(req.base_url, req.api_key, req.model,
                      [{"role": "user", "content":
                        "把下面这段转录压缩成要点，保留关键信息和时间戳标记，不超过 200 字：\n" + p}],
                      max_tokens=600, temperature=0.2)
        summaries.append(s)
    joined = "\n\n".join(summaries)
    return joined[:budget]


@app.post("/api/llm/chat")
def api_llm_chat(req: LLMReq):
    segs, src = _load_segments(req.tid)
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")

    # 三级策略：短文本全文；长文本检索相关段落
    full = "\n".join(f"[{_ts(s.get('start_ms', -1))}] {s['text']}" for s in segs)
    if len(full) <= 8000:
        context = full
        mode = "full"
    else:
        hits = _bm25_search(segs, question, top_k=30)
        context = "\n".join(f"[{_ts(s.get('start_ms', -1))}] {s['text']}" for s in hits)
        mode = "retrieval"

    system = (
        "你是转录稿问答助手。只依据提供的转录内容回答，不要编造。\n"
        "回答中如引用某句话，在句末标注其 [时间戳]。\n"
        "如果转录里确实找不到答案，直接说「文稿中未提及」。"
    )
    messages = [{"role": "system", "content": system}]
    for h in (req.history or [])[-6:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    messages.append({"role": "user", "content": f"转录内容：\n{context}\n\n问题：{question}"})

    out = _llm_call(req.base_url, req.api_key, req.model, messages, max_tokens=2000)
    return {"result": out, "mode": mode, "source": src}


def _bm25_search(segs: list[dict], query: str, top_k: int = 30) -> list[dict]:
    """纯 Python BM25，无需向量库。中文按字符 bigram 切分。"""
    import math
    import re as _re

    def tokenize(s: str) -> list[str]:
        s = s.lower()
        chars = _re.findall(r"[\u4e00-\u9fff]|[a-z0-9]+", s)
        grams = list(chars)
        for i in range(len(chars) - 1):
            if len(chars[i]) == 1 and len(chars[i + 1]) == 1:
                grams.append(chars[i] + chars[i + 1])
        return grams

    docs = [tokenize(s["text"]) for s in segs]
    n = len(docs)
    if n == 0:
        return segs[:top_k]
    avgdl = sum(len(d) for d in docs) / n or 1
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    k1, b = 1.5, 0.75
    q = set(tokenize(query))
    scores = []
    for i, d in enumerate(docs):
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for t in q:
            if t not in tf:
                continue
            idf = math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
            score += idf * (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl))
        scores.append((score, i))
    scores.sort(reverse=True)
    picked = sorted(i for _, i in scores[:top_k])
    return [segs[i] for i in picked]


class TestReq(BaseModel):
    base_url: str
    api_key: str
    model: str
    kind: str = "llm"     # llm / asr


@app.post("/api/test/connection")
def api_test_connection(req: TestReq):
    """测试服务商配置是否可用。不记录 key。"""
    import requests
    base = req.base_url.rstrip("/")
    try:
        if req.kind == "llm":
            r = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {req.api_key}",
                         "Content-Type": "application/json"},
                json={"model": req.model,
                      "messages": [{"role": "user", "content": "回复 OK"}],
                      "max_tokens": 8},
                timeout=(10, 30),
            )
            if r.status_code != 200:
                return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
            return {"ok": True, "detail": "连接成功"}
        r = requests.get(f"{base}/models",
                         headers={"Authorization": f"Bearer {req.api_key}"},
                         timeout=(10, 20))
        if r.status_code == 200:
            return {"ok": True, "detail": "连接成功"}
        return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}


@app.get("/api/audio/{tid}")
def api_audio(tid: str, request: Request = None):
    """提供原始媒体供播放器使用。"""
    t = _get_owned(tid, require_user(request))
    rt = getattr(t, "_runtime", {}) or {}
    # 上传文件已持久化到 UPLOAD_DIR 时按 id 找回（服务重启后 _runtime 已空）
    path = rt.get("upload_path") or ""
    if not path or not os.path.exists(path):
        for p in UPLOAD_DIR.glob(f"{tid}*"):
            path = str(p)
            break
    if not path or not os.path.exists(path):
        path = t.source if os.path.exists(t.source) else ""
    if not path:
        raise HTTPException(404, "原始媒体已清理，无法播放")
    return FileResponse(path)


@app.get("/api/health")
def api_health():
    return {"ok": True, "queued": QUEUE._pending.qsize()}


def main():
    import argparse
    import uvicorn

    p = argparse.ArgumentParser(description="audio-transcribe web")
    # 云平台（Railway / Render / Fly）通过 $PORT 注入端口，且要求监听 0.0.0.0
    p.add_argument("--host", default=os.environ.get("AT_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    p.add_argument("--concurrency", type=int, default=int(os.environ.get("AT_CONCURRENCY", "1")))
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()

    QUEUE.concurrency = max(1, min(8, a.concurrency))

    # 容器/服务器上不弹浏览器：监听非回环地址时自动关掉
    no_browser = a.no_browser or a.host not in ("127.0.0.1", "localhost")
    if not no_browser:
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{a.host}:{a.port}")).start()

    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
