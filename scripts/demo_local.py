# -*- coding: utf-8 -*-
"""本地演示实例（离线可用）：起假飞书 + 示例数据 + 门户，给维护者看新版前端。

用途：校园网/实验室不可达时，在本机拉起一个**完整可点**的门户（含示例数据），
不连接真实飞书、不碰生产数据。数据目录默认 `backups/demo-run/`，可随时删除。

用法：
    .venv/bin/python scripts/demo_local.py                 # 启动（默认 8080 端口）
    .venv/bin/python scripts/demo_local.py --port 8081     # 换端口
    .venv/bin/python scripts/demo_local.py --stop          # 停止

登录：用户名 `demo` / 密码 `demo-portal-2026`（仅本演示库；D2 本站账号登录页）。
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)                                  # 供 import app.*
sys.path.insert(0, os.path.join(REPO, "tests", "e2e"))    # 供 import harness
DATA_DIR = os.path.join(REPO, "backups", "demo-run")
PID_FILE = os.path.join(DATA_DIR, "demo.pid")
USERNAME, PASSWORD = "demo", "demo-portal-2026"


def _stop():
    if os.path.isfile(PID_FILE):
        try:
            pid = int(open(PID_FILE, encoding="utf-8").read().strip())
            os.kill(pid, signal.SIGTERM)
            print("已停止演示实例（pid=%d）" % pid)
        except Exception as e:                       # noqa: BLE001
            print("停止失败（可能已退出）:", e)
        os.remove(PID_FILE)
    else:
        print("没有正在运行的演示实例")


def _seed_account(path):
    from app.auth.passwords import hash_password
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {"version": 1, "accounts": {
        "ou_demo_0001": {
            "username": USERNAME, "display": "演示账号", "tenant_key": "demo",
            "hash": hash_password(PASSWORD), "status": "active",
            "created_at": time.time(), "password_updated_at": time.time(),
            "last_login_at": None, "last_seen_org_at": time.time()}}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)


def _local_ips():
    """取本机可访问地址；没有 hostname 命令时退回 socket，避免脚本崩在打印地址上。"""
    import shutil
    if shutil.which("hostname"):
        try:
            out = subprocess.run(["hostname", "-I"], capture_output=True,
                                 text=True, timeout=3).stdout.split()
            if out:
                return [ip for ip in out if ":" not in ip]
        except Exception:                            # noqa: BLE001
            pass
    ips = []
    try:
        import socket
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80))
        ips.append(sk.getsockname()[0])
        sk.close()
    except Exception:                                # noqa: BLE001
        pass
    return ips


def main():
    ap = argparse.ArgumentParser(description="本地演示实例（离线示例数据）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--redirect-host", action="append", default=[],
                    metavar="HOST",
                    help="额外允许的登录回调主机（如局域网 IP）；可重复"
                         "。127.0.0.1/localhost/本机 IP 已自动加入")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--serve", action="store_true",
                    help="保持前台常驻（假飞书在启动器进程内，退出会导致后续同步失败）")
    args = ap.parse_args()
    if args.stop:
        return _stop()

    os.makedirs(DATA_DIR, exist_ok=True)
    cfg = {
        "AUTH_SESSIONS_FILE": os.path.join(DATA_DIR, "sessions.json"),
        "AUTH_ACCOUNTS_FILE": os.path.join(DATA_DIR, "accounts.json"),
        "AUTH_AUDIT_FILE": os.path.join(DATA_DIR, "audit.log"),
        "AUTH_EMERGENCY_FILE": os.path.join(DATA_DIR, "emergency_users.json"),
        "PORTAL_DB": os.path.join(DATA_DIR, "portal.db"),
        "DEVICES_FILE": os.path.join(DATA_DIR, "devices.json"),
        "PRESETS_FILE": os.path.join(DATA_DIR, "presets.json"),
        "MOTOR_PRESETS_FILE": os.path.join(DATA_DIR, "motor_presets.json"),
        "FIRMWARE_DIR": os.path.join(DATA_DIR, "firmware"),
        "FEISHU_CONFIG_FILE": os.path.join(DATA_DIR, "absent.json"),
    }
    _seed_account(cfg["AUTH_ACCOUNTS_FILE"])
    for k, v in cfg.items():
        if k.endswith("_DIR"):
            os.makedirs(v, exist_ok=True)

    # 假飞书 + 示例数据（与验收套件同一份数据集：含逾期/受阻/质量项）
    import harness
    import test_portal_sync as tps
    # 自动挑空闲端口（假飞书 + 门户），避免上次残留把演示卡在"地址已占用"
    import socket as _s

    def _free(preferred):
        for cand in range(preferred, preferred + 40):
            sk = _s.socket()
            try:
                sk.bind(("127.0.0.1", cand))
                sk.close()
                return cand
            except OSError:
                sk.close()
        return preferred

    fw_port = _free(8901)
    args.port = _free(args.port)
    harness._feishu_port = fw_port
    fake = harness.start_fake_feishu(tenant_key="tenant-test")
    fake.bitable = tps.fake_data(with_bad=True)

    env = dict(os.environ)
    env.update(cfg)
    # 登录回调主机：127.0.0.1/localhost 与探测到的本机 IP 默认放行，其余靠 --redirect-host。
    # 用户从哪个地址打开页面，就从那个地址回调，state cookie 才带得回来。
    redirect_hosts = ["127.0.0.1", "localhost"]
    for ip in _local_ips() + list(args.redirect_host):
        if ip and ip not in redirect_hosts:
            redirect_hosts.append(ip)
    env.update({
        "WEB_HOST": args.host, "WEB_PORT": str(args.port),
        "FEISHU_API_BASE": "http://127.0.0.1:%d" % fw_port,
        "FEISHU_APP_ID": "cli_demo", "FEISHU_APP_SECRET": "demo-secret",
        "FEISHU_BASE_APP_TOKEN": "demo_base_token",
        "FEISHU_TABLES_JSON": harness.FAKE_TABLES_JSON,
        "FEISHU_ALLOWED_TENANT_KEYS": "tenant-test",
        # 让"飞书登录"按钮在演示里可用（走假飞书授权页，不联外网）
        "FEISHU_REDIRECT_URL": "http://127.0.0.1:%d/auth/feishu/callback" % args.port,
        "FEISHU_REDIRECT_HOSTS": ",".join(redirect_hosts),
        "PORTAL_SYNC_INTERVAL": "60", "PORTAL_MANUAL_MIN_GAP": "1",
        "AUTH_EMERGENCY_ENABLED": "0",
    })
    log = open(os.path.join(DATA_DIR, "demo.log"), "w")
    proc = subprocess.Popen([os.path.join(REPO, ".venv/bin/uvicorn"), "app.main:app",
                             "--host", args.host, "--port", str(args.port)],
                            cwd=REPO, env=env, stdout=log, stderr=log,
                            start_new_session=True)
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    base = "http://127.0.0.1:%d" % args.port
    for _ in range(60):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(base + "/login", timeout=3)
            break
        except Exception:                            # noqa: BLE001
            continue

    # 登录 + 触发一轮同步（示例数据）
    body = json.dumps({"username": USERNAME, "password": PASSWORD}).encode()
    req = urllib.request.Request(base + "/api/auth/local/login", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        token = json.loads(r.read().decode()).get("session")
    trig = urllib.request.Request(base + "/api/portal/sync", data=b"{}",
                                  headers={"Content-Type": "application/json",
                                           "Authorization": "Bearer " + (token or "")},
                                  method="POST")
    try:
        urllib.request.urlopen(trig, timeout=10).read()
    except Exception:                                # noqa: BLE001
        pass
    for _ in range(40):
        time.sleep(1)
        st = urllib.request.Request(base + "/api/portal/sync-status",
                                    headers={"Authorization": "Bearer " + (token or "")})
        try:
            with urllib.request.urlopen(st, timeout=8) as r:
                s = json.loads(r.read().decode())
            if s.get("has_snapshot") and not s.get("syncing"):
                print("示例数据就绪：counts=%s" % s.get("counts"))
                break
        except Exception:                            # noqa: BLE001
            pass

    ips = _local_ips()
    print("\n=== 本地演示已启动（示例数据，不连真实飞书） ===")
    print("  浏览器打开： http://127.0.0.1:%d/" % args.port)
    for ip in ips[:2]:
        print("       或    ： http://%s:%d/" % (ip, args.port))
    print("  登录：用户名 %s / 密码 %s" % (USERNAME, PASSWORD))
    print("  飞书登录可用主机：%s（换地址打开时用 --redirect-host 追加）"
          % ",".join(redirect_hosts))
    print("  数据目录：%s（删除即清理；停止：--stop）" % DATA_DIR)
    print("  服务日志：%s" % os.path.join(DATA_DIR, "demo.log"))
    if args.serve:
        print("  常驻中（Ctrl+C 退出；或用 --stop 停止）")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            _stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
