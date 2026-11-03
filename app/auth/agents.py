# -*- coding: utf-8 -*-
"""Agent 接入凭据库（MCP，2026-11）：独立令牌 + scope。

设计要点：
- 存储：`config/auth/agents.json`（JSON、600、临时文件 + `os.replace` 原子替换）；
- **明文令牌只在创建时返回一次**，文件里只存 sha256 摘要——文件泄露不等于可用；
- 每个令牌绑定一个成员 open_id（数据可见性沿用该成员身份：组长看本组，组员看公开范围）
  与一组 scope（见 `app/mcp/scopes.py`），越权调用由 MCP 端点拒绝；
- 撤销即刻生效：`verify()` 每次都读库判撤销，不缓存"已撤销"结论；
- `last_used_at` 由调用方在成功后 `touch()`，写盘失败不影响调用。
- 审计：创建/撤销写 `agent_create`、`agent_revoke`；工具调用由 MCP 端点写 `mcp_tool_call`。
  审计里**不得**出现明文令牌或摘要。
"""
import hashlib
import json
import os
import secrets
import threading
import time

from .. import config
from .accounts import audit

_lock = threading.RLock()
_cache = {"key": None, "doc": None}
TOKEN_PREFIX = "agent_"
NAME_MAX = 40
NOTE_MAX = 200


def _path():
    return config.AUTH_AGENTS_FILE


def _empty():
    return {"version": 1, "agents": {}}


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
        if isinstance(raw, dict) and isinstance(raw.get("agents"), dict):
            raw.setdefault("version", 1)
            _cache["key"], _cache["doc"] = key, raw
            return raw
    except FileNotFoundError:
        pass
    except Exception:
        pass                          # 损坏按空库处理；创建事件在审计里可追
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


def digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def new_token() -> str:
    """agent 令牌：`agent_` + 32 字节随机 hex（只在创建响应里出现一次）。"""
    return TOKEN_PREFIX + secrets.token_hex(32)


def _public(rec: dict) -> dict:
    """对外返回的记录：去掉令牌摘要与内部字段。"""
    return {k: v for k, v in rec.items() if k != "token_digest"}


def create(name: str, open_id: str, scopes, created_by: str = "maintainer",
           note: str = "") -> dict:
    """签发一个 agent 令牌；返回 {"agent": {...}, "token": "<明文，仅此一次>"}。"""
    # scope 白名单由 MCP 模块定义（懒导入：auth 不依赖 mcp 包加载顺序）
    from ..mcp import scopes as _scopes
    name = (name or "").strip()
    if not name or len(name) > NAME_MAX:
        raise ValueError("名称不能为空且不超过 %d 个字符" % NAME_MAX)
    if not isinstance(open_id, str) or not open_id.strip():
        raise ValueError("缺少绑定的成员身份（open_id）")
    scopes = _scopes.normalize(scopes)
    if not scopes:
        raise ValueError("至少需要一个 scope")
    note = (note or "").strip()[:NOTE_MAX]
    token = new_token()
    now = time.time()
    with _lock:
        doc = _load()
        agent_id = "ag_" + secrets.token_hex(6)
        while agent_id in doc["agents"]:
            agent_id = "ag_" + secrets.token_hex(6)
        rec = {"id": agent_id, "name": name,
               "token_digest": digest(token),
               "open_id": open_id.strip(),
               "scopes": sorted({str(s).strip() for s in scopes if str(s).strip()}),
               "created_by": created_by, "created_at": now,
               "last_used_at": None, "revoked_at": None, "note": note}
        doc["agents"][agent_id] = rec
        _save(doc)
    audit("agent_create", agent=agent_id, name=name, by=created_by,
          scopes=",".join(rec["scopes"]), open_id=rec["open_id"])
    return {"agent": _public(rec), "token": token}


def verify(token: str):
    """令牌 → 记录（已撤销/不存在返回 None）。"""
    if not isinstance(token, str) or not token.strip():
        return None
    target = digest(token.strip())
    for rec in _load()["agents"].values():
        if rec.get("token_digest") == target:
            if rec.get("revoked_at"):
                return None
            return _public(rec)
    return None


def get(agent_id: str):
    rec = _load()["agents"].get(agent_id or "")
    return _public(rec) if rec else None


def list_agents() -> list:
    out = [_public(rec) for rec in _load()["agents"].values()]
    out.sort(key=lambda r: (r.get("created_at") or 0), reverse=True)
    return out


def revoke(agent_id: str, by: str = "maintainer"):
    """撤销令牌（幂等）；返回更新后的记录，不存在返回 None。"""
    with _lock:
        doc = _load()
        rec = doc["agents"].get(agent_id or "")
        if not rec:
            return None
        if not rec.get("revoked_at"):
            rec["revoked_at"] = time.time()
            rec["revoked_by"] = by
            _save(doc)
            audit("agent_revoke", agent=agent_id, by=by)
        return _public(rec)


def touch(agent_id: str) -> None:
    """记录最近使用时间；失败只丢一个时间戳，不影响调用。"""
    try:
        with _lock:
            doc = _load()
            rec = doc["agents"].get(agent_id or "")
            if not rec:
                return
            rec["last_used_at"] = time.time()
            _save(doc)
    except Exception:
        pass
