"""应急账号存储：创建 / 校验 / 轮换 / 撤销。

- 独立 JSON 文件（默认 config/auth/emergency_users.json，env 可覆盖）；
- 只存 PBKDF2 散列，无明文、无默认密码；
- 不依赖飞书、互联网或管理快照初始化——文件缺失 = 无应急账号（安全态）；
- 轮换密码可同时撤销该账号全部既有会话（revoked_after 时间戳）；
- 失败限频在登录端点（guard/main）实现，这里只管账号态。
"""
import json
import threading
import time

from .. import config
from .passwords import hash_password, verify_password

_lock = threading.Lock()


def _load() -> dict:
    try:
        raw = json.loads(config.AUTH_EMERGENCY_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("users"), dict):
            return raw
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"version": 1, "users": {}}


def _save(doc: dict) -> None:
    with _lock:
        config.AUTH_EMERGENCY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.AUTH_EMERGENCY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(config.AUTH_EMERGENCY_FILE)


def create(username: str, password: str, note: str = "") -> dict:
    """创建或重置应急账号（同名覆盖 = 轮换密码，并撤销旧会话）。"""
    if not username or len(username) > 40:
        raise ValueError("用户名需为 1-40 字符")
    doc = _load()
    existed = username in doc["users"]
    doc["users"][username] = {
        "username": username,
        "hash": hash_password(password),
        "note": note[:80],
        "created": time.time(),
        "disabled": False,
        # 轮换后旧会话失效：会话创建时间早于该值即拒绝
        "revoked_after": time.time(),
    }
    _save(doc)
    return {"username": username, "created": True, "rotated": existed}


def disable(username: str) -> dict:
    """撤销账号：禁用登录并废止其全部既有会话。"""
    doc = _load()
    u = doc["users"].get(username)
    if not u:
        raise ValueError(f"账号不存在: {username}")
    u["disabled"] = True
    u["revoked_after"] = time.time()
    _save(doc)
    return {"username": username, "disabled": True}


def enable(username: str) -> dict:
    doc = _load()
    u = doc["users"].get(username)
    if not u:
        raise ValueError(f"账号不存在: {username}")
    u["disabled"] = False
    _save(doc)
    return {"username": username, "disabled": False}


def list_users() -> list:
    out = []
    for u in _load()["users"].values():
        out.append({"username": u["username"], "note": u.get("note", ""),
                    "created": u.get("created"), "disabled": u.get("disabled", False)})
    return sorted(out, key=lambda x: x["username"])


def authenticate(username: str, password: str):
    """校验凭据。返回 (True, info) / (False, 原因)。

    原因：no_such_user / disabled / bad_password / bad_stored_hash
    （no_such_user 与 bad_password 对外统一为 401，不区分枚举暴露面。）
    """
    doc = _load()
    u = doc["users"].get(username)
    if not u:
        return False, "no_such_user"
    if u.get("disabled"):
        return False, "disabled"
    if not verify_password(password, u.get("hash", "")):
        return False, "bad_password"
    return True, {"revoked_after": u.get("revoked_after", 0),
                  "note": u.get("note", "")}
