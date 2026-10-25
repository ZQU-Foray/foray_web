"""单STA回归：真实HTTP路由、模拟TCP固件和独立设备预设。"""
import asyncio
import json
import socket
import threading
import time

import websockets

from harness import BASE, api
import harness
from test_transports import FakeFirmwareServer

PASS, FAIL = [], []


class CountingListener:
    """最小 TCP 监听：统计连接次数，并保持连接（用于证明探测没有去连 UART 端口）。

    保持连接是有意的：串口会话的 is_open 取决于底层流是否仍连接，
    若 accept 后立刻 close，服务端会把会话判为已断开，影响“忙时探测”断言。
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepts = 0
        self.conns = []
        self.running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            while self.running:
                conn, _ = self.sock.accept()
                self.accepts += 1
                self.conns.append(conn)
        except Exception:
            pass

    def stop(self):
        self.running = False
        for conn in self.conns:
            try:
                conn.close()
            except Exception:
                pass
        try:
            self.sock.close()
        except Exception:
            pass


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  ✓ " if condition else "  ✗ ") + name
          + (f"  [{detail}]" if detail and not condition else ""))


async def read_telemetry():
    async with websockets.connect(BASE.replace("http", "ws") + "/ws/v2/devices/regression-sta/shoot", additional_headers=harness.ws_headers()) as ws:
        for _ in range(20):
            event = json.loads(await asyncio.wait_for(ws.recv(), 3))
            if event.get("type") == "telemetry":
                return True
    return False


def run():
    fake = FakeFirmwareServer()
    root = "/api/v2/devices/regression-sta"
    other = "/api/v2/devices/regression-other"
    try:
        for name in ("regression-sta", "regression-other"):
            code, _ = api("/api/v2/devices", {
                "id": name, "name": name, "transport": "vllink_tcp",
                "host": "127.0.0.1", "uart_port": fake.port,
            })
            check(name + " 注册", code == 200)
        code, result = api(root + "/shoot/open", {})
        check("单STA连接接口返回200", code == 200 and result.get("status", {}).get("open"))
        time.sleep(0.2)
        code, status = api(root + "/shoot/status")
        check("单STA收到三电机遥测", code == 200 and len(status["telemetry"]["slots"]) == 3)
        check("单STA WebSocket收到遥测", asyncio.run(read_telemetry()))
        code, result = api(root + "/shoot/ping", {}, "POST")
        check("单STA RTT返回", code == 200 and result["rtt_ms"] >= 0)
        check("USB默认设备保持断开", not api("/api/shoot/status")[1]["open"])
        check("另一STA保持断开", not api(other + "/shoot/status")[1]["open"])
        check("单STA综合状态接口", api(root + "/status")[0] == 200)
        doc = {"motor_presets": {"0": {"0": {
            "presets": [{"name": "单STA参数", "params": {"kp_speed": 0.3}}],
            "default": -1,
        }}}}
        code, result = api(root + "/shoot/presets", doc, "PUT")
        check("单STA电机预设保存并返回同一结构", code == 200 and result == doc)
        check("单STA电机预设可读回", api(root + "/shoot/presets")[1] == doc)
        check("另一STA预设独立", api(other + "/shoot/presets")[1] == {"motor_presets": {}})
        check("非法STA电机预设返回400", api(root + "/shoot/presets", {"motor_presets": {"9": {}}}, "PUT")[0] == 400)
        commands = {"port": "auto", "baudrate": 115200, "commands": []}
        check("单STA串口预设保存", api(root + "/presets", commands, "PUT")[0] == 200)
        check("单STA串口预设读回", api(root + "/presets")[1] == commands)
        check("单STA断开成功", api(root + "/shoot/close", {}, "POST")[0] == 200)
        check("单STA断开状态", not api(root + "/shoot/status")[1]["open"])

        # ---- 健康探测语义（2026-10-19）：只探 DAP、绝不碰 UART 端口 ----
        # 背景：真实 Basic2 的 TCP-UART 端口若被前端每 5s 的健康探测反复连接，
        # 可能影响正在使用的串口会话；修复后 probe 只连 dap_port。
        dap = CountingListener()
        uart = CountingListener()
        probe_id = "regression-probe"
        try:
            code, _ = api("/api/v2/devices", {
                "id": probe_id, "name": probe_id, "transport": "vllink_tcp",
                "host": "127.0.0.1", "dap_port": dap.port,
                "uart_port": uart.port})
            check("探测用例设备注册", code == 200)
            time.sleep(0.2)
            code, r = api(f"/api/v2/devices/{probe_id}/probe", {})
            check("探测接口返回200", code == 200, str(r))
            check("探测只连接 DAP 端口", dap.accepts >= 1, f"dap={dap.accepts}")
            check("探测不连接 UART 端口", uart.accepts == 0, f"uart={uart.accepts}")
            check("探测报告 DAP 可达", r.get("ports", {}).get("dap") is True, str(r))
            check("探测报告串口会话未打开",
                  r.get("ports", {}).get("uart_session") is False, str(r))
            # 打开串口后探测应转为“忙”，且不再新建任何连接
            code, _ = api(f"/api/v2/devices/{probe_id}/serial/open", {})
            time.sleep(0.4)
            dap_before, uart_before = dap.accepts, uart.accepts
            code, r = api(f"/api/v2/devices/{probe_id}/probe", {})
            check("串口会话打开时探测返回忙",
                  code == 200 and r.get("busy") is True, str(r))
            check("会话忙时探测不新建连接",
                  dap.accepts == dap_before and uart.accepts == uart_before,
                  f"dap {dap_before}->{dap.accepts}, uart {uart_before}->{uart.accepts}")
            check("探测反映串口会话已打开",
                  r.get("ports", {}).get("uart_session") is True, str(r))
            api(f"/api/v2/devices/{probe_id}/serial/close", {})
        finally:
            api(f"/api/v2/devices/{probe_id}", method="DELETE")
            dap.stop()
            uart.stop()
    finally:
        api(root + "/shoot/close", {}, "POST")
        fake.stop()
        api(root, method="DELETE")
        api(other, method="DELETE")
