"""阶段 3 认证验收（真实 HTTP/WS 行为，受控测试身份 + 假飞书服务器）。

覆盖提示词要求的全部场景：
匿名拒绝（页面/API/WS）、应急登录与限频、应急越权（管理页/API → 403）、
OAuth state 重放拒绝、组织不匹配拒绝、退出撤销（API+WS）、
复核不可用（独立状态，非"被移除"）、断网新浏览器应急登录、
服务重启会话持久化、WS 命令前撤销检查。
"""
import asyncio
import json
import time
import urllib.request

import websockets

import harness
from harness import BASE, api

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def http_get(path, headers=None, follow=False):
    """返回 (status, location_or_body)。"""
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedir)
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with op.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def t_anonymous():
    print("== 匿名访问拒绝 ==")
    for path in ("/api/status", "/api/serial/open", "/api/v2/devices",
                 "/api/shoot/status", "/api/flash/log", "/api/auth/me"):
        code, _ = http_get(path)
        check(f"匿名 {path} → 401", code == 401, str(code))
    for path in ("/", "/projects", "/tasks", "/schedule", "/quality",
                 "/projects/rP1", "/tasks/rT1", "/schedule/rS1", "/debug/"):
        code, loc = http_get(path)
        check(f"匿名页面 {path} → 302 登录页",
              code == 302 and loc.startswith("/login?next="), f"{code} {loc}")
    # WS 匿名 → 4401
    async def ws_anon():
        try:
            async with websockets.connect(
                    BASE.replace("http", "ws") + "/ws/shoot", open_timeout=5) as ws:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    return ("data", msg)
                except websockets.ConnectionClosed as e:
                    return ("closed", e.rcvd.code if e.rcvd else None)
        except Exception as e:
            return ("error", str(e)[:60])
    kind, detail = asyncio.run(ws_anon())
    check("匿名 WS /ws/shoot → 4401", detail == 4401, f"{kind}:{detail}")


def t_emergency_login():
    print("== 应急登录基础 ==")
    code, body = api("/api/auth/emergency/login",
                     {"username": "test-ops", "password": "错误密码"}, auth=None)
    check("错误密码 → 401", code == 401, f"{code}")
    code, me = api("/api/auth/me")
    check("应急身份 me 返回角色", code == 200 and me.get("role") == "emergency", str(me)[:80])


def t_rate_limit():
    """限频放最后：锁定 test-ops 60s，不再影响其他场景。"""
    print("== 应急登录限频 ==")
    locked = False
    for _ in range(6):
        c, b = api("/api/auth/emergency/login",
                   {"username": "test-ops", "password": "x"}, auth=None)
        if c == 429:
            locked = True
            break
    check("连续失败触发锁定 429", locked)
    c, b = api("/api/auth/emergency/login",
               {"username": "test-ops", "password": harness._TEST_EMERGENCY_PASSWORD},
               auth=None)
    check("锁定期内正确密码仍 429", c == 429, str(c))


def t_emergency_forbidden():
    print("== 应急越权（仅调试） ==")
    code, loc = http_get("/", headers={"Authorization": "Bearer " + harness._auth["emergency"]})
    check("应急访问门户页 → 302 login?forbidden", code == 302 and "forbidden=1" in (loc or ""),
          f"{code} {loc}")
    code, body = api("/api/portal/overview")   # 管理 API（阶段 4 前无实现，但鉴权先行）
    check("应急访问管理 API → 403", code == 403, str(code))
    code, body = api("/api/status")
    check("应急访问调试 API → 200", code == 200)
    code, body = api("/api/shoot/status")
    check("应急访问 shoot → 200", code == 200)


def t_oauth():
    print("== OAuth（假飞书全链） ==")
    # 组织不匹配：fake 返回其它 tenant_key
    harness.start_fake_feishu(tenant_key="tenant-OTHER")
    sid = harness.login_member()
    check("组织不匹配拒绝建会话", sid is None)
    harness.start_fake_feishu(tenant_key="tenant-test")
    sid = harness.login_member()
    check("正确组织成员登录成功", bool(sid))
    code, me = api("/api/auth/me", auth="member")
    check("成员 me 角色正确", code == 200 and me.get("role") == "member", str(me)[:80])

    # state 重放：手工走 start 拿 state，consume 两次
    import urllib.parse
    code1, loc1 = http_get("/auth/feishu/start?next=/")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(loc1).query).get("state", [""])[0]
    check("start 发放 state", bool(state))
    # 坏 code：state 有效但 code 被假飞书拒绝 → 502，不建立会话
    code2, body2 = api("/api/auth/exchange", {"code": "BAD-code", "state": state}, auth=None)
    check("坏 code → 502 且不建立会话",
          code2 == 502 and not (isinstance(body2, dict) and body2.get("session")), f"{code2}")
    # state 已被消费（上面那次消耗了 state，无论 token 成败）→ 重放拒绝
    code3, body3 = api("/api/auth/exchange", {"code": "fake-auth-code", "state": state}, auth=None)
    check("state 重放拒绝（400）", code3 == 400, f"{code3}")


def t_static_surface():
    """静态面收敛：/debug 无尾斜杠跳转、入口页 HTML 不可匿名直取。"""
    print("== 静态面收敛（/debug 与页面 HTML） ==")
    code, loc = http_get("/debug")
    check("匿名 /debug → 302 登录页",
          code == 302 and (loc or "").startswith("/login?next="), f"{code} {loc}")
    for path in ("/debug/index.html", "/debug/portal/pages/overview.html",
                 "/portal/pages/overview.html"):
        code, loc = http_get(path)
        check(f"匿名 {path} → 302 登录页",
              code == 302 and (loc or "").startswith("/login"), f"{code} {loc}")
    for path in ("/debug/style.css", "/debug/app.js", "/debug/favicon.png",
                 "/portal/portal.css", "/portal/portal.js"):
        code, _ = http_get(path)
        check(f"匿名资源 {path} → 200（离线可用）", code == 200, str(code))

    # 已登录：/debug 307 → /debug/，页面可读；门户页面 HTML 按角色鉴权
    em = {"Authorization": "Bearer " + harness._auth["emergency"]}
    code, loc = http_get("/debug", headers=em)
    check("应急 /debug → 307 /debug/", code == 307 and loc == "/debug/", f"{code} {loc}")
    code, _ = http_get("/debug/", headers=em)
    check("应急 /debug/ → 200 入口页", code == 200, str(code))
    code, _ = http_get("/debug/index.html", headers=em)
    check("应急 /debug/index.html → 200", code == 200, str(code))
    code, loc = http_get("/portal/pages/overview.html", headers=em)
    check("应急取门户页面 HTML → 302 forbidden",
          code == 302 and "forbidden=1" in (loc or ""), f"{code} {loc}")
    if not harness._auth.get("member"):
        harness.start_fake_feishu(tenant_key="tenant-test")
        harness.login_member()
    me = {"Authorization": "Bearer " + harness._auth["member"]}
    code, _ = http_get("/portal/pages/overview.html", headers=me)
    check("成员取门户页面 HTML → 200", code == 200, str(code))


def _extra_account_login(username):
    """专用账号登录（不存在/失败返回 None，不抛异常）。"""
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    req = urllib.request.Request(
        BASE + "/api/auth/emergency/login",
        data=json.dumps({"username": username,
                         "password": harness._TEST_EMERGENCY_PASSWORD}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(NoRedir).open(req, timeout=10) as r:
            for k, v in r.headers.items():
                if k.lower() == "set-cookie" and "dsh_session=" in v:
                    return v.split("dsh_session=", 1)[1].split(";", 1)[0]
    except urllib.error.HTTPError:
        return None
    return None


def t_logout_and_ws():
    print("== 退出撤销（API + WS） ==")
    tok = _extra_account_login("test-ops2")
    if not tok:  # 首次需要创建账号
        from app.auth import emergency as _em
        from app import config as _cfg
        _em.create("test-ops2", harness._TEST_EMERGENCY_PASSWORD, note="e2e logout")
        tok = _extra_account_login("test-ops2")
    check("新会话建立", bool(tok))
    code, _ = http_get("/api/status", headers={"Authorization": "Bearer " + tok})
    check("新会话 API 可用", code == 200, str(code))
    # WS 连接 + 中途 logout → 周期检查应关闭
    async def ws_revoke():
        async with websockets.connect(
                BASE.replace("http", "ws") + "/ws/shoot",
                additional_headers={"Authorization": "Bearer " + tok},
                open_timeout=5) as ws:
            req2 = urllib.request.Request(
                BASE + "/api/auth/logout", data=b"{}", method="POST",
                headers={"Authorization": "Bearer " + tok})
            urllib.request.urlopen(req2, timeout=10).read()
            try:
                # 复核周期 2s（测试加速）。无遥测帧时 recv 会超时——那**不代表失败**
                # （此前把它当失败，与 2s 复核周期赛跑，导致全量回归偶发假失败）；
                # 超时只继续等待，deadline 前必须收到 4403 关闭才算通过。
                deadline = time.time() + 16
                while time.time() < deadline:
                    try:
                        await asyncio.wait_for(ws.recv(), 2)
                    except asyncio.TimeoutError:
                        continue
                return "timeout-no-close"
            except websockets.ConnectionClosed as e:
                return e.rcvd.code if e.rcvd else None
    code_close = asyncio.run(ws_revoke())
    check("logout 后 WS 被关闭（4403）", code_close == 4403, str(code_close))
    code, _ = http_get("/api/status", headers={"Authorization": "Bearer " + tok})
    check("logout 后 API 401", code == 401, str(code))


def t_ws_command_guard():
    print("== WS 命令前撤销检查 ==")
    # 应急会话连 /ws/serial，登录另一会话把它 revoke，然后发 open 命令
    tok = harness._auth["emergency"]
    async def ws_cmd():
        async with websockets.connect(
                BASE.replace("http", "ws") + "/ws/serial",
                additional_headers={"Authorization": "Bearer " + tok},
                open_timeout=5) as ws:
            # 撤销该会话（通过新建管理员操作不可行——直接用 logout 端点会撤销当前
            # 共享测试会话；改为专用会话）
            return ws
    from app.auth import emergency as _em
    _em.create("test-ops3", harness._TEST_EMERGENCY_PASSWORD, note="e2e ws-cmd")
    tok2 = _extra_account_login("test-ops3")

    async def scenario():
        async with websockets.connect(
                BASE.replace("http", "ws") + "/ws/serial",
                additional_headers={"Authorization": "Bearer " + tok2},
                open_timeout=5) as ws:
            r2 = urllib.request.Request(
                BASE + "/api/auth/logout", data=b"{}", method="POST",
                headers={"Authorization": "Bearer " + tok2})
            urllib.request.urlopen(r2, timeout=10).read()
            try:
                await ws.send(json.dumps({"type": "open", "port": "auto"}))
                msg = await asyncio.wait_for(ws.recv(), 5)
                return ("data", msg[:60])
            except websockets.ConnectionClosed as e:
                return ("closed", e.rcvd.code if e.rcvd else None)
            except asyncio.TimeoutError:
                return ("timeout", None)
    kind, detail = asyncio.run(scenario())
    check("撤销会话发控制指令被拒（4403）",
          kind == "closed" and detail == 4403, f"{kind}:{detail}")


def t_recheck_unavailable():
    print("== 复核不可用（独立状态） ==")
    # 成员会话 + fake 复核失败 → 宽限后 403 recheck_unavailable（非 401）
    harness.start_fake_feishu(tenant_key="tenant-test", fail_contact=True)
    sid = harness.login_member()
    check("复核故障下仍可完成登录（初始 ok）", bool(sid))
    # 等待：interval 2s + grace 3s → 约 5-8s 后收紧
    deadline = time.time() + 15
    got, debug_code = None, None
    while time.time() < deadline:
        # D2 口径：复核不可用只收紧管理面；调试面必须继续可用（断网仅调试）
        code, body = http_get("/api/portal/overview",
                              headers={"Authorization": "Bearer " + sid})
        debug_code, _ = http_get("/api/status",
                                 headers={"Authorization": "Bearer " + sid})
        if code == 403:
            got = body
            break
        time.sleep(1)
    check("复核不可用 → 管理 API 403（非 401 被移除）", got is not None, str(got)[:80])
    check("复核不可用 → 调试 API 仍 200（断网仅调试，D2）", debug_code == 200,
          str(debug_code))
    # http_get 在 HTTPError 时只返回 Location，取 body 需直连一次
    import urllib.error as _ue
    import urllib.request as _ur
    body_text = ""
    try:
        _ur.urlopen(_ur.Request(BASE + "/api/portal/overview",
                                headers={"Authorization": "Bearer " + sid}),
                    timeout=10)
    except _ue.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")
    except Exception as e:                      # noqa: BLE001
        body_text = "请求失败: " + str(e)[:60]
    check("403 带 recheck_unavailable 独立 code",
          "recheck_unavailable" in body_text, body_text[:90])
    # 恢复 fake 后自动回到 ok（宽限内复核成功）
    harness.start_fake_feishu(tenant_key="tenant-test")
    deadline = time.time() + 15
    ok_again = False
    while time.time() < deadline:
        code, _ = http_get("/api/status", headers={"Authorization": "Bearer " + sid})
        if code == 200:
            ok_again = True
            break
        time.sleep(1)
    check("上游恢复后成员访问自动恢复", ok_again)


def t_offline_new_browser():
    print("== 断网新浏览器（应急可用） ==")
    # fake 全停（模拟断网）：新应急会话登录不受影响
    harness.stop_fake_feishu()
    req = urllib.request.Request(
        BASE + "/api/auth/emergency/login",
        data=json.dumps({"username": "test-ops",
                         "password": harness._TEST_EMERGENCY_PASSWORD}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.build_opener(urllib.request.HTTPRedirectHandler()).open(req, timeout=10) as r:
        ok = r.status == 200
    check("断网时新应急登录成功", ok)
    code, _ = http_get("/api/status",
                       headers={"Authorization": "Bearer " + harness._auth["emergency"]})
    check("既有应急会话不受断网影响", code == 200, str(code))


def t_restart_persistence():
    print("== 服务重启会话持久化 ==")
    tok_before = harness._auth["emergency"]
    check("重启服务", harness.restart_service())
    # 重启后复核后台/会话文件恢复；应急会话仍有效
    code, _ = http_get("/api/status", headers={"Authorization": "Bearer " + tok_before})
    check("重启后既有应急会话仍有效", code == 200, str(code))
    # 重建 fake + member 会话同理
    harness.start_fake_feishu(tenant_key="tenant-test")
    sid = harness.login_member()
    check("重启后成员 OAuth 可再登录", bool(sid))


def run():
    t_anonymous()
    t_emergency_login()
    t_emergency_forbidden()
    t_oauth()
    t_static_surface()
    t_logout_and_ws()
    t_ws_command_guard()
    t_recheck_unavailable()
    t_offline_new_browser()
    t_restart_persistence()
    t_rate_limit()
    # 收尾：恢复默认 fake（emergency 会话经重启验证仍有效，无需重建）
    harness.start_fake_feishu(tenant_key="tenant-test")


def _ensure_service():
    """独立运行时自起服务（run_all 内已有服务则复用）。"""
    import urllib.request as _u
    try:
        _u.urlopen(BASE + "/login", timeout=2)
        return True
    except Exception:
        pass
    return harness.start_service(with_member=True)


if __name__ == "__main__":
    if not _ensure_service():
        print("✗ 测试服务未就绪")
        raise SystemExit(1)
    try:
        run()
    finally:
        harness.stop_service()
    print(f"\n认证: {len(PASS)} 通过, {len(FAIL)} 失败")
    raise SystemExit(1 if FAIL else 0)
