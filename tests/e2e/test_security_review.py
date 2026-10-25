"""审查修复回归：固件边界、命令参数、来源、登录与缓存。"""
import asyncio
from http.cookiejar import CookieJar
from io import BytesIO
from pathlib import Path
import tempfile
import sys
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import harness
sys.path.insert(0, harness.REPO)

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name, flush=True)


def hop(opener, url, headers=None):
    try:
        return opener.open(urllib.request.Request(url, headers=headers or {}), timeout=10)
    except urllib.error.HTTPError as exc:
        return exc


def run():
    from app import config, firmware, flashing
    from app.portal import writer
    from app.portal.sync import SyncError
    from app.auth.guard import same_origin
    from starlette.requests import Request

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "firmware"
        root.mkdir()
        sibling = Path(td) / "firmware-other"
        sibling.mkdir()
        outside = sibling / "secret.bin"
        outside.write_bytes(b"secret")
        (root / "link.bin").symlink_to(outside)
        with patch.object(config, "FIRMWARE_DIR", root), patch.object(config, "MAX_UPLOAD_BYTES", 8):
            for name in ("../firmware-other/secret.bin", str(outside), "link.bin", "x}.bin", "x\\y.bin", "x\n.bin"):
                try:
                    firmware.save_upload(name, b"bad")
                    rejected = False
                except ValueError:
                    rejected = True
                check("拒绝非法固件路径 " + repr(name), rejected)
            check("外部文件未被修改", outside.read_bytes() == b"secret")
            firmware.save_upload("good.bin", b"old")
            try:
                firmware.save_upload_stream("good.bin", BytesIO(b"123456789"))
            except ValueError:
                pass
            check("超限上传保留原文件并清理临时文件",
                  (root / "good.bin").read_bytes() == b"old" and not list(root.glob(".upload-*")))
            check("上限内上传成功", firmware.save_upload("good.bin", b"12345678")["size"] == 8)
            check("固件列表排除符号链接", all(x["name"] != "link.bin" for x in firmware.list_firmware()))
    check("命令参数中的分号保持字面量", flashing._tcl_arg("host; harmless") == "{host; harmless}")
    for value in ("host} ; harmless", "host\nnext", "host\\"):
        try:
            flashing._tcl_arg(value)
            rejected = False
        except ValueError:
            rejected = True
        check("拒绝命令语法边界 " + repr(value), rejected)

    for origin, allowed in (("http://local:8090", True), ("https://local:8090", False),
                            ("http://other:8090", False), ("null", False)):
        req = Request({"type": "http", "method": "POST", "scheme": "http",
                       "server": ("local", 8090), "path": "/", "query_string": b"",
                       "headers": [(b"host", b"local:8090"), (b"origin", origin.encode())]})
        check("来源检查 " + origin, same_origin(req) == allowed)

    w = object.__new__(writer.FeishuWriter)
    from unittest.mock import Mock
    w._http = Mock()
    w._http._req_json.side_effect = SyncError("network", "模拟上游已收到请求但回包超时")
    try:
        w._call("POST", "/records", {"name": "测试"})
    except SyncError:
        pass
    check("新建超时不盲目重试", w._http._req_json.call_count == 1)

    check("隔离服务启动", harness.start_service(with_member=True))
    if not harness._auth.get("member"):
        return
    no_redirect = urllib.request.build_opener(harness._NoRedirect())
    headers = harness.ws_headers("member")
    for path in ("/", "/api/auth/me", "/api/portal/overview", "/auth/feishu/start"):
        with hop(no_redirect, harness.BASE + path, headers) as response:
            check("敏感响应禁缓存 " + path, response.headers.get("Cache-Control") == "no-store")
    req = urllib.request.Request(harness.BASE + "/api/shoot/estop", data=b"{}",
          headers={**headers, "Content-Type": "application/json", "Origin": "https://untrusted.invalid"})
    try:
        response = no_redirect.open(req)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        check("跨站调试请求被拒绝", response.code == 403)

    jar = CookieJar()
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), harness._NoRedirect())
    with hop(browser, harness.BASE + "/auth/feishu/start?next=//untrusted.invalid") as r:
        authorize = r.headers["Location"]
    with hop(browser, authorize) as r:
        callback = r.headers["Location"]
    with hop(no_redirect, callback) as r:
        check("其他浏览器不能消费登录回调", r.code == 400)
    with hop(browser, callback) as r:
        check("原浏览器仍可完成登录", r.code == 302 and r.headers.get("Location", "").startswith("/?"))
        check("登录跳转不能跳到外站", "untrusted.invalid" not in r.headers.get("Location", ""))
    check("登录完成清理临时授权 Cookie", not any(c.name.startswith("oauth_") for c in jar))

    async def ws_cross_origin():
        import websockets
        async with websockets.connect(harness.BASE.replace("http", "ws") + "/ws/shoot",
                                      origin="https://untrusted.invalid",
                                      additional_headers=headers) as ws:
            try:
                await ws.recv()
            except websockets.exceptions.ConnectionClosed as exc:
                return exc.rcvd.code == 4403
        return False
    check("跨站实时连接被拒绝", asyncio.run(ws_cross_origin()))
    print(f"安全回归：通过 {len(PASS)}，失败 {len(FAIL)}", flush=True)


if __name__ == "__main__":
    try:
        run()
    finally:
        harness.stop_service()
    raise SystemExit(bool(FAIL))
