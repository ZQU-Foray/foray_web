"""端到端测试基建：测试服务启停、临时数据目录、有状态固件 mock。

设计要点（对应计划阶段 0）：
- 每次运行使用独立的 FIRMWARE_DIR / 预设文件，绝不触碰真实数据；
- UI 套件通过 playwright 路由拦截注入有状态 mock（模式/型号/参数即时回显），
  模拟真实固件"命令 → 遥测确认"的闭环语义；
- 浏览器依赖系统已安装的 chromium（playwright install 缓存）。
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 测试端口可用 E2E_PORT 覆盖：多人/多进程并行回归时各用各的端口，互不清理对方服务
PORT = int(os.environ.get("E2E_PORT", "8090"))
BASE = f"http://127.0.0.1:{PORT}"

_proc = None
_tmpdir = None
_auth = {"emergency": None, "member": None, "local": None}
_feishu_port = 0
_TEST_EMERGENCY_PASSWORD = "e2e-test-password-97531"

# 假 bitable 表映射（start/restart 共用，保持完全一致——阶段 6 修复：
# restart 漏此键曾使重启后同步恒 config 失败）
FAKE_TABLES_JSON = json.dumps({
    "projects": {"table_id": "tblFixPortalSync1",
                 "fields": {"name": "fldPname", "description": "fldPdesc",
                            "owner_group": "fldPown", "collab_groups": "fldPcol",
                            "owners": "fldPowners", "plan_start": "fldPps",
                            "plan_end": "fldPpe", "status": "fldPstatus"}},
    "tasks": {"table_id": "tblFixPortalSync2",
              "fields": {"name": "fldTname", "project": "fldTproj",
                         "owner_group": "fldTown", "collab_groups": "fldTcol",
                         "owners": "fldTowners", "priority": "fldTpri",
                         "status": "fldTstatus", "start_time": "fldTstart",
                         "due_time": "fldTdue", "due_precision": "fldTprec",
                         "done_time": "fldTdone", "description": "fldTdesc",
                         "blocker_note": "fldTblk"}},
    "milestones": {"table_id": "tblFixPortalSync3",
                   "fields": {"name": "fldMname", "kind": "fldMkind",
                              "target_time": "fldMtime", "time_precision": "fldMprec",
                              "project": "fldMproj", "visibility": "fldMvis",
                              "description": "fldMdesc"}},
    "schedule": {"table_id": "tblFixPortalSync4",
                 "fields": {"name": "fldSname", "start_time": "fldSstart",
                            "end_time": "fldSend", "all_day": "fldSaday",
                            "kind": "fldSkind", "groups": "fldSgrp",
                            "participants": "fldSpart", "location": "fldSloc",
                            "project": "fldSproj", "description": "fldSdesc",
                            "status": "fldSstatus"}},
    "members": {"table_id": "tblFixPortalSync5",
                "fields": {"member": "fldMem", "valid_tasks": "fldMvalid",
                           "done_tasks": "fldMdone", "due_tasks": "fldMdue",
                           "on_time_tasks": "fldMok", "overdue_tasks": "fldMlate",
                           "missing_delivery": "fldMmiss",
                           "tasks_without_due": "fldMnodue",
                           "completion_rate": "fldMrate", "on_time_rate": "fldMotr",
                           "summary_note": "fldMnote", "last_check_in": "fldMci",
                           "work_hours": "fldMhours",
                           "attendance_anomalies": "fldManom",
                           "synced_at": "fldMsync", "sync_note": "fldMsnote",
                           # 周结算（2026-10-20）：门户写 周初时长/结算周/上周时长，其余为飞书公式
                           "week_base": "fldMwb", "week_key": "fldMwk",
                           "last_week_hours": "fldMlw", "week_hours": "fldMwh",
                           "week_ok": "fldMwok", "last_week_ok": "fldMlok",
                           # 请假周（2026-10-21）：组长在考勤页设置
                           "leave_week": "fldMleave"}},
    # 花名册与物资采购：门户按需使用（roster 判定组长身份；procurement 是物资明细表）。
    # 2026-10-12 起物资采购带 赛季/一二级分类/采购编号/日期/单价/数量/总价/采购人/发票 列。
    "roster": {"table_id": "tblFixPortalSync6",
               "fields": {"duty": "fldRduty", "group": "fldRgrp",
                          "leaders": "fldRlead", "members": "fldRmem"}},
    "procurement": {"table_id": "tblFixPortalSync7",
                    "fields": {"name": "fldPrname", "groups": "fldPrgrp",
                               "link": "fldPrlink", "purpose": "fldPrpurp",
                               "attachments": "fldPratt",
                               "leader_approval": "fldPrlead",
                               "purchased": "fldPrbuy",
                               "season": "fldPrseason",
                               "category1": "fldPrcat1", "category2": "fldPrcat2",
                               "code": "fldPrcode", "purchase_date": "fldPrdate",
                               "unit_price": "fldPrprice", "quantity": "fldPrqty",
                               "total_price": "fldPrtotal", "purchaser": "fldPrwho",
                               "invoice_done": "fldPrinv",
                               "invoice_files": "fldPrinvf"}},
    # 值日表（2026-10-20）：维护者导入、门户只读；每行日期或星期之一 + 值日人员
    "duty": {"table_id": "tblFixPortalSync8",
             "fields": {"date": "fldDdate", "weekday": "fldDwd", "people": "fldDppl"}},
})

# 周结算与值日表的字段元数据（各套件的假飞书数据共用，须与上面的映射一一对应）
MEMBER_WEEK_META = [("周初时长", "fldMwb"), ("结算周", "fldMwk"), ("上周时长", "fldMlw"),
                    ("本周时长", "fldMwh"), ("本周达标", "fldMwok"), ("上周达标", "fldMlok"),
                    ("请假周", "fldMleave")]
DUTY_META = [("值日安排", "fldDtitle"), ("日期", "fldDdate"), ("星期", "fldDwd"),
             ("值日人员", "fldDppl")]


def week_key(delta_weeks=0):
    """北京时间本周一（或前后若干周）的日期 YYYY-MM-DD：「结算周」列的取值。"""
    import datetime as _d
    now = _d.datetime.now(_d.timezone(_d.timedelta(hours=8)))
    monday = now.date() - _d.timedelta(days=now.weekday()) + _d.timedelta(weeks=delta_weeks)
    return monday.strftime("%Y-%m-%d")


def ensure_browser_env():
    """为无头浏览器补齐运行环境：仓库自带的共享库与字体（免系统安装）。

    仅当系统缺库（典型报错 libnspr4.so 找不到）时启用；已具备系统依赖的
    机器不受影响。库来源为 Debian 官方 .deb 解包（见 tests/README.md）。
    """
    libs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser-libs")
    libdir = os.path.join(libs, "lib")
    if not os.path.isdir(libdir):
        return
    cur = os.environ.get("LD_LIBRARY_PATH", "")
    if libdir not in cur:
        os.environ["LD_LIBRARY_PATH"] = (libdir + ":" + cur).rstrip(":")
    # 动态生成字体配置（指向仓库内字体目录）。写到临时目录而非仓库内，
    # 避免测试运行改写工作树（隔离纪律：业务与仓库文件零写入）
    fc = os.path.join(tempfile.gettempdir(), "shootweb-e2e-browser", "fonts.conf")
    os.makedirs(os.path.dirname(fc), exist_ok=True)
    # 仓库自带字体 + 本机用户字体目录（CJK 字体体积大，不入仓库；
    # 无 CJK 字体时截图里中文会渲染成方框，验收证据不可读）
    import pathlib
    dirs = [os.path.join(libs, "fonts")]
    for extra in (pathlib.Path.home() / ".fonts-cjk",
                  pathlib.Path.home() / ".fonts",
                  pathlib.Path.home() / ".local/share/fonts"):
        if extra.is_dir():
            dirs.append(str(extra))
    with open(fc, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
                '<fontconfig>\n' +
                "".join('  <dir>%s</dir>\n' % d for d in dirs) +
                '  <cachedir>' + os.path.join(libs, "fcache") +
                '</cachedir>\n</fontconfig>\n')
    os.environ.setdefault("FONTCONFIG_FILE", fc)


def _wait_online(timeout=15.0):
    """等待测试服务就绪（用公开路径 /login 探活——阶段 3 起 /api/* 需认证）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/login", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _kill_stale_service():
    """清理占用测试端口的孤儿 uvicorn（防泄漏进程互相干扰，仅杀 uvicorn）。"""
    import subprocess
    try:
        r = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
    except Exception:
        return
    for line in r.stdout.splitlines():
        if f":{PORT} " not in line or "uvicorn" not in line:
            continue
        for tok in line.split("pid=")[1:]:
            pid = tok.split(",")[0].split(")")[0].strip()
            if pid.isdigit():
                try:
                    os.kill(int(pid), 15)
                except Exception:
                    pass


def _uvicorn_cmd(host, port):
    """跨平台启动命令：用当前解释器 -m uvicorn（Windows 的 venv 没有 bin/uvicorn）。"""
    return [sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", host, "--port", str(port)]


def start_service(with_member=False, env_extra=None):
    """以独立临时数据目录启动测试服务，返回是否就绪。

    阶段 3 起：认证全量启用（与生产同码路径，无测试绕过）。
    自动创建受控测试应急账号并登录（_auth["emergency"]）；
    with_member=True 时再经假飞书服务器完成成员 OAuth（_auth["member"]）。
    """
    global _proc, _tmpdir, _auth, _feishu_port
    stop_service()
    _kill_stale_service()
    _tmpdir = tempfile.mkdtemp(prefix="shootweb-test-")
    import socket as _s
    sk = _s.socket(); sk.bind(("127.0.0.1", 0))
    _feishu_port = sk.getsockname()[1]; sk.close()
    env = dict(os.environ)
    env.update({
        "WEB_PORT": str(PORT),
        "WEB_HOST": "127.0.0.1",
        "FIRMWARE_DIR": os.path.join(_tmpdir, "firmware"),
        "PRESETS_FILE": os.path.join(_tmpdir, "presets.json"),
        "MOTOR_PRESETS_FILE": os.path.join(_tmpdir, "motor_presets.json"),
        "DEVICES_FILE": os.path.join(_tmpdir, "devices.json"),
        # 认证（阶段 3）：全部数据文件在临时目录
        "AUTH_SESSIONS_FILE": os.path.join(_tmpdir, "sessions.json"),
        "AUTH_EMERGENCY_FILE": os.path.join(_tmpdir, "emergency_users.json"),
        "AUTH_RECHECK_INTERVAL": "2",      # 测试加速（生产 300s）
        "AUTH_RECHECK_GRACE": "3",
        "AUTH_WS_RECHECK": "2",
        # 飞书配置指向假服务器（未启动时复核不可用——不影响应急身份）
        "FEISHU_API_BASE": f"http://127.0.0.1:{_feishu_port}",
        "FEISHU_APP_ID": "cli_test_fake",
        "FEISHU_APP_SECRET": "test-secret",
        "FEISHU_REDIRECT_URL": f"http://127.0.0.1:{PORT}/auth/feishu/callback",
        "FEISHU_ALLOWED_TENANT_KEYS": "tenant-test",
        "FEISHU_BASE_APP_TOKEN": "FakeBaseTokenForSync",
        "FEISHU_CONFIG_FILE": os.path.join(_tmpdir, "feishu-absent.json"),
        # 阶段 4：门户快照临时库 + 假 bitable 表映射（数据由 FakeFeishu 注入）
        "PORTAL_DB": os.path.join(_tmpdir, "portal.db"),
        "FEISHU_TABLES_JSON": FAKE_TABLES_JSON,
        "PORTAL_SYNC_INTERVAL": "3600",   # 测试不自动跑，全部手动触发
        "PORTAL_MANUAL_MIN_GAP": "1",       # 测试缩短手动限频
        # D2（阶段 9）：本站账号库与审计隔离到临时目录
        "AUTH_ACCOUNTS_FILE": os.path.join(_tmpdir, "accounts.json"),
        "AUTH_AUDIT_FILE": os.path.join(_tmpdir, "audit.log"),
        "AUTH_LOCAL_MAX_FAIL": "3", "AUTH_LOCAL_LOCK_SEC": "5",
        # 应急账号 D2 起生产默认关闭；测试基建仍用其构造"仅调试"身份，
        # 过渡期显式开启（生产部署不带此变量）
        "AUTH_EMERGENCY_ENABLED": "1",
    })
    # 测试进程用同一实现写应急账号文件——必须先把 config 指到临时目录
    # （emergency 模块每次调用动态读 config.AUTH_EMERGENCY_FILE），
    # 否则会误写到真实 config/auth/（生产目录）。
    sys.path.insert(0, REPO)
    from app import config as _cfg
    from app.auth import emergency as _em
    _cfg.AUTH_EMERGENCY_FILE = __import__("pathlib").Path(
        os.path.join(_tmpdir, "emergency_users.json"))
    _em.create("test-ops", _TEST_EMERGENCY_PASSWORD, note="e2e 受控测试身份")
    if env_extra:
        env.update(env_extra)
    _proc = subprocess.Popen(
        _uvicorn_cmd("127.0.0.1", PORT),
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_online():
        stop_service()
        return False
    _auth["emergency"] = _login_emergency()
    ok = bool(_auth["emergency"])
    if ok and with_member:
        ok = bool(login_member())
    if not ok:
        stop_service()
        return False
    return True


def restart_service(env_extra=None):
    """停服务并以同一临时目录重启（会话持久化验证用）。"""
    global _proc
    if _proc:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
        _proc = None
    env = dict(os.environ)
    env.update({
        "WEB_PORT": str(PORT), "WEB_HOST": "127.0.0.1",
        "FIRMWARE_DIR": os.path.join(_tmpdir, "firmware"),
        "PRESETS_FILE": os.path.join(_tmpdir, "presets.json"),
        "MOTOR_PRESETS_FILE": os.path.join(_tmpdir, "motor_presets.json"),
        "DEVICES_FILE": os.path.join(_tmpdir, "devices.json"),
        "AUTH_SESSIONS_FILE": os.path.join(_tmpdir, "sessions.json"),
        "AUTH_EMERGENCY_FILE": os.path.join(_tmpdir, "emergency_users.json"),
        "AUTH_RECHECK_INTERVAL": "2", "AUTH_RECHECK_GRACE": "3",
        "AUTH_WS_RECHECK": "2",
        "FEISHU_API_BASE": f"http://127.0.0.1:{_feishu_port}",
        "FEISHU_APP_ID": "cli_test_fake", "FEISHU_APP_SECRET": "test-secret",
        "FEISHU_REDIRECT_URL": f"http://127.0.0.1:{PORT}/auth/feishu/callback",
        "FEISHU_ALLOWED_TENANT_KEYS": "tenant-test",
        # 重启同样必须隔离：否则回落生产 config/feishu.json 与
        # config/portal_snapshot.db（§8.1 规则 8；阶段 5 回归曾因此触碰真实快照库）
        "FEISHU_BASE_APP_TOKEN": "FakeBaseTokenForSync",
        "FEISHU_CONFIG_FILE": os.path.join(_tmpdir, "feishu-absent.json"),
        "FEISHU_TABLES_JSON": FAKE_TABLES_JSON,
        "PORTAL_DB": os.path.join(_tmpdir, "portal.db"),
        "PORTAL_SYNC_INTERVAL": "3600", "PORTAL_MANUAL_MIN_GAP": "1",
        "AUTH_ACCOUNTS_FILE": os.path.join(_tmpdir, "accounts.json"),
        "AUTH_AUDIT_FILE": os.path.join(_tmpdir, "audit.log"),
        "AUTH_LOCAL_MAX_FAIL": "3", "AUTH_LOCAL_LOCK_SEC": "5",
        "AUTH_EMERGENCY_ENABLED": "1",
    })
    if env_extra:
        env.update(env_extra)
    _proc = subprocess.Popen(
        _uvicorn_cmd("127.0.0.1", PORT),
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _wait_online()


def stop_service():
    stop_fake_feishu()
    if _proc:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
    import shutil
    if _tmpdir:
        shutil.rmtree(_tmpdir, ignore_errors=True)


def auth_cookie(role="emergency"):
    """playwright add_cookies 用的会话 Cookie（受控测试身份）。"""
    sid = _auth.get(role)
    if not sid:
        raise RuntimeError(f"测试身份 {role} 未登录（start_service 阶段失败）")
    return {"name": "dsh_session", "value": sid,
            "url": BASE, "httpOnly": True}


def ws_headers(role="emergency"):
    """websockets 连接头（受控测试身份）。"""
    sid = _auth.get(role)
    if not sid:
        raise RuntimeError(f"测试身份 {role} 未登录")
    return {"Authorization": "Bearer " + sid}


def _extract_session_cookie(resp_headers):
    for k, v in resp_headers:
        if k.lower() == "set-cookie" and "dsh_session=" in v:
            return v.split("dsh_session=", 1)[1].split(";", 1)[0]
    return None


def _login_emergency():
    """受控测试身份：应急登录拿会话令牌（生产无此账号/密码）。"""
    req = urllib.request.Request(
        BASE + "/api/auth/emergency/login",
        data=json.dumps({"username": "test-ops",
                         "password": _TEST_EMERGENCY_PASSWORD}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return _extract_session_cookie(r.headers.items())
    except Exception:
        return None


def _login_member():
    """经假飞书服务器完成成员 OAuth（state 全链真实）。fake 需已启动。"""
    from http.cookiejar import CookieJar
    opener_noredir = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()), _NoRedirect())

    def hop(url):
        try:
            opener_noredir.open(urllib.request.Request(url), timeout=15)
            return None                       # 意外 200
        except urllib.error.HTTPError as e:
            return e.headers.get("Location", "")
    authorize = hop(BASE + "/auth/feishu/start?next=/")
    if not authorize:
        return None
    callback = hop(authorize)
    if not callback:
        return None
    try:
        opener_noredir.open(urllib.request.Request(callback), timeout=15)
        return None
    except urllib.error.HTTPError as e:
        return _extract_session_cookie(e.headers.items())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_fake_feishu = None


def start_fake_feishu(**kwargs):
    """启动（或替换）全局假飞书服务器。member 复核依赖其存活。"""
    global _fake_feishu
    stop_fake_feishu()
    _fake_feishu = FakeFeishu(**kwargs)
    return _fake_feishu


def _feishu_calls(prefix):
    """假飞书按接口前缀的请求计数（token/bitable/contact/other；None=未启动）。"""
    if not _fake_feishu:
        return None
    return _fake_feishu.calls_by_path.get(prefix, 0)


def stop_fake_feishu():
    global _fake_feishu
    if _fake_feishu:
        _fake_feishu.stop()
        _fake_feishu = None


def seed_local_account(username="e2e-local", password="e2e-local-password-1",
                       open_id="ou_e2e_local_001", display="E2E本地账号"):
    """在临时账号库里直接写入一个已开通账号（不走飞书），返回 (用户名, 密码)。"""
    import json as _json
    data = {"version": 1, "accounts": {
        open_id: {
            "username": username, "display": display, "tenant_key": "tenant-test",
            "hash": __import__("app.auth.passwords", fromlist=["x"]).hash_password(password),
            "status": "active", "created_at": time.time(),
            "password_updated_at": time.time(), "last_login_at": None,
            "last_seen_org_at": time.time()}}}
    p = os.path.join(_tmpdir, "accounts.json")
    with open(p, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False)
    os.chmod(p, 0o600)
    return username, password


def login_local(username="e2e-local", password="e2e-local-password-1"):
    """本站账号登录（D2）；返回会话令牌或 None。"""
    sid = _login_local(username, password)
    _auth["local"] = sid or _auth.get("local")
    return _auth["local"]


def _login_local(username, password):
    import json as _json
    body = _json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(BASE + "/api/auth/local/login", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return _json.loads(r.read().decode()).get("session")
    except Exception:
        return None


def _http_json(path, method="GET", body=None, token=None):
    import json as _json
    data = _json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, _json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception:
        return None, {}


def ensure_onboarded(token, username="e2e-member", password="e2e-member-password-1"):
    """D2：飞书首登的会话需先设置本站用户名+密码，套件默认自动完成。"""
    if not token:
        return None
    code, me = _http_json("/api/auth/me", token=token)
    if code != 200 or me.get("onboarded"):
        return me or None
    code, r = _http_json("/api/auth/account/onboard", method="POST",
                         body={"username": username, "password": password,
                               "password2": password}, token=token)
    if code != 200:
        # 用户名可能已被其它 open_id 占用：换一个后缀重试一次
        code, r = _http_json("/api/auth/account/onboard", method="POST",
                             body={"username": username + "-b",
                                   "password": password, "password2": password},
                             token=token)
    return r


def login_member(onboard=True):
    """成员 OAuth 登录（自动确保 fake 在跑）；默认顺带完成 D2 开通。

    返回会话令牌或 None。需要"尚未开通"的原始会话时用 onboard=False
    （D2 身份套件用）。
    """
    if not _fake_feishu:
        start_fake_feishu()
    sid = _login_member()
    if sid:
        _auth["member"] = sid
    if onboard and sid:
        ensure_onboarded(sid)
    # 返回"本次登录结果"：失败即 None（负数用例依赖），成员槽保留旧值供其它调用使用
    return sid


def _single_record(path):
    """/…/tables/{table_id}/records/{record_id} → (table_id, record_id)；不是单条路径返回 None。"""
    m = re.search(r"/tables/([^/]+)/records/([^/?]+)$", path)
    if not m or m.group(2) in ("batch_get", "search", "batch_create", "batch_update",
                               "batch_delete"):
        return None
    return m.group(1), m.group(2)


class FakeFeishu:
    """假飞书开放平台（仅测试）：authorize/token/user_info/tenant_token/复核。

    可控：tenant_key（组织核验）、member_status（复核结果）、
    fail_token / fail_contact（注入上游故障）。
    """

    def __init__(self, tenant_key="tenant-test",
                 member_status=None, fail_token=False, fail_contact=False,
                 bitable=None):
        self.tenant_key = tenant_key
        self.member_status = member_status or {
            "is_activated": True, "is_resigned": False, "is_exited": False,
            "is_frozen": False, "is_unjoin": False}
        self.fail_token = fail_token
        self.fail_contact = fail_contact
        # bitable 同步数据/故障注入（阶段 4 测试）：
        # {table_id: {"meta": [(field_name, field_id)...], "records": [item...]}}
        self.bitable = bitable or {}
        self.sync_fail = {"table": None, "mode": None, "left": 0}
        self.calls = 0            # 收到多少次请求（用于"页面不轮询飞书"的可证伪断言）
        self.calls_by_path = {}   # 按接口前缀计数（bitable=同步 / contact=成员复核）
        # 黑洞注入：命中的路径只接受连接、永不回包（模拟上游"挂起"而非
        # "拒连"——两者触发的是不同的失败路径：挂起要考超时上限）
        self.hang_paths = []      # 路径子串列表（空 = 不注入）
        self.hang_seconds = 30.0  # 挂起时长（线程为 daemon，不阻塞收尾）
        # 记录写入（缓存库第 3 步测试）：新建按 client_token 去重（与飞书实测一致）；
        # write_fail 注入写故障：mode=500（临时，按 left 次数）/ reject（字段不存在，永久）
        self.client_tokens = {}
        self.write_fail = {"mode": None, "left": 0}
        self.writes = []          # [(方法, table_id, record_id, 字段)]
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _count(self, path):
                outer.calls += 1
                key = ("token" if "tenant_access_token" in path else
                       "bitable" if "/bitable/" in path else
                       "contact" if "/contact/" in path else "other")
                outer.calls_by_path[key] = outer.calls_by_path.get(key, 0) + 1
                # 黑洞路径：收下请求但不回包（计数照记，便于断言"黑洞确实命中"）
                if any(h in path for h in outer.hang_paths):
                    time.sleep(outer.hang_seconds)

            def do_GET(self):
                self._count(self.path)
                from urllib.parse import urlparse, parse_qs
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/open-apis/authen/v1/authorize":
                    cb = q["redirect_uri"][0] + "?code=fake-auth-code&state=" + q["state"][0]
                    self.send_response(302)
                    self.send_header("Location", cb)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif u.path == "/open-apis/authen/v1/user_info":
                    self._json({"code": 0, "data": {
                        "open_id": "ou_e2e_fake_member_001",
                        "tenant_key": outer.tenant_key,
                        "name": "E2E测试成员"}})
                elif _single_record(u.path):
                    tid, rid = _single_record(u.path)
                    rec = outer._find(tid, rid)
                    if rec is None:
                        self._json({"code": 1254043, "msg": "RecordIdNotFound"})
                    else:
                        self._json({"code": 0, "data": {"record": {
                            "record_id": rid, "fields": rec.get("fields", {}),
                            "last_modified_time": rec.get("last_modified_time")}}})
                elif "/records" in u.path:
                    if outer.sync_fail["mode"] and outer.sync_fail["table"] in u.path:
                        m = outer.sync_fail["mode"]
                        if m == "500":
                            self._json({"code": -1, "msg": "fake 500"}, 500)
                            return
                        if m == "429" and outer.sync_fail["left"] > 0:
                            outer.sync_fail["left"] -= 1
                            self._json({"code": 1254290, "msg": "TooManyRequest"}, 429)
                            return
                        if m == "token_expire" and outer.sync_fail["left"] > 0:
                            outer.sync_fail["left"] -= 1
                            self._json({"code": 99991663,
                                        "msg": "fake: token expired"})
                            return
                        if m == "bad_total":
                            items = outer.bitable.get(u.path.split("/tables/")[-1]
                                                      .split("/records")[0],
                                                      {}).get("records", [])
                            self._json({"code": 0, "data": {
                                "items": items[:1], "has_more": False,
                                "total": len(items) + 1}})
                            return
                    tid = u.path.split("/tables/")[-1].split("/records")[0]
                    data = outer.bitable.get(tid, {})
                    items = data.get("records", [])
                    ps = min(int(q.get("page_size", [20])[0]), 500)
                    off = int((q.get("page_token", ["off:0"])[0].split(":")[-1])
                              or 0) if "page_token" in q else 0
                    page = items[off:off + ps]
                    self._json({"code": 0, "data": {
                        "items": page,
                        "has_more": off + ps < len(items),
                        "page_token": f"off:{off + ps}",
                        "total": len(items)}})
                elif "/fields" in u.path and "/tables/" in u.path:
                    tid = u.path.split("/tables/")[-1].split("/fields")[0]
                    data = outer.bitable.get(tid, {})
                    meta = data.get("meta", [])
                    ps = int(q.get("page_size", [100])[0])
                    page = meta[:ps]
                    self._json({"code": 0, "data": {
                        "items": [{"field_name": n, "field_id": i} for n, i in page],
                        "has_more": False, "total": len(meta)}})
                elif u.path.startswith("/open-apis/contact/v3/users/"):
                    if outer.fail_contact:
                        self._json({"code": 99991672, "msg": "fake: contact denied"}, 200)
                    else:
                        self._json({"code": 0, "data": {"user": {
                            "status": outer.member_status}}})
                else:
                    self._json({"code": -1, "msg": "fake 404"}, 404)

            def do_POST(self):
                self._count(self.path)
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
                if self.path == "/open-apis/auth/v3/tenant_access_token/internal":
                    self._json({"code": 0, "tenant_access_token": "t-fake-e2e",
                                "expire": 7200})
                elif self.path == "/open-apis/authen/v2/oauth/token":
                    if outer.fail_token or body.get("code") != "fake-auth-code":
                        self._json({"code": 20040, "msg": "fake: bad code"})
                    else:
                        self._json({"code": 0, "data": {"access_token": "u-fake-e2e",
                                                        "expires_in": 7200}})
                elif re.search(r"/tables/[^/]+/records(\?|$)", self.path):
                    if outer._write_failed(self):
                        return
                    from urllib.parse import urlparse, parse_qs
                    u = urlparse(self.path)
                    tid = u.path.split("/tables/")[-1].split("/records")[0]
                    ct = (parse_qs(u.query).get("client_token") or [None])[0]
                    if ct and ct in outer.client_tokens:
                        rid = outer.client_tokens[ct]
                    else:
                        rid = "recE2E%04d" % (len(outer.client_tokens) + len(outer.writes) + 1)
                        outer.bitable.setdefault(tid, {"meta": [], "records": []})[
                            "records"].append({"record_id": rid,
                                               "fields": dict(body.get("fields") or {}),
                                               "last_modified_time": int(time.time() * 1000)})
                        if ct:
                            outer.client_tokens[ct] = rid
                        outer.writes.append(("POST", tid, rid, body.get("fields")))
                    self._json({"code": 0, "data": {"record": {
                        "record_id": rid, "fields": body.get("fields") or {}}}})
                elif "/records/batch_get" in self.path:
                    # 记录链接（阶段 5）：官方 shared_url 语义
                    tid = self.path.split("/tables/")[-1].split("/records")[0]
                    data = outer.bitable.get(tid, {})
                    by_id = {r.get("record_id"): r for r in data.get("records", [])}
                    recs = []
                    for rid in body.get("record_ids") or []:
                        item = {"record_id": rid, "fields": {}}
                        if rid in by_id:
                            item["fields"] = by_id[rid].get("fields", {})
                            if body.get("with_shared_url"):
                                item["shared_url"] = (
                                    "http://fake.feishu.local/record/" + str(rid))
                        recs.append(item)
                    self._json({"code": 0, "data": {"records": recs}})
                else:
                    self._json({"code": -1, "msg": "fake 404"}, 404)

            def do_PUT(self):
                self._count(self.path)
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
                from urllib.parse import urlparse
                hit = _single_record(urlparse(self.path).path)
                if not hit:
                    self._json({"code": -1, "msg": "fake 404"}, 404)
                    return
                if outer._write_failed(self):
                    return
                rec = outer._find(*hit)
                if rec is None:
                    self._json({"code": 1254043, "msg": "RecordIdNotFound"})
                    return
                for k, v in (body.get("fields") or {}).items():
                    if v is None:
                        rec["fields"].pop(k, None)
                    else:
                        rec["fields"][k] = v
                rec["last_modified_time"] = int(time.time() * 1000)
                outer.writes.append(("PUT", hit[0], hit[1], body.get("fields")))
                self._json({"code": 0, "data": {"record": {
                    "record_id": hit[1], "fields": body.get("fields") or {}}}})

            def do_DELETE(self):
                self._count(self.path)
                from urllib.parse import urlparse
                hit = _single_record(urlparse(self.path).path)
                if not hit:
                    self._json({"code": -1, "msg": "fake 404"}, 404)
                    return
                if outer._write_failed(self):
                    return
                recs = outer.bitable.get(hit[0], {}).get("records", [])
                keep = [r for r in recs if r.get("record_id") != hit[1]]
                if len(keep) == len(recs):
                    self._json({"code": 1254043, "msg": "RecordIdNotFound"})
                    return
                outer.bitable[hit[0]]["records"] = keep
                outer.writes.append(("DELETE", hit[0], hit[1], None))
                self._json({"code": 0, "data": {"deleted": True, "record_id": hit[1]}})

        class Srv(ThreadingHTTPServer):
            """测试服务器：客户端提前断开（黑洞超时放弃等）不打整段堆栈。"""

            def handle_error(self, request, client_address):
                import sys as _s
                if isinstance(_s.exc_info()[1], (BrokenPipeError,
                                                 ConnectionResetError)):
                    return               # 预期噪声：被测方已超时放弃
                super().handle_error(request, client_address)

        self.srv = Srv(("127.0.0.1", _feishu_port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def _find(self, tid, rid):
        for r in self.bitable.get(tid, {}).get("records", []):
            if r.get("record_id") == rid:
                return r
        return None

    def _write_failed(self, handler):
        """写故障注入：命中则直接回错误响应并返回 True。"""
        mode = self.write_fail["mode"]
        if mode == "500" and self.write_fail["left"] > 0:
            self.write_fail["left"] -= 1
            handler._json({"code": -1, "msg": "fake 500"}, 500)
            return True
        if mode == "reject":
            handler._json({"code": 1254045, "msg": "FieldNameNotFound"})
            return True
        return False

    def stop(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass


def api(path, body=None, method=None, expect=200, auth="emergency"):
    """REST 快捷请求；返回 (状态码, JSON 或文本)。

    auth: "emergency"/"member"（受控测试身份自动附带 Bearer）；
          None = 匿名请求（鉴权测试用）。
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if auth and _auth.get(auth):
        headers["Authorization"] = "Bearer " + _auth[auth]
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers,
        method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


class FakeTelemetry:
    """有状态固件 mock：记录命令并即时反映到 status 遥测（UI 测试注入用）。

    状态模型与真实固件一致：
    - mode/model/param(target_*) 命令即时写入遥测回显；
    - enable 命令写 enabled 位；
    - telemetry.link 恒 1（掉线恢复场景由调用方切换）。
    """

    def __init__(self, slots=None, link=1):
        self.slots = slots or [
            {"rpm": 0.0, "deg": 0.0, "current": 0.0, "torque": 0.0,
             "temperature": 30 + i, "fault": 0, "mode": 0, "model": 0,
             "enabled": 0}
            for i in range(3)]
        self.link = link
        self.calls = []
        self.open = True
        self.presets_doc = {"motor_presets": {}}

    def status_body(self):
        return {
            "open": self.open, "port": "/dev/mock0", "baudrate": 115200,
            "rtt_ms": 3.2, "telemetry_count": 1,
            "telemetry": (None if not self.open else
                          {"slots": [dict(s) for s in self.slots], "link": self.link}),
            "profiles": [
                {"id": 0, "name": "M2006", "gear_ratio": 36,
                 "max_output_rpm": 500, "max_torque": 1.0},
                {"id": 1, "name": "M3508", "gear_ratio": 19.2,
                 "max_output_rpm": 500, "max_torque": 1.5},
                {"id": 2, "name": "GM6020C", "gear_ratio": 1,
                 "max_output_rpm": 320, "max_torque": 1.5}],
            "modes": ["disable", "rpm", "position", "torque"],
            "fault_bits": {}, "defaults": {},
        }

    def apply_command(self, path, body):
        """命令落地：返回 {ok, status}；非法槽位模拟真实固件拒绝。"""
        self.calls.append((path, body))
        b = body or {}
        slot = b.get("slot", 0)
        if not 0 <= slot < 3:
            return {"ok": False, "status": "bad_slot"}
        if path == "mode":
            self.slots[slot]["mode"] = b.get("mode", 0)
        elif path == "model":
            self.slots[slot]["model"] = b.get("model", 0)
        elif path == "enable":
            self.slots[slot]["enabled"] = 1 if b.get("on") else 0
        elif path == "param" and b.get("key", "").startswith("target_"):
            kind = b["key"][7:]
            field = {"rpm": "rpm", "deg": "deg", "torque": "torque"}[kind]
            self.slots[slot][field + "_target"] = b.get("value", 0)
        return {"ok": True, "status": "ok", "slot": slot}


MOCK_ROUTE_SCRIPT = """(payload) => {
  // 由 Python 侧经 evaluate 注入 window.__MOCK__ 状态对象后启用
}"""


async def install_mock(page, fake, base=BASE):
    """为页面安装 /api/shoot/* 路由拦截（有状态 mock）。"""
    async def handle(route):
        url = route.request.url
        path = url.split("/api/shoot/")[-1].split("?")[0]
        method = route.request.method
        # PUT/POST 均解析请求体（此前仅 POST，导致 PUT 存档恒空——阶段 0 修复）
        body = route.request.post_data_json if method in ("POST", "PUT") else None
        if path == "status":
            await route.fulfill(json=fake.status_body())
        elif path == "presets":
            if method == "PUT":
                fake.presets_doc = body or {"motor_presets": {}}
            fake.calls.append(("presets", body))
            await route.fulfill(json=fake.presets_doc)
        elif path == "ping":
            await route.fulfill(json={"ok": True, "rtt_ms": 3.2})
        else:
            await route.fulfill(json=fake.apply_command(path, body))
    await page.route(base + "/api/shoot/**", handle)
