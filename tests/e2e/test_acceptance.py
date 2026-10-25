"""阶段 6 综合验收：故障态优先（断网/身份边界/定时并发/负载观察）。

对应验收清单 6-1 ~ 6-7（自动化部分）；6-8 真机走查的非侵入探测与
6-9 硬件项见 docs/portal-acceptance.md 分类标注。
全部隔离：临时数据目录 + 临时 SQLite + 假 TCP 固件 + 假 OpenOCD +
假飞书（可停/可注错），绝不触碰 /opt、80 端口与真实 Base。
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import websockets

import harness
from harness import BASE, api

PASS, FAIL = [], []
SKIPPED = []        # 未执行项（如真机不可达）：独立通道，**不计入通过数**（§8.1 规则 9）
NOTES = {}          # 供验收报告引用的定量观察（帧率/计数等）


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def skip(name, detail=""):
    """未执行项：与 check 分离，避免把 SKIP 计成通过。"""
    SKIPPED.append(name)
    print(("  ⊘ " if False else "  - ") + name + (f"  [{detail}]" if detail else ""))


def note(key, value):
    NOTES[key] = value
    print(f"  · {key}: {value}")


# ---------------- 假 OpenOCD（烧录链路在断网态可用的证据） ----------------

def make_fake_openocd():
    d = tempfile.mkdtemp(prefix="fake-ocd-")
    p = os.path.join(d, "openocd")
    with open(p, "w") as f:
        f.write('#!/bin/bash\necho "** programming finished"; '
                'echo "** verified ok"; exit 0\n')
    os.chmod(p, 0o755)
    return p


def _read_db_meta():
    """直读临时快照库 meta（断网态无身份可查 API 时的观测通道）。"""
    import sqlite3
    c = sqlite3.connect(harness._tmpdir + "/portal.db")
    rows = dict(c.execute("SELECT key, value FROM meta").fetchall())
    c.close()
    return rows


def http_get(path, headers=None):
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedir)
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with op.open(req, timeout=8) as r:
            return r.status, r.read()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]


def t_offline_and_decoupling():
    """6-1/6-2/6-3：断网应急、离线重启、飞书故障不阻塞硬件链路。"""
    print("== 6-1/6-3 断网（假飞书停止）下的应急登录与硬件链路 ==")
    ok = harness.start_service(env_extra={"OPENOCD_BIN": make_fake_openocd(),
                                          "OPENOCD_TCP_BIN": make_fake_openocd(),
                                          "FLASH_TIMEOUT": "20"})
    check("服务就绪（启动不等待飞书）", ok)
    harness.start_fake_feishu(tenant_key="tenant-test")
    harness.login_member()
    # 建立快照（在线时）——供断网后"应急不可见缓存"验证
    harness._fake_feishu.bitable = _small_data()
    _manual_sync()
    _, st = api("/api/portal/sync-status", auth="member")
    check("在线首同步成功（快照建立）", st.get("counts", {}).get("tasks") == 2)

    # —— 断网：停假飞书（FEISHU_API_BASE 成死端口）——
    harness.stop_fake_feishu()
    time.sleep(0.5)

    # 新浏览器语义：全新应急登录
    code, body = api("/api/auth/emergency/login",
                     {"username": "test-ops",
                      "password": harness._TEST_EMERGENCY_PASSWORD}, auth=None)
    check("断网时新应急登录可用", code == 200, str(code))
    # 飞书登录：配置在则 302 到授权页（授权页在断网时由浏览器侧加载失败）。
    # 记录为设计现状 + 已知体验限制（服务端不做预探测，避免每次登录多一跳）。
    code, body = http_get("/auth/feishu/start")
    check("断网时飞书登录仍 302 到授权页（新授权实际不可用，不伪装本地成功）",
          code == 302, str(code))
    # 可证伪：断网时用任意 code 走换码 → 必须失败（证明"新登录实际不可用"，
    # 而不是只看 302 就算通过）
    code, body = api("/api/auth/exchange", {"code": "offline-code", "state": "x"}, auth=None)
    check("断网态飞书换码失败（新登录实际不可用，非仅 302）",
          code != 200, f"{code} {str(body)[:40]}")
    note("断网飞书登录", "start→302 仍存在（服务端不预探测）；换码失败 "
         f"code={code}；浏览器侧授权页加载失败未实测（推断）")

    # 调试资源本地加载（无外链）
    for p in ("/login", "/portal/portal.css", "/portal/portal.js"):
        code, _ = http_get(p)
        check(f"资源 {p} 本地可用", code == 200, str(code))

    # 应急不可见缓存管理数据（断网态边界维持）
    code, body = api("/api/portal/overview")
    check("断网态应急访问管理数据仍 403", code == 403, str(code))

    # 硬件模拟链路：假 TCP 固件 shoot + 假 OpenOCD 烧录
    from test_transports import FakeFirmwareServer
    from app import shoot as shoot_mod          # 帧类型常量（核对急停帧）
    fw = FakeFirmwareServer()
    try:
        api("/api/v2/devices", {"id": "acc-sta", "name": "acc",
                                "transport": "vllink_tcp", "host": "127.0.0.1",
                                "uart_port": fw.port})
        c, b = api("/api/v2/devices/acc-sta/shoot/open", {})
        check("断网态 shoot 打开（假 TCP 固件）", c == 200 and b["status"]["open"])
        time.sleep(0.4)
        c, st2 = api("/api/v2/devices/acc-sta/shoot/status")
        check("断网态遥测解码正常", c == 200 and st2["telemetry"] is not None)
        # 急停：断网时必须仍可用（安全兜底不依赖任何上游）
        c, be = api("/api/v2/devices/acc-sta/shoot/estop", {})
        check("断网态急停可用（安全语义）",
              c == 200 and be.get("ok") is True, f"{c} {be}")
        check("断网态急停帧下发到假固件",
              any(mt == shoot_mod.T_ESTOP for mt, _ in fw.received),
              str([mt for mt, _ in fw.received][-5:]))
        # 烧录（假 OpenOCD；固件文件先上传）
        boundary = "----acc"
        payload = (f"--{boundary}\r\nContent-Disposition: form-data; "
                   'name="file"; filename="acc.bin"\r\n\r\n').encode() + b"\x00" + \
                  f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            BASE + "/api/firmware/upload", data=payload, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Authorization": "Bearer " + harness._auth["emergency"]})
        with urllib.request.urlopen(req, timeout=10) as r:
            check("断网态固件上传", r.status == 200)
        c, b = api("/api/v2/devices/acc-sta/flash",
                   {"firmware": "acc.bin", "target": "stm32h723vg"})
        check("断网态烧录任务启动（假 OpenOCD）", c == 200, str(b)[:80])
        deadline = time.time() + 25
        state = None
        while time.time() < deadline:
            _, lg = api("/api/v2/devices/acc-sta/flash/log")
            state = lg.get("state")
            if state != "running":
                break
            time.sleep(0.8)
        check("断网态烧录成功（模拟链路完整）", state == "success", str(state))
    finally:
        api("/api/v2/devices/acc-sta/shoot/close", {}, "POST")
        api("/api/v2/devices/acc-sta", method="DELETE")
        fw.stop()

    # 飞书故障注入不阻塞：同步失败（死端口超时）期间调试 API 依旧
    # 先触发一轮注定失败的同步，再在**其进行中**测调试 API 延迟（原实现测在触发之前）
    _fail_trig = api("/api/portal/sync", body={}, method="POST", auth="member")
    time.sleep(0.3)
    samples = []
    for _ in range(5):
        t0 = time.time()
        c, _ = api("/api/v2/devices")       # 调试 API（应急）
        samples.append(time.time() - t0)
        time.sleep(0.2)
    dt = max(samples)
    check("同步故障/挂起期间调试 API 即时可用（5 次采样 max<1.5s）",
          c == 200 and dt < 1.5, f"max={dt:.2f}s")
    note("同步故障期间调试 API 延迟", f"5 次采样 max={dt:.3f}s（触发失败同步后测量）")
    # 手动触发一轮注定失败的同步（member 在宽限内触发），失败后旧快照保留。
    # 观测改读临时库：断网超宽限后 member 被 403 收紧，无身份可查 sync-status
    # ——记录为运维观察（见验收文档"已知限制"）。
    code, r = api("/api/portal/sync", body={}, method="POST", auth="member")
    time.sleep(8)                            # 死端口超时 + 重试窗口
    db_meta = _read_db_meta()
    check("断网同步失败保留旧快照（版本不变）",
          int(db_meta.get("current_version", 0)) == st["snapshot_version"],
          f"{db_meta.get('current_version')} vs {st['snapshot_version']}")
    # 只接受 network：config 属"表映射缺失"这类配置回归（阶段 6 的 A1 缺陷模式），
    # 若把它算通过，会把配置回归掩盖成"断网正常"
    check("失败记录错误类别 = network（不含 config）",
          db_meta.get("last_error") == "network", str(db_meta.get("last_error")))
    code, r = api("/api/portal/projects", auth="member")
    if code == 200:
        check("失败后管理数据仍可读（旧快照）", r.get("has_snapshot") is True)
    else:
        check("失败后管理数据仍可读（member 已过宽限 → 403 独立状态）",
              code == 403, str(code))
        note("断网观测者限制", "断网超宽限后唯一 member 被收紧，sync 状态只能经服务器"
             "本地库查看（运维手册应记录）")

    print("== 6-2 离线重启（同临时目录，断网态） ==")
    tok = harness._auth["emergency"]
    check("断网态重启服务", harness.restart_service(
        env_extra={"OPENOCD_BIN": make_fake_openocd(),
                           "OPENOCD_TCP_BIN": make_fake_openocd(), "FLASH_TIMEOUT": "20"}))
    code, _ = http_get("/api/status", headers={"Authorization": "Bearer " + tok})
    check("重启后既有应急会话仍有效", code == 200, str(code))
    code, body = api("/api/auth/emergency/login",
                     {"username": "test-ops",
                      "password": harness._TEST_EMERGENCY_PASSWORD}, auth=None)
    check("重启后（仍断网）新应急登录可用", code == 200, str(code))
    # 重启后资源本地加载（原实现的资源检查发生在重启前，归属错误）。
    # 匿名探测：登录页与静态资源必须 200（离线自足），受保护页面必须 302 到
    # /login（不依赖飞书的本地兜底）——两者都 200 反而是鉴权失效。
    after = {p: http_get(p)[0] for p in ("/login", "/portal/portal.css",
                                         "/portal/portal.js")}
    check("重启后门户静态资源本地可用（离线自足）",
          all(v == 200 for v in after.values()), str(after))
    code_q, _ = http_get("/quality")
    check("重启后受保护页面匿名 → 302 /login（本地判定，不等飞书）",
          code_q == 302, str(code_q))
    code, r = api("/api/portal/projects", auth="member") if harness._auth.get("member") else (0, {})
    # member 会话重启后复核不可用收紧（断网 > 宽限）→ 403 独立状态
    if harness._auth.get("member"):
        deadline = time.time() + 20
        got = None
        while time.time() < deadline:
            c2, b2 = http_get("/api/portal/overview",
                              headers={"Authorization": "Bearer " + harness._auth["member"]})
            if c2 == 403:
                got = b2
                break
            time.sleep(1)
        check("断网重启后成员管理访问 → 403 独立状态（不伪称退出组织）",
              got is not None and b"recheck_unavailable" in got, str(got)[:90])


def _small_data():
    from test_portal_sync import fake_data as _fd
    d = _fd(with_bad=False)
    # 精简：2 任务即可
    d["tblFixPortalSync2"]["records"] = d["tblFixPortalSync2"]["records"][:2]
    return d


def _manual_sync():
    time.sleep(1.2)
    req = urllib.request.Request(
        BASE + "/api/portal/sync", data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + harness._auth["member"],
                 "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass
    time.sleep(6)


def t_identity_channels():
    """6-4：三通道身份矩阵补缺（过期显式用例、成员移除收紧、CLI 矩阵）。"""
    print("== 6-4 身份边界补缺（过期 / 成员移除 / CLI） ==")
    # 服务与 fake 已由上一段重建（断网态）——重新拉起 fake 做成员路径
    harness.start_fake_feishu(tenant_key="tenant-test")
    harness._auth["emergency"] = harness._login_emergency()
    sid_m = harness.login_member()
    check("重建成员会话", bool(sid_m))

    # 过期会话：改临时会话文件的 emergency 会话为已过期 → 重启（会话文件仅
    # 启动时加载，外部修改需重启生效——测试即验证重启加载路径）
    import hashlib
    sid_e = harness._auth["emergency"]
    dg_e = hashlib.sha256(sid_e.encode()).hexdigest()
    dg_m = hashlib.sha256(sid_m.encode()).hexdigest()
    sj = json.load(open(harness._tmpdir + "/sessions.json", encoding="utf-8"))
    sj["sessions"][dg_e]["expires"] = time.time() - 10
    json.dump(sj, open(harness._tmpdir + "/sessions.json", "w", encoding="utf-8"))
    _ocd = make_fake_openocd()
    check("重启（加载已过期会话文件）", harness.restart_service(env_extra={
        "OPENOCD_BIN": _ocd, "OPENOCD_TCP_BIN": _ocd, "FLASH_TIMEOUT": "20"}))
    code, _ = http_get("/api/status", headers={"Authorization": "Bearer " + sid_e})
    check("过期会话 REST → 401", code == 401, str(code))

    async def ws_expired():
        try:
            async with websockets.connect(
                    BASE.replace("http", "ws") + "/ws/shoot",
                    additional_headers={"Authorization": "Bearer " + sid_e},
                    open_timeout=5) as ws:
                try:
                    await asyncio.wait_for(ws.recv(), 3)
                    return "data"
                except websockets.ConnectionClosed as e:
                    return e.rcvd.code if e.rcvd else None
        except Exception:
            return "error"
    r = asyncio.run(ws_expired())
    check("过期会话 WS 握手 → 4401（唯一可能值）", r == 4401, str(r))

    # 4408 只发生在"已建立连接后会话到期"的周期复核路径：用短 TTL 重启制造该场景
    check("重启（成员会话 TTL=3s，制造到期）", harness.restart_service(env_extra={
        "OPENOCD_BIN": _ocd, "OPENOCD_TCP_BIN": _ocd, "FLASH_TIMEOUT": "20",
        "AUTH_SESSION_MEMBER_TTL": "3", "AUTH_WS_RECHECK": "1"}))
    sid_short = harness.login_member()
    check("短 TTL 成员登录成功", bool(sid_short))

    async def ws_expire():
        try:
            async with websockets.connect(
                    BASE.replace("http", "ws") + "/ws/shoot",
                    additional_headers={"Authorization": "Bearer " + sid_short},
                    open_timeout=5) as ws:
                deadline = time.time() + 12
                while time.time() < deadline:
                    try:
                        await asyncio.wait_for(ws.recv(), 1)
                    except asyncio.TimeoutError:
                        continue
                return "timeout-no-close"
        except websockets.ConnectionClosed as e:
            return e.rcvd.code if e.rcvd else None
        except Exception as e:
            return "error:" + str(e)[:40]
    r2 = asyncio.run(ws_expire())
    check("已建立连接 + 会话到期 → 4408（周期复核路径）", r2 == 4408, str(r2))
    # 恢复常规 TTL
    check("重启（恢复常规 TTL）", harness.restart_service(env_extra={
        "OPENOCD_BIN": _ocd, "OPENOCD_TCP_BIN": _ocd, "FLASH_TIMEOUT": "20"}))
    # 恢复应急身份（过期用例只针对旧 token）
    harness._auth["emergency"] = harness._login_emergency()
    # 成员旧 token 也已随文件/重启失效——重新登录 member
    sid_m = harness.login_member()
    check("重建成员会话（第二轮）", bool(sid_m))
    dg_m = hashlib.sha256(sid_m.encode()).hexdigest()
    harness._fake_feishu.member_status = {
        "is_activated": True, "is_resigned": True, "is_exited": True,
        "is_frozen": False, "is_unjoin": False}
    deadline = time.time() + 20
    gone = False
    while time.time() < deadline:
        code, _ = http_get("/api/portal/overview",
                           headers={"Authorization": "Bearer " + sid_m})
        if code == 401:
            gone = True
            break
        time.sleep(1)
    check("成员移除后复核收紧 → 401", gone, str(code))
    # 恢复正常状态供后续
    harness._fake_feishu.member_status = {
        "is_activated": True, "is_resigned": False, "is_exited": False,
        "is_frozen": False, "is_unjoin": False}

    # CLI 三通道矩阵（子进程真跑 wsctl）
    env = dict(os.environ)
    env["WSCTL_PASSWORD"] = harness._TEST_EMERGENCY_PASSWORD   # 密码经 env（不作参数）
    def run_cli(args, home=None):
        e = dict(env)
        if home:
            e["HOME"] = home
        return subprocess.run([sys.executable, "wsctl.py", "--server", BASE] + args,
                              capture_output=True, text=True, timeout=30, env=e)
    home1 = tempfile.mkdtemp(prefix="wsctl-anon-")
    r = run_cli(["status"], home=home1)
    check("CLI 匿名 → 401 提示登录", r.returncode == 2 and "login" in r.stderr,
          r.stderr.strip()[:80])
    home2 = tempfile.mkdtemp(prefix="wsctl-em-")
    r = run_cli(["login", "--method", "emergency", "--username", "test-ops"], home=home2)
    ok_login = r.returncode == 0 and "已登录" in r.stdout
    check("CLI 应急登录（交互密码经 env）", ok_login, (r.stdout or r.stderr)[:80])
    r = run_cli(["status"], home=home2)
    check("CLI 应急调试可用", r.returncode == 0 and "在线" in r.stdout,
          (r.stdout or r.stderr)[:80])
    api("/api/v2/devices", {"id": "cli-dev", "name": "cli",
                            "transport": "vllink_tcp", "host": "127.0.0.1",
                            "uart_port": 1})
    r = run_cli(["--device", "cli-dev", "status"], home=home2)
    check("CLI --device 参数保留（v2 路由）", r.returncode == 0, (r.stdout or "")[:60])
    # 仅看 returncode 无法证明 --device 真被使用（旧端点同样 200）；
    # 不存在的设备名必须失败，才说明路径确实按设备构造
    r_bad = run_cli(["--device", "no-such-device-xyz", "status"], home=home2)
    check("CLI --device 真按设备取路径（不存在的设备应 404 失败）",
          r_bad.returncode != 0 and "404" in (r_bad.stderr or ""),
          f"rc={r_bad.returncode} {(r_bad.stderr or r_bad.stdout)[:70]}")
    api("/api/v2/devices/cli-dev", method="DELETE")
    # 复核不可用（member 经 CLI）：fake contact 注错 + 宽限过 → 独立状态文案
    home3 = tempfile.mkdtemp(prefix="wsctl-mem-")
    # CLI member：浏览器式 exchange（fake code/state）
    class NoRedir2(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedir2)
    try:
        op.open(urllib.request.Request(BASE + "/auth/feishu/start"), timeout=10)
        state = ""
    except urllib.error.HTTPError as e:
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(e.headers.get("Location", "")).query).get("state", [""])[0]
    req = urllib.request.Request(
        BASE + "/api/auth/exchange",
        data=json.dumps({"code": "fake-auth-code", "state": state}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r2:
        sess = json.loads(r2.read().decode())["session"]
    sf = os.path.join(home3, ".config", "wsctl")
    os.makedirs(sf, exist_ok=True)
    import hashlib as _h
    sfile = os.path.join(sf, "session-" + _h.sha256(BASE.encode()).hexdigest()[:12] + ".json")
    json.dump({"server": BASE, "session": sess, "user": "feishu:test",
               "role": "member", "expires": time.time() + 3600},
              open(sfile, "w"), )
    os.chmod(sfile, 0o600)
    harness._fake_feishu.fail_contact = True
    deadline = time.time() + 20
    rr = None
    while time.time() < deadline:
        rr = run_cli(["status"], home=home3)
        if "无法确认成员身份" in rr.stderr or "403" in rr.stderr:
            break
        time.sleep(1.5)
    rr = rr or type("R", (), {"stderr": ""})()
    check("CLI 成员复核不可用 → 403 独立状态（非退出组织）",
          "无法确认成员身份" in rr.stderr or "403" in rr.stderr,
          rr.stderr.strip()[:90])
    harness._fake_feishu.fail_contact = False


def t_scheduler_and_switch():
    """6-5：60s 定时路径（集成）与 PORTAL_SYNC_ENABLED=0 回退开关。"""
    print("== 6-5 定时同步与回退开关 ==")
    # 定时路径：短间隔（5s）重启，观察两轮自动同步（零手动触发）
    harness._fake_feishu.bitable = _small_data()
    check("重启（定时 5s）", harness.restart_service(env_extra={
        "OPENOCD_BIN": make_fake_openocd(), "FLASH_TIMEOUT": "20",
        "PORTAL_SYNC_INTERVAL": "5", "PORTAL_MANUAL_MIN_GAP": "1"}))
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    _, st0 = api("/api/portal/sync-status", auth="member")
    att0 = st0.get("last_attempt_at") or 0
    deadline = time.time() + 25
    ok_rounds = 0
    last_att = att0
    while time.time() < deadline:
        time.sleep(2)
        _, st = api("/api/portal/sync-status", auth="member")
        att = st.get("last_attempt_at") or 0
        if att > last_att + 1 and not st.get("syncing"):
            last_att = att
            if st.get("counts", {}).get("tasks") == 2 and not st.get("last_error"):
                ok_rounds += 1
        if ok_rounds >= 2:
            break
    check("定时同步自动执行 ≥2 轮且成功（零手动触发，counts=2 无错）",
          ok_rounds >= 2, f"ok_rounds={ok_rounds}")
    note("定时同步", f"5s 间隔实测成功 {ok_rounds} 轮/25s 窗口（counts.tasks=2）")

    # 回退开关：ENABLED=0 重启 → 同步停、快照可读、调试正常
    check("重启（同步关闭）", harness.restart_service(env_extra={
        "OPENOCD_BIN": make_fake_openocd(), "FLASH_TIMEOUT": "20",
        "PORTAL_SYNC_ENABLED": "0", "PORTAL_MANUAL_MIN_GAP": "1",
        # 关键：观察窗内必须有定时机会，否则"零飞书请求"恒真（开关失效也看不出来）
        "PORTAL_SYNC_INTERVAL": "5"}))
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    _, st = api("/api/portal/sync-status", auth="member")
    check("开关关闭：sync_enabled=false", st.get("sync_enabled") is False)
    code, r = api("/api/portal/sync", body={}, method="POST", auth="member")
    check("开关关闭：手动刷新明确拒绝", r.get("accepted") is False and "关闭" in r.get("detail", ""),
          str(r.get("detail")))
    code, r = api("/api/portal/projects", auth="member")
    check("开关关闭：既有快照仍可读", r.get("has_snapshot") is True)
    _, st_sw = api("/api/portal/sync-status", auth="member")
    check("开关关闭：定时周期为 5s（观察窗内确有定时机会）",
          st_sw.get("sync_interval_sec") == 5, str(st_sw.get("sync_interval_sec")))
    c0 = harness._feishu_calls("bitable")
    time.sleep(12)                            # >2 个定时周期
    c1 = harness._feishu_calls("bitable")
    check("开关关闭：零飞书请求（跨 ≥2 个定时周期计数不增）", c1 == c0, f"{c0}->{c1}")
    c, _ = api("/api/status")
    check("开关关闭：调试 API 正常", c == 200)


def t_scan_gating():
    """R7 收敛验证：/api/v2/devices/scan 会主动扫 /24 内网。

    基线风险 R7 要求"引入鉴权后收敛 + 文档标注"——此处把两件事都做成可证伪
    断言：匿名必须 401；调试身份可用但扫描范围受 SCAN_SUBNET 约束、耗时有界。
    """
    print("== R7 内网扫描接口的鉴权收敛与范围约束 ==")
    check("重启（扫描范围限定空洞网段）", harness.restart_service(env_extra={
        "OPENOCD_BIN": make_fake_openocd(), "FLASH_TIMEOUT": "20",
        "SCAN_SUBNET": "127.0.1"}))
    harness._auth["emergency"] = harness._login_emergency()
    code, r = api("/api/v2/devices/scan", body={}, method="POST", auth=None)
    check("扫描接口匿名 → 401（不再是开放的内网扫描入口）",
          code == 401, f"{code} {str(r)[:60]}")
    t0 = time.time()
    code, r = api("/api/v2/devices/scan", body={}, method="POST")
    dt = time.time() - t0
    check("调试身份可用（200 + found/subnet 结构）",
          code == 200 and isinstance(r.get("found"), list) and "subnet" in r,
          f"{code} {str(r)[:60]}")
    check("扫描范围受 SCAN_SUBNET 约束（空洞网段无发现）",
          r.get("subnet") == "127.0.1.0/24" and r.get("found") == [],
          f"subnet={r.get('subnet')} found={len(r.get('found') or [])}")
    check("扫描耗时有界（<15s，不因 /24 全扫挂住请求）", dt < 15.0, f"{dt:.2f}s")
    note("内网扫描", f"SCAN_SUBNET=127.0.1 全扫耗时 {dt:.2f}s，found=0；"
         f"匿名 401")


def _trigger_sync(retries=3):
    """触发一轮手动同步（被合并/限频则等待后重试）。

    返回 (最后一次响应, 该次请求耗时)。限频窗口 1s，故重试间隔 2.5s。
    """
    r, dt = {}, 0.0
    for _ in range(retries):
        t = time.time()
        _, r = api("/api/portal/sync", body={}, method="POST", auth="member")
        dt = time.time() - t
        if r.get("accepted") is True:
            return r, dt
        time.sleep(2.5)
    return r, dt


def _wait_round_idle(timeout, want_ok=False, after=None):
    """等待同步轮结束：syncing=False 且 last_attempt_at 连续两次不变。

    after 非 None 时还要求 last_attempt_at > after（必须观察到**新一轮**的尝试，
    否则可能在"上一轮早已空闲"的状态上提前收敛）。
    返回是否收敛（want_ok=True 时还要求本轮无错误）。
    """
    deadline = time.time() + timeout
    last_att, stable = None, 0
    while time.time() < deadline:
        _, st = api("/api/portal/sync-status", auth="member")
        att = st.get("last_attempt_at") or 0
        started = after is None or att > after
        if started and not st.get("syncing") and att and att == last_att:
            stable += 1
            if stable >= 2 and (not want_ok or not st.get("last_error")):
                return True
        else:
            stable = 0
        last_att = att
        time.sleep(1)
    return False


def t_upstream_hang():
    """6-3 加强：上游"黑洞"（接受连接、永不回包）下的超时自愈。

    与"死端口拒连"是两条不同失败路径：拒连即时返回错误，黑洞会把请求挂在
    等待里。此处验证 ①重试/超时上限把挂起变成**有界**失败（不永久占住同步
    线程）②挂起窗口内调试链路仍即时可用 ③清障后自动恢复成功。
    """
    print("== 6-3 上游黑洞（挂起）超时自愈 ==")
    check("重启（短超时，为黑洞留出有界等待）", harness.restart_service(env_extra={
        "OPENOCD_BIN": make_fake_openocd(), "FLASH_TIMEOUT": "20",
        "PORTAL_SYNC_TIMEOUT": "3", "PORTAL_SYNC_RETRIES": "1",
        "PORTAL_SYNC_BACKOFF": "1", "PORTAL_SYNC_INTERVAL": "3600",
        "PORTAL_MANUAL_MIN_GAP": "1"}))
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    harness._fake_feishu.bitable = _small_data()
    harness._fake_feishu.hang_paths = []
    # 先做一轮正常同步：黑洞轮必须有"旧快照"可对照（版本不变）。
    # 先等启动轮收尾，否则手动触发会被合并/限频（限频窗口 1s）而观察不到新一轮
    _wait_round_idle(30)
    _, st_base0 = api("/api/portal/sync-status", auth="member")
    att_base = st_base0.get("last_attempt_at") or 0
    _trigger_sync()
    _wait_round_idle(40, want_ok=True, after=att_base)
    _, st_ok = api("/api/portal/sync-status", auth="member")
    check("黑洞前基线：一轮同步成功并发布快照",
          not st_ok.get("last_error") and bool(st_ok.get("snapshot_version")),
          f"err={st_ok.get('last_error')} ver={st_ok.get('snapshot_version')}")
    base_ver = st_ok.get("snapshot_version")

    # 注入黑洞：bitable 请求被挂住（token / contact 路径不受影响，故成员会话
    # 复核仍然可用，观测通道不必退化为直读数据库）
    harness._fake_feishu.hang_paths = ["/records", "/fields"]
    bt0 = harness._feishu_calls("bitable")
    _, st_pre = api("/api/portal/sync-status", auth="member")
    att_pre = st_pre.get("last_attempt_at") or 0
    t0 = time.time()
    r, dt_trig = _trigger_sync()
    check("黑洞下手动刷新入口不阻塞（异步受理 <2s）",
          r.get("accepted") is True and dt_trig < 2.0, f"{dt_trig:.2f}s {r}")
    time.sleep(0.5)
    # 挂起窗口内的调试链路延迟（原 6-3 只覆盖"拒连"故障形态）
    samples = []
    for _ in range(5):
        t1 = time.time()
        c, _ = api("/api/v2/devices")
        samples.append(time.time() - t1)
        time.sleep(0.2)
    dt = max(samples)
    check("上游挂起期间调试 API 即时可用（5 次采样 max<1.5s）",
          c == 200 and dt < 1.5, f"max={dt:.3f}s")
    note("上游挂起期间调试 API 延迟", f"5 次采样 max={dt:.3f}s（黑洞窗口内）")

    idle = _wait_round_idle(45, after=att_pre)
    dt_round = time.time() - t0
    bt1 = harness._feishu_calls("bitable")
    _, st_hang = api("/api/portal/sync-status", auth="member")
    check("黑洞确实命中上游（请求到达并计数，非空跑）",
          (bt1 - bt0) >= 1, f"bitable +{bt1 - bt0}")
    check("超时上限把挂起收敛为有界失败（同步线程未被永久占住）",
          idle, "45s 内未收敛")
    # 有界性要可证伪：既不能秒失败（说明没真挂住），也不能无上限。
    # 期望 ≈ 超时 3s + 退避 1s + 重试超时 3s ≈ 7s
    check("挂起轮耗时有界且确实等满超时（3s ≤ 耗时 ≤ 20s）",
          idle and 3.0 <= dt_round <= 20.0, f"{dt_round:.2f}s")
    note("黑洞轮收敛耗时", f"{dt_round:.2f}s（超时 3s + 退避 1s + 重试超时 3s；"
         f"注：last_duration_sec={st_hang.get('last_duration_sec')} 只在成功轮写入，"
         "失败轮沿用上一成功值，不能当失败轮耗时）")
    check("挂起失败归类 network（非 config）",
          st_hang.get("last_error") == "network", str(st_hang.get("last_error")))
    _, pg = api("/api/portal/projects", auth="member")
    check("挂起失败后旧快照仍可读且版本不变",
          pg.get("has_snapshot") is True and pg.get("snapshot_version") == base_ver,
          f"{pg.get('snapshot_version')} vs {base_ver}")

    # 自愈：清除黑洞 → 下一轮成功（证明挂起没有污染调度/令牌缓存）
    harness._fake_feishu.hang_paths = []
    _, st_pre2 = api("/api/portal/sync-status", auth="member")
    att_pre2 = st_pre2.get("last_attempt_at") or 0
    _trigger_sync()
    ok = _wait_round_idle(40, want_ok=True, after=att_pre2)
    _, st2 = api("/api/portal/sync-status", auth="member")
    # 失败的挂起轮不发布版本；清障后这轮数据与上一成功轮完全相同，
    # 按 sync.run_sync() 的 content_hash 闸门"无变化不发布新版本"——
    # 因此这里断言的是自愈本身（本轮成功、错误清空、版本不倒退），
    # 不再要求版本前进（那与闸门契约冲突，见 test_portal_sync 的同口径断言）
    check("清障后同步自动恢复（错误清空、未发布新版本）",
          ok and not st2.get("last_error")
          and st2.get("snapshot_version") == base_ver,
          f"ok={ok} err={st2.get('last_error')} ver={st2.get('snapshot_version')}")


async def _open_pages(pw, n, path):
    pages = []
    browser = await pw.chromium.launch()
    for _ in range(n):
        pg = await browser.new_page(viewport={"width": 1280, "height": 800})
        await pg.context.add_cookies([harness.auth_cookie("member")])
        await pg.goto(BASE + path + ("&" if "?" in path else "?") + "poll_ms=1000",
                      wait_until="domcontentloaded")
        pages.append(pg)
    return browser, pages


def t_concurrency():
    """6-6：多页面并发浏览不放大飞书请求；多人手动刷新合并。"""
    print("== 6-6 多页面并发 ==")
    harness.ensure_browser_env()
    from playwright.async_api import async_playwright

    # 干净计数环境（同步开、长间隔）
    _ocd4 = make_fake_openocd()
    ok = harness.restart_service(env_extra={
        "OPENOCD_BIN": _ocd4, "OPENOCD_TCP_BIN": _ocd4, "FLASH_TIMEOUT": "20",
        "PORTAL_SYNC_INTERVAL": "3600", "PORTAL_MANUAL_MIN_GAP": "2"})
    assert ok
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            pages, reqs = [], {}
            for i in range(3):
                pg = await browser.new_page(viewport={"width": 1280, "height": 800})
                await pg.context.add_cookies([harness.auth_cookie("member")])
                # 页面侧请求计数：证明轮询打的是本地 API（Playwright 无内建
                # 请求历史属性，须显式挂 request 监听）
                reqs[i] = []
                pg.on("request", lambda r, log=reqs[i]:
                      log.append(r.url) if "/api/" in r.url else None)
                await pg.goto(BASE + "/?poll_ms=1000", wait_until="domcontentloaded")
                pages.append(pg)
            # 观察窗 15s：3 页轮询期间触发 1 次手动同步
            c0 = harness._feishu_calls("bitable")
            await asyncio.sleep(2)
            req = urllib.request.Request(
                BASE + "/api/portal/sync", data=b"{}", method="POST",
                headers={"Authorization": "Bearer " + harness._auth["member"],
                         "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            await asyncio.sleep(13)
            c1 = harness._feishu_calls("bitable")
            # 一轮同步的真实成本 = 配置表数 × (fields 1 + records 1 + 记录链接 batch_get 1)，
            # 不展示单条链接的表（sync.NO_LINK_TABLES，如值日表）少 batch_get 那 1 次。
            # 按 harness.FAKE_TABLES_JSON 实际表数推导，新增语义表时不必再改这里
            # （2026-10-20 起含 roster / procurement / duty 共 8 张）。
            from app.portal.sync import NO_LINK_TABLES
            tables = json.loads(harness.FAKE_TABLES_JSON)
            n_tables = len(tables)
            expect = 3 * n_tables - sum(1 for t in tables if t in NO_LINK_TABLES)
            grew = c1 - c0
            page_reqs = sum(len(v) for v in reqs.values())
            note("多人浏览飞书请求",
                 f"3 页面轮询 + 1 次手动同步：bitable 请求 {c0}->{c1}（增量 {grew}）；"
                 f"同期页面请求 {page_reqs} 次")
            check("3 页面并发：飞书请求恰为一轮同步成本（不随人数放大）",
                  expect - 2 <= grew <= expect + 2,
                  f"{grew}（期望≈{expect}＝{n_tables} 表×3）")
            # 阶段 8（8-7）：1 页面对照组——同样时长内 1 个页面的请求量应与
            # 3 页面同量级（证明轮询打本地、人数不放大），而不是 1/3
            c1p0 = harness._feishu_calls("bitable")
            reqs1 = []
            pg1 = await browser.new_page(viewport={"width": 1280, "height": 800})
            await pg1.context.add_cookies([harness.auth_cookie("member")])
            pg1.on("request", lambda r, log=reqs1:
                   log.append(r.url) if "/api/" in r.url else None)
            await pg1.goto(BASE + "/?poll_ms=1000", wait_until="domcontentloaded")
            await asyncio.sleep(6)
            c1p1 = harness._feishu_calls("bitable")
            await pg1.close()
            grew_1p = c1p1 - c1p0
            note("1 页面对照组", f"6s 内单页面本地 API 请求 {len(reqs1)} 次、"
                 f"bitable 增量 {grew_1p}")
            check("6-6 对照：单页面轮询不产生飞书请求（0 增量）",
                  grew_1p == 0, str(grew_1p))
            check("6-6 对照：单页面本地 API 请求 ≥4 次（轮询确实在跑）",
                  len(reqs1) >= 4, str(len(reqs1)))
            check("页面轮询远多于飞书请求（轮询打本地 API，不上飞书）",
                  page_reqs >= 20 and grew <= expect + 2,
                  f"page_reqs={page_reqs} grew={grew} 期望≈{expect}")
            # 三页面几乎同时点刷新 → 只跑一轮（计数再增 ≤8）+ 至少一个页面拿到合并/限频反馈
            c2 = harness._feishu_calls("bitable")
            results = []
            for pg in pages:
                r = await pg.evaluate("""() => fetch('/api/portal/sync', {method: 'POST'})
                    .then(r => r.json()).then(j => j.accepted + '|' + j.detail)""")
                results.append(r)
            await asyncio.sleep(10)
            c3 = harness._feishu_calls("bitable")
            grew2 = c3 - c2
            merged = sum(1 for x in results if ("合并" in x) or ("频繁" in x))
            note("多人手动刷新", f"3 页面同刻刷新：新增同步请求 {grew2}，反馈 {results}")
            check("多人手动刷新：至多一轮同步（增量 ≤ 一轮成本）",
                  0 <= grew2 <= expect + 2, f"{grew2}（一轮≈{expect}）")
            check("同刻刷新至少一页拿到『合并/限频』反馈", merged >= 1, str(results))
            for pg in pages:
                await pg.close()
            await browser.close()
    asyncio.run(run())


def t_telemetry_under_load():
    """6-7：持续遥测下的同步 + 多页面轮询：定量观察（不凭空结论）。"""
    print("== 6-7 遥测负载观察 ==")
    from test_transports import FakeFirmwareServer
    from playwright.async_api import async_playwright
    harness.ensure_browser_env()
    fw = FakeFirmwareServer()
    try:
        api("/api/v2/devices", {"id": "load-sta", "name": "load",
                                "transport": "vllink_tcp", "host": "127.0.0.1",
                                "uart_port": fw.port})
        api("/api/v2/devices/load-sta/shoot/open", {})
        time.sleep(0.5)

        async def measure(seconds, with_load):
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                pages = []
                if with_load:
                    for _ in range(2):
                        pg = await browser.new_page()
                        await pg.context.add_cookies([harness.auth_cookie("member")])
                        await pg.goto(BASE + "/?poll_ms=1000",
                                      wait_until="domcontentloaded")
                        pages.append(pg)
                intervals = []
                async with websockets.connect(
                        BASE.replace("http", "ws") + "/ws/v2/devices/load-sta/shoot",
                        additional_headers=harness.ws_headers()) as ws:
                    t0 = time.time()
                    last = t0
                    # 负载触发放后台线程：观测循环全程持续 recv（阶段 6 修复：
                    # 旧写法 sleep 期间不读 socket，把缓冲涌出误判为 6.7s 断流）
                    trigger = None
                    if with_load:
                        async def _trigger():
                            await asyncio.sleep(seconds / 3)
                            req = urllib.request.Request(
                                BASE + "/api/portal/sync", data=b"{}", method="POST",
                                headers={"Authorization": "Bearer " + harness._auth["member"],
                                         "Content-Type": "application/json"})
                            await asyncio.to_thread(
                                lambda: urllib.request.urlopen(req, timeout=10).read())
                        trigger = asyncio.create_task(_trigger())
                    n = 0
                    while time.time() - t0 < seconds:
                        try:
                            m = json.loads(await asyncio.wait_for(ws.recv(), 3))
                        except asyncio.TimeoutError:
                            intervals.append(3.0)   # 显式记录 3s 无帧
                            continue
                        if m.get("type") == "telemetry":
                            now = time.time()
                            intervals.append(now - last)
                            last = now
                            n += 1
                    if trigger:
                        await trigger
                for pg in pages:
                    await pg.close()
                await browser.close()
                return n, intervals
        # 负载确实发生过的判据：被触发的同步会读假飞书 bitable；轮询页面
        # 自身只读本地快照 API、不读 bitable，故该增量只能来自同步。
        bt0 = harness._feishu_calls("bitable") or 0
        n0, iv0 = asyncio.run(measure(15, with_load=False))
        bt1 = harness._feishu_calls("bitable") or 0
        n1, iv1 = asyncio.run(measure(20, with_load=True))
        bt2 = harness._feishu_calls("bitable") or 0
        rate0 = n0 / 15.0
        rate1 = n1 / 20.0
        gap0 = max(iv0) if iv0 else 0
        gap1 = max(iv1) if iv1 else 0
        note("遥测基线（无负载 15s）", f"{n0} 帧，{rate0:.0f} 帧/s，最大间隔 {gap0*1000:.0f}ms")
        # 阶段 8（8-7）：真实节奏对照——假固件同时按"标称 20Hz"与"实测间隔"
        # 两种口径计算，避免只用自比结论
        real_hz = (n0 / 15.0)
        note("遥测节奏对照", f"标称 20Hz vs 实测 {real_hz:.1f}Hz（同一窗口，"
             f"差异 {abs(real_hz-20):.1f}Hz = 假固件自身抖动）")
        note("遥测负载（同步+2页轮询 20s）", f"{n1} 帧，{rate1:.0f} 帧/s，最大间隔 {gap1*1000:.0f}ms")
        note("负载轮 bitable 调用增量", f"+{bt2 - bt1}（基线段 +{bt1 - bt0}）")
        # 无此判据时，"负载下无断流"可能只是负载根本没发生
        check("6-7 负载轮确实发生同步上游调用", (bt2 - bt1) >= 4,
              f"bitable +{bt2 - bt1}")
        # 只记录与宽松判定：负载下帧率不低于基线 70% 且无 >1s 断流
        check("负载下遥测帧率 ≥ 基线 70%", rate1 >= rate0 * 0.7,
              f"{rate1:.0f} vs {rate0:.0f}")
        check("负载下无 >1s 遥测断流", gap1 < 1.0, f"max {gap1*1000:.0f}ms")
    finally:
        api("/api/v2/devices/load-sta/shoot/close", {}, "POST")
        api("/api/v2/devices/load-sta", method="DELETE")
        fw.stop()


def t_real_env_probe():
    """6-8 非侵入部分：130:8000 门户与 80 旧服务并存探测（只读）。"""
    print("== 6-8 真机非侵入探测（192.168.1.130） ==")
    import socket
    def probe(port, path, expect=None):
        url = f"http://192.168.1.130:{port}{path}"
        class NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(NoRedir)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "portal-acceptance-probe/1.0")
        try:
            with op.open(req, timeout=8) as r:
                return r.status, r.headers.get("location", "")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("location", "")
        except Exception as e:
            return None, str(e)[:60]
    # 先探可达性：当前会话所在网络不在实验室局域网时 → SKIP（环境性，
    # 不算失败；历史实证见 docs/portal-progress.md 2026-09-09/10 的 130 记录）
    code0, _ = probe(8000, "/login")
    if code0 is None:
        skip("[130] 真机非侵入探测（当前网络不可达，引用历史实证）")
        note("6-8 真机探测", "当前会话网络无法路由到 192.168.1.130（内网隔离），"
             "本轮未执行；以 2026-09-09/10 维护者/部署轮的 130 实测记录为准"
             "（真实同步 2/3/2/2、匿名 302、fixtures.js 404、真实 OAuth 登录留痕）")
        return
    for port, path, want, label in [
        (8000, "/", 302, "匿名 / → 登录"),
        (8000, "/api/portal/overview", 401, "管理 API 匿名 401"),
        (8000, "/debug/", 302, "调试台受控"),
        (8000, "/portal/portal.css", 200, "门户资源本地加载"),
        (8000, "/portal/fixtures.js", 404, "fixture 已删（生产纯净）"),
        (80, "/", None, "80 旧服务仍在线（勿动）"),
    ]:
        code, extra = probe(port, path)
        if want is None:
            check(f"[130:{port}] {label}", code is not None, str(code))
        else:
            check(f"[130:{port}] {label}（{path} → {want}）", code == want,
                  f"{code} {extra[:60]}")


def run():
    t_offline_and_decoupling()
    t_identity_channels()
    t_scan_gating()
    t_scheduler_and_switch()
    t_concurrency()
    t_upstream_hang()
    t_telemetry_under_load()
    t_real_env_probe()
    print("\n== 定量观察汇总（供验收报告） ==")
    for k, v in NOTES.items():
        print(f"  {k}: {v}")
    if SKIPPED:
        print(f"\n未执行（不计入通过数）: {len(SKIPPED)} 项")
        for s in SKIPPED:
            print(f"  - {s}")


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
    print(f"\n验收: {len(PASS)} 通过, {len(FAIL)} 失败, {len(SKIPPED)} 未执行")
    raise SystemExit(1 if FAIL else 0)
