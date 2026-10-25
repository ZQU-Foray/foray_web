"""统一访问策略（D1 → D2）：HTTP 中间件 + WebSocket 守卫。

D2（阶段 9）新增：
- **开通闸门**：飞书首登的 member 会话在设置本站用户名+密码前，除开通页/开通接口/
  身份自查/登出/静态资源外，任何页面与接口都拦到 `/onboarding`；
- **限时改密窗口**：飞书再验证（忘记密码）后可改密 10 分钟（会话 extra.reset_until）；
- **应急账号默认关闭**：`AUTH_EMERGENCY_ENABLED=0`（生产口径"无应急账号"）。

路径分类（全部旧 /api、设备级 /api/v2、6 条 WS、页面入口都在矩阵内，
无匿名调试旁路）：

| 类别 | 路径 | 匿名 | emergency | member | member+复核不可用 |
| --- | --- | --- | --- | --- | --- |
| 公开 | /login、/auth/feishu/start|callback、/api/auth/emergency/login、/api/auth/exchange、静态资产 | 放行 | 放行 | 放行 | 放行 |
| 门户页面 | / /projects /tasks /schedule | 302 登录 | 403/302 拒 | 放行 | 302 login?recheck=1 |
| 调试页面 | /debug /debug/ | 302 登录 | 放行 | 放行 | 302 login?recheck=1 |
| 身份 API | /api/auth/me、/api/auth/logout | 401 | 放行 | 放行 | 放行（自查身份） |
| 管理 API | /api/portal/* | 401 | 403 | 放行 | 403 recheck_unavailable |
| 调试 API | 其余 /api/*（旧+新） | 401 | 放行 | 放行 | 403 recheck_unavailable |
| 调试 WS | /ws/*（6 条） | 4401 | 放行 | 放行 | 4403；命令前再查 |

错误语义：401=未认证；403=权限不足；复核不可用为独立 code（不伪称被移除）。
断网不取消鉴权：复核失败只收紧 member 为 recheck_unavailable，emergency 不受影响。
"""
import asyncio
import time
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse, RedirectResponse

from .. import config
from . import sessions

ROLE_MEMBER = "member"
ROLE_EMERGENCY = "emergency"

PUBLIC_EXACT = {"/login", "/auth/feishu/start", "/auth/feishu/callback",
                "/api/auth/exchange", "/api/auth/local/login",
                # 应急端点保持可达但自身返回 404（停用提示），便于旧书签得到明确答复
                "/api/auth/emergency/login"}
PORTAL_PAGES = {"/", "/projects", "/tasks", "/schedule", "/quality",
                "/procurement", "/attendance"}
# 开通页与找回页：未登录时也允许打开（它们自己会引导去登录/飞书）
ONBOARDING_PAGES = {"/onboarding", "/reset"}
ONBOARDING_API = {"/api/auth/account/onboard", "/api/auth/account/reset-password"}
SELF_API = {"/api/auth/me", "/api/auth/logout"}
ADMIN_API_PREFIX = "/api/portal"
_ASSET_EXT = (".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico",
              ".woff", ".woff2", ".ttf", ".map")


def extract_token(request):
    """Cookie（浏览器）或 Authorization: Bearer（wsctl）取会话令牌。"""
    tok = request.cookies.get(config.SESSION_COOKIE)
    if tok:
        return tok
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def classify(path: str) -> str:
    if path in PUBLIC_EXACT:
        return "public"
    if path == "/debug" or path.startswith("/debug/"):
        # 调试台：静态子资源公开（离线可用、无敏感信息）；入口页与任何
        # .html/其它路径都要求登录——不能靠"扩展名不在资产表"就落到默认公开。
        tail = path.rsplit("/", 1)[-1]
        if "." in tail and "." + tail.rsplit(".", 1)[-1].lower() in _ASSET_EXT:
            return "public"
        return "debug_page"
    if path.startswith("/app/"):
        # Vue 构建产物（2026-10 起）：带哈希的脚本/样式/图片等资源公开（与 /ui/ 同口径）；
        # 入口 HTML 只经受控路由返回，这里任何非资源路径一律按管理页鉴权。
        tail = path.rsplit("/", 1)[-1]
        if "." in tail and "." + tail.rsplit(".", 1)[-1].lower() in _ASSET_EXT:
            return "public"
        return "portal_page"
    if path.startswith("/portal/"):
        # 门户页面 HTML 只经受控路由（/、/projects…）暴露；/portal/pages/*.html
        # 走静态挂载时按管理页鉴权，避免匿名直取页面。
        if path.startswith("/portal/pages/"):
            return "portal_page"
        return "public"                      # 门户 css/js/fixture 等资源
    if path in ONBOARDING_PAGES:
        return "onboarding_page"
    if path in ONBOARDING_API:
        return "onboarding_api"
    if path.startswith("/ui/"):
        # 阶段 10：统一设计资源（tokens/base）。CSS 与字体等公开；
        # 若将来放进 HTML，一律按管理页鉴权（避免默认落到 public）。
        tail = path.rsplit("/", 1)[-1]
        if "." in tail and "." + tail.rsplit(".", 1)[-1].lower() in _ASSET_EXT:
            return "public"
        return "portal_page"
    if path == "/favicon.ico":
        return "public"
    if path.startswith("/docs") or path.startswith("/openapi.json"):
        return "public"                      # API 文档（记录在案）
    # 门户页面：列表页与其下的详情路径（/projects/{id} 等，阶段 5）
    if path in PORTAL_PAGES or any(
            path.startswith(p + "/") for p in PORTAL_PAGES if p != "/"):
        return "portal_page"
    if path in SELF_API:
        return "self_api"
    if path.startswith(ADMIN_API_PREFIX):
        return "admin_api"
    if path.startswith("/api/") or path.startswith("/auth/"):
        return "debug_api"
    if path.startswith("/ws/"):
        return "ws"
    return "public"                          # 其余（未来路由）默认最低公开


def _decision(path_kind: str, role: str, state: str,
              needs_onboard: bool = False):
    """返回 None（放行）或 (status, payload, is_page)。

    needs_onboard：飞书首登但尚未设置本站用户名+密码 → 只放行开通相关路径。
    """
    if path_kind in ("public",):
        return None
    if state in ("revoked", "expired"):
        return (401, {"detail": "未认证或会话已失效"}, path_kind.endswith("_page"))
    if needs_onboard:
        if path_kind in ("onboarding_page", "onboarding_api", "self_api"):
            return None
        if path_kind.endswith("_page"):
            return (302, "/onboarding", True)
        return (403, {"detail": "尚未完成本站账号开通（请先设置用户名与密码）",
                      "code": "onboarding_required"}, False)
    if state == "recheck_unavailable":
        # D2 口径（维护者确认）：断网时**仅允许调试**——管理数据必须联网核验成员身份，
        # 调试（含烧录/串口/遥测）不得因上游不可用而中断。
        if path_kind in ("self_api", "debug_page", "debug_api"):
            return None
        if path_kind == "onboarding_page":
            return None
        if path_kind.endswith("_page"):
            return (302, "/login?recheck=1", True)
        return (403, {"detail": "暂时无法确认成员身份（上游复核不可用，非退出组织）",
                      "code": "recheck_unavailable"}, False)
    # state == ok
    if path_kind == "portal_page":
        if role != ROLE_MEMBER:
            return (403, {"detail": "应急身份仅可访问调试模块"}, True)
        return None
    if path_kind == "admin_api":
        if role != ROLE_MEMBER:
            return (403, {"detail": "应急身份仅可访问调试模块"}, False)
        return None
    return None                              # debug_page/self_api/debug_api：成员+应急


async def auth_middleware(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
        return JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
    tok = extract_token(request)
    rec = sessions.get(tok) if tok else None
    state = sessions.session_state(rec) if rec else None
    kind = classify(request.url.path)
    onboard = bool(rec and rec.get("role") == ROLE_MEMBER
                   and not (rec.get("extra") or {}).get("onboarded", False))
    decision = _decision(kind, rec.get("role") if rec else None, state,
                         needs_onboard=onboard) if rec else (None, None, None)
    if rec is None:
        # 未认证：页面 → 登录；API → 401；公开 → 放行
        if kind == "public":
            return await call_next(request)
        if kind.endswith("_page"):
            return RedirectResponse("/login?next=" + request.url.path, status_code=302)
        return JSONResponse({"detail": "未认证"}, status_code=401)
    if decision is None:
        request.state.session = rec
        request.state.session_token = tok
        return await call_next(request)
    status, payload, is_page = decision
    if status == 302:
        return RedirectResponse(payload, status_code=302)
    if is_page:
        # 页面级 403（应急访问管理页）→ 引导回登录页说明，避免裸 403
        return RedirectResponse("/login?forbidden=1", status_code=302)
    return JSONResponse(payload, status_code=status)


def install(app):
    """注册认证中间件与复核后台任务（lifespan 内调用 start_recheck_loop）。"""
    app.middleware("http")(auth_middleware)


# ---------------- WebSocket 守卫 ----------------

def same_origin(connection):
    """浏览器来源必须同源；无来源头的命令行客户端继续使用会话鉴权。"""
    origin = connection.headers.get("origin") or connection.headers.get("referer")
    if not origin:
        return True
    try:
        source = urlsplit(origin)
        target = urlsplit(str(connection.url))
        scheme = {"ws": "http", "wss": "https"}.get(target.scheme, target.scheme)
        return (source.scheme in ("http", "https")
                and source.scheme == scheme
                and source.hostname == target.hostname
                and (source.port or (443 if source.scheme == "https" else 80))
                == (target.port or (443 if scheme == "https" else 80))
                and source.username is None and source.password is None)
    except ValueError:
        return False

async def ws_auth(ws, allow=(ROLE_MEMBER, ROLE_EMERGENCY)):
    """WS 握手鉴权：失败 accept 后立即以 4401/4403 关闭（客户端可读原因）。

    返回 (token, record) 或 None（已关闭）。周期复核由 check_ws_session 承担。
    """
    from fastapi import WebSocket
    assert isinstance(ws, WebSocket)
    tok = ws.cookies.get(config.SESSION_COOKIE) if ws.cookies else None
    if not tok:
        auth = ws.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    await ws.accept()
    if not same_origin(ws):
        await ws.close(code=4403, reason="跨站连接被拒绝")
        return None
    rec = sessions.get(tok) if tok else None
    state = sessions.session_state(rec) if rec else None
    if not rec or state in ("revoked", "expired"):
        await ws.close(code=4401, reason="未认证或会话已失效")
        return None
    if rec.get("role") not in allow:
        await ws.close(code=4403, reason="权限不足")
        return None
    if rec.get("role") == ROLE_MEMBER and not (rec.get("extra") or {}).get(
            "onboarded", False):
        await ws.close(code=4403, reason="尚未完成本站账号开通")
        return None
    # 复核不可用（断网）不关闭 WS：调试通道必须在断网时可用（D2 口径）
    return tok, rec


async def ws_periodic_check(ws, tok, interval=None):
    """长连接周期复核：过期/撤销/复核不可用 → 关闭（失效后不继续接受指令）。"""
    interval = interval or config.WS_RECHECK_SEC
    while True:
        await asyncio.sleep(interval)
        rec = sessions.get(tok) if tok else None
        state = sessions.session_state(rec) if rec else "expired"
        if state == "revoked" or state == "expired":
            await ws.close(code=4408 if state == "expired" else 4403,
                           reason="会话已失效")
            return
        # recheck_unavailable（断网）：保持连接，调试不受影响（D2 口径）


def ws_command_allowed(tok) -> bool:
    """命令处理前的本地撤销即时检查（ws_serial 的 open/close/tx 前）。

    断网（recheck_unavailable）不阻断调试指令；撤销/过期仍立即拒绝。
    """
    rec = sessions.get(tok) if tok else None
    if not rec:
        return False
    return sessions.session_state(rec) in ("ok", "recheck_unavailable")


def _touch_org_seen(open_id: str) -> None:
    """记录"最近确认仍在组织"（低频写，避免每轮每个会话都落盘）。"""
    if not open_id:
        return
    try:
        from . import accounts
        acc = accounts.get(open_id)
        if acc and time.time() - float(acc.get("last_seen_org_at") or 0) > 600:
            accounts.touch_org_seen(open_id)
    except Exception:
        pass


def _drop_left_member(open_id: str, detail: str = "") -> None:
    """成员已不在组织：**删除本地账号**并撤销其全部会话（不可逆，维护者口径 3）。

    只有"复核明确返回 gone"才走到这里；上游不可用（unavailable）不会触发删除。
    """
    if not open_id:
        return
    try:
        from . import accounts
        r = accounts.delete(open_id, reason="left_org", detail=detail)
        n = sessions.revoke_by_user("feishu:" + open_id)
        if r.get("deleted") or n:
            import logging
            logging.getLogger("app.auth.guard").warning(
                "成员离开组织：删除本地账号 %s（username=%s），撤销会话 %d 个",
                open_id[-8:], r.get("username"), n)
    except Exception:
        pass


# ---------------- 成员复核后台循环 ----------------

async def recheck_loop():
    """周期复核活跃 member 会话；emergency 永不复核（断网可用，计划 §6.6）。"""
    from . import feishu
    while True:
        try:
            for tok, rec in list(sessions.iter_member_sessions()):
                # 仅跳过已撤销/已离开：其余（含 recheck_unavailable）都要继续复核，
                # 否则上游恢复后会话永远无法回到 ok（恢复死锁）。
                if rec.get("revoked") or rec.get("user_gone"):
                    continue
                if time.time() >= rec.get("expires", 0):
                    continue
                state, detail = await asyncio.to_thread(
                    feishu.recheck_member, (rec.get("extra") or {}).get("open_id", ""))
                if state == "ok":
                    sessions.mark_recheck(tok, ok=True)
                    _touch_org_seen((rec.get("extra") or {}).get("open_id", ""))
                elif state == "gone":
                    sessions.mark_recheck(tok, ok=False, gone=True)
                    _drop_left_member((rec.get("extra") or {}).get("open_id", ""),
                                      detail or "复核返回已离职/已退出")
                # unavailable：不更新 last_recheck_ok，由宽限期自然收紧
        except Exception:
            pass
        await asyncio.sleep(max(1, config.RECHECK_INTERVAL_SEC))
