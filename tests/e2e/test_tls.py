# -*- coding: utf-8 -*-
"""阶段 9（9-7）：HTTPS / TLS 用例——自签本地 CA + uvicorn TLS + Secure Cookie + wss + 80→443。

全部隔离：临时证书目录 + 独立端口（8092 TLS / 8093 跳转）+ 与主测试服务同一临时数据目录，
不触碰生产、不依赖外部 CA（内网域名无法用 Let's Encrypt HTTP-01，维护者选定自签 CA）。
"""
import asyncio
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                        # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
TLS_PORT, REDIR_PORT = 8092, 8093
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def _port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _req(url, ctx=None, method="GET", body=None, headers=None, no_redirect=True):
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    # 自签 CA 校验必须挂在 HTTPSHandler 上（OpenerDirector.open 不接受 context）
    handlers = [urllib.request.HTTPSHandler(context=ctx)] if ctx else []
    handlers.append(NoRedir if no_redirect else urllib.request.HTTPRedirectHandler())
    op = urllib.request.build_opener(*handlers)
    try:
        with op.open(req, timeout=10) as r:
            # 返回 r.headers（大小写不敏感的 Message），不要 dict()——uvicorn 发的是小写
            return r.status, r.headers, r.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:                                   # noqa: BLE001
        class _Empty(dict):
            def get(self, *a, **k):
                return ""
        return None, _Empty(), "%s: %s" % (type(e).__name__, str(e)[:80])


def run():
    print("== 9-7 HTTPS（自签本地 CA） ==")
    tlsdir = tempfile.mkdtemp(prefix="tls-drill-")
    r = subprocess.run([PY, "scripts/tls_issue.py", "--dir", tlsdir,
                        "--host", "portal.example.com", "--ip", "127.0.0.1"],
                       capture_output=True, text=True, cwd=REPO, timeout=120)
    check("签发本地 CA + 服务器证书", r.returncode == 0 and
          os.path.isfile(os.path.join(tlsdir, "server.crt")), r.stderr[-120:])
    ca = os.path.join(tlsdir, "ca.crt")
    check("CA/服务器私钥权限 600",
          oct(os.stat(os.path.join(tlsdir, "ca.key")).st_mode)[-3:] == "600"
          and oct(os.stat(os.path.join(tlsdir, "server.key")).st_mode)[-3:] == "600")
    san = subprocess.run(["openssl", "x509", "-in", os.path.join(tlsdir, "server.crt"),
                          "-noout", "-text"], capture_output=True, text=True).stdout
    check("证书 SAN 含域名与 IP", "DNS:portal.example.com" in san and "IP Address:127.0.0.1" in san)

    # 起一个 TLS 实例（与主测试服务共享数据目录，独立端口）
    env = dict(os.environ)
    env.update({
        "WEB_PORT": str(TLS_PORT), "WEB_HOST": "127.0.0.1",
        "TLS_CERT_FILE": os.path.join(tlsdir, "server.crt"),
        "TLS_KEY_FILE": os.path.join(tlsdir, "server.key"),
        "AUTH_SESSIONS_FILE": os.path.join(harness._tmpdir, "sessions.json"),
        "AUTH_EMERGENCY_FILE": os.path.join(harness._tmpdir, "emergency_users.json"),
        "AUTH_ACCOUNTS_FILE": os.path.join(harness._tmpdir, "accounts.json"),
        "AUTH_AUDIT_FILE": os.path.join(harness._tmpdir, "audit.log"),
        "AUTH_EMERGENCY_ENABLED": "1",
        "PORTAL_DB": os.path.join(harness._tmpdir, "portal.db"),
        "FEISHU_CONFIG_FILE": os.path.join(harness._tmpdir, "feishu-absent.json"),
        "PORTAL_SYNC_INTERVAL": "3600",
    })
    proc = subprocess.Popen([os.path.join(REPO, ".venv/bin/uvicorn"), "app.main:app",
                             "--host", "127.0.0.1", "--port", str(TLS_PORT),
                             "--ssl-certfile", os.path.join(tlsdir, "server.crt"),
                             "--ssl-keyfile", os.path.join(tlsdir, "server.key")],
                            cwd=REPO, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        ctx = ssl.create_default_context(cafile=ca)
        deadline = time.time() + 25
        code = None
        while time.time() < deadline:
            code, _, _ = _req("https://127.0.0.1:%d/login" % TLS_PORT, ctx=ctx)
            if code:
                break
            time.sleep(0.5)
        check("TLS 实例就绪：https://…/login → 200（CA 校验通过）", code == 200, str(code))

        # 不信任自签 CA 时应当失败（证明真的在校验证书，不是静默明文）
        code_bad, _, err = _req("https://127.0.0.1:%d/login" % TLS_PORT,
                                ctx=ssl.create_default_context())
        if code_bad is not None:      # 兜底：把"没报错"也当作可诊断信息
            err = "意外成功（%s）" % code_bad
        check("不装 CA 时校验失败（自签证书未被信任）",
              code_bad is None and "CERTIFICATE" in err.upper(), err[:70])

        # 明文打到 TLS 端口必须失败
        code_plain, _, _ = _req("http://127.0.0.1:%d/login" % TLS_PORT)
        check("明文 HTTP 打 TLS 端口失败（不降级）", code_plain is None, str(code_plain))

        # 管理 API 在 HTTPS 下语义不变
        code_api, _, _ = _req("https://127.0.0.1:%d/api/portal/overview" % TLS_PORT, ctx=ctx)
        check("HTTPS 下匿名管理 API → 401", code_api == 401, str(code_api))

        # Cookie Secure：应急登录（测试基建开启）走 TLS 拿会话
        body = json.dumps({"username": "test-ops",
                           "password": harness._TEST_EMERGENCY_PASSWORD}).encode()
        code, hdrs, txt = _req("https://127.0.0.1:%d/api/auth/emergency/login" % TLS_PORT,
                               ctx=ctx, method="POST", body=body,
                               headers={"Content-Type": "application/json"})
        cookie = hdrs.get("Set-Cookie", "")
        check("TLS 下会话 Cookie 带 Secure", code == 200 and "Secure" in cookie,
              f"{code} {cookie[:60]}")
        tok = None
        if "dsh_session=" in cookie:
            tok = cookie.split("dsh_session=")[1].split(";")[0]
        check("拿到会话令牌（供 wss 用）", bool(tok))

        # wss：TLS 上的 WebSocket 握手（证书校验通过即证明 wss 可用）
        async def wss_probe():
            import websockets
            try:
                async with websockets.connect(
                        "wss://127.0.0.1:%d/ws/shoot" % TLS_PORT, ssl=ctx,
                        additional_headers={"Authorization": "Bearer " + (tok or "")},
                        open_timeout=8) as ws:
                    # 握手成功即证明 wss 可用；3s 内无遥测（假设备未开）不算失败
                    try:
                        await asyncio.wait_for(ws.recv(), 3)
                        return "open+data"
                    except asyncio.TimeoutError:
                        return "open(no-data)"
            except websockets.ConnectionClosed as e:
                return "closed:%s" % (e.rcvd.code if e.rcvd else "?")
            except Exception as e:                            # noqa: BLE001
                return "err:%s:%s" % (type(e).__name__, str(e)[:70])
        res = asyncio.run(wss_probe())
        print("  · wss 探测结果: %s" % res)
        check("wss 握手完成（非 TLS 错误；连接后关闭属鉴权/设备语义）",
              res.startswith("open") or res.startswith("closed:440"), str(res))

        print("-- 80 → 443 跳转服务 --")
        redir = subprocess.Popen([PY, "scripts/tls_redirect.py",
                                  "--listen-port", str(REDIR_PORT),
                                  "--target-host", "127.0.0.1",
                                  "--target-port", str(TLS_PORT)],
                                 cwd=REPO, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.2)
            code, hdrs, _ = _req("http://127.0.0.1:%d/tasks?x=1" % REDIR_PORT)
            loc = hdrs.get("Location", "")
            check("80 端口 301 到 https 并保留路径",
                  code == 301 and loc.startswith("https://") and "/tasks?x=1" in loc,
                  f"{code} {loc}")
            # 跟随跳转应落到 TLS 实例的登录页
            code2, _, _ = _req("http://127.0.0.1:%d/login" % REDIR_PORT, ctx=ctx,
                               no_redirect=False)
            check("跟随跳转后到达 HTTPS 登录页（200）", code2 == 200, str(code2))
        finally:
            redir.terminate()
            redir.wait(timeout=5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\nTLS: {len(PASS)} 通过, {len(FAIL)} 失败")
    return len(PASS), len(FAIL)


if __name__ == "__main__":
    def _ensure():
        try:
            urllib.request.urlopen(harness.BASE + "/login", timeout=2)
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
