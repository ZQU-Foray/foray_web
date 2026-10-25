# -*- coding: utf-8 -*-
"""阶段 9（D2）身份用例：开通闸门 / 本站账号登录 / 断网仅调试 / 离职删除 / 重设密码 /
应急默认关闭 / 无公开注册。"""
import json
import os
import sys
import time

import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                       # noqa: E402
from harness import BASE, api        # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def note(key, value):
    print(f"  · {key}: {value}")


def _post(path, body, tok=None):
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(BASE + path, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:120]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 302：用例要断言的就是跳转本身。"""

    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get(path, tok=None):
    """裸 HTTP GET（带 Bearer，不跟随跳转），返回 (status, location, body)。"""
    req = urllib.request.Request(BASE + path)
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with _OPENER.open(req, timeout=10) as r:
            return r.status, r.headers.get("location", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("location", ""), e.read().decode("utf-8", "replace")


def _accounts():
    p = os.path.join(harness._tmpdir, "accounts.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)["accounts"]
    except Exception:
        return {}


def _audit_events():
    p = os.path.join(harness._tmpdir, "audit.log")
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    out.append(json.loads(ln))
    except Exception:
        pass
    return out


def t_onboarding_gate():
    """未开通的飞书会话：除开通页/接口外一律拦住。"""
    print("== 9-2 开通闸门（未设置用户名密码前不放行） ==")
    harness.start_fake_feishu(tenant_key="tenant-test")
    # 复现"从未开通"：清空账号库（本套件最后运行，清理不影响其它套件；
    # 其它套件依赖的账号会在它们的 login_member() 里自动重建）
    p_acc = os.path.join(harness._tmpdir, "accounts.json")
    if os.path.exists(p_acc):
        os.remove(p_acc)
    check("重启（账号库为空 + 假飞书）", harness.restart_service(env_extra={
        "PORTAL_SYNC_INTERVAL": "3600", "PORTAL_MANUAL_MIN_GAP": "1"}))
    harness._auth["emergency"] = harness._login_emergency()
    # 走真实飞书回调建立 member 会话；onboard=False → 保留"尚未开通"状态
    mm = harness.login_member(onboard=False)
    check("飞书回调建立会话（该 open_id 尚未开通）", bool(mm))
    code, r = api("/api/portal/overview", auth="member")
    check("未开通：管理 API → 403 onboarding_required",
          code == 403 and r.get("code") == "onboarding_required", f"{code} {str(r)[:80]}")
    code, loc, _ = _get("/tasks", tok=mm)
    check("未开通：门户页面 → 302 /onboarding",
          code == 302 and "onboarding" in loc, f"{code} {loc}")
    code_anon, loc_anon, _ = _get("/onboarding")
    check("匿名访问开通页 → 302 登录（页面只对已核验的飞书会话有意义）",
          code_anon == 302 and "login" in loc_anon, f"{code_anon} {loc_anon}")
    code_ob, _, _ = _get("/onboarding", tok=mm)
    check("未开通会话访问开通页 → 200（闸门放行自身）", code_ob == 200, str(code_ob))
    code, loc, _ = _get("/debug/", tok=mm)
    check("未开通：调试台也拦住（302 到开通页）",
          code == 302 and "onboarding" in loc, f"{code} {loc}")

    # 开通
    code, r = _post("/api/auth/account/onboard",
                    {"username": "alice", "password": "alice-password-1",
                     "password2": "alice-password-1"}, tok=mm)
    check("开通成功（设置用户名+密码）", code == 200 and r.get("username") == "alice",
          f"{code} {str(r)[:80]}")
    check("账号库写入 open_id → 用户名", any(a.get("username") == "alice"
                                              for a in _accounts().values()))
    code, r = api("/api/portal/overview", auth="member")
    check("开通后管理 API 放行（200）", code == 200, f"{code} {str(r)[:60]}")
    code, _, _ = _get("/debug/", tok=mm)
    check("开通后调试台放行", code == 200, str(code))
    # 无公开注册端点：匿名 401（非公开），已登录访问则 404（端点不存在）
    code_anon, _ = _post("/api/auth/account/register", {"username": "x", "password": "y"})
    code_auth, _ = _post("/api/auth/account/register",
                         {"username": "x", "password": "y"}, tok=mm)
    check("无公开注册端点（匿名 401 + 已登录 404）",
          code_anon == 401 and code_auth == 404, f"{code_anon}/{code_auth}")
    code, _ = _post("/api/auth/account/onboard",
                    {"username": "bob", "password": "bob-password-1",
                     "password2": "bob-password-1"})
    check("匿名调用开通接口被拒（401）", code == 401, str(code))
    return mm


def t_local_login_and_offline(mm):
    """本站账号登录：联网门户+调试；断网仅调试；错误密码限频。"""
    print("== 9-3 本站账号登录（联网 / 断网） ==")
    check("重启（假飞书在线、复核窄宽限）", harness.restart_service(env_extra={
        "PORTAL_SYNC_INTERVAL": "3600", "PORTAL_MANUAL_MIN_GAP": "1"}))
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    code, r = _post("/api/auth/local/login", {"username": "alice",
                                              "password": "alice-password-1"})
    check("正确用户名+密码 → 建立会话", code == 200 and r.get("session"), f"{code} {str(r)[:70]}")
    tok = r.get("session")
    code, _, _ = _get("/api/auth/me", tok=tok)
    check("本站账号会话可自查身份（200）", code == 200, str(code))
    code, _, _ = _get("/api/portal/overview", tok=tok)
    check("联网时管理数据可读（200）", code == 200, str(code))
    code, r2 = _post("/api/auth/local/login", {"username": "alice", "password": "wrong-pass-1"})
    check("错误密码 → 401（不泄露账号是否存在）", code == 401, f"{code} {str(r2)[:60]}")
    # 限频：连续失败触发 429（测试参数 MAX_FAIL=3）
    codes = [_post("/api/auth/local/login", {"username": "alice", "password": "bad-x-%d" % i})[0]
             for i in range(4)]
    check("连续失败触发锁定（429）", 429 in codes, str(codes))

    # 断网：停掉假飞书 → 复核不可用（宽限 3s）→ 管理 API 403、调试 API 放行
    tok2 = tok
    harness.stop_fake_feishu()          # 模拟断网（上游不可达）
    deadline = time.time() + 30
    code_ov, state = None, None
    while time.time() < deadline:
        code_ov, _, _ = _get("/api/portal/overview", tok=tok2)
        code_me, _, me = _get("/api/auth/me", tok=tok2)
        if code_me == 200:
            try:
                state = json.loads(me).get("recheck")
            except Exception:
                state = None
        if code_ov == 403:
            break
        time.sleep(1)
    check("断网后管理 API → 403（复核不可用，非退出组织）", code_ov == 403,
          f"{code_ov} state={state}")
    code_dev, _, _ = _get("/api/v2/devices", tok=tok2)
    check("断网后调试 API 仍可用（本站账号登录 → 仅调试）", code_dev == 200, str(code_dev))
    # 断网时仍可用本站账号登录（离线登录能力）
    code, r3 = _post("/api/auth/local/login", {"username": "alice",
                                               "password": "alice-password-1"})
    check("断网时本站账号仍可登录（离线兜底）", code == 200, f"{code} {str(r3)[:60]}")
    note("断网权限", f"管理 API={code_ov}、调试 API={code_dev}、recheck={state}")


def t_left_org_deletes_account():
    """成员退出组织：本地账号被删除 + 全部会话撤销 + 审计留痕。"""
    print("== 9-4 离职 → 删除本地账号并全撤会话 ==")
    check("重启（有账号 + 假飞书）", harness.restart_service(env_extra={
        "PORTAL_SYNC_INTERVAL": "1", "AUTH_RECHECK_INTERVAL": "2",
        "AUTH_RECHECK_GRACE": "2", "PORTAL_MANUAL_MIN_GAP": "1"}))
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    _, r = _post("/api/auth/local/login", {"username": "alice", "password": "alice-password-1"})
    tok = (r or {}).get("session")
    check("本站账号会话建立", bool(tok))
    check("会话可用（Bearer 自查 200）", _get("/api/auth/me", tok=tok)[0] == 200)
    check("账号在库", any(a.get("username") == "alice" for a in _accounts().values()))
    # 注入"已离职"，等复核循环删除
    harness._fake_feishu.member_status = {
        "is_activated": True, "is_resigned": True, "is_exited": False,
        "is_frozen": False, "is_unjoin": False}
    deadline = time.time() + 40
    gone = False
    while time.time() < deadline:
        if not any(a.get("username") == "alice" for a in _accounts().values()):
            gone = True
            break
        time.sleep(2)
    check("复核确认离职后本地账号被删除", gone, str(list(_accounts().keys()))[:60])
    code, _, _ = _get("/api/auth/me", tok=tok)
    check("其既有会话被撤销（401）", code == 401, str(code))
    ev = [e for e in _audit_events() if e.get("event") == "account_deleted"]
    check("审计记录删除事件（含原因 left_org）",
          bool(ev) and ev[-1].get("reason") == "left_org", str(ev[-1:] if ev else [])[:100])
    check("删除后无法再用本站密码登录",
          _post("/api/auth/local/login", {"username": "alice",
                                          "password": "alice-password-1"})[0] == 401)


def _hop(url):
    """不跟随跳转的 GET：返回 (status, Location)。"""
    try:
        with _OPENER.open(urllib.request.Request(url), timeout=15) as r:
            return r.status, r.headers.get("location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("location", "")


def t_password_reset_via_feishu():
    """9-5 忘记密码（联网路径）：飞书再验证 → 限时改密窗口 → 新密码可登录/旧密码失效。"""
    print("== 9-5 忘记密码（飞书再验证后重设） ==")
    check("重启（假飞书在线）", harness.restart_service(env_extra={
        "PORTAL_SYNC_INTERVAL": "3600", "PORTAL_MANUAL_MIN_GAP": "1"}))
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()          # 自动开通 e2e-member
    # 真实流程：start?purpose=reset → 假飞书授权 → 回调（带 reset 窗口）
    # 必须带 Cookie：oauth_purpose 决定回调走"重置"分支
    import http.cookiejar as _cj
    jar = _cj.CookieJar()
    cop = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                      _NoRedirect())

    def chop(url):
        try:
            with cop.open(urllib.request.Request(url), timeout=15) as r:
                return r.status, r.headers.get("location", "")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("location", "")

    code, loc = chop(BASE + "/auth/feishu/start?purpose=reset")
    check("发起重置：start 302 到飞书授权", code == 302 and loc.startswith("http"), str(loc)[:60])
    code, cb = chop(loc)
    check("假飞书授权回跳回调", code == 302 and "/auth/feishu/callback" in cb, str(cb)[:80])
    code, final = chop(cb)
    check("回调后跳转 /reset（改密窗口已开）", code == 302 and final.endswith("/reset"),
          f"{code} {final}")
    tok = None
    for c in jar:
        if c.name == "dsh_session":
            tok = c.value
    check("回调下发新会话（改密窗口凭据）", bool(tok))
    code, r = _post("/api/auth/account/reset-password",
                    {"password": "alice-new-password-9",
                     "password2": "alice-new-password-9"}, tok=tok)
    check("窗口内重设成功", code == 200, f"{code} {str(r)[:70]}")
    code, _ = _post("/api/auth/local/login", {"username": "e2e-member",
                                              "password": "e2e-member-password-1"})
    check("旧密码失效（401）", code == 401, str(code))
    code, r = _post("/api/auth/local/login", {"username": "e2e-member",
                                              "password": "alice-new-password-9"})
    check("新密码可登录（200）", code == 200 and r.get("session"), f"{code} {str(r)[:60]}")
    # 窗口是一次性的：重设后再次调用应被拒（窗口已清除）
    code, _ = _post("/api/auth/account/reset-password",
                    {"password": "another-password-7", "password2": "another-password-7"},
                    tok=tok)
    check("改密窗口一次性（重设后再次调用被拒）", code == 403, str(code))
    # 恢复默认测试密码，避免影响其它套件
    from app.auth import accounts as _acc
    oid, _a = _acc.by_username("e2e-member")
    if oid:
        _acc.set_password(oid, "e2e-member-password-1", by="test")


def t_emergency_disabled_by_default():
    """生产口径：应急登录默认关闭（404），且无任何应急账号。"""
    print("== 9-6 应急账号默认关闭 ==")
    harness.stop_fake_feishu()
    check("重启（不设 AUTH_EMERGENCY_ENABLED → 默认关闭）",
          harness.restart_service(env_extra={"AUTH_EMERGENCY_ENABLED": "0",
                                             "PORTAL_SYNC_INTERVAL": "3600"}))
    code, r = _post("/api/auth/emergency/login", {"username": "x", "password": "y"})
    check("应急登录端点 → 404（已停用）", code == 404, f"{code} {str(r)[:60]}")
    code, _, _ = _get("/login")
    check("登录页仍可访问", code == 200, str(code))


def run():
    mm = t_onboarding_gate()
    t_local_login_and_offline(mm)
    t_left_org_deletes_account()
    t_password_reset_via_feishu()
    t_emergency_disabled_by_default()
    print(f"\nD2 身份: {len(PASS)} 通过, {len(FAIL)} 失败")
    return len(PASS), len(FAIL)


if __name__ == "__main__":
    def _ensure():
        try:
            urllib.request.urlopen(BASE + "/login", timeout=2)
            return True
        except Exception:
            return harness.start_service()
    if not _ensure():
        print("✗ 测试服务未就绪")
        raise SystemExit(1)
    try:
        run()
    finally:
        harness.stop_service()
    raise SystemExit(1 if FAIL else 0)
