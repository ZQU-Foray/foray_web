"""REST 契约测试（阶段 0 基线，无需浏览器）。

覆盖：status 形状、固件库增删查、烧录参数校验、指令预设读写、
电机预设校验（槽位/数量白名单）、shoot/serial 初始状态。
"""
import json
import os
import urllib.request

import harness
from harness import BASE, api, REPO

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def run():
    print("== API 契约 ==")

    # status 形状
    code, st = api("/api/status")
    check("status 200", code == 200)
    for key in ("devices", "serial", "flash", "targets", "probes", "firmware_dir"):
        check(f"status 含 {key}", key in st)

    # 固件库：上传/列表/删除
    boundary = "----shootwebtest"
    payload = (f"--{boundary}\r\n"
               'Content-Disposition: form-data; name="file"; filename="t.bin"\r\n'
               "Content-Type: application/octet-stream\r\n\r\n").encode() + b"\x00\x01\x02" + \
              (f"\r\n--{boundary}--\r\n").encode()
    import harness as _h
    req = urllib.request.Request(
        BASE + "/api/firmware/upload", data=payload, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": "Bearer " + _h._auth["emergency"]})
    with urllib.request.urlopen(req, timeout=10) as r:
        up = json.loads(r.read().decode())
    check("固件上传", r.status == 200 and up.get("name") == "t.bin")

    code, fl = api("/api/firmware")
    check("固件列表含上传项", any(i["name"] == "t.bin" for i in fl.get("items", [])))

    code, _ = api("/api/firmware/t.bin", method="DELETE")
    check("固件删除", code == 200)
    code, _ = api("/api/firmware/t.bin", method="DELETE")
    check("重复删除幂等", code == 200)

    # 烧录参数校验
    code, r = api("/api/flash", {"firmware": "不存在.elf", "target": "stm32h723vg"})
    check("烧录未知固件 → 400", code == 400, str(r))
    code, r = api("/api/flash", {"firmware": "x.elf", "target": "不存在的芯片"})
    check("烧录未知目标 → 400", code == 400, str(r))

    # 指令预设读写
    code, pr = api("/api/presets")
    check("指令预设读取", code == 200 and "commands" in pr)
    code, _ = api("/api/presets", {"port": "auto", "baudrate": 115200,
                                   "commands": [{"id": "t", "name": "t",
                                                 "data": "x", "encoding": "text",
                                                 "newline": "lf"}]}, method="PUT")
    check("指令预设写入", code == 200)

    # 电机预设校验
    code, r = api("/api/shoot/presets", {"motor_presets": {"9": {"0": {
        "presets": [], "default": -1}}}}, method="PUT")
    check("电机预设非法槽位 → 400", code == 400, str(r))
    code, r = api("/api/shoot/presets", {"motor_presets": {"0": {"0": {
        "presets": [{"name": f"p{i}", "params": {"kp_speed": 1}} for i in range(6)],
        "default": -1}}}}, method="PUT")
    check("电机预设超 5 套 → 400", code == 400, str(r))
    code, r = api("/api/shoot/presets", {"motor_presets": {"0": {"0": {
        "presets": [{"name": "p", "params": {"hack_key": 1}}], "default": -1}}}},
        method="PUT")
    check("电机预设非法键 → 400", code == 400, str(r))
    code, r = api("/api/shoot/presets", {"motor_presets": {}}, method="PUT")
    check("电机预设空文档可写", code == 200)

    # shoot/serial 初始状态
    code, ss = api("/api/shoot/status")
    check("shoot 初始未开", code == 200 and ss.get("open") is False)
    code, _ = api("/api/shoot/ping", {})
    check("未开会话 ping → 400", code == 400)

    # 烧录日志空闲形状
    code, lg = api("/api/flash/log")
    check("烧录日志空闲形状", code == 200 and lg.get("state") == "idle" and
          lg.get("lines") == [] and "next" in lg)

    # ---- 阶段 1：设备注册表 ----
    code, dl = api("/api/v2/devices")
    check("注册表含 legacy-default", code == 200 and
          any(d["id"] == "legacy-default" for d in dl.get("devices", [])))

    code, r = api("/api/v2/devices", {"id": "sta-a", "transport": "vllink_tcp",
                                      "host": "192.168.1.50", "name": "台架 A",
                                      "dap_port": 4441, "uart_port": 20010,
                                      "capabilities": {"flash": {"available": "unknown",
                                                                 "source": "declared"}}})
    check("注册 STA 设备", code == 200 and r.get("id") == "sta-a", str(r))

    code, r = api("/api/v2/devices/sta-a", {"host": "192.168.1.99"}, method="PATCH")
    check("IP 更新 id 不变", code == 200 and r["host"] == "192.168.1.99")

    code, r = api("/api/v2/devices/sta-a", {"transport": "usb"}, method="PATCH")
    check("transport 不可改", code == 400)

    code, r = api("/api/v2/devices", {"id": "bad id!", "transport": "usb",
                                      "probe_serial": "X"})
    check("非法 id 拒绝", code == 400)

    code, r = api("/api/v2/devices", {"id": "dup-serial", "transport": "usb",
                                      "probe_serial": "SN123"})
    code2, r2 = api("/api/v2/devices", {"id": "dup-serial-2", "transport": "usb",
                                        "probe_serial": "SN123"})
    check("重复物理端点拒绝", code == 200 and code2 == 400, f"{code}/{code2}")

    code, r = api("/api/v2/devices/legacy-default", method="DELETE")
    check("legacy-default 不可删", code == 400)
    code, r = api("/api/v2/devices/sta-a", method="DELETE")
    check("正常删除", code == 200)

    # choose_port 显式端口语义（单元级，直接调用）
    import sys
    sys.path.insert(0, REPO)
    from app import devices as devmod
    ports = [{"port": "/dev/ttyACM0", "kind": "vllink"}]
    check("显式存在 → 命中", devmod.choose_port(ports, "/dev/ttyACM0")["port"] == "/dev/ttyACM0")
    check("显式缺失 → 失败", devmod.choose_port(ports, "/dev/ttyACM9") is None)


if __name__ == "__main__":
    run()
    print(f"\nAPI: {len(PASS)} 通过, {len(FAIL)} 失败")
    raise SystemExit(1 if FAIL else 0)
