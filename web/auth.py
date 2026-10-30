"""访问控制与每日限额。

三种模式，由环境变量决定：

1. 完全开放（默认，什么都不设）
   本地自用 / 自己部署给自己用。行为和以前完全一致。

2. 固定密码（AT_ACCESS_PASSWORD）
   演示站。所有人用同一个密码进入，按 IP 记每日限额。

3. 多账户（AT_USERS_JSON）
   演示站进阶。每个账户独立密码与限额，admin 无限额。
   格式：{"用户名": {"password": "明文或 sha256:哈希", "role": "admin"|"guest",
                     "daily_tasks": 10, "daily_minutes": 60}}

安全约束：
  - 密码比对用 secrets.compare_digest，防时序侧信道
  - token 是 HMAC 签名的，服务端不存会话表，重启不失联
  - Cookie 带 HttpOnly + SameSite=Lax；HTTPS 下自动加 Secure
  - 不打印任何密码 / token 到日志
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("AUDIO_TRANSCRIBE_HOME", Path.home() / ".audio-transcribe"))
USAGE_FILE = HOME / "usage.json"

COOKIE_NAME = "at_session"
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 天

# 每日默认限额（仅 guest 用；admin 永远不限）
DEFAULT_GUEST_TASKS = 10
DEFAULT_GUEST_MINUTES = 60


# ---------------------------------------------------------------- 配置


@dataclass
class User:
    name: str
    role: str = "guest"          # admin / guest
    # None = 不限；0 = 一次都不给。两者行为不同，别用 0 表示不限。
    daily_tasks: int | None = DEFAULT_GUEST_TASKS
    daily_minutes: int | None = DEFAULT_GUEST_MINUTES

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class Config:
    enabled: bool = False
    users: dict[str, tuple[str, User]] = field(default_factory=dict)
    secret: str = ""
    allow_register: bool = False  # 预留，当前不开放自助注册


def _sha(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _load_config() -> Config:
    """从环境变量读出配置。密码不落日志。"""
    cfg = Config(secret=os.environ.get("AT_SECRET_KEY", "").strip())

    def _opt_int(v):
        """None / "" / 缺省 → None（不限）；其余转 int。"""
        if v is None or v == "":
            return None
        return int(v)

    users_json = os.environ.get("AT_USERS_JSON", "").strip()
    if users_json:
        # 支持两种写法：直接放 JSON，或者放一个文件路径。
        # 多账户配置通常比较长，塞进环境变量容易引号转义出错，
        # 也存在 shell 历史里。指向文件更稳妥（文件权限自己控 600）。
        try:
            if users_json.startswith("{"):
                raw = json.loads(users_json)
            else:
                p = Path(users_json).expanduser()
                if not p.is_file():
                    raise SystemExit(f"AT_USERS_JSON 既不是 JSON 也不是文件：{users_json}")
                raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"AT_USERS_JSON 不是合法 JSON：{exc}") from exc
        for name, spec in raw.items():
            pwd = str(spec.get("password", ""))
            if not pwd:
                raise SystemExit(f"用户 {name} 没配 password")
            u = User(
                name=name,
                role="admin" if spec.get("role") == "admin" else "guest",
                # 显式写 null 或不写 = 不限（None）；
                # 写 0 = 一次都不给；写正数 = 按这个限额。
                # 不能简单用 spec.get(k, 默认值)，那样「不写」会悄悄
                # 拿到默认额度，文档里「不写就是不限」就成了空话。
                daily_tasks=_opt_int(spec.get("daily_tasks")),
                daily_minutes=_opt_int(spec.get("daily_minutes")),
            )
            cfg.users[name] = (pwd, u)
        cfg.enabled = bool(cfg.users)

    if not cfg.enabled:
        pwd = os.environ.get("AT_ACCESS_PASSWORD", "").strip()
        if pwd:
            cfg.users["guest"] = (pwd, User(
                name="guest",
                role="guest",
                daily_tasks=int(os.environ.get("AT_DAILY_LIMIT", DEFAULT_GUEST_TASKS)),
                daily_minutes=int(os.environ.get("AT_DAILY_MINUTES", DEFAULT_GUEST_MINUTES)),
            ))
            cfg.enabled = True

    # 全局限额覆盖（两种模式下都生效，对 guest 有效）
    task_cap = os.environ.get("AT_DAILY_LIMIT", "").strip()
    min_cap = os.environ.get("AT_DAILY_MINUTES", "").strip()
    if cfg.enabled:
        for _name, (_pwd, u) in cfg.users.items():
            if u.is_admin:
                continue
            if task_cap:
                u.daily_tasks = int(task_cap)
            if min_cap:
                u.daily_minutes = int(min_cap)

    if cfg.enabled and not cfg.secret:
        # 没给固定密钥就生成一个并落盘。落盘是为了重启后登录状态不掉，
        # 而不是每次重启换一把把所有人踢下线。
        cfg.secret = _persisted_secret()
    return cfg


def _persisted_secret() -> str:
    """密钥文件：HOME/.secret，权限 0600。不存在就生成。"""
    f = HOME / ".secret"
    try:
        if f.exists():
            val = f.read_text(encoding="utf-8").strip()
            if len(val) >= 32:
                return val
        val = secrets.token_hex(32)
        HOME.mkdir(parents=True, exist_ok=True)
        # 先建空文件再改权限，避免权限窗口期
        fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(val)
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        return val
    except OSError:
        # 写不了盘就退化到内存随机，宁可重启掉线也不能没有密钥
        return secrets.token_hex(32)


CONFIG = _load_config()


def enabled() -> bool:
    return CONFIG.enabled


# ---------------------------------------------------------------- 密码校验


def check_password(username: str, password: str) -> User | None:
    if not CONFIG.enabled:
        return None
    entry = CONFIG.users.get(username)
    if not entry:
        # 用户不存在也走一次哈希，避免时间差暴露用户名是否存在
        _sha(password)
        return None
    stored, user = entry
    if stored.startswith("sha256:"):
        ok = hmac.compare_digest(stored[7:], _sha(password))
    else:
        ok = hmac.compare_digest(stored, password)
    return user if ok else None


# ---------------------------------------------------------------- Token


def _sign(payload: str) -> str:
    return hmac.new(CONFIG.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token(user: User) -> str:
    """token = base64(json) . hmac —— 无状态，服务端不存会话表。"""
    body = json.dumps(
        {"u": user.name, "r": user.role, "exp": int(time.time()) + TOKEN_TTL},
        separators=(",", ":"), ensure_ascii=False,
    )
    import base64
    b64 = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return f"{b64}.{_sign(b64)}"


def read_token(token: str) -> User | None:
    if not token or "." not in token:
        return None
    b64, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(b64)):
        return None
    import base64
    try:
        pad = "=" * (-len(b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(b64 + pad))
    except Exception:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    name = data.get("u", "")
    entry = CONFIG.users.get(name)
    if not entry:
        # 用户被删了 → token 立刻失效
        return None
    return entry[1]


# ---------------------------------------------------------------- 用量记账


class Usage:
    """按 用户名 + 日期 记账。日期变了自然归零，不需要定时清理。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def _load(self):
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _flush(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    @staticmethod
    def today() -> str:
        return time.strftime("%Y-%m-%d")

    def _key(self, user: str) -> str:
        return f"{self.today()}|{user}"

    def get(self, user: str) -> dict:
        with self._lock:
            rec = self._data.get(self._key(user)) or {}
            return {
                "tasks": int(rec.get("tasks", 0)),
                "seconds": float(rec.get("seconds", 0)),
            }

    def check(self, user_obj: User) -> tuple[bool, str]:
        """能不能再来一个任务。admin 永远放行。"""
        if user_obj.is_admin:
            return True, ""
        cur = self.get(user_obj.name)
        # None = 不限；0 = 一次都不给。别写成 `if user_obj.daily_tasks`
        if user_obj.daily_tasks is not None and cur["tasks"] >= user_obj.daily_tasks:
            return False, f"今日转录次数已用完（{user_obj.daily_tasks} 次/天），明天 0 点重置"
        if user_obj.daily_minutes is not None and cur["seconds"] >= user_obj.daily_minutes * 60:
            return False, f"今日音频时长已用完（{user_obj.daily_minutes} 分钟/天），明天 0 点重置"
        return True, ""

    def add_task(self, user: str):
        with self._lock:
            k = self._key(user)
            rec = self._data.setdefault(k, {"tasks": 0, "seconds": 0})
            rec["tasks"] = int(rec.get("tasks", 0)) + 1
            self._flush()

    def add_seconds(self, user: str, seconds: float):
        if seconds <= 0:
            return
        with self._lock:
            k = self._key(user)
            rec = self._data.setdefault(k, {"tasks": 0, "seconds": 0})
            rec["seconds"] = float(rec.get("seconds", 0)) + seconds
            self._flush()

    def summary(self, user_obj: User) -> dict:
        cur = self.get(user_obj.name)
        if user_obj.is_admin:
            return {"role": "admin", "unlimited": True,
                    "used_tasks": cur["tasks"],
                    "used_minutes": round(cur["seconds"] / 60, 1)}
        # 注意 0 和「不限」是两回事：
        # AT_DAILY_LIMIT=0 表示一次都不给（管理员临时停用某人），
        # 以前用 `if user_obj.daily_tasks` 判断，0 会被当成不限，
        # 前端显示 left_tasks=null、额度校验也直接放行，等于没设限。
        # 裸的 None 才代表不限（AT_USERS_JSON 里可以不写这个字段）。
        lt = user_obj.daily_tasks
        lm = user_obj.daily_minutes
        return {
            "role": "guest",
            "unlimited": False,
            "limit_tasks": lt,
            "limit_minutes": lm,
            "used_tasks": cur["tasks"],
            "used_minutes": round(cur["seconds"] / 60, 1),
            "left_tasks": None if lt is None else max(0, lt - cur["tasks"]),
            "left_minutes": (None if lm is None
                             else round(max(0.0, lm - cur["seconds"] / 60), 1)),
        }


USAGE = Usage(USAGE_FILE)
