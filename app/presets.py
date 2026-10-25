"""预设串口指令 + 串口参数，JSON 文件存储，Web 端可编辑。

presets.json 结构:
{
  "port": "auto",          # 串口设备名，auto = 自动选择
  "baudrate": 115200,
  "commands": [
    {"id": "...", "name": "按钮名", "data": "start",
     "encoding": "text",   # text | hex
     "newline": "crlf"}    # crlf | lf | cr | none
  ]
}
"""
import json
import threading

from . import config

_lock = threading.Lock()

DEFAULTS = {
    "port": config.SERIAL_PORT_DEFAULT,
    "baudrate": config.SERIAL_BAUD_DEFAULT,
    "commands": [
        {"id": "example_start", "name": "示例-启动", "data": "start",
         "encoding": "text", "newline": "crlf"},
        {"id": "example_stop", "name": "示例-停止", "data": "stop",
         "encoding": "text", "newline": "crlf"},
    ],
}

_NEWLINE = {"crlf": b"\r\n", "lf": b"\n", "cr": b"\r", "none": b""}


def _path(device_id=None):
    """非默认设备独立保存；默认设备继续使用原文件。"""
    if not device_id or device_id == "legacy-default":
        return config.PRESETS_FILE
    from . import device_registry
    device_registry.get(device_id)
    return config.PRESETS_FILE.parent / "device-presets" / device_id / config.PRESETS_FILE.name


def load(device_id=None):
    """读取配置，缺失字段用默认值补齐；文件不存在时自动生成。"""
    try:
        raw = json.loads(_path(device_id).read_text(encoding="utf-8"))
        return {**DEFAULTS, **raw}
    except FileNotFoundError:
        save(DEFAULTS, device_id)
        return dict(DEFAULTS)
    except Exception:
        return dict(DEFAULTS)


def save(data: dict, device_id=None):
    with _lock:
        _path(device_id).parent.mkdir(parents=True, exist_ok=True)
        _path(device_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def encode_command(cmd: dict) -> bytes:
    """把预设指令编码为串口字节流。"""
    data = str(cmd.get("data", ""))
    if cmd.get("encoding") == "hex":
        body = bytes.fromhex(data.replace(" ", ""))
    else:
        body = data.encode("utf-8")
    return body + _NEWLINE.get(cmd.get("newline", "crlf"), b"\r\n")
