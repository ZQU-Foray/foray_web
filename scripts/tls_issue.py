# -*- coding: utf-8 -*-
"""本地 CA 与服务器证书签发（阶段 9，HTTPS；不依赖外部 CA / 公网）。

场景：门户域名为内网地址（`portal.example.com` → 10.0.0.1），公网无法回连，
Let's Encrypt HTTP-01 不可用；维护者选定**自签内网 CA**方案。

产物（默认 `/opt/portal/tls`，目录 700）：
    ca.key    CA 私钥（600，绝不外发）
    ca.crt    CA 证书（分发给成员设备安装，或给 CLI/健康检查当 --cafile）
    server.key 服务器私钥（600）
    server.crt 服务器证书（SAN 含域名与 IP）
    server.csr / ca.srl  中间产物（可删）

用法：
    python scripts/tls_issue.py --dir /opt/portal/tls \
        --host portal.example.com --ip 192.168.1.130 --days 730
    python scripts/tls_issue.py --dir /opt/portal/tls --renew      # 证书到期前重签
    python scripts/tls_issue.py --dir /opt/portal/tls --print-fingerprint

续期：`--renew` 复用已有 CA，只重签服务器证书（成员无需重新安装 CA）。
"""
import argparse
import os
import subprocess
import sys
import tempfile


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.exit("命令失败: %s\n%s" % (" ".join(cmd), (r.stderr or r.stdout)[-400:]))
    return r.stdout


def issue(dirpath, hosts, ips, days, renew, ca_days):
    os.makedirs(dirpath, exist_ok=True)
    # 目录 755 + 证书 644（ca.crt 要分发给成员设备）；私钥单独 600
    os.chmod(dirpath, 0o755)
    ca_key = os.path.join(dirpath, "ca.key")
    ca_crt = os.path.join(dirpath, "ca.crt")
    srv_key = os.path.join(dirpath, "server.key")
    srv_csr = os.path.join(dirpath, "server.csr")
    srv_crt = os.path.join(dirpath, "server.crt")

    if not os.path.exists(ca_crt) or not os.path.exists(ca_key):
        print("== 生成本地 CA（%d 天） ==" % ca_days)
        run(["openssl", "req", "-x509", "-newkey", "rsa:4096", "-sha256",
             "-days", str(ca_days), "-nodes", "-keyout", ca_key, "-out", ca_crt,
             "-subj", "/O=Foray Team/CN=Foray Portal Local CA",
             "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign"])
        os.chmod(ca_key, 0o600)
        os.chmod(ca_crt, 0o644)
    else:
        print("== 复用已有 CA：%s ==" % ca_crt)

    san = ",".join(["DNS:" + h for h in hosts] + ["IP:" + i for i in ips])
    ext = os.path.join(dirpath, "san.cnf")
    with open(ext, "w", encoding="utf-8") as f:
        f.write("[v3_req]\nsubjectAltName=%s\n"
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n" % san)

    if os.path.exists(srv_crt) and not renew:
        print("服务器证书已存在（如需重签请加 --renew）：%s" % srv_crt)
    else:
        print("== 签发服务器证书（%d 天，SAN=%s） ==" % (days, san))
        if os.path.exists(srv_key):
            os.remove(srv_csr) if os.path.exists(srv_csr) else None
        run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-sha256", "-nodes",
             "-keyout", srv_key, "-out", srv_csr,
             "-subj", "/O=Foray Team/CN=%s" % hosts[0]])
        with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as _:
            pass
        run(["openssl", "x509", "-req", "-in", srv_csr, "-CA", ca_crt,
             "-CAkey", ca_key, "-CAcreateserial", "-out", srv_crt,
             "-days", str(days), "-sha256", "-extfile", ext, "-extensions", "v3_req"])
        os.chmod(srv_key, 0o600)
        os.chmod(srv_crt, 0o644)

    run(["openssl", "verify", "-CAfile", ca_crt, srv_crt])
    print("校验通过：server.crt 由本地 CA 签发，SAN 覆盖 %s" % san)
    return ca_crt, srv_crt


def fingerprint(path):
    out = run(["openssl", "x509", "-in", path, "-noout", "-fingerprint", "-sha256",
               "-subject", "-dates"])
    return out.strip()


def main():
    ap = argparse.ArgumentParser(description="本地 CA 与服务器证书签发（阶段 9）")
    ap.add_argument("--dir", default="/opt/portal/tls")
    ap.add_argument("--host", action="append", default=[],
                    help="SAN 域名（可多次；签发时必填）")
    ap.add_argument("--ip", action="append", default=[],
                    help="SAN IP（可多次；默认只含 127.0.0.1）")
    ap.add_argument("--days", type=int, default=730, help="服务器证书有效期（天）")
    ap.add_argument("--ca-days", type=int, default=3650, help="CA 有效期（天）")
    ap.add_argument("--renew", action="store_true", help="用现有 CA 重签服务器证书")
    ap.add_argument("--print-fingerprint", action="store_true")
    args = ap.parse_args()

    hosts = args.host
    ips = args.ip or ["127.0.0.1"]
    if args.print_fingerprint:
        for name in ("ca.crt", "server.crt"):
            p = os.path.join(args.dir, name)
            if os.path.exists(p):
                print("== %s ==\n%s" % (name, fingerprint(p)))
        return 0
    if not hosts:
        ap.error("请用 --host 指定证书域名")
    ca, crt = issue(args.dir, hosts, ips, args.days, args.renew, args.ca_days)
    print("\nCA 证书（分发/信任用）: %s" % ca)
    print("服务器证书            : %s" % crt)
    print("\n" + fingerprint(ca))
    print(fingerprint(crt))
    print("\n下一步：把 ca.crt 安装到成员设备（或交给 CLI/健康检查当 --cafile）；"
          "私钥 ca.key/server.key 权限 600，绝不外发。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
