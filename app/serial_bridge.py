"""无线串口桥：pyserial <-> WebSocket 全双工转发。

Vllink Basic2 的"USB CDC串口-无线"模式对主机是透明串口：
AP 端插服务器 USB -> 出现 /dev/ttyACMx，STA 端 TXD/RXD 接 STM32，
读写该串口即完成无线收发。

WS 事件格式（服务端 -> 客户端）:
  {"type": "sys", "data": "...", "ts": 123.4}      系统消息
  {"type": "rx" , "b64": "...", "ts": 123.4}       收到的串口数据
  {"type": "tx" , "b64": "...", "ts": 123.4}       已发送的串口数据（回显）

客户端 -> 服务端:
  {"type": "open", "port": "auto"|"/dev/ttyACM0"}  打开串口（波特率取 presets）
  {"type": "close"}                                 关闭串口
  {"type": "tx", "b64": "..."}                      发送字节
"""
import asyncio
import base64
import logging
import threading
import time
from collections import deque

import serial

from . import config, presets, transports

log = logging.getLogger(__name__)


class SerialManager:
    def __init__(self, opener=None):
        self._loop = None
        self._ser = None
        self._opener = opener or transports.usb_serial_opener
        self._gen = 0            # 连接代次：旧读线程退出不得清理新连接
        self._stop = threading.Event()
        self._thread = None
        self._port_info = None
        self._history = deque(maxlen=1000)  # 断线重连回放
        self._clients = set()               # set[asyncio.Queue]
        self._rx_bytes = 0       # 本次连接累计接收字节（串口排障用）
        self._last_rx_at = 0.0   # 最近一次收到数据的时刻
        self._loop_warned = False

    def attach_loop(self, loop):
        self._loop = loop

    @property
    def is_open(self):
        return self._ser is not None and self._ser.is_open

    def status(self):
        p = presets.load()
        return {
            "open": self.is_open,
            "port": self._port_info["port"] if self._port_info else None,
            "label": self._port_info["label"] if self._port_info else None,
            "baudrate": int(p.get("baudrate", config.SERIAL_BAUD_DEFAULT)),
            "rx_bytes": self._rx_bytes,
            "last_rx_at": self._last_rx_at,
        }

    # ---------- 事件分发 ----------

    def _emit(self, msg):
        msg = {**msg, "ts": time.time()}
        self._history.append(msg)
        if self._loop is None:
            # 事件循环未绑定会静默丢失所有订阅推送（history 有、WS 永远收不到）。
            # 只在首次告警，避免刷屏；该状态属启动顺序缺陷，需修复而非降级。
            if not self._loop_warned:
                self._loop_warned = True
                log.error("串口事件循环未绑定，WS 订阅推送会被丢弃（检查 sessions.init）")
            return
        for q in list(self._clients):
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, msg)
            except Exception:
                pass

    def _emit_sys(self, text):
        self._emit({"type": "sys", "data": text})

    # ---------- 打开 / 关闭 / 发送 ----------

    def open(self, port=None, baudrate=None):
        if self.is_open:
            self.close()
        p = presets.load()
        baud = int(baudrate) if baudrate else int(p.get("baudrate", config.SERIAL_BAUD_DEFAULT))
        want = port or p.get("port") or config.SERIAL_PORT_DEFAULT
        try:
            ser = self._opener(None if want == "auto" else want, baud)
        except Exception as e:
            log.warning("打开串口失败: %s（want=%s baud=%s）", e, want, baud)
            self._emit_sys(f"打开串口失败: {e}")
            return False
        if ser is None:
            log.warning("打开串口失败: 未找到可用串口（want=%s）", want)
            self._emit_sys("未找到可用串口，请检查 AP 端是否插入服务器 USB")
            return False
        self._gen += 1
        gen = self._gen
        self._ser = ser
        self._port_info = {"port": getattr(ser, "port", str(want)),
                           "label": getattr(ser, "label", "")}
        self._rx_bytes = 0
        self._last_rx_at = 0.0
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, args=(gen,), daemon=True)
        self._thread.start()
        endpoint = self._port_info["port"]
        log.info("串口已打开: %s @ %s", endpoint, baud)
        self._emit_sys(f"串口已打开: {endpoint} @ {baud}")
        return True

    def close(self):
        was_open = self._ser is not None
        port = self._port_info["port"] if self._port_info else "?"
        self._gen += 1              # 使旧读线程代次失效
        self._stop.set()
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self._port_info = None
        if was_open:
            log.info("串口已关闭: %s", port)
            self._emit_sys(f"串口已关闭: {port}")

    def write(self, data: bytes):
        if not self.is_open:
            self._emit_sys("串口未打开，无法发送")
            return False
        try:
            self._ser.write(data)
            self._emit({"type": "tx", "b64": base64.b64encode(data).decode()})
            return True
        except Exception as e:
            self._emit_sys(f"发送失败: {e}")
            return False

    # ---------- 后台读线程 ----------

    def _reader(self, gen):
        while not self._stop.is_set():
            try:
                n = self._ser.in_waiting
                data = self._ser.read(n if n > 0 else 1)
                if data:
                    self._rx_bytes += len(data)
                    self._last_rx_at = time.time()
                    self._emit({"type": "rx", "b64": base64.b64encode(data).decode()})
            except serial.SerialException as e:
                log.warning("串口异常: %s", e)
                self._emit_sys(f"串口异常: {e}")
                break
            except Exception as e:
                # 主动 close 时读线程也会在这里退出，属正常路径，仅记录调试日志
                log.debug("串口读线程退出: %r", e)
                break
        # 线程退出 = 端口被拔出或异常；代次不匹配说明已有新连接，不得清理
        if gen == self._gen and self._ser is not None:
            endpoint = self._port_info["port"] if self._port_info else "?"
            log.warning("串口连接已断开: %s", endpoint)
            self._emit_sys("串口连接已断开")
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
            self._port_info = None

    # ---------- WebSocket 订阅 ----------

    def subscribe(self, replay=True):
        q = asyncio.Queue(maxsize=1000)
        if replay:
            for m in list(self._history)[-200:]:
                q.put_nowait(m)
        self._clients.add(q)
        return q

    def unsubscribe(self, q):
        self._clients.discard(q)


