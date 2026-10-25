"""统一字节流传输：USB Serial 与 TCP UART 共用一套打开接口（计划阶段 2）。

管理器（serial_bridge / shoot）只依赖本模块返回的流对象，其接口对齐
pyserial：read(n) / write(data) / in_waiting / close / is_open。
TCP 实现以内部接收缓冲模拟 in_waiting 与超时读语义；
EOF / 连接中断表现为 read 返回空或抛异常，由管理器读线程按断线处理。

测试注入：loop://（pyserial 环回）与 make_tcp_opener(host, port) 可直接
构造 opener，供假固件服务器矩阵测试（软件通过档；实机 TCP 行为见
docs/vllink-capabilities.md 待实测条目）。
"""
import socket
import threading

import serial

from . import devices


# ---------- USB（现行路径，行为保持不变） ----------

def usb_serial_opener(port=None, baud=115200):
    """USB CDC 串口：port 为 None/"auto" 时自动选择；显式指定缺失返回 None。"""
    ports = devices.list_serial_ports()
    info = devices.choose_port(
        ports, explicit=None if port in (None, "auto") else port)
    if not info:
        return None
    return serial.Serial(info["port"], baud, timeout=0.1)


# ---------- TCP UART（Basic2 TCP-UART，字节透传） ----------

class TcpStream:
    """以 socket 模拟 pyserial 接口；接收线程持续入缓冲，read 带超时预算。"""

    def __init__(self, host, port, timeout=0.1):
        self._timeout = timeout
        # 与 pyserial 的 .port 对齐：日志/状态里能直接看出真实端点
        self.port = f"{host}:{port}"
        self.label = "TCP-UART"
        self._sock = socket.create_connection((host, port), timeout=3.0)
        self._sock.settimeout(0.05)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._rx = threading.Thread(target=self._rx_loop, daemon=True)
        self._closed = False
        self._rx.start()

    def _rx_loop(self):
        """后台接收：任何异常视为断线，置关闭标志。"""
        try:
            while not self._closed:
                try:
                    data = self._sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                with self._lock:
                    self._buf += data
        except Exception:
            pass
        finally:
            self._closed = True
            try:
                self._sock.close()
            except Exception:
                pass

    @property
    def is_open(self):
        return not self._closed

    @property
    def in_waiting(self):
        with self._lock:
            return len(self._buf)

    def read(self, size=1):
        """带超时预算的读取；超时返回空字节（与 pyserial 语义一致）。"""
        import time
        deadline = time.monotonic() + self._timeout
        while True:
            with self._lock:
                if self._buf:
                    out = bytes(self._buf[:size])
                    del self._buf[:size]
                    return out
            if not self.is_open:
                # 断线且缓冲已空：抛异常驱动读线程退出（对齐 pyserial 语义）
                raise OSError("TCP 连接已断开")
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.005)

    def write(self, data):
        if self._closed:
            raise OSError("TCP 连接已关闭")
        self._sock.sendall(data)

    def close(self):
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


def make_tcp_opener(host, port):
    """构造 TCP opener：忽略 port/baud 参数（TCP UART 的实际波特率烧录在
    Basic2 配置里，Web 侧 baud 仅作为期望值记录——计划 §5 的契约）。"""
    def opener(port_arg=None, baud=115200):
        return TcpStream(host, port)
    return opener


def make_loop_opener():
    """测试用环回串口（loop://），写入可被自身读出。"""
    def opener(port_arg=None, baud=115200):
        return serial.serial_for_url("loop://", timeout=0.05)
    return opener


def opener_for_device(dev):
    """按注册表设备记录构造 opener。"""
    if dev.get("transport") == "vllink_tcp":
        return make_tcp_opener(dev["host"], dev.get("uart_port") or 20010)
    return usb_serial_opener
