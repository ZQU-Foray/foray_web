# -*- coding: utf-8 -*-
"""本地账号库（D2，阶段 9）：飞书首次登录开通 → 本站用户名 + 密码。

设计要点（见 docs/portal-identity-d2.md）：
- 存储：`config/auth/accounts.json`（JSON、600、临时文件 + `os.replace` 原子替换）；
- 只存 PBKDF2 散列（复用 `passwords.hash_password`），**明文密码永不落盘**；
- 用户名大小写不敏感唯一；索引在内存构建，写入后重建；
- **无公开注册**：只有飞书 OAuth 回调（已通过组织校验）才能调用 `create`；
- 审计：`config/auth/audit.log`（JSONL 追加；开通/登录成功失败/改密/重置/删除/离职删除），
  不记录密码、散列或会话令牌；超过 5 MB 自动轮转为 `.1`；
- 删除是**不可逆**的（成员退出组织时按维护者口径删除），审计里保留旧用户名以便追溯。
"""
import json
import os
import re
import threading
import time

from .. import config
from .passwords import hash_password, verify_password

_lock = threading.RLock()
USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff-]{3,32}$")
RESERVED = {"admin", "root", "system", "feishu", "portal", "administrator",
            "维护者", "管理员"}
AUDIT_MAX_BYTES = 5 * 1024 * 1024

# 读取缓存：会话状态每个已认证请求都要查一次账号，用（mtime, size）当版本键，
# 文件未变时直接复用上次解析结果；写路径在 _save 里刷新，不会读到旧值。
_cache = {"key": None, "doc": None}


# ---------------- 路径与读写 ----------------

def _path():
    return config.AUTH_ACCOUNTS_FILE


def _audit_path():
    return config.AUTH_AUDIT_FILE


def _empty():
    return {"version": 1, "accounts": {}}


def _file_key():
    """账号库的版本键（mtime 纳秒 + 大小）；文件不存在返回 None。"""
    try:
        st = _path().stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load() -> dict:
    key = _file_key()
    if key is not None and _cache["doc"] is not None and _cache["key"] == key:
        return _cache["doc"]
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("accounts"), dict):
            raw.setdefault("version", 1)
            _cache["key"], _cache["doc"] = key, raw
            return raw
    except FileNotFoundError:
        pass
    except Exception:
        pass                      # 损坏 = 视为空库；审计里留有写入记录可追
    empty = _empty()
    _cache["key"], _cache["doc"] = key, empty
    return empty


def _save(doc: dict) -> None:
    with _lock:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)        # 原子替换：崩溃不会留下半截账号库
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        # 写盘后 mtime 已变：顺手刷新读取缓存，避免下一次 _load 多读一遍文件
        _cache["key"], _cache["doc"] = _file_key(), doc


def audit(event: str, **fields) -> None:
    """追加一条审计（JSONL）。字段里**不得**出现密码/散列/令牌。"""
    try:
        p = _audit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.exists() and p.stat().st_size > AUDIT_MAX_BYTES:
                os.replace(p, p.with_name(p.name + ".1"))
        except OSError:
            pass
        rec = {"ts": round(time.time(), 3), "event": event}
        rec.update({k: v for k, v in fields.items() if v is not None})
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except Exception:
        pass                       # 审计失败不得影响登录主流程，但调用方可另记日志


# ---------------- 查询 ----------------

def _index(doc: dict):
    by_user = {}
    for open_id, acc in doc["accounts"].items():
        u = (acc.get("username") or "").strip().lower()
        if u:
            by_user[u] = open_id
    return by_user


def get(open_id: str):
    return _load()["accounts"].get(open_id)


def by_username(username: str):
    """按用户名取 (open_id, account)；大小写不敏感。"""
    doc = _load()
    oid = _index(doc).get((username or "").strip().lower())
    return (oid, doc["accounts"][oid]) if oid else (None, None)


def list_accounts() -> list:
    doc = _load()
    out = []
    for open_id, a in doc["accounts"].items():
        out.append({"open_id": open_id, "username": a.get("username"),
                    "display": a.get("display", ""), "status": a.get("status"),
                    "has_password": bool(a.get("hash")),
                    "created_at": a.get("created_at"),
                    "last_login_at": a.get("last_login_at"),
                    "last_seen_org_at": a.get("last_seen_org_at")})
    return sorted(out, key=lambda x: (x["username"] or ""))


def needs_onboarding(open_id: str) -> bool:
    a = get(open_id)
    return not (a and a.get("hash") and a.get("status") == "active")


# ---------------- 开通 / 改密 ----------------

def validate_username(username: str):
    """返回 None 或错误文案（对外可展示）。"""
    u = (username or "").strip()
    if not u:
        return "用户名不能为空"
    if not USERNAME_RE.match(u):
        return "用户名需 3-32 个字符，可用中文、字母、数字、下划线、连字符"
    if u.lower() in RESERVED:
        return "该用户名为保留名，请换一个"
    return None


def create(open_id: str, username: str, password: str, display: str = "",
           tenant_key: str = "") -> dict:
    """飞书校验通过后开通账号（幂等：同 open_id 重复调用 = 重置密码）。"""
    u = (username or "").strip()
    err = validate_username(u)
    if err:
        raise ValueError(err)
    if not password or len(password) < 8:
        raise ValueError("密码至少 8 位")
    if password.lower() == u.lower():
        raise ValueError("密码不能与用户名相同")
    with _lock:
        doc = _load()
        owner = _index(doc).get(u.lower())
        if owner and owner != open_id:
            raise ValueError("该用户名已被占用，请换一个")
        existed = open_id in doc["accounts"]
        old = doc["accounts"].get(open_id) or {}
        if old.get("status") not in (None, "active"):
            # 已停用/已删除的账号不允许重新开通，否则「禁用」形同虚设
            raise ValueError("账号已被停用，请联系管理员")
        now = time.time()
        doc["accounts"][open_id] = {
            "username": u,
            "display": display or old.get("display", ""),
            "tenant_key": tenant_key or old.get("tenant_key", ""),
            "hash": hash_password(password),
            "status": "active",
            "created_at": old.get("created_at", now),
            "password_updated_at": now,
            "last_login_at": old.get("last_login_at"),
            "last_seen_org_at": now,
            # 保留既有撤销时间戳：重置密码不该把「改密即失效」的历史抹掉
            "revoked_after": old.get("revoked_after", 0),
        }
        _save(doc)
    audit("account_onboard" if not existed else "account_password_reset",
          open_id=open_id, username=u, by="feishu")
    return {"open_id": open_id, "username": u, "created": not existed}


def set_password(open_id: str, password: str, by: str = "maintainer") -> dict:
    err = None
    if not password or len(password) < 8:
        err = "密码至少 8 位"
    if err:
        raise ValueError(err)
    with _lock:
        doc = _load()
        a = doc["accounts"].get(open_id)
        if not a:
            raise ValueError("账号不存在")
        if password.lower() == (a.get("username") or "").lower():
            raise ValueError("密码不能与用户名相同")
        a["hash"] = hash_password(password)
        a["password_updated_at"] = time.time()
        a["revoked_after"] = time.time()      # 改密即撤销旧会话
        username = a.get("username")
        _save(doc)
    audit("password_reset", open_id=open_id, username=username, by=by)
    return {"open_id": open_id, "username": username, "reset": True}


def disable(open_id: str, by: str = "maintainer") -> dict:
    with _lock:
        doc = _load()
        a = doc["accounts"].get(open_id)
        if not a:
            raise ValueError("账号不存在")
        a["status"] = "disabled"
        a["revoked_after"] = time.time()
        username = a.get("username")
        _save(doc)
    audit("account_disabled", open_id=open_id, username=username, by=by)
    return {"open_id": open_id, "disabled": True}


def enable(open_id: str) -> dict:
    with _lock:
        doc = _load()
        a = doc["accounts"].get(open_id)
        if not a:
            raise ValueError("账号不存在")
        a["status"] = "active"
        username = a.get("username")
        _save(doc)
    audit("account_enabled", open_id=open_id, username=username)
    return {"open_id": open_id, "disabled": False}


def delete(open_id: str, reason: str, detail: str = "") -> dict:
    """删除账号（不可逆）。用于成员退出组织或维护者手工清理。"""
    with _lock:
        doc = _load()
        a = doc["accounts"].pop(open_id, None)
        if not a:
            return {"open_id": open_id, "deleted": False}
        _save(doc)
    audit("account_deleted", open_id=open_id, username=a.get("username"),
          display=a.get("display"), reason=reason, detail=detail or None)
    return {"open_id": open_id, "username": a.get("username"), "deleted": True}


# ---------------- 登录校验 ----------------

def authenticate(username: str, password: str, via: str = "local"):
    """校验本地凭据。返回 (True, account) / (False, 原因)。

    原因：no_such_user / disabled / bad_password / no_password
    （对外统一 401，不区分，避免用户名枚举。）
    成功与失败都在这里写审计——调用方漏写也不会丢登录记录。
    """
    open_id, a = by_username(username)
    audit_login = {"open_id": open_id, "username": (username or "")[:40]}
    if not a:
        audit("login_failed", reason="no_such_user", **audit_login)
        return False, "no_such_user"
    if not a.get("hash"):
        audit("login_failed", reason="no_password", **audit_login)
        return False, "no_password"
    if a.get("status") != "active":
        audit("login_failed", reason="disabled", **audit_login)
        return False, "disabled"
    if not verify_password(password, a.get("hash", "")):
        audit("login_failed", reason="bad_password", **audit_login)
        return False, "bad_password"
    audit("login_ok", via=via, open_id=open_id, username=a.get("username"))
    return True, {"open_id": open_id, **{k: a.get(k) for k in
                                         ("username", "display", "revoked_after")}}


def touch_login(open_id: str) -> None:
    """登录成功后更新时间戳（审计已在 authenticate 内写入，避免重复）。"""
    with _lock:
        doc = _load()
        a = doc["accounts"].get(open_id)
        if not a:
            return
        a["last_login_at"] = time.time()
        _save(doc)


def touch_org_seen(open_id: str) -> None:
    """复核确认该成员仍在组织内（用于"最后确认在组织"时间）。"""
    with _lock:
        doc = _load()
        a = doc["accounts"].get(open_id)
        if not a:
            return
        a["last_seen_org_at"] = time.time()
        _save(doc)
