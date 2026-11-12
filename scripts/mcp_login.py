#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备码登录：用飞书身份自助换发一个本站 agent 令牌（MCP 只读）。

流程（RFC 8628 风格，实现在 app/mcp/device_auth.py）：

1. 本脚本向门户申请设备码，打印一个用户码（形如 `ABCD-EFGH`）和一个网址；
2. 你在浏览器里打开那个网址，用飞书登录并点「批准」；
3. 脚本轮询到批准后一次性拿到令牌，写到本机 600 权限的文件里。

令牌可以随时在门户里撤销（维护者创建/撤销见 docs/portal-mcp.md）。
本脚本只用标准库，不依赖第三方包，也不读写门户的任何配置文件。

用法：

    python scripts/mcp_login.py --base-url http://127.0.0.1:8080
    python scripts/mcp_login.py --client-name cursor --out ~/.config/foray-mcp/token.json

拿到令牌后，客户端配置里把它当 Bearer 用：

    {"mcpServers": {"foray-portal": {
        "url": "http://127.0.0.1:8080/mcp",
        "headers": {"Authorization": "Bearer <令牌>"}}}}
"""
import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_OUT = "~/.config/foray-mcp/token.json"
DEFAULT_INTERVAL = 5.0


def _post(url: str, payload: dict, timeout: float = 15.0) -> tuple:
    """POST JSON，返回 (status, body_dict)；4xx 也正常返回，方便读错误码。"""
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 地址：%s" % url)
    data = json.dumps(payload).encode("utf-8")
    # 协议已在上方校验为 http/https，所以下面两处 urlopen/Request 放行 S310
    req = urllib.request.Request(url, data=data, method="POST",  # noqa: S310
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"error": "http_%d" % exc.code, "raw": raw}


def _save(path: pathlib.Path, doc: dict) -> None:
    """原子写并收紧权限：令牌是凭据，不能给别人读。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用飞书身份换发门户 MCP 的 agent 令牌")
    ap.add_argument("--base-url", default=os.environ.get("FORAY_BASE_URL", "http://127.0.0.1:8080"),
                    help="门户地址，默认读 FORAY_BASE_URL 或 http://127.0.0.1:8080")
    ap.add_argument("--client-name", default="foray-mcp-cli",
                    help="客户端名字（会显示在确认页与门户令牌列表里）")
    ap.add_argument("--out", default=os.environ.get("FORAY_MCP_TOKEN_FILE", DEFAULT_OUT),
                    help="令牌保存路径，默认 %s" % DEFAULT_OUT)
    ap.add_argument("--no-save", action="store_true", help="只打印，不落盘")
    ap.add_argument("--timeout", type=float, default=600.0, help="等待批准的最长秒数")
    args = ap.parse_args(argv)

    base = args.base_url.rstrip("/")
    status, start = _post(base + "/oauth/device/authorize",
                          {"client_name": args.client_name})
    if status != 200 or not start.get("device_code"):
        print("申请设备码失败：%s" % json.dumps(start, ensure_ascii=False), file=sys.stderr)
        return 2

    user_code = start.get("user_code", "")
    expires_in = float(start.get("expires_in") or 600)
    interval = float(start.get("interval") or DEFAULT_INTERVAL)
    confirm_url = start.get("verification_uri_complete") or start.get("verification_uri") or ""
    print("用户码：%s" % user_code)
    print("确认地址：%s" % confirm_url)
    print("（用飞书登录后点「批准」；%d 分钟内有效）" % int(expires_in))
    sys.stdout.flush()

    deadline = time.time() + min(args.timeout, expires_in)
    while time.time() < deadline:
        time.sleep(interval)
        status, body = _post(base + "/oauth/device/token",
                             {"device_code": start["device_code"]})
        err = body.get("error")
        if status == 200 and body.get("access_token"):
            return _finish(args, base, body)
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = float(body.get("interval") or interval) + 5.0
            continue
        if err in ("access_denied", "expired_token"):
            print("授权未完成：%s（%s）" % (err, body.get("error_description") or ""),
                  file=sys.stderr)
            return 3
        print("轮询失败：HTTP %d %s" % (status, json.dumps(body, ensure_ascii=False)),
              file=sys.stderr)
        return 2
    print("等待超时：没有在有效期内完成批准，请重新运行本脚本", file=sys.stderr)
    return 3


def _finish(args, base: str, body: dict) -> int:
    token = body["access_token"]
    doc = {
        "access_token": token,
        "token_type": body.get("token_type") or "Bearer",
        "scope": body.get("scope") or "",
        "agent_id": body.get("agent_id") or "",
        "open_id": body.get("open_id") or "",
        "client_name": body.get("client_name") or "",
        "mcp_url": base + "/mcp",
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    print("已拿到令牌：agent_id=%s scope=%s" % (doc["agent_id"], doc["scope"]))
    if args.no_save:
        print("\n令牌（只显示这一次，请自行保管）：\n%s" % token)
        return 0
    path = pathlib.Path(os.path.expanduser(args.out))
    _save(path, doc)
    print("已保存到 %s（权限 600）" % path)
    print("\n客户端配置（把令牌粘进 headers 即可）：")
    print(json.dumps({"mcpServers": {"foray-portal": {
        "url": doc["mcp_url"],
        "headers": {"Authorization": "Bearer <令牌，见上面文件>"}}}},
        ensure_ascii=False, indent=2))
    print("\nClaude Code 一行版（令牌从上面那个文件读，不写进 shell 历史）：")
    print('export FORAY_TOKEN=$(python3 -c \'import json,pathlib,sys;'
          'print(json.loads(pathlib.Path(sys.argv[1]).read_text())["access_token"])\' %s)' % path)
    print('claude mcp add --transport http foray-portal %s '
          '--header "Authorization: Bearer $FORAY_TOKEN"' % doc["mcp_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
