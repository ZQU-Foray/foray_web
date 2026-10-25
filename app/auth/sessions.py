"""持久本地会话：文件存储 + 原子写 + 状态判定。

会话令牌原文只在 Cookie/Authorization 中出现；文件存 sha256 摘要。
状态判定 session_state() 是唯一裁决口径（HTTP 中间件、WS 周期检查、
命令前检查共用），返回：
- ok                 放行（emergency 永远 ok——不复核，见计划 §6.6）
- expired            绝对有效期已过
- revoked            已退出/被撤销/成员已离开组织
- recheck_unavailable 成员上游复核超过宽限期不可用（≠被移除，单独状态）
"""
import json
import threading
import time

from .. import config
from .passwords import new_session_token, token_digest

_lock = threading.Lock()
_store = None          # {"version":1, "sessions":{digest:{...}}}


def _load() -> dict:
    global _store
    if _store is None:
        try:
            raw = json.loads(config.AUTH_SESSIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict):
                _store = raw
                return _store
        except FileNotFoundError:
            pass
        except Exception:
            pass
        _store = {"version": 1, "sessions": {}}
    return _store


def _save() -> None:
    with _lock:
        config.AUTH_SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.AUTH_SESSIONS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_store, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(config.AUTH_SESSIONS_FILE)


def create(user: str, display: str, role: str, extra: dict = None) -> dict:
    """建立会话，返回 {token, expires, ...}（token 仅此一次返回明文）。"""
    token = new_session_token()
    ttl = (config.SESSION_EMERGENCY_TTL_SEC if role == "emergency"
           else config.SESSION_MEMBER_TTL_SEC)
    now = time.time()
    record = {
        "user": user,                    # feishu:<open_id> | emergency:<name>
        "display": display,
        "role": role,                    # member | emergency
        "created": now,
        "expires": now + ttl,
        "revoked": False,
        "last_recheck_ok": now,          # member：最近一次复核成功时间
        "user_gone": False,              # member：已退出组织（复核证实）
        "extra": extra or {},
    }
    doc = _load()
    doc["sessions"][token_digest(token)] = record
    _save()
    return {"token": token, **record}


def get(token: str):
    """按令牌原文取会话记录（无则 None）。"""
    return _load()["sessions"].get(token_digest(token))


def revoke(token: str) -> bool:
    rec = get(token)
    if not rec:
        return False
    rec["revoked"] = True
    _save()
    return True


def revoke_by_user(user: str) -> int:
    n = 0
    for rec in _load()["sessions"].values():
        if rec.get("user") == user and not rec.get("revoked"):
            rec["revoked"] = True
            n += 1
    if n:
        _save()
    return n


def mark_onboarded(token: str) -> bool:
    """开通完成：把会话标记为已开通（中间件据此放行门户与调试）。"""
    rec = get(token)
    if not rec:
        return False
    rec.setdefault("extra", {})["onboarded"] = True
    _save()
    return True


def mark_reset_allowed(token: str) -> bool:
    """飞书再验证通过后，给会话一个限时改密窗口（默认 10 分钟）。"""
    rec = get(token)
    if not rec:
        return False
    rec.setdefault("extra", {})["reset_until"] = time.time() + config.AUTH_RESET_TTL_SEC
    _save()
    return True


def reset_allowed(rec: dict, now: float = None) -> bool:
    if not rec:
        return False
    until = (rec.get("extra") or {}).get("reset_until", 0)
    return bool(until) and (now or time.time()) < until


def clear_reset(token: str) -> None:
    rec = get(token)
    if rec:
        (rec.get("extra") or {}).pop("reset_until", None)
        _save()



def mark_recheck(key: str, ok: bool, gone: bool = False) -> None:
    """按会话 key（文件中的摘要键）更新复核结果。

    key 来自 iter_member_sessions()（复核循环）；会话原文令牌只在创建时
    返回一次，进程内不保留映射，故统一以摘要键为操作句柄。
    """
    doc = _load()
    rec = doc["sessions"].get(key)
    if not rec:
        return
    if ok:
        rec["last_recheck_ok"] = time.time()
    if gone:
        rec["user_gone"] = True
    _save()


def iter_member_sessions():
    for tok, rec in _load()["sessions"].items():
        if rec.get("role") == "member" and not rec.get("revoked"):
            yield tok, rec


def session_state(rec: dict, now: float = None) -> str:
    """唯一裁决口径（见模块 docstring）。"""
    now = now or time.time()
    if rec.get("revoked") or rec.get("user_gone"):
        return "revoked"
    if now >= rec.get("expires", 0):
        return "expired"
    if rec.get("role") == "emergency":
        # 应急凭据撤销：会话创建时间早于账号 revoked_after 即失效（懒检查）
        ra = (rec.get("extra") or {}).get("revoked_after", 0)
        if ra and rec.get("created", 0) < ra:
            return "revoked"
        return "ok"
    # member：复核宽限期（断网不自动取消鉴权——只收紧为不可用状态）
    if now - rec.get("last_recheck_ok", 0) > config.RECHECK_GRACE_SEC + config.RECHECK_INTERVAL_SEC:
        return "recheck_unavailable"
    return "ok"


def prune() -> int:
    """清理已过期/撤销会话（后台低频调用）。"""
    doc = _load()
    dead = [t for t, r in doc["sessions"].items()
            if session_state(r) in ("expired", "revoked")
            and time.time() - r.get("expires", 0) > 7 * 86400]
    for t in dead:
        doc["sessions"].pop(t, None)
    if dead:
        _save()
    return len(dead)
