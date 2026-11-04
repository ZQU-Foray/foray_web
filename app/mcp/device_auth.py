# -*- coding: utf-8 -*-
"""MCP 令牌自助签发的设备码授权（2026-11，RFC 8628 风格）。

为什么要有它：agent 令牌原本只能由维护者用 curl + 浏览器会话手工签发，
成员想接自己的 AI 客户端得先找维护者。这里把「签发」交还给成员本人：
**飞书只用来证明你是谁**，换出来的仍是本站 agent 令牌（可撤销、按 scope 收窄、
可审计），飞书 token 一个字节都不落盘。

端点（前缀 /oauth/device）：

| 方法 | 路径 | 谁用 | 鉴权 |
| --- | --- | --- | --- |
| POST | /oauth/device/authorize | CLI 申请设备码 | 无（按 IP 限频） |
| POST | /oauth/device/token | CLI 轮询取令牌 | 无（凭 device_code） |
| GET | /oauth/device | 人在浏览器打开确认页 | 无（页面自己引导登录） |
| POST | /oauth/device/confirm | 浏览器提交批准/拒绝 | 成员会话（走 admin_api 门禁） |

错误码与 RFC 8628 对齐：`authorization_pending` / `slow_down` /
`access_denied` / `expired_token`；协议层错误统一 `invalid_request`。
"""
import html
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import config
from ..auth import agents, guard
from . import device_store, scopes

router = APIRouter()

# 确认页限频（防有人拿页面当用户码爆破台）：按 IP 计数，与申请共用一个桶
_confirm_lock = threading.Lock()
_confirm_fail = {}          # ip -> [窗口起点, 失败次数]
_CONFIRM_MAX_FAIL = 10


def _client_ip(request: Request) -> str:
    return getattr(getattr(request, "client", None), "host", "") or ""


def _base_url(request: Request) -> str:
    """确认页地址：协议与主机取自本次请求（仅供同一个人看，无跨站回跳）。"""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http")
    scheme = "https" if proto.split(",")[0].strip() in ("https", "wss") else "http"
    host = request.headers.get("host") or request.url.netloc or "127.0.0.1"
    return "%s://%s" % (scheme, host)


def _scopes_for(open_id: str) -> list:
    """按角色派生 scope：所有成员得默认只读域，维护者额外得 sync.read。

    第一版不接受调用方自选 scope —— 能自选就等于能自我提权。
    """
    out = list(scopes.DEFAULT_SCOPES)
    if open_id and open_id in config.PORTAL_MAINTAINER_IDS:
        out.append("sync.read")
    return out


def _err(error: str, status: int = 400, **extra):
    body = {"error": error}
    body.update(extra)
    return JSONResponse(body, status_code=status)


@router.post("/oauth/device/authorize")
async def device_authorize(request: Request):
    """申请设备码：返回 device_code（保密）与 user_code（给人看）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    ip = _client_ip(request)
    if not device_store.rate_ok(ip):
        return _err("slow_down", 429, error_description="申请过于频繁，请稍后再试")
    rec = device_store.create(client_name=str(body.get("client_name") or ""), ip=ip)
    base = _base_url(request)
    return JSONResponse({
        "device_code": rec["device_code"],
        "user_code": rec["user_code"],
        "verification_uri": base + "/oauth/device",
        "verification_uri_complete": base + "/oauth/device?user_code=" + rec["user_code"],
        "expires_in": rec["expires_in"],
        "interval": rec["interval"],
    })


@router.post("/oauth/device/token")
async def device_token(request: Request):
    """轮询换令牌：批准后一次性返回 agent 令牌明文。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    device_code = str((body or {}).get("device_code") or "") if isinstance(body, dict) else ""
    if not device_code:
        return _err("invalid_request", 400, error_description="缺少 device_code")
    result, payload = device_store.claim(device_code)
    if result == "pending":
        return _err("authorization_pending", 400, interval=payload["interval"])
    if result == "slow_down":
        return _err("slow_down", 400, interval=payload["interval"])
    if result == "denied":
        return _err("access_denied", 400, error_description="用户拒绝了授权")
    if result == "expired":
        return _err("expired_token", 400, error_description="设备码已失效，请重新申请")
    agent = agents.get(payload["agent_id"]) or {}
    return JSONResponse({
        "access_token": payload["token"],
        "token_type": "Bearer",
        "scope": " ".join(agent.get("scopes") or []),
        "agent_id": payload["agent_id"],
        "open_id": payload["open_id"],
        "client_name": payload["client_name"],
    })


# ---------------- 浏览器侧：确认页与批准 ----------------

_PAGE_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 32px 16px; font: 15px/1.6 -apple-system,
       "Segoe UI", "Microsoft YaHei", sans-serif; background: #f6f7f9; color: #1b1d21; }
.card { max-width: 520px; margin: 0 auto; background: #fff; border-radius: 12px;
        padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
h1 { font-size: 18px; margin: 0 0 12px; }
p { margin: 8px 0; }
code { background: #f0f1f3; padding: 2px 6px; border-radius: 4px; font-size: 14px; }
ul { padding-left: 20px; margin: 8px 0; }
li { margin: 2px 0; }
input[type=text] { width: 100%; box-sizing: border-box; padding: 10px 12px;
                   border: 1px solid #c9cdd4; border-radius: 8px; font: inherit;
                   letter-spacing: 2px; text-transform: uppercase; }
.row { display: flex; gap: 12px; margin-top: 16px; }
button, a.btn { flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid #c9cdd4;
                background: #fff; color: inherit; font: inherit; cursor: pointer;
                text-align: center; text-decoration: none; }
button.primary { background: #0b62d6; border-color: #0b62d6; color: #fff; }
.muted { color: #6b7280; font-size: 13px; }
.err { color: #b3261e; }
"""


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    doc = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
           "<title>%s</title><style>%s</style></head><body><div class=\"card\">%s</div>"
           "</body></html>") % (html.escape(title), _PAGE_CSS, body)
    return HTMLResponse(doc, status_code=status_code)


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _session_of(request: Request) -> dict:
    """公开路径由中间件直接放行，会话要自己从 cookie/Bearer 里取。"""
    from ..auth import sessions
    tok = guard.extract_token(request)
    rec = sessions.get(tok) if tok else None
    if not rec or sessions.session_state(rec) != "ok":
        return {}
    return rec


def _tenant_ok(session: dict) -> bool:
    """组织核验：登录时已核过一次，这里再按当前配置核对会话里的 tenant_key。"""
    from ..auth import feishu as auth_feishu
    cfg = auth_feishu.feishu_config() or {}
    allowed = cfg.get("allowed_tenant_keys") or []
    tenant = (session.get("extra") or {}).get("tenant_key") or ""
    return bool(tenant) and tenant in allowed


def _confirm_allowed(ip: str) -> bool:
    """确认页失败计数：同一个 IP 十次对不上用户码就冷却。"""
    now = time.time()
    with _confirm_lock:
        slot = _confirm_fail.get(ip or "-")
        if not slot or now - slot[0] >= 600:
            return True
        return slot[1] < _CONFIRM_MAX_FAIL


def _confirm_failed(ip: str) -> None:
    now = time.time()
    with _confirm_lock:
        slot = _confirm_fail.get(ip or "-")
        if not slot or now - slot[0] >= 600:
            _confirm_fail[ip or "-"] = [now, 1]
        else:
            slot[1] += 1


def _confirm_ok(ip: str) -> None:
    with _confirm_lock:
        _confirm_fail.pop(ip or "-", None)


@router.get("/oauth/device")
async def device_page(request: Request, user_code: str = ""):
    """确认页：未登录给飞书登录入口，已登录展示 scope 并批准/拒绝。"""
    code = device_store.format_user_code(user_code)
    sess = _session_of(request)
    extra = sess.get("extra") or {}
    open_id = str(extra.get("open_id") or "")
    base = _base_url(request)
    next_path = "/oauth/device" + ("?user_code=" + code if code else "")

    if not sess:
        body = ("<h1>授权 AI 客户端接入门户</h1>"
                "<p>这个页面用来批准一个 AI 客户端（MCP）读取战队只读数据。"
                "请先用飞书登录，确认是你本人在操作。</p>"
                "<form method=\"get\" action=\"%s\">"
                "<p class=\"muted\">用户码（命令行里显示的那串，如 ABCD-EFGH）</p>"
                "<input type=\"text\" name=\"user_code\" value=\"%s\" "
                "autocomplete=\"off\" spellcheck=\"false\">"
                "<div class=\"row\"><a class=\"btn primary\" href=\"%s\">"
                "用飞书登录</a><button type=\"submit\">继续</button></div></form>"
                "<p class=\"muted\">看不到用户码时先在终端运行 "
                "<code>python scripts/mcp_login.py</code>。</p>") % (
            _esc(base + "/oauth/device"), _esc(code),
            _esc("/auth/feishu/start?next=" + next_path))
        return _page("授权 AI 客户端接入", body)

    if not extra.get("onboarded"):
        body = ("<h1>先完成本站账号开通</h1>"
                "<p>飞书登录成功，但还没设置本站用户名与密码，暂时不能签发令牌。</p>"
                "<div class=\"row\"><a class=\"btn primary\" href=\"/onboarding\">"
                "去开通</a></div>")
        return _page("需要先开通账号", body, status_code=403)

    if not code:
        body = ("<h1>输入用户码</h1>"
                "<p class=\"muted\">命令行会显示一个形如 <code>ABCD-EFGH</code> 的用户码。</p>"
                "<form method=\"get\" action=\"%s\">"
                "<input type=\"text\" name=\"user_code\" autocomplete=\"off\" spellcheck=\"false\">"
                "<div class=\"row\"><button class=\"primary\" type=\"submit\">继续</button></div>"
                "</form>") % _esc(base + "/oauth/device")
        return _page("输入用户码", body)

    rec = device_store.find_by_user_code(code)
    if not rec:
        body = ("<h1>用户码无效或已过期</h1>"
                "<p class=\"err\">这串码不存在、已被使用，或者超过了 10 分钟有效期。</p>"
                "<p class=\"muted\">回到终端重新运行 <code>python scripts/mcp_login.py</code>。"
                "</p>")
        return _page("用户码无效", body, status_code=400)

    granted = _scopes_for(open_id)
    items = "".join("<li><code>%s</code> %s</li>" % (_esc(s), _esc(scopes.SCOPES.get(s, "")))
                    for s in granted)
    client = rec.get("client_name") or "未命名客户端"
    body = ("<h1>允许这个客户端读取门户数据？</h1>"
            "<p>客户端：<strong>%s</strong></p>"
            "<p>将授予只读权限：</p><ul>%s</ul>"
            "<p class=\"muted\">只读、不能修改任何数据；随时可以在门户里撤销。</p>"
            "<form method=\"post\" action=\"%s\">"
            "<input type=\"hidden\" name=\"user_code\" value=\"%s\">"
            "<div class=\"row\">"
            "<button class=\"primary\" name=\"action\" value=\"approve\" type=\"submit\">"
            "批准</button>"
            "<button name=\"action\" value=\"deny\" type=\"submit\">拒绝</button>"
            "</div></form>") % (_esc(client), items, _esc(base + "/oauth/device/confirm"),
                                _esc(code))
    return _page("批准 AI 客户端接入", body)


@router.post("/oauth/device/confirm")
async def device_confirm(request: Request):
    """浏览器批准/拒绝：需要已开通的成员会话。"""
    ip = _client_ip(request)
    if not _confirm_allowed(ip):
        return _page("尝试次数过多", "<h1>尝试次数过多</h1>"
                     "<p>用户码连续对不上，请 10 分钟后再试。</p>", status_code=429)
    content_type = (request.headers.get("content-type") or "").lower()
    if "json" in content_type:
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        try:
            body = dict(await request.form())
        except Exception:
            body = {}
    body = body if isinstance(body, dict) else {}
    if not guard.same_origin(request):
        # 纵深防御：会话 cookie 已是 SameSite=Lax，跨站表单带不上 cookie；
        # 这里再按 Origin/Referer 挡一次，避免将来有人调宽 cookie 策略
        return _page("来源不受信任", "<h1>来源不受信任</h1>"
                     "<p>请在门户页面里完成批准。</p>", status_code=403)
    user_code = str(body.get("user_code") or "")
    action = str(body.get("action") or "approve").strip().lower()
    sess = _session_of(request)
    if not sess:
        _confirm_failed(ip)
        return _page("需要登录", "<h1>请先用飞书登录</h1>"
                     "<p>批准令牌必须绑定你的成员身份。</p>", status_code=401)
    extra = sess.get("extra") or {}
    open_id = str(extra.get("open_id") or "")
    if not open_id:
        return _page("身份不足", "<h1>当前会话没有飞书身份</h1>", status_code=403)
    if not extra.get("onboarded"):
        return _page("需要先开通账号",
                     "<h1>先完成本站账号开通</h1>"
                     "<p>开通后才能签发令牌。</p>", status_code=403)
    if not _tenant_ok(sess):
        return _page("组织不匹配", "<h1>组织不匹配</h1>"
                     "<p>该飞书账号不属于本组织，不能签发令牌。</p>", status_code=403)
    rec = device_store.find_by_user_code(user_code)
    if not rec:
        _confirm_failed(ip)
        return _page("用户码无效", "<h1>用户码无效或已过期</h1>", status_code=400)
    _confirm_ok(ip)
    if action == "deny":
        device_store.deny(user_code, by=open_id)
        return _page("已拒绝", "<h1>已拒绝授权</h1>"
                     "<p class=\"muted\">终端会收到 <code>access_denied</code>。</p>")
    active = [a for a in agents.list_agents()
              if a.get("open_id") == open_id and not a.get("revoked_at")]
    if len(active) >= int(config.MCP_DEVICE_MAX_ACTIVE):
        return _page("令牌数量已达上限",
                     "<h1>令牌数量已达上限</h1><p>你已有 %d 个未撤销令牌，"
                     "请先在门户里撤销不用的令牌。</p>" % len(active), status_code=409)
    created = agents.create(
        name=(rec.get("client_name") or "mcp-device")[:40],
        open_id=open_id, scopes=_scopes_for(open_id), created_by="feishu_oauth",
        note="设备码自助签发（飞书授权）")
    device_store.approve(user_code, open_id=open_id,
                         agent_id=created["agent"]["id"], token=created["token"],
                         approved_by=open_id)
    return _page("已批准", "<h1>已批准</h1>"
                 "<p>令牌已生成，回到终端即可继续，命令行会把令牌存到本机。</p>"
                 "<p class=\"muted\">令牌明文不会在浏览器里显示，也不会重复下发。</p>")
