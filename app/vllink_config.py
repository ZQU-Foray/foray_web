"""VLLINK Basic2 服务端 USB 配置（阶段 5）。

通过 libusb/PyUSB 控制传输（端点 0 厂商请求）读写 Basic2 配置，
替代浏览器 WebUSB。协议对齐官方 WebUSB 规范（spec_dap_protocal.md）。
"""
import logging
import struct
import usb.core
import usb.util

log = logging.getLogger(__name__)

VLLINK_VID = 0x1209
VLLINK_PID = 0x6666

# 厂商配置命令（对齐 WebUSB 协议）
CMD_CONFIG_READ = 0x91
CMD_CONFIG_WRITE = 0x92
CMD_CONFIG_COMMIT = 0x93
CMD_CONFIG_INFO = 0x94


class VllinkUsbConfig:
    """通过 USB 控制传输读写 Basic2 配置。"""

    def __init__(self, serial=None):
        self.dev = None
        self.serial = serial

    def find(self):
        """查找 VLLINK 设备；serial 为 None 取第一只。"""
        args = {"idVendor": VLLINK_VID, "idProduct": VLLINK_PID}
        if self.serial:
            args["serial_number"] = self.serial
        self.dev = usb.core.find(**args)
        return self.dev is not None

    def _ctrl_read(self, cmd, value=0, index=0, length=256):
        """控制传输读。"""
        return self.dev.ctrl_transfer(
            0xC0, cmd, value, index, length)   # vendor IN

    def _ctrl_write(self, cmd, data, value=0, index=0):
        """控制传输写。"""
        return self.dev.ctrl_transfer(
            0x40, cmd, value, index, data)      # vendor OUT

    def get_info(self):
        """获取设备信息（容量、版本等）。"""
        if not self.dev:
            raise OSError("设备未打开")
        raw = self._ctrl_read(CMD_CONFIG_INFO, length=64)
        # 返回原始字节，由上层解析
        return bytes(raw)

    def read_config(self):
        """读取完整配置文本。"""
        if not self.dev:
            raise OSError("设备未打开")
        raw = self._ctrl_read(CMD_CONFIG_READ, length=4096)
        return bytes(raw).decode("utf-8", errors="replace").rstrip("\x00")

    def write_config(self, text):
        """写入配置文本（分块）。"""
        if not self.dev:
            raise OSError("设备未打开")
        data = text.encode("utf-8")
        # 分块写入（每块 ≤ 64 字节对齐端点缓冲）
        CHUNK = 64
        for i in range(0, len(data), CHUNK):
            self._ctrl_write(CMD_CONFIG_WRITE, data[i:i + CHUNK])
        return True

    def commit_config(self):
        """提交配置到持久存储。"""
        if not self.dev:
            raise OSError("设备未打开")
        self._ctrl_write(CMD_CONFIG_COMMIT, b"")
        return True

    def close(self):
        if self.dev:
            usb.util.dispose_resources(self.dev)
            self.dev = None


def parse_config(text):
    """解析配置文本为键值对字典。"""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def build_config(orig_text, patch):
    """在原配置文本上合并 patch 字段（保留未知键原值）。"""
    lines = orig_text.splitlines()
    patched = dict(patch)
    out = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            k = stripped.split("=", 1)[0].strip()
            if k in patched:
                out.append(f"{k}={patched[k]}")
                del patched[k]
                continue
        out.append(line)
    # 新增键追加
    for k, v in patched.items():
        out.append(f"{k}={v}")
    return "\n".join(out) + "\n"


def config_schema():
    """返回可编辑字段 schema（供前端表单渲染）。"""
    return {
        "fields": [
            {"key": "Alias", "label": "设备别名", "type": "text", "max": 32},
            {"key": "Mode", "label": "工作模式", "type": "select",
             "options": ["有线", "无线主机", "无线设备"]},
            {"key": "Wireless_Device_SSID", "label": "Wi-Fi 名称", "type": "text"},
            {"key": "Wireless_Device_Password", "label": "Wi-Fi 密码", "type": "password",
             "blank_keep": True},
            {"key": "Wireless_Host_Channel", "label": "无线信道", "type": "select",
             "options": ["0", "1", "6", "11", "13"]},
            {"key": "Mode3_TCP_UART", "label": "TCP 串口", "type": "select",
             "options": ["关闭", "开启"]},
        ]
    }
