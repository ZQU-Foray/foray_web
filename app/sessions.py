"""按设备会话管理：每台注册设备独立的串口/发射调试实例与统一互斥（阶段 2）。

- SessionStore.get(device_id) 惰性创建 DeviceSession；
- 同一设备内原始串口与 shoot 互斥（open 一方先 close 另一方），
  不同设备互不影响；
- 设备维护锁（threading.Lock）预留给阶段 3 烧录独占；
- 全局单 uvicorn worker 前提下进程内字典即可（计划 §4.2）。
"""
import asyncio
import threading

from . import device_registry, transports
from .flashing import FlashManager
from .serial_bridge import SerialManager
from .shoot import ShootManager


class DeviceSession:
    def __init__(self, device, loop):
        self.device = device
        self.maint_lock = threading.Lock()      # 维护锁：烧录/改配置独占（阶段 3+）
        opener = transports.opener_for_device(device)
        self.serial = SerialManager(opener)
        self.shoot = ShootManager(opener)
        self.flash = FlashManager(device)
        # 事件循环绑定（阶段 4 修复的既有缺陷：多 STA 重构后 loop 未传递，
        # _broadcast 因 _loop=None 静默丢弃订阅推送——WS 仅剩连接时直发帧）
        self.serial.attach_loop(loop)
        self.shoot.attach_loop(loop)
        self.flash.attach_loop(loop)

    # ---------- 统一互斥入口（REST 与 WS 共用） ----------

    def open_serial(self, port=None, baudrate=None):
        self.shoot.close()
        return self.serial.open(port, baudrate)

    def open_shoot(self, port=None, baud=None):
        self.serial.close()
        return self.shoot.open(port, baud)


class SessionStore:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()
        self._loop = None

    def attach_loop(self, loop):
        self._loop = loop

    def _make(self, device):
        return DeviceSession(device, self._loop)

    def get(self, device_id):
        """按注册表设备取会话；设备不存在抛 ValueError（端点转 404）。"""
        device = device_registry.get(device_id)   # 不存在即抛
        with self._lock:
            sess = self._sessions.get(device_id)
            if sess is None:
                sess = self._make(device)
                self._sessions[device_id] = sess
            return sess

    def peek(self, device_id):
        """只读查看已建会话；不存在返回 None（不创建，供健康探测等只读路径用）。"""
        with self._lock:
            return self._sessions.get(device_id)

    def close_all(self):
        with self._lock:
            for sess in self._sessions.values():
                sess.serial.close()
                sess.shoot.close()
            self._sessions.clear()


_store = SessionStore()


def store() -> SessionStore:
    return _store


def init(loop):
    """lifespan 入口：绑定事件循环并预热 legacy 会话。"""
    _store.attach_loop(loop)
    try:
        _store.get("legacy-default")
    except Exception:
        pass
