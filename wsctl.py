#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebServer 命令行控制工具（HTTP API 客户端，纯标准库、无第三方依赖）。

其它程序 / agent 无需 SSH 登录服务器，在本机即可远程：
  - status / firmware:     查看状态与固件
  - firmware upload / rm:  上传、删除固件
  - flash:                 触发烧录并实时打印日志，退出码 0=成功 / 1=失败 / 3=超时
  - serial open/close/send/read: 无线串口开、关、发、读（读数据走 stdout，系统消息走 stderr）
  - presets get/set:       预设指令

服务器地址：环境变量 WS_SERVER，或 --server。
默认 http://0.0.0.0（仅指向本机）：在其它机器上运行时必须用 WS_SERVER/--server
指定服务器，例如 http://192.168.1.113

示例:
  python wsctl.py status
  python wsctl.py firmware list
  python wsctl.py firmware upload ./mc02.elf
  python wsctl.py flash mc02.elf --target stm32h723vg --probe jlink
  python wsctl.py serial open && python wsctl.py serial send --text start --nl crlf
  python wsctl.py serial read --follow --timeout 10
"""
import argparse
import base64
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 默认 0.0.0.0 = 本机（在服务器本机跑 agent 时适用）；远程机器请设置 WS_SERVER
DEFAULT_SERVER = os.environ.get("WS_SERVER", "http://0.0.0.0")

# 输出统一 UTF-8，避免 Windows 管道/重定向下出现乱码（agent 解析更稳）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class ApiError(Exception):
    pass


def session_file(server):
    """受保护会话文件路径（按服务器地址区分；目录 0700 / 文件 0600）。"""
    import hashlib
    from pathlib import Path
    key = hashlib.sha256(server.encode()).hexdigest()[:12]
    d = Path.home() / ".config" / "wsctl"
    return d / f"session-{key}.json"


class Client:
    def __init__(self, server, device=None, cafile=""):
        self.server = server.rstrip("/")
        self.device = device or "legacy-default"
        self.cafile = cafile or os.environ.get("WSCTL_CAFILE", "")
        self._token = None
        self._load_session()

    # ---------- 会话文件 ----------

    def _load_session(self):
        try:
            with open(session_file(self.server), encoding="utf-8") as fh:
                doc = json.load(fh)
            if doc.get("server") == self.server and doc.get("session"):
                self._token = doc["session"]
        except Exception:
            self._token = None

    def save_session(self, doc):
        from pathlib import Path
        f = session_file(self.server)
        f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        f.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(f, 0o600)
        self._token = doc.get("session")

    def clear_session(self):
        try:
            os.unlink(session_file(self.server))
        except FileNotFoundError:
            pass
        self._token = None

    def api_path(self, sub):
        """按设备生成业务路径（与前端 deviceCtx 同规则：legacy-default 走旧端点）。"""
        if self.device == "legacy-default":
            return "/api/" + sub
        return "/api/v2/devices/" + urllib.parse.quote(self.device, safe="") + "/" + sub

    def ws_path(self, sub):
        if self.device == "legacy-default":
            return "/ws/" + sub
        return "/ws/v2/devices/" + urllib.parse.quote(self.device, safe="") + "/" + sub

    # ---------- 请求 ----------

    def request(self, method, path, body=None, headers=None, timeout=15,
                auth=True):
        url = self.server + path
        data = None
        hdrs = dict(headers or {})
        # HTTPS（阶段 9）：自签 CA 用 --cafile / WSCTL_CAFILE 校验
        ctx = None
        if url.startswith("https"):
            import ssl as _ssl
            ctx = (_ssl.create_default_context(cafile=self.cafile)
                   if getattr(self, "cafile", "") else _ssl.create_default_context())
        if auth and self._token:
            hdrs.setdefault("Authorization", "Bearer " + self._token)
        if body is not None and not isinstance(body, bytes):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        else:
            data = body
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8", "replace")).get("detail", "")
            except Exception:
                pass
            if e.code == 401 and auth:
                raise ApiError("HTTP 401: 未认证或会话已失效 —— 请先 `wsctl.py login`"
                               "（密码交互输入，不作命令行参数）")
            raise ApiError(f"HTTP {e.code}: {detail or e.reason}")
        except urllib.error.URLError as e:
            raise ApiError(
                f"无法连接服务器 {self.server}: {e.reason}"
                "（默认 0.0.0.0 仅指向本机；请用 --server <url> 或环境变量 WS_SERVER 指定真实服务器地址）")
        if not raw:
            return {}
        return json.loads(raw)


def die(msg, code=2):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------- 子命令 ----------------

def _post_login(cli, path, body):
    """登录请求：返回 (json, set_cookie_token)。"""
    req = urllib.request.Request(
        cli.server + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            doc = json.loads(r.read().decode())
            tok = None
            for k, v in r.headers.items():
                if k.lower() == "set-cookie" and "dsh_session=" in v:
                    tok = v.split("dsh_session=", 1)[1].split(";", 1)[0]
            return doc, tok
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("detail", "")
        except Exception:
            detail = e.reason
        raise ApiError(f"登录失败 HTTP {e.code}: {detail}")


def cmd_login(cli, args):
    """登录：本站账号（D2，推荐）/ 飞书 / 应急（已停用，兼容旧脚本）。"""
    if args.method in ("local", "emergency"):
        if args.method == "local":
            username = args.username or input("用户名: ").strip()
            password = os.environ.get("WSCTL_PASSWORD") or getpass.getpass("密码: ")
            r, _ = _post_login(cli, "/api/auth/local/login",
                               {"username": username, "password": password})
            if not r.get("session"):
                die("登录失败：响应无会话令牌（用户名或密码错误？）")
            cli.save_session({"server": cli.server, "session": r["session"],
                              "user": r.get("user"), "role": r.get("role"),
                              "expires": r.get("expires"),
                              "auth_method": r.get("auth_method", "local")})
            print(f"已登录: {r.get('user')}（角色 {r.get('role')}，本站账号）  "
                  f"会话文件: {session_file(cli.server)}")
            return
    if args.method == "emergency":
        username = args.username or input("用户名: ").strip()
        password = os.environ.get("WSCTL_PASSWORD") or getpass.getpass("密码: ")
        r, tok = _post_login(cli, "/api/auth/emergency/login",
                             {"username": username, "password": password})
        if not tok:
            die("登录响应缺少会话 Cookie")
        cli.save_session({"server": cli.server, "session": tok,
                          "user": r.get("user"), "role": r.get("role"),
                          "expires": r.get("expires")})
        print(f"已登录: {r.get('user')}（角色 {r.get('role')}）  "
              f"会话文件: {session_file(cli.server)}")
        return
    # feishu：先打印授权 URL（用户浏览器完成授权后从回调 URL 复制 code 与 state）
    if not (args.code and args.state):
        class _NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(_NoRedir).open(
                cli.server + "/auth/feishu/start", timeout=10)
            print("授权地址不可用（未配置飞书？）"); sys.exit(1)
        except urllib.error.HTTPError as e:
            print("1) 在浏览器打开并完成授权：\n   " + e.headers.get("Location", ""))
            print("2) 授权后浏览器跳转到回调地址（含 code 与 state 参数），复制两者后执行：")
            print("   wsctl.py login --method feishu --code <code> --state <state>")
            return
    r, _ = _post_login(cli, "/api/auth/exchange",
                       {"code": args.code, "state": args.state})
    if not r.get("session"):
        die("交换失败：响应无会话令牌")
    cli.save_session({"server": cli.server, "session": r["session"],
                      "user": r.get("user"), "role": r.get("role"),
                      "expires": r.get("expires")})
    print(f"已登录: {r.get('user')}（角色 {r.get('role')}）")


def cmd_logout(cli, args):
    try:
        cli.request("POST", "/api/auth/logout", {})
    except ApiError:
        pass
    cli.clear_session()
    print("已退出并删除会话文件")


def cmd_status(cli, args):
    s = cli.request("GET", cli.api_path("status"))
    if getattr(args, "json", False):
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return
    print(f"服务: {cli.server} 在线")
    probes = s["devices"]["probes"]
    if probes:
        for p in probes:
            print(f"调试器: [{p['label']}] VID:PID={p['vid']}:{p['pid']} SN={p.get('serial') or '-'}")
    else:
        print("调试器: 未检测到")
    ports = s["devices"]["ports"]
    for p in ports:
        print(f"串口:   {p['port']} ({p['label']})")
    if not ports:
        print("串口:   未检测到")
    f = s["flash"]
    st = {"idle": "空闲", "running": "烧录中", "success": "成功", "failed": "失败"}.get(f["state"], f["state"])
    extra = ""
    if f["state"] != "idle":
        extra = f" exit={f['exit_code']} 用时{f['elapsed']}s 固件={f.get('firmware')}"
    print(f"烧录:   {st}{extra}")
    ser = s["serial"]
    print(f"串口桥: {'已打开 ' + str(ser['port']) + ' @ ' + str(ser['baudrate']) if ser['open'] else '未打开'}")
    print(f"目标:   " + ", ".join(f"{t['key']}={t['label']}" for t in s["targets"]))


def cmd_flash(cli, args):
    try:
        r = cli.request("POST", cli.api_path("flash"),
                        {"firmware": args.firmware, "target": args.target, "probe": args.probe})
    except ApiError as e:
        die(str(e))
    job = r.get("job")
    print(f"烧录任务已启动: job={job} target={args.target} probe={args.probe}", file=sys.stderr)
    if args.no_wait:
        return
    since = 0
    start = time.time()
    deadline = start + args.timeout
    state = "running"
    while True:
        if time.time() > deadline:
            print(f"烧录超时（>{args.timeout:.0f}s）", file=sys.stderr)
            sys.exit(3)
        try:
            lg = cli.request("GET", cli.api_path("flash") + f"/log?since={since}", timeout=10)
        except ApiError as e:
            print(f"查询日志失败: {e}", file=sys.stderr)
            time.sleep(1)
            continue
        since = lg.get("total", since)
        for l in lg.get("lines", []):
            if l["s"] == "sys":
                print(f"[sys] {l['line']}")
            else:
                print(l["line"])
        state = lg.get("state")
        if state != "running":
            break
        time.sleep(0.8)
    ec = lg.get("exit_code")
    print(f"=== 结果: {state} (exit={ec}, 用时 {lg.get('elapsed')}s) ===", file=sys.stderr)
    sys.exit(0 if state == "success" else 1)


def cmd_firmware(cli, args):
    if args.action == "list":
        r = cli.request("GET", "/api/firmware")
        if not r.get("items"):
            print("(无固件)")
            return
        for it in r["items"]:
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(it["mtime"]))
            print(f"{it['name']:<24} {it['size']/1024:>8.1f} KB  {mtime}")
        return
    if args.action == "upload":
        if not args.file:
            die("upload 需要文件路径")
        path = args.file
        if not os.path.isfile(path):
            die(f"文件不存在: {path}")
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            content = fh.read()
        boundary = "----wsctl" + str(int(time.time() * 1000))
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        try:
            r = cli.request("POST", "/api/firmware/upload", body=body,
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        except ApiError as e:
            die(str(e))
        print(f"已上传: {r['name']} ({r['size']/1024:.1f} KB)")
        return
    if args.action == "rm":
        if not args.file:
            die("rm 需要固件名")
        try:
            cli.request("DELETE", "/api/firmware/" + urllib.request.quote(args.file))
        except ApiError as e:
            die(str(e))
        print(f"已删除: {args.file}")


def _serial_data(args):
    if args.b64:
        return base64.b64decode(args.b64)
    if args.hex:
        return bytes.fromhex(args.hex.replace(" ", ""))
    if args.text is not None:
        nl = {"crlf": "\r\n", "lf": "\n", "none": ""}[args.nl]
        return (args.text + nl).encode("utf-8")
    die("send 需要 --text / --hex / --b64 之一")


def _print_event(ev):
    if ev["type"] == "rx":
        try:
            sys.stdout.write(base64.b64decode(ev["b64"]).decode("utf-8", "replace"))
        except Exception:
            pass
        sys.stdout.flush()
    elif ev["type"] == "tx":
        print(f"[tx] {base64.b64decode(ev['b64']).decode('utf-8', 'replace')}", file=sys.stderr)
    else:
        print(f"[sys] {ev.get('data', '')}", file=sys.stderr)


def cmd_serial(cli, args):
    if args.action == "open":
        try:
            r = cli.request("POST", cli.api_path("serial/open"),
                            {"port": args.port or "auto", "baudrate": args.baud})
        except ApiError as e:
            die(str(e))
        st = r["status"]
        print(f"串口已打开: {st['port']} @ {st['baudrate']}")
        return
    if args.action == "close":
        cli.request("POST", cli.api_path("serial/close"), {})
        print("串口已关闭")
        return
    if args.action == "send":
        data = _serial_data(args)
        try:
            r = cli.request("POST", cli.api_path("serial/send"), {"b64": base64.b64encode(data).decode()})
        except ApiError as e:
            die(str(e))
        if not r.get("ok"):
            die("发送失败（串口未打开？）")
        print(f"已发送 {len(data)} 字节", file=sys.stderr)
        return
    if args.action == "read":
        # 一次性读取：最近 60 秒事件；--follow：从当前时刻起持续读取
        since = 0 if not args.follow else time.time()
        deadline = time.time() + args.timeout if args.timeout else None
        try:
            while True:
                r = cli.request("GET", cli.api_path("serial/history") + f"?since={since}", timeout=10)
                if r.get("events"):
                    since = r["events"][-1]["ts"]
                    for ev in r["events"]:
                        _print_event(ev)
                if not args.follow:
                    break
                if deadline and time.time() > deadline:
                    break
                time.sleep(0.4)
        except KeyboardInterrupt:
            pass


def cmd_presets(cli, args):
    if args.action == "get":
        p = cli.request("GET", cli.api_path("presets"))
        if args.file:
            with open(args.file, "w", encoding="utf-8") as fh:
                json.dump(p, fh, ensure_ascii=False, indent=2)
            print(f"已保存到 {args.file}")
        else:
            print(json.dumps(p, ensure_ascii=False, indent=2))
        return
    if args.action == "set":
        if not args.file:
            die("set 需要 JSON 文件路径")
        with open(args.file, encoding="utf-8") as fh:
            p = json.load(fh)
        cli.request("PUT", cli.api_path("presets"), p)
        print("预设已更新")


def main():
    ap = argparse.ArgumentParser(prog="wsctl.py", description="STM32 WebServer 命令行控制工具")
    ap.add_argument("--cafile", default=os.environ.get("WSCTL_CAFILE", ""),
                    help="自签 CA 证书路径（HTTPS 门户；等价 WSCTL_CAFILE）")
    ap.add_argument("--server", default=DEFAULT_SERVER,
                    help=f"服务器地址（默认 $WS_SERVER 或 {DEFAULT_SERVER}）")
    ap.add_argument("--device", default="legacy-default",
                    help="目标设备 ID（默认 legacy-default 走旧端点；其他设备走 /api/v2）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p0 = sub.add_parser("login", help="登录并保存受保护会话文件（~/.config/wsctl/）")
    p0.add_argument("--method", choices=["local", "emergency", "feishu"],
                    default="local",
                    help="local=本站账号（D2，推荐）；feishu=浏览器授权；"
                         "emergency=已停用（默认关闭）")
    p0.add_argument("--username", help="local/emergency: 用户名（缺省交互输入）")
    p0.add_argument("--code", help="feishu: 浏览器完成授权后回调 URL 中的 code")
    p0.add_argument("--state", help="feishu: 回调 URL 中的 state（login 打印的 URL 即包含）")

    p0b = sub.add_parser("logout", help="退出登录并删除会话文件")

    p1 = sub.add_parser("status", help="查看设备/服务状态")
    p1.add_argument("--json", action="store_true", help="输出原始 JSON")

    p2 = sub.add_parser("flash", help="触发烧录并等待结果")
    p2.add_argument("firmware", help="固件名（服务器 firmware/ 目录中的文件）")
    p2.add_argument("--target", default="stm32h723vg", help="目标芯片 key（默认 stm32h723vg）")
    p2.add_argument("--probe", default="auto", help="调试器 key（默认 auto）")
    p2.add_argument("--no-wait", action="store_true", help="启动后立即返回")
    p2.add_argument("--timeout", type=float, default=300, help="等待超时秒数（默认 300）")

    p3 = sub.add_parser("firmware", help="固件管理")
    p3.add_argument("action", choices=["list", "upload", "rm"])
    p3.add_argument("file", nargs="?", help="upload: 本地文件; rm: 固件名")

    p4 = sub.add_parser("serial", help="无线串口控制")
    p4.add_argument("action", choices=["open", "close", "send", "read"])
    p4.add_argument("--port", help="串口设备（默认 auto）")
    p4.add_argument("--baud", type=int, help="波特率（默认取预设）")
    p4.add_argument("--text", help="发送文本")
    p4.add_argument("--hex", help="发送十六进制字节（如 'AA 01 64'）")
    p4.add_argument("--b64", help="发送 base64 字节")
    p4.add_argument("--nl", choices=["crlf", "lf", "none"], default="none", help="文本结尾换行（默认 none）")
    p4.add_argument("--follow", action="store_true", help="read: 持续读取")
    p4.add_argument("--timeout", type=float, default=0, help="read: 超时秒数（0=不限）")

    p5 = sub.add_parser("presets", help="预设指令读写")
    p5.add_argument("action", choices=["get", "set"])
    p5.add_argument("file", nargs="?", help="get: 保存到文件; set: 从文件读取")

    args = ap.parse_args()
    cli = Client(args.server, device=getattr(args, "device", None),
                 cafile=getattr(args, "cafile", ""))
    try:
        handlers = {"status": cmd_status, "flash": cmd_flash, "firmware": cmd_firmware,
                    "serial": cmd_serial, "presets": cmd_presets,
                    "login": cmd_login, "logout": cmd_logout}
        handlers[args.cmd](cli, args)
    except ApiError as e:
        die(str(e))


if __name__ == "__main__":
    main()
