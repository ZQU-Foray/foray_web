# -*- coding: utf-8 -*-
"""设备码授权仓储（MCP 自助签发，2026-11）。

流程（RFC 8628 风格，飞书只做身份证明，对外凭据仍是本站 agent 令牌）：

1. CLI 申请设备码 → 拿到 `device_code`（保密）与 `user_code`（给人看）；
2. 用户在浏览器打开确认页，用飞书登录后批准；
3. CLI 轮询，一次性领走 agent 令牌明文。

存储要点：
- `config/auth/device_codes.json`，原子写 + 权限 600；
- **设备码与用户码都只存 sha256 摘要**：文件泄露也无法直接冒用；
- 待领取的令牌明文只在「批准 → 领取」这段窗口里留在本文件，领取成功立即删除；
- 一次性：领取成功、被拒绝、过期后记录都会被清理，同一设备码不可能换两次。
"""
import hashlib
import json
import logging
import os
import secrets
import threading
import time

from .. import config
from ..auth.accounts import audit

log = logging.getLogger(__name__)

_lock = threading.RLock()
_cache = {"key": None, "doc": None}

STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_DENIED = "denied"

# 用户码字母表：去掉容易看错的 0/O/1/I，只剩 20 个符号
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_USER_CODE_LEN = 8
_CLIENT_NAME_MAX = 40

# 来源 IP 限频（进程内计数，单 worker 部署下足够）
_rate_lock = threading.Lock()
_rate = {}          # ip -> [窗口起点, 计数]


def _path():
    return config.AUTH_DEVICE_FILE


def _empty():
    return {"version": 1, "devices": {}}


def _file_key():
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
        if isinstance(raw, dict) and isinstance(raw.get("devices"), dict):
            raw.setdefault("version", 1)
            _cache["key"], _cache["doc"] = key, raw
            return raw
    except FileNotFoundError:
        pass                          # 首次使用：库文件还没建，正常情况
    except Exception as exc:          # 损坏按空库处理：丢一批未完成授权，不影响令牌库
        log.warning("设备码库读取失败，按空库处理：%s", exc)
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
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        _cache["key"], _cache["doc"] = _file_key(), doc


def digest(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def normalize_user_code(raw: str) -> str:
    """用户码归一：去分隔符与空白、转大写（允许用户抄成 `abcd-efgh`）。"""
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def format_user_code(code: str) -> str:
    """给用户看的形式：前四位-后四位。"""
    code = normalize_user_code(code)
    if len(code) != _USER_CODE_LEN:
        return code
    return code[:4] + "-" + code[4:]


def _new_user_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_USER_CODE_LEN))


def _public(rec: dict) -> dict:
    """对外可返回的记录：去掉摘要与待领取明文。"""
    return {k: v for k, v in rec.items()
            if k not in ("device_digest", "user_digest", "token")}


def cleanup(now: float = None) -> int:
    """清理过期与被领取/拒绝的记录；返回清理条数。"""
    now = now or time.time()
    with _lock:
        doc = _load()
        keep, dropped = {}, 0
        for key, rec in doc["devices"].items():
            if rec.get("expires_at", 0) <= now or rec.get("state") in (
                    "consumed", STATE_DENIED):
                dropped += 1
                continue
            keep[key] = rec
        if dropped:
            doc["devices"] = keep
            _save(doc)
        return dropped


def create(client_name: str = "", ip: str = "", now: float = None) -> dict:
    """新建一个待批准设备码；返回 {"device_code", "user_code", ...}（明文仅此一次）。"""
    now = now or time.time()
    cleanup(now)
    device_code = secrets.token_urlsafe(32)
    user_code = _new_user_code()
    ttl = int(config.MCP_DEVICE_TTL_SEC)
    interval = int(config.MCP_DEVICE_INTERVAL_SEC)
    rec = {"device_digest": digest(device_code),
           "user_digest": digest(user_code),
           "user_code_hint": format_user_code(user_code),
           "state": STATE_PENDING,
           "client_name": (client_name or "").strip()[:_CLIENT_NAME_MAX],
           "ip": ip or "",
           "created_at": now,
           "expires_at": now + ttl,
           "interval": interval,
           "next_poll_at": now,
           "open_id": None,
           "agent_id": None,
           "token": None,
           "approved_at": None,
           "approved_by": None}
    with _lock:
        doc = _load()
        doc["devices"][rec["device_digest"]] = rec
        _save(doc)
    audit("agent_device_start", ip=ip or "-",
          client=rec["client_name"] or "-", code=rec["user_code_hint"])
    return {"device_code": device_code, "user_code": format_user_code(user_code),
            "expires_in": ttl, "interval": interval}


def find_by_user_code(raw: str):
    """按用户码找到待批准记录（返回内部记录副本，含摘要键）。"""
    target = digest(normalize_user_code(raw))
    now = time.time()
    for rec in _load()["devices"].values():
        if rec.get("user_digest") != target:
            continue
        if rec.get("state") != STATE_PENDING:
            return None
        if rec.get("expires_at", 0) <= now:
            return None
        return dict(rec)
    return None


def get_by_device(device_code: str):
    """按设备码取记录（含状态）；不存在返回 None。"""
    if not isinstance(device_code, str) or not device_code:
        return None
    rec = _load()["devices"].get(digest(device_code))
    return dict(rec) if rec else None


def _update(device_digest: str, **fields):
    with _lock:
        doc = _load()
        rec = doc["devices"].get(device_digest)
        if not rec:
            return None
        rec.update(fields)
        _save(doc)
        return dict(rec)


def approve(user_code: str, open_id: str, agent_id: str, token: str,
            approved_by: str = "", now: float = None):
    """把待批准记录标记为已批准，并暂存待领取的令牌明文。

    返回更新后的记录；记录不存在/已过期/已批准返回 None。
    """
    now = now or time.time()
    rec = find_by_user_code(user_code)
    if not rec:
        return None
    return _update(rec["device_digest"], state=STATE_APPROVED, open_id=open_id,
                   agent_id=agent_id, token=token, approved_at=now,
                   approved_by=approved_by or open_id)


def deny(user_code: str, by: str = "") -> bool:
    """用户拒绝授权：记录标记为拒绝，随后由 cleanup 清理。"""
    rec = find_by_user_code(user_code)
    if not rec:
        return False
    _update(rec["device_digest"], state=STATE_DENIED, approved_by=by)
    return True


def claim(device_code: str, now: float = None):
    """CLI 轮询取令牌。

    返回 (result, payload)：
    - ("pending", {"interval": n}) 尚未批准
    - ("slow_down", {"interval": n}) 轮询过快（不消耗有效期）
    - ("expired", None) 设备码不存在或已过期
    - ("denied", None) 用户拒绝
    - ("ok", {token, open_id, agent_id, client_name, scopes_unused}) 领取成功（一次性）
    """
    now = now or time.time()
    rec = get_by_device(device_code)
    if not rec:
        return "expired", None
    if rec.get("expires_at", 0) <= now:
        cleanup(now)
        return "expired", None
    if rec.get("state") == STATE_DENIED:
        cleanup(now)
        return "denied", None
    interval = int(rec.get("interval") or config.MCP_DEVICE_INTERVAL_SEC)
    if rec.get("state") == STATE_PENDING:
        # 未批准时也按 interval 约束轮询频率，避免 CLI 空转打满
        if now < float(rec.get("next_poll_at") or 0):
            return "slow_down", {"interval": interval}
        _update(rec["device_digest"], next_poll_at=now + interval)
        return "pending", {"interval": interval}
    token = rec.get("token")
    if not token:
        cleanup(now)
        return "expired", None
    # 一次性：先把明文从库里清掉，再返回给调用方
    _update(rec["device_digest"], state="consumed", token=None)
    cleanup(now)
    audit("agent_device_grant", agent=rec.get("agent_id") or "-",
          open_id=rec.get("open_id") or "-", ip=rec.get("ip") or "-")
    return "ok", {"token": token, "open_id": rec.get("open_id"),
                  "agent_id": rec.get("agent_id"),
                  "client_name": rec.get("client_name") or ""}


def rate_ok(ip: str, now: float = None) -> bool:
    """申请限频：每个来源 IP 每分钟 `MCP_DEVICE_RATE_PER_MIN` 次。"""
    now = now or time.time()
    limit = int(config.MCP_DEVICE_RATE_PER_MIN)
    key = ip or "-"
    with _rate_lock:
        slot = _rate.get(key)
        if not slot or now - slot[0] >= 60:
            _rate[key] = [now, 1]
            return True
        if slot[1] >= limit:
            return False
        slot[1] += 1
        return True


def reset_rate() -> None:
    """清空限频计数（测试用）。"""
    with _rate_lock:
        _rate.clear()
