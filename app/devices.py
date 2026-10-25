"""USB 设备探测。

Linux（部署环境）下使用 pyudev；其他平台退回 pyserial 枚举串口，
保证在 Windows 开发机上也能运行并调试前端。
"""
import logging
import re

import serial.tools.list_ports as _list_ports

log = logging.getLogger(__name__)

try:
    import pyudev
    _HAS_PYUDEV = True
except Exception:  # pragma: no cover - 非 Linux 平台
    _HAS_PYUDEV = False

# (vid, pid) -> 设备类型
USB_KINDS = {
    ("1209", "6666"): "vllink",   # Vllink Basic2 CMSIS-DAP
    ("0d28", "0204"): "daplink",  # DAPLink (mbed)
    ("1366", "0101"): "jlink",
    ("1366", "0105"): "jlink",
    ("1366", "1015"): "jlink",
}

# 自动选择调试器的优先级
SERIAL_PREFER = ("vllink", "daplink", "jlink")

_LABELS = {
    "vllink": "Vllink Basic2 (无线)",
    "daplink": "DAPLink",
    "jlink": "J-Link",
    None: "未知 USB 串口",
}

# USB 串口：CDC-ACM (ttyACM*) / 传统 USB 串口芯片 (ttyUSB*) / hwid 含 USB
_USB_PORT_RE = re.compile(r"(ttyacm|ttyusb)")

_HWID_VIDPID = re.compile(r"VID:PID=([0-9a-fA-F]{4}):([0-9a-fA-F]{4})")
_HWID_SER = re.compile(r"SER=(\S+)", re.I)


def _label_for(kind):
    return _LABELS.get(kind, "未知 USB 设备")  # kind 为 None 时用于非 USB 串口兜底


def _fix_vllink_emulation(kind, serial):
    """vllink 可伪装成 mbed(0d28:0204)，按序列号前缀修正类型。"""
    if kind == "daplink" and (serial or "").lower().startswith("vllink"):
        return "vllink"
    return kind


def list_probes():
    """调试器列表: [{kind, vid, pid, serial, devnode, label}]"""
    if not _HAS_PYUDEV:
        return []
    ctx = pyudev.Context()
    out = []
    for dev in ctx.list_devices(subsystem="usb", DEVTYPE="usb_device"):
        vid = dev.get("ID_VENDOR_ID", "")
        pid = dev.get("ID_MODEL_ID", "")
        kind = USB_KINDS.get((vid, pid))
        if kind is None:
            continue
        serial = dev.get("ID_SERIAL_SHORT", "") or dev.get("ID_SERIAL", "")
        kind = _fix_vllink_emulation(kind, serial)
        out.append({
            "kind": kind,
            "vid": vid,
            "pid": pid,
            "serial": serial,
            "devnode": dev.get("DEVNAME", ""),
            "label": _label_for(kind),
        })
    return out


def list_serial_ports():
    """串口列表: [{port, kind, vid, pid, serial, hwid, label}]

    只保留 USB 串口（ttyACM*/ttyUSB* 或 hwid 含 USB），
    过滤掉 ttyS* 等主板物理串口，避免界面出现大量"未知设备"。
    """
    out = []
    for p in _list_ports.comports():
        if not _USB_PORT_RE.search(p.device.lower()) and "usb" not in (p.hwid or "").lower():
            continue
        hwid = p.hwid or ""
        m = _HWID_VIDPID.search(hwid)
        vid = m.group(1).lower() if m else ""
        pid = m.group(2).lower() if m else ""
        kind = USB_KINDS.get((vid, pid))
        s = _HWID_SER.search(hwid)
        sn = s.group(1) if s else (p.serial_number or "")
        kind = _fix_vllink_emulation(kind, sn)
        out.append({
            "port": p.device,
            "kind": kind,
            "vid": vid,
            "pid": pid,
            "serial": sn,
            "hwid": hwid,
            "label": _label_for(kind),
        })
    return out


def find_probe(kind=None):
    """按类型找调试器；kind=None 时按优先级自动选择。
    无 pyudev 的平台（如 Windows 开发机）回退到串口枚举结果。
    """
    probes = list_probes()
    if not probes:  # 非 Linux：从串口 hwid 推导（丢失 devnode，但保留 vid/pid/serial）
        probes = [{"kind": p["kind"], "vid": p["vid"], "pid": p["pid"],
                   "serial": p["serial"], "label": p["label"]}
                  for p in list_serial_ports() if p["kind"]]
    if kind:
        for p in probes:
            if p["kind"] == kind:
                return p
        return None
    for k in SERIAL_PREFER:
        for p in probes:
            if p["kind"] == k:
                return p
    return probes[0] if probes else None


def choose_port(ports, explicit=None, hint_kind=None):
    """从串口列表里挑一个：
    explicit 优先且必须存在（多目标环境下缺失即失败，禁止回退自动选择——
    否则命令会误发到另一台设备）；其次 hint_kind；再按优先级；最后第一个。
    """
    if not ports:
        return None
    if explicit and explicit != "auto":
        for p in ports:
            if p["port"] == explicit:
                return p
        return None          # 显式指定不存在：明确失败，不回退
    if hint_kind:
        for p in ports:
            if p["kind"] == hint_kind:
                return p
    for p in ports:
        if p["kind"] in SERIAL_PREFER:
            return p
    for p in ports:
        pl = p["port"].lower()
        if "ttyacm" in pl or "ttyusb" in pl or "usb" in (p["hwid"] or "").lower():
            return p
    return ports[0]
