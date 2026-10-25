"""飞书 OAuth 与成员状态复核（接口均为阶段 1 已核实，见 docs/feishu-setup.md）。

- 配置来源：环境变量优先（FEISHU_APP_ID/FEISHU_APP_SECRET/FEISHU_REDIRECT_URL/
  FEISHU_ALLOWED_TENANT_KEYS 逗号分隔），否则 FEISHU_CONFIG_FILE json；均无 = 未配置。
- API 基地址 FEISHU_API_BASE 可覆盖（隔离测试用本地假飞书服务器走全链 HTTP）。
- 成员复核：GET /open-apis/contact/v3/users/{open_id} 读 status.is_resigned/
  is_exited（阶段 1 复验实证字段级权限已开通）。
- 用户 OAuth scope 留空（登录只需 user_info 默认授权——阶段 1 复验修正结论）。
"""
import json
import secrets
import threading
import time
import urllib.error
import urllib.request

from .. import config

_tt_cache = {"value": None, "expire_at": 0.0}
_tt_lock = threading.Lock()
_states = {}             # oauth state -> created_ts（内存，10 分钟 TTL，一次性）
_state_lock = threading.Lock()


# ---------------- 配置 ----------------

def feishu_config():
    """返回 {app_id, app_secret, redirect_url, allowed_tenant_keys}；未配置返回 None。"""
    import os
    cfg = {
        "app_id": os.environ.get("FEISHU_APP_ID", ""),
        "app_secret": os.environ.get("FEISHU_APP_SECRET", ""),
        "redirect_url": os.environ.get("FEISHU_REDIRECT_URL", ""),
    }
    env_tk = os.environ.get("FEISHU_ALLOWED_TENANT_KEYS", "")
    if env_tk:
        cfg["allowed_tenant_keys"] = [t.strip() for t in env_tk.split(",") if t.strip()]
    if not (cfg["app_id"] and cfg["app_secret"]):
        # 尝试配置文件（env: 引用语法与 scripts/feishu_verify.py 一致）
        try:
            raw = json.loads(config.FEISHU_CONFIG_FILE.read_text(encoding="utf-8"))
            sec = raw.get("app_secret_ref", "")
            secret = (__import__("os").environ.get(sec[4:], "")
                      if sec.startswith("env:") else "")
            cfg = {
                "app_id": raw.get("app_id", ""),
                "app_secret": secret,
                "redirect_url": (raw.get("oauth") or {}).get("redirect_url", ""),
                "allowed_tenant_keys": raw.get("allowed_tenant_keys") or [],
            }
        except FileNotFoundError:
            return None
        except Exception:
            return None
    if not (cfg.get("app_id") and cfg.get("app_secret")
            and cfg.get("redirect_url")):
        return None
    return cfg


# ---------------- OAuth state（CSRF 防护，一次性） ----------------

def new_state() -> str:
    s = secrets.token_hex(24)
    with _state_lock:
        now = time.time()
        for k in [k for k, t in _states.items() if now - t > config.OAUTH_STATE_TTL_SEC]:
            _states.pop(k, None)
        _states[s] = now
    return s


def consume_state(state: str) -> bool:
    """一次性消费；不存在 / 过期 / 重放均返回 False。"""
    with _state_lock:
        ts = _states.pop(state, None)
    if ts is None:
        return False
    return time.time() - ts <= config.OAUTH_STATE_TTL_SEC


# ---------------- HTTP 基础 ----------------

def _http(method, path, body=None, headers=None, timeout=15):
    url = config.FEISHU_API_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"code": -1, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def _tenant_token(cfg):
    """应用身份令牌（缓存复用，过期前刷新；幂等语义见阶段 1 实证）。"""
    with _tt_lock:
        now = time.time()
        if _tt_cache["value"] and now < _tt_cache["expire_at"] - 120:
            return _tt_cache["value"]
        r = _http("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                  {"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]})
        if r.get("code") != 0 or not r.get("tenant_access_token"):
            return None
        _tt_cache["value"] = r["tenant_access_token"]
        _tt_cache["expire_at"] = now + r.get("expire", 7200)
        return _tt_cache["value"]


# ---------------- 登录链路 ----------------

def authorize_url(state: str, cfg=None) -> str:
    """构造授权页 URL（阶段 1 已核实端点与参数；scope 留空）。

    真实环境授权页在 accounts.feishu.cn；API 基地址被覆盖为本地假服务器
    （隔离测试）时复用同基地址。
    """
    cfg = cfg or feishu_config()
    import urllib.parse
    base = config.FEISHU_API_BASE
    if base.startswith("https://open.feishu.cn"):
        base = "https://accounts.feishu.cn"
    return base + "/open-apis/authen/v1/authorize?" + urllib.parse.urlencode({
        "client_id": cfg["app_id"],
        "response_type": "code",
        "redirect_uri": cfg["redirect_url"],
        "state": state,
    })


def exchange_code(code: str, cfg=None):
    """授权码 → user_access_token（阶段 1 已核实 v2 端点）。

    返回 (user_info, None) 或 (None, error)。
    """
    cfg = cfg or feishu_config()
    if not cfg:
        return None, "feishu_not_configured"
    r = _http("POST", "/open-apis/authen/v2/oauth/token", {
        "grant_type": "authorization_code",
        "client_id": cfg["app_id"],
        "client_secret": cfg["app_secret"],
        "code": code,
        "redirect_uri": cfg["redirect_url"],
    })
    tok = (r.get("data") or {}).get("access_token") or r.get("access_token")
    if not tok:
        return None, f"token_exchange_failed: code={r.get('code')} msg={str(r.get('msg'))[:80]}"
    ui = _http("GET", "/open-apis/authen/v1/user_info",
               headers={"Authorization": "Bearer " + tok})
    info = (ui.get("data") or {})
    if ui.get("code") != 0 or not info.get("open_id"):
        return None, f"user_info_failed: code={ui.get('code')}"
    return info, None


def tenant_allowed(info: dict, cfg=None) -> bool:
    """组织核验：tenant_key 必须在白名单内（空白名单 = 拒绝所有，不默认放行）。"""
    cfg = cfg or feishu_config()
    allowed = (cfg or {}).get("allowed_tenant_keys") or []
    return bool(allowed) and info.get("tenant_key") in allowed


# ---------------- 成员状态复核（出站 HTTP，无需公网入站） ----------------

def recheck_member(open_id: str):
    """返回 ("ok"|"gone"|"unavailable", detail)。

    gone = is_resigned/is_exited（实证字段）；unavailable = 网络/权限错误
    ——不可用不得解释为已移除（由会话层以时间宽限收紧）。
    """
    cfg = feishu_config()
    if not cfg:
        return "unavailable", "feishu_not_configured"
    tok = _tenant_token(cfg)
    if not tok:
        return "unavailable", "tenant_token_failed"
    r = _http("GET", f"/open-apis/contact/v3/users/{open_id}?user_id_type=open_id",
              headers={"Authorization": "Bearer " + tok})
    if r.get("code") != 0:
        return "unavailable", f"code={r.get('code')}"
    st = (((r.get("data") or {}).get("user")) or {}).get("status") or {}
    if st.get("is_resigned") or st.get("is_exited"):
        return "gone", "resigned_or_exited"
    return "ok", ",".join(sorted(st)) or "status_empty"
