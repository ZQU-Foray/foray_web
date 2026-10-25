# -*- coding: utf-8 -*-
"""HTTPS 开关（阶段 9）：在服务器上一条命令启用/停用 TLS（自签内网 CA）。

前提：端口转发已把 **443 → 130:443** 打开（实测未开时 8443/443 均被拒）。
本脚本只改 `/opt/portal/.env`、安装/启停跳转单元并重启服务，**不动 systemd 之外的系统配置**。

用法（在 /opt/portal 下，用项目 venv，需 sudo 权限）：
    sudo .venv/bin/python scripts/portal_tls.py status
    sudo .venv/bin/python scripts/portal_tls.py enable     # 签发证书 + 门户转 443 + 80 跳转
    sudo .venv/bin/python scripts/portal_tls.py disable    # 回退到 80 明文（临时排障用）
"""
import argparse
import os
import shutil
import subprocess
import sys

APP_DIR = os.environ.get("APP_DIR", "/opt/portal")
ENV_FILE = os.path.join(APP_DIR, ".env")
TLS_DIR = os.path.join(APP_DIR, "tls")
UNIT_SRC = os.path.join(APP_DIR, "deploy", "portal-redirect.service")
UNIT_DST = "/etc/systemd/system/portal-redirect.service"
# 门户域名与服务器内网 IP 由环境变量提供（仓库公开，不写死真实地址）；签证书时缺失会报错
HOST = os.environ.get("PORTAL_HOST", "")
IP = os.environ.get("PORTAL_IP", "")


def sh(cmd, check=True, quiet=False):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True)
    if not quiet:
        print("$ %s" % (cmd if isinstance(cmd, str) else " ".join(cmd)))
        if r.stdout.strip():
            print(r.stdout.rstrip()[:1200])
        if r.stderr.strip():
            print("[stderr]", r.stderr.rstrip()[:400])
    if check and r.returncode != 0:
        sys.exit("命令失败（exit=%d）" % r.returncode)
    return r


def read_env():
    kv = {}
    if os.path.isfile(ENV_FILE):
        for ln in open(ENV_FILE, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                kv[k.strip()] = v.strip()
    return kv


def write_env(kv):
    order = ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_REDIRECT_URL",
             "FEISHU_ALLOWED_TENANT_KEYS", "WEB_HOST", "WEB_PORT", "TLS_ARGS"]
    lines = ["# 门户生产环境（portal_tls.py 维护 TLS 相关两行）"]
    for k in order:
        if k in kv:
            lines.append("%s=%s" % (k, kv[k]))
    for k, v in kv.items():
        if k not in order:
            lines.append("%s=%s" % (k, v))
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_FILE)


def status():
    kv = read_env()
    print("WEB_PORT   = %s" % kv.get("WEB_PORT", "(默认 8000)"))
    print("TLS_ARGS   = %s" % kv.get("TLS_ARGS", "(未设置)"))
    print("证书目录   = %s（存在=%s）" % (TLS_DIR, os.path.isdir(TLS_DIR)))
    sh("systemctl is-active portal.service portal-redirect.service", check=False)
    sh("ss -ltn | grep -E ':(80|443) ' || echo '无 80/443 监听'", check=False)
    if os.path.isfile(os.path.join(TLS_DIR, "server.crt")):
        sys.path.insert(0, APP_DIR)
        from scripts.tls_issue import fingerprint  # type: ignore
        print(fingerprint(os.path.join(TLS_DIR, "server.crt")))
    return 0


def enable(port=443):
    """启用 TLS。

    port=443（标准）：门户跑 443，80 交给 portal-redirect 做 301。
    port=80（过渡）：**只在 443 尚未转发时**使用——门户直接以 TLS 跑在 80，
    此时 http:// 链接会失效（浏览器先试明文），且无法做 301。
    """
    cert = os.path.join(TLS_DIR, "server.crt")
    key = os.path.join(TLS_DIR, "server.key")
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        if not (HOST and IP):
            sys.exit("首次签发证书需要设置 PORTAL_HOST（门户域名）与 PORTAL_IP（服务器内网 IP）")
        sh([sys.executable, os.path.join(APP_DIR, "scripts", "tls_issue.py"),
            "--dir", TLS_DIR, "--host", HOST, "--ip", IP, "--ip", "127.0.0.1"])
    kv = read_env()
    kv["WEB_PORT"] = str(port)
    kv["TLS_ARGS"] = "--ssl-certfile %s --ssl-keyfile %s" % (cert, key)
    kv.setdefault("WEB_HOST", "0.0.0.0")
    write_env(kv)
    print("已写 %s：WEB_PORT=%d + TLS_ARGS" % (ENV_FILE, port))
    if port == 443:
        if os.path.isfile(UNIT_SRC):
            shutil.copy2(UNIT_SRC, UNIT_DST)
            sh("systemctl daemon-reload")
            sh("systemctl enable --now portal-redirect.service")
        else:
            print("警告：缺少 %s，80 端口不会跳转" % UNIT_SRC)
    else:
        # 过渡模式：80 被门户自己占用，跳转服务必须停掉，否则抢端口
        sh("systemctl disable --now portal-redirect.service", check=False)
        print("过渡模式：门户直接占用 %d（无 80→443 跳转）" % port)
    sh("systemctl restart portal.service")
    sh("sleep 3; systemctl is-active portal.service", check=False)
    ca = os.path.join(TLS_DIR, "ca.crt")
    sh([os.path.join(APP_DIR, ".venv/bin/python"), "-c",
        "import ssl,urllib.request as u;"
        "ctx=ssl.create_default_context(cafile='%s');"
        "print('https /login ->', u.urlopen('https://127.0.0.1:%d/login',timeout=8,context=ctx).status)"
        % (ca, port)], check=False)
    if port == 443:
        sh([os.path.join(APP_DIR, ".venv/bin/python"), "-c",
            "import urllib.request as u,urllib.error as e;"
            "\ntry:\n u.urlopen('http://127.0.0.1/',timeout=8)\nexcept e.HTTPError as x:\n"
            " print('http / ->', x.code, x.headers.get('Location'))"], check=False)
    print("\n下一步：把 %s 安装到成员设备（信任一次）；" % ca)
    print("并把飞书回调登记追加 https://%s/auth/feishu/callback（portal_tls 不改飞书侧）。" % HOST)
    return 0


def disable():
    kv = read_env()
    kv["WEB_PORT"] = "80"
    kv.pop("TLS_ARGS", None)
    write_env(kv)
    sh("systemctl disable --now portal-redirect.service", check=False)
    sh("systemctl restart portal.service")
    sh("sleep 3; systemctl is-active portal.service", check=False)
    print("已回退到 80 端口明文（证书与单元文件保留，随时可再次 enable）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="门户 HTTPS 开关（阶段 9）")
    ap.add_argument("action", choices=("status", "enable", "disable"))
    ap.add_argument("--port", type=int, default=443,
                    help="TLS 监听端口：443（标准，80 做 301）或 80（过渡：443 未转发时）")
    args = ap.parse_args()
    if args.action == "enable":
        return enable(args.port)
    return {"status": status, "disable": disable}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
