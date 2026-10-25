# -*- coding: utf-8 -*-
"""80 → 443 跳转（阶段 9 HTTPS）：门户本体跑在 443（TLS），80 只做 301。

用法（由 deploy/portal-redirect.service 调用）：
    python scripts/tls_redirect.py --listen-port 80 --target-host portal.example.com --target-port 443

只做 301，不提供任何内容：不要在这里挂应用（避免出现"两个门户"）。
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    ap = argparse.ArgumentParser(description="HTTP → HTTPS 301 跳转")
    ap.add_argument("--listen-host", default="0.0.0.0")
    ap.add_argument("--listen-port", type=int, default=80)
    ap.add_argument("--target-host", default="")
    ap.add_argument("--target-port", type=int, default=443)
    args = ap.parse_args()

    class H(BaseHTTPRequestHandler):
        server_version = "portal-tls-redirect"

        def log_message(self, *a):        # 静默：跳转本身无需日志噪声
            pass

        def _redirect(self):
            host = args.target_host or (self.headers.get("Host") or "").split(":")[0]
            port = "" if args.target_port == 443 else ":%d" % args.target_port
            url = "https://%s%s%s" % (host, port, self.path)
            self.send_response(301)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = _redirect

    srv = ThreadingHTTPServer((args.listen_host, args.listen_port), H)
    print("HTTP→HTTPS 跳转已启动：%s:%d → https://%s:%d"
          % (args.listen_host, args.listen_port,
             args.target_host or "(按 Host)", args.target_port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
