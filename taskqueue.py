"""任务队列：单任务并发（可配），带进度事件与取消。

设计：
  - 线程池 + 有界队列，默认并发 1（避免云转录接口限流）
  - 每个任务一个内存事件缓冲，SSE 订阅时先补发历史事件，再接实时流
  - 任务元数据落盘到 ~/.audio-transcribe/tasks.json，重启不丢失历史
  - 取消是协作式的：在 pipeline 的可中断边界检查
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HOME = Path(os.environ.get("AUDIO_TRANSCRIBE_HOME", Path.home() / ".audio-transcribe"))
OUTPUT_DIR = HOME / "outputs"
UPLOAD_DIR = HOME / "uploads"
TASKS_FILE = HOME / "tasks.json"


@dataclass
class Task:
    id: str
    kind: str                  # transcribe
    source: str = ""           # 文件名或链接（展示用）
    source_type: str = ""      # file / url
    engine: str = ""
    fmt: str = "srt"
    clean: str = "llm"
    status: str = "queued"     # queued / running / done / error / cancelled
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    output_path: str = ""
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    degraded: bool = False
    elapsed: float = 0.0
    owner: str = ""          # 提交者（多账户模式下用于隔离历史）
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _events: list = field(default_factory=list, repr=False)
    _subs: list = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # 可序列化字段白名单（不能直接 asdict：会深拷贝线程锁）
    _PUBLIC_FIELDS = (
        "id", "kind", "source", "source_type", "engine", "fmt", "clean",
        "status", "stage", "progress", "message", "output_path", "error",
        "created", "started", "finished", "degraded", "elapsed", "owner",
    )

    def public(self) -> dict:
        return {k: getattr(self, k) for k in self._PUBLIC_FIELDS}


class Queue:
    def __init__(self, concurrency: int = 1):
        self.concurrency = max(1, min(8, int(concurrency or 1)))
        self._pending: queue.Queue[str] = queue.Queue()
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._runner: Callable[[Task], None] | None = None
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # ---------- 持久化 ----------

    def _load(self):
        if not TASKS_FILE.exists():
            return
        try:
            data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for raw in data:
            try:
                t = Task(
                    id=raw["id"], kind=raw.get("kind", "transcribe"),
                    source=raw.get("source", ""), source_type=raw.get("source_type", ""),
                    engine=raw.get("engine", ""), fmt=raw.get("fmt", "srt"),
                    clean=raw.get("clean", "llm"),
                )
                # 重启后，未完成的任务标记为中断（线程已不在）
                st = raw.get("status", "done")
                t.status = st if st in ("done", "error", "cancelled") else "error"
                if t.status == "error" and st in ("queued", "running"):
                    t.error = "服务重启，任务已中断"
                t.output_path = raw.get("output_path", "")
                t.message = raw.get("message", "")
                t.created = raw.get("created", time.time())
                t.finished = raw.get("finished", 0)
                t.elapsed = raw.get("elapsed", 0)
                t.degraded = raw.get("degraded", False)
                t.progress = raw.get("progress", 0)
                self._tasks[t.id] = t
            except (KeyError, TypeError):
                continue

    def _save(self):
        tmp = TASKS_FILE.with_suffix(".tmp")
        try:
            data = [t.public() for t in sorted(self._tasks.values(),
                                               key=lambda x: x.created, reverse=True)][:200]
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(TASKS_FILE)
        except OSError:
            pass

    # ---------- 生命周期 ----------

    def set_runner(self, fn: Callable[[Task], None]):
        self._runner = fn

    def start(self):
        if self._started:
            return
        self._started = True
        for i in range(self.concurrency):
            th = threading.Thread(target=self._worker, name=f"at-worker-{i}", daemon=True)
            th.start()
            self._workers.append(th)

    def _worker(self):
        while True:
            tid = self._pending.get()
            if tid is None:
                break
            with self._lock:
                task = self._tasks.get(tid)
            if not task or task.status != "queued":
                continue
            try:
                if self._runner:
                    self._runner(task)
            except Exception as exc:
                task.status = "error"
                task.error = str(exc)[:800]
                self._emit(task, {"stage": "error", "msg": task.error, "status": "error"})
            finally:
                task.finished = time.time()
                task.elapsed = round(task.finished - (task.started or task.created), 1)
                self._save()

    def submit(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.id] = task
        self._pending.put(task.id)
        self._emit(task, {"stage": "queued", "msg": "任务已排队", "status": "queued"})
        self._save()
        return task

    def get(self, tid: str) -> Task | None:
        with self._lock:
            return self._tasks.get(tid)

    def list(self, limit: int = 100) -> list[Task]:
        with self._lock:
            items = sorted(self._tasks.values(), key=lambda t: t.created, reverse=True)
        return items[:limit]

    def cancel(self, tid: str) -> bool:
        t = self.get(tid)
        if not t:
            return False
        if t.status in ("done", "error", "cancelled"):
            return False
        t._cancel.set()
        t.status = "cancelled"
        t.message = "已取消"
        self._emit(t, {"stage": "cancelled", "msg": "任务已取消", "status": "cancelled"})
        self._save()
        return True

    def delete(self, tid: str) -> bool:
        with self._lock:
            t = self._tasks.pop(tid, None)
        if not t:
            return False
        # 删除产物文件
        if t.output_path and os.path.exists(t.output_path):
            try:
                os.unlink(t.output_path)
            except OSError:
                pass
        # 删除该任务的上传媒体（文件名以任务 id 开头）
        try:
            for p in UPLOAD_DIR.glob(f"{tid}*"):
                p.unlink(missing_ok=True)
        except OSError:
            pass
        self._save()
        return True

    # ---------- 事件 ----------

    def _emit(self, task: Task, ev: dict):
        ev = dict(ev)
        ev.setdefault("ts", time.time())
        with task._lock:
            task._events.append(ev)
            if len(task._events) > 500:
                task._events = task._events[-500:]
            subs = list(task._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    def emit(self, task: Task, ev: dict):
        """供 runner 调用。"""
        stage = ev.get("stage", "")
        if stage:
            task.stage = stage
        if ev.get("msg"):
            task.message = ev["msg"]
        if "current" in ev and "total" in ev:
            total = ev["total"] or 1
            task.progress = round(ev["current"] / total, 3)
        self._emit(task, ev)

    def subscribe(self, task: Task) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with task._lock:
            # 补发历史事件，保证刷新页面后能看到完整进度
            for ev in task._events:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    break
            task._subs.append(q)
        return q

    def unsubscribe(self, task: Task, q: queue.Queue):
        with task._lock:
            if q in task._subs:
                task._subs.remove(q)
