"""设备注册表：稳定逻辑 ID 的设备档案，多 STA 改造的地基（计划阶段 1）。

设计要点：
- 持久化 config/devices.json，原子写入 + 旧文件备份；
- id 为稳定逻辑 ID（不用 IP / USB 索引）；IP 变化只更新字段；
- USB 身份以 probe_serial 绑定，重复注册同一物理端点会被拒绝；
- legacy-default：首次启动自动生成，承接现行单设备语义；
- 能力（capabilities）分 声明/探测/实测 三档来源，默认保守。
"""
import json
import logging
import re
import shutil
import threading
import time

from . import config, net_policy

_lock = threading.Lock()

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TRANSPORTS = ("usb", "vllink_tcp")
PROBE_KINDS = (None, "vllink", "daplink", "jlink")
CAP_KEYS = ("flash", "uart", "shoot", "config_usb", "config_tcp")


def _empty_doc():
    return {"version": 1, "devices": {}}


def _validate_device(dev):
    """校验单条设备档案；非法抛 ValueError。"""
    if not isinstance(dev, dict):
        raise ValueError("设备必须是对象")
    dev_id = dev.get("id")
    if not isinstance(dev_id, str) or not _ID_RE.match(dev_id):
        raise ValueError("id 需为小写字母/数字/连字符（≤32 字符）")
    if dev.get("transport") not in TRANSPORTS:
        raise ValueError(f"transport 必须是 {TRANSPORTS}")
    name = dev.get("name", dev_id)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name 不能为空")
    if len(name) > 40:
        raise ValueError("name 过长（≤40 字符）")
    target = dev.get("target")
    if target is not None and target not in config.TARGETS:
        raise ValueError(f"未知目标芯片 {target}")
    kind = dev.get("probe_kind")
    if kind not in PROBE_KINDS:
        raise ValueError(f"probe_kind 非法：{kind}")
    caps = dev.get("capabilities", {})
    if not isinstance(caps, dict):
        raise ValueError("capabilities 必须是对象")
    for k, v in caps.items():
        if k not in CAP_KEYS:
            raise ValueError(f"未知能力键 {k}")
        if not isinstance(v, dict) or v.get("available") not in (True, False, "unknown"):
            raise ValueError(f"能力 {k} 需要 {{available, source}}")
        if v.get("source") not in ("declared", "probed", "tested"):
            raise ValueError(f"能力 {k} 来源必须是 declared/probed/tested")
    if dev["transport"] == "usb":
        if not dev.get("probe_serial"):
            # USB 设备建议绑定序列号；未绑定时仅允许 legacy-default 生成阶段
            if dev_id != "legacy-default":
                raise ValueError("USB 设备需要 probe_serial")
    else:  # vllink_tcp
        host = dev.get("host")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("TCP 设备需要 host")
        ok, why = net_policy.check_host(host)
        if not ok:
            if net_policy.enforcing():
                raise ValueError(why)
            logging.getLogger(__name__).warning("设备 host 越界（仅告警）：%s", why)
        for pk in ("dap_port", "uart_port"):
            port = dev.get(pk)
            if port is not None and not (isinstance(port, int) and 1 <= port <= 65535):
                raise ValueError(f"{pk} 非法")
    # 期望/观测 UART 参数分开；未知就存 None
    for k in ("expected_uart", "observed_uart"):
        v = dev.get(k)
        if v is not None and not isinstance(v, dict):
            raise ValueError(f"{k} 必须是对象或 null")
    return dev


def load():
    """读取注册表；损坏时返回空文档（不自动覆盖磁盘，保底可修复）。"""
    try:
        raw = json.loads(config.DEVICES_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("devices"), dict):
            return raw
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return _empty_doc()


def save(doc):
    """校验全量文档并原子写入；写前备份旧文件。"""
    if not isinstance(doc, dict) or not isinstance(doc.get("devices"), dict):
        raise ValueError("文档结构非法")
    clean = {"version": 1, "devices": {}}
    seen_serials = set()
    for dev_id, dev in doc["devices"].items():
        if not isinstance(dev, dict) or dev.get("id") != dev_id:
            raise ValueError(f"设备 {dev_id} 的 id 字段不一致")
        _validate_device(dev)
        # 同一物理 USB 端点（序列号）不允许注册两台
        sn = dev.get("probe_serial")
        if dev.get("transport") == "usb" and sn:
            if sn in seen_serials:
                raise ValueError(f"USB 序列号 {sn} 已被其它设备注册（同一物理端点）")
            seen_serials.add(sn)
        clean["devices"][dev_id] = dev
    with _lock:
        config.DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        if config.DEVICES_FILE.exists():
            shutil.copy2(config.DEVICES_FILE,
                         config.DEVICES_FILE.with_suffix(".json.bak"))
        tmp = config.DEVICES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(config.DEVICES_FILE)
    return clean


def bootstrap_legacy():
    """首次启动：生成 legacy-default（承接现行单设备语义），幂等。"""
    doc = load()
    if "legacy-default" in doc["devices"]:
        return doc, False
    doc["devices"]["legacy-default"] = {
        "id": "legacy-default",
        "name": "默认 USB 设备（现行单设备）",
        "transport": "usb",
        "probe_kind": None,
        "target": None,
        "probe_serial": None,      # 现行为“自动选择”，不锁定具体硬件
        "capabilities": {
            "flash": {"available": True, "source": "tested"},
            "uart": {"available": True, "source": "tested"},
            "shoot": {"available": "unknown", "source": "declared"},
        },
        "expected_uart": {"baudrate": config.SERIAL_BAUD_DEFAULT},
        "observed_uart": None,
        "created": time.time(),
    }
    return save(doc), True


def register(dev):
    """注册/覆盖一条设备（按 id 幂等），返回规整后的文档。"""
    doc = load()
    dev = dict(dev)
    dev.setdefault("created", time.time())
    doc["devices"][dev["id"]] = dev
    return save(doc)


def update(device_id, patch):
    """部分更新：仅允许 name/target/host/端口/能力/绑定字段，id 与 transport 不可改。"""
    doc = load()
    dev = doc["devices"].get(device_id)
    if not dev:
        raise ValueError(f"设备不存在: {device_id}")
    mutable = {"name", "target", "host", "dap_port", "uart_port",
               "probe_serial", "capabilities", "expected_uart",
               "observed_uart", "config_binding"}
    for k, v in (patch or {}).items():
        if k not in mutable:
            raise ValueError(f"字段不可修改: {k}")
        dev[k] = v
    if "host" in (patch or {}):
        # 改 host 等于给门户换一个连接目标，必须和注册走同一套网段校验
        ok, why = net_policy.check_host(dev.get("host"))
        if not ok:
            if net_policy.enforcing():
                raise ValueError(why)
            logging.getLogger(__name__).warning("设备 host 越界（仅告警）：%s", why)
    return save(doc)


def remove(device_id):
    """移除设备；legacy-default 拒绝删除。"""
    if device_id == "legacy-default":
        raise ValueError("legacy-default 不可删除")
    doc = load()
    if device_id not in doc["devices"]:
        raise ValueError(f"设备不存在: {device_id}")
    del doc["devices"][device_id]
    return save(doc)


def get(device_id):
    dev = load()["devices"].get(device_id)
    if not dev:
        raise ValueError(f"设备不存在: {device_id}")
    return dev
