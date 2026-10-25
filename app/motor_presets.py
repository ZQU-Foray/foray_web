"""电机调参预设：按（槽位 id × 型号）命名空间存储于 Web 服务器。

结构 motor_presets.json:
{
  "motor_presets": {
    "<slot 0-2>": {
      "<model 0-2>": {
        "presets": [ {"name": "...", "params": {"kp_speed": 0.02, ...}}, ... ],
        "default": <索引，-1 表示无默认>
      }
    }
  }
}

每个 (slot, model) 最多 5 套，共 3×3×5 = 45 套。
参数键白名单与 shoot.KEYS ∪ {gear_ratio} 一致，值必须为有限数字。
"""
import json
import math
import threading

from . import config

_lock = threading.Lock()

# 允许写入预设的参数键（PID/限幅/死区 + 减速比）
_ALLOWED_KEYS = set()

MAX_PRESETS = 5


def _init_allowed():
    from .shoot import KEYS
    _ALLOWED_KEYS.update(KEYS.keys())
    _ALLOWED_KEYS.add("gear_ratio")


_init_allowed()


def _empty_doc():
    return {"motor_presets": {}}


def _path(device_id=None):
    """非默认设备独立保存；默认设备继续使用原文件。"""
    if not device_id or device_id == "legacy-default":
        return config.MOTOR_PRESETS_FILE
    from . import device_registry
    device_registry.get(device_id)
    return config.MOTOR_PRESETS_FILE.parent / "device-presets" / device_id / config.MOTOR_PRESETS_FILE.name


def load(device_id=None):
    """读取预设文档；文件不存在或损坏时返回空文档（不自动重建）。"""
    try:
        raw = json.loads(_path(device_id).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("motor_presets"), dict):
            return raw
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return _empty_doc()


def validate(doc):
    """校验并规整文档；非法则抛 ValueError（由 REST 层转 400）。"""
    if not isinstance(doc, dict):
        raise ValueError("文档必须是对象")
    mp = doc.get("motor_presets", {})
    if not isinstance(mp, dict):
        raise ValueError("motor_presets 必须是对象")
    cleaned = {}
    for slot_str, models in mp.items():
        if slot_str not in ("0", "1", "2"):
            raise ValueError(f"非法槽位 {slot_str}")
        if not isinstance(models, dict):
            raise ValueError(f"槽位 {slot_str} 内容必须是对象")
        cm = {}
        for model_str, cell in models.items():
            if model_str not in ("0", "1", "2"):
                raise ValueError(f"非法型号 {model_str}")
            if not isinstance(cell, dict):
                raise ValueError(f"({slot_str},{model_str}) 内容必须是对象")
            presets = cell.get("presets", [])
            if not isinstance(presets, list) or len(presets) > MAX_PRESETS:
                raise ValueError(f"({slot_str},{model_str}) 预设数量超限（≤{MAX_PRESETS}）")
            cp = []
            for p in presets:
                if not isinstance(p, dict) or not isinstance(p.get("name"), str):
                    raise ValueError("预设需要 name 字符串")
                params = p.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("params 必须是对象")
                clean_params = {}
                for k, v in params.items():
                    if k not in _ALLOWED_KEYS:
                        raise ValueError(f"非法参数键 {k}")
                    if isinstance(v, bool) or not isinstance(v, (int, float)) \
                            or not math.isfinite(v):
                        raise ValueError(f"参数 {k} 必须是有限数字")
                    clean_params[k] = float(v)
                cp.append({"name": p["name"][:24], "params": clean_params})
            default = cell.get("default", -1)
            if default is not None and default != -1:
                if not isinstance(default, int) or not (-1 <= default < len(cp)):
                    raise ValueError("default 必须是有效索引或 -1")
            cm[model_str] = {"presets": cp, "default": default if default is not None else -1}
        cleaned[slot_str] = cm
    return {"motor_presets": cleaned}


def save(doc, device_id=None):
    """校验并写入磁盘，返回规整后的文档。"""
    clean = validate(doc)
    with _lock:
        _path(device_id).parent.mkdir(parents=True, exist_ok=True)
        _path(device_id).write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean
