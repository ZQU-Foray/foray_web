"""设备 host 访问策略（审计 P1-5：内网探测链）。

设备注册、更新与探活三处入口共用本模块：host 必须是合法 IP，且落在允许网段
内（显式白名单 + SCAN_SUBNET 推断的子网 + 回环网段）。默认强制，设置
DEVICE_HOST_ENFORCE=0 时只记录告警放行，便于灰度期先观察再收紧。
"""
import ipaddress
import logging
import os
import socket

from . import config

log = logging.getLogger(__name__)

_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")


def enforcing() -> bool:
    """是否对越界 host 直接拒绝（0 = 仅告警放行）。"""
    return bool(config.DEVICE_HOST_ENFORCE)


def _subnet_from_env():
    """解析 SCAN_SUBNET：支持 192.168.1 / 192.168.1.0 / 192.168.1.0/24 三种写法。"""
    raw = (os.environ.get("SCAN_SUBNET") or "").strip()
    if not raw:
        return None
    try:
        if "/" in raw:
            return ipaddress.ip_network(raw, strict=False)
        if raw.count(".") == 2:
            return ipaddress.ip_network(raw + ".0/24", strict=False)
        if raw.count(".") == 3:
            return ipaddress.ip_network(raw + "/24", strict=False)
    except ValueError:
        log.warning("SCAN_SUBNET 无法解析，已忽略：%s", raw)
    return None


def _local_subnet():
    """推断本机所在 /24（与扫描接口同口径：UDP 连接取默认出口地址）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    try:
        return ipaddress.ip_network(ip + "/24", strict=False)
    except ValueError:
        return None


def allowed_networks():
    """返回当前允许的网段列表（显式白名单 + 推断子网 + 回环）。"""
    nets = []
    for item in (config.DEVICE_HOST_ALLOWLIST or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning("DEVICE_HOST_ALLOWLIST 项无法解析，已忽略：%s", item)
    for net in (_subnet_from_env(), _local_subnet()):
        if net is not None and net not in nets:
            nets.append(net)
    if _LOOPBACK not in nets:
        nets.append(_LOOPBACK)
    return nets


def check_host(host):
    """校验 host：必须是合法 IP 且落在允许网段内，返回 (是否允许, 原因)。"""
    text = str(host or "").strip()
    if not text:
        return False, "设备 host 不能为空"
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False, f"设备 host 必须是 IP 地址（收到 {text!r}）"
    for net in allowed_networks():
        if addr in net:
            return True, ""
    nets = "、".join(str(net) for net in allowed_networks())
    return False, f"设备 host {addr} 不在允许网段内（{nets}）"
