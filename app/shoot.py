"""发射机构电机调试协议服务。

独立持有串口（与原始串口控制台互斥），实现 MCU 侧 ShootDebugTask 的
帧协议：[A5 5A][type][seq][len16][payload][crc8]，小端。
  下行: Ping/CfgWrite/CfgApply/ModeSet/EnableSet/ModelSet/Estop/FaultClear
  上行: Pong/Telemetry(50Hz)/Ack

职责：保活心跳（300ms）、遥测解码与广播、命令 ACK 关联（线程事件）、
型号档案镜像（供页面下拉与一级校验；换算真值仍在 MCU）。
"""
import asyncio
import logging
import struct
import threading
import time
from collections import deque

import serial

from . import config, transports

log = logging.getLogger(__name__)

HEAD0, HEAD1 = 0xA5, 0x5A
T_PING, T_CFG_WRITE, T_CFG_APPLY, T_MODE, T_ENABLE, T_MODEL, T_ESTOP, T_FAULT_CLEAR = (
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08)
T_PONG, T_TELEM, T_ACK, T_PROFILE = 0x81, 0x82, 0x83, 0x84

ACK_STATUS = {0: "ok", 1: "rejected", 2: "bad_slot", 3: "bad_model",
              4: "duplicate", 5: "busy"}

MODES = ["disable", "rpm", "position", "torque"]

# TLV 参数键（与 MCU DebugCommand::Key 一一对应）
KEYS = {
    "model": 1, "mode": 2, "enable": 3, "gear_ratio": 4,
    "kp_speed": 10, "ki_speed": 11, "kd_speed": 12, "kf_speed": 13,
    "il_speed": 14, "dz_speed": 15,
    "kp_angle": 20, "ki_angle": 21, "kd_angle": 22, "kf_angle": 23,
    "il_angle": 24, "dz_angle": 25,
    "target_rpm": 30, "target_deg": 31, "target_torque": 32,
}

# 型号档案镜像（换算与限幅以 MCU 上行为准，此表用于页面展示与一级校验）
PROFILES = [
    {"id": 0, "name": "M2006", "kt": 0.18, "gear_ratio": 36.0,
     "command_full_scale": 10000, "current_full_scale": 10.0,
     "max_output_rpm": 500.0, "max_current": 3.0, "max_torque": 1.0,
     "temp_warn": 60, "temp_limit": 80, "max_devices": 8},
    {"id": 1, "name": "M3508", "kt": 0.30, "gear_ratio": 19.2,
     "command_full_scale": 16384, "current_full_scale": 20.0,
     "max_output_rpm": 500.0, "max_current": 3.0, "max_torque": 1.5,
     "temp_warn": 60, "temp_limit": 80, "max_devices": 8},
    {"id": 2, "name": "GM6020C", "kt": 0.741, "gear_ratio": 1.0,
     "command_full_scale": 16384, "current_full_scale": 3.0,
     "max_output_rpm": 320.0, "max_current": 3.0, "max_torque": 1.5,
     "temp_warn": 55, "temp_limit": 70, "max_devices": 7},
]

# 保守默认参数（与 MCU DebugMotor::Init 一致）：页面预填，减少空输入
DEFAULT_PARAMS = {
    "kp_speed": 0.02, "ki_speed": 0.10, "kd_speed": 0.0, "kf_speed": 0.0,
    "il_speed": 1.5, "dz_speed": 0.0,
    "kp_angle": 0.5, "ki_angle": 0.0, "kd_angle": 0.0, "kf_angle": 0.0,
    "il_angle": 400.0, "dz_angle": 0.0,
    "target_rpm": 0.0, "target_deg": 0.0, "target_torque": 0.0,
}

SLOT_COUNT = 3
TELEM_PERIOD_S = 0.02
KEEPALIVE_S = 0.3
ACK_TIMEOUT_S = 1.0
LINK_TIMEOUT_MS = 500

_CRC_TABLE = []
for _i in range(256):
    _v = _i
    for _ in range(8):
        _v = ((_v << 1) ^ 0x07) & 0xFF if _v & 0x80 else (_v << 1) & 0xFF
    _CRC_TABLE.append(_v)


def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC_TABLE[crc ^ b]
    return crc


def encode(seq: int, msg_type: int, payload: bytes = b"") -> bytes:
    return bytes([HEAD0, HEAD1, msg_type, seq,
                  len(payload) & 0xFF, len(payload) >> 8]) + payload + \
        bytes([crc8(payload)])


def decode_telemetry(payload: bytes):
    """74B 载荷 → (3 槽字典, 链路标志)。"""
    slots = []
    for i in range(SLOT_COUNT):
        rpm, deg, cur, tq, temp, fault, mode, model, enabled = \
            struct.unpack_from("<4fhHBBB", payload, i * 24)
        slots.append({
            "rpm": rpm, "deg": deg, "current": cur, "torque": tq,
            "temperature": temp, "fault": fault, "mode": mode,
            "model": model, "enabled": enabled,
        })
    link = struct.unpack_from("<H", payload, 72)[0]
    return slots, link


FAULT_BITS = {0: "offline", 1: "temp_warn", 2: "over_temp", 3: "over_current",
              4: "cfg_rejected", 5: "link_lost", 6: "estop"}


class _AckWaiter:
    def __init__(self):
        self.event = threading.Event()
        self.result = None  # (status, slot)


class ShootManager:
    """串口协议会话：读线程 + 保活线程 + asyncio 广播。"""

    def __init__(self, opener=None):
        self._loop = None
        self._ser = None
        self._opener = opener or transports.usb_serial_opener
        self._gen = 0            # 连接代次：旧读线程退出不得清理新连接
        self._stop = threading.Event()
        self._reader = None
        self._keepalive = None
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._ack_lock = threading.Lock()
        self._ack_waiters = {}          # seq -> _AckWaiter
        self._clients = set()           # set[asyncio.Queue]
        self.port = None
        self.baud = config.SERIAL_BAUD_DEFAULT
        self.rtt_ms = None
        self.last_pong = 0.0
        self.telemetry = None           # 最近一帧解码结果
        self.telemetry_count = 0
        self._telem_history = deque(maxlen=200)

    # ---------- 生命周期 ----------

    def attach_loop(self, loop):
        self._loop = loop

    @property
    def is_open(self):
        return self._ser is not None and self._ser.is_open

    def open(self, port=None, baud=None):
        if self.is_open:
            self.close()
        self.baud = int(baud) if baud else config.SERIAL_BAUD_DEFAULT
        try:
            ser = self._opener(None if port in (None, "auto") else port, self.baud)
        except Exception as e:
            log.warning("发射调试: 打开失败: %s", e)
            return False
        if ser is None:
            log.warning("发射调试: 未找到可用串口")
            return False
        self._gen += 1
        gen = self._gen
        self._ser = ser
        self.port = getattr(ser, "port", str(port or "auto"))
        self._stop.clear()
        self._reader = threading.Thread(target=self._reader_loop, args=(gen,), daemon=True)
        self._keepalive = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._reader.start()
        self._keepalive.start()
        log.info("发射调试串口已打开: %s @ %d", self.port, self.baud)
        return True

    def close(self):
        port = self.port
        self._gen += 1              # 使旧读线程代次失效
        self._stop.set()
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self.port = None
        self.telemetry = None
        self.rtt_ms = None
        self._wake_all_waiters()
        if port:
            log.info("发射调试串口已关闭: %s", port)

    def status(self):
        return {
            "open": self.is_open,
            "port": self.port,
            "baudrate": self.baud,
            "rtt_ms": self.rtt_ms,
            "telemetry_count": self.telemetry_count,
            "telemetry": self.telemetry,
            "profiles": PROFILES,
            "modes": MODES,
            "fault_bits": FAULT_BITS,
            "defaults": DEFAULT_PARAMS,
        }

    # ---------- 事件 ----------

    def _broadcast(self, msg: dict):
        if not self._loop:
            return
        msg = {**msg, "ts": time.time()}
        for q in list(self._clients):
            def _put(q=q):
                try:
                    q.put_nowait(msg)
                except Exception:
                    try:                      # 满：丢最旧一条再入（遥测保最新）
                        q.get_nowait()
                        q.put_nowait(msg)
                    except Exception:
                        pass
            try:
                self._loop.call_soon_threadsafe(_put)
            except Exception:
                pass

    def subscribe(self):
        q = asyncio.Queue(maxsize=200)   # 有界：防慢客户端内存膨胀（§2.3 #6）
        self._clients.add(q)
        return q

    def unsubscribe(self, q):
        self._clients.discard(q)

    # ---------- 命令 ----------

    def _next_seq(self):
        with self._seq_lock:
            self._seq = (self._seq + 1) & 0xFF or 1
            return self._seq

    def _send(self, msg_type, payload=b""):
        if not self.is_open:
            return None
        seq = self._next_seq()
        try:
            self._ser.write(encode(seq, msg_type, payload))
        except Exception as e:
            log.warning("发射调试发送失败: %s", e)
            return None
        return seq

    def _wake_all_waiters(self):
        with self._ack_lock:
            for w in self._ack_waiters.values():
                w.event.set()
            self._ack_waiters.clear()

    def command(self, msg_type, payload=b""):
        """发送命令并等待 ACK（同步，供线程池端点调用）。"""
        if not self.is_open:
            return {"ok": False, "error": "串口未打开"}
        waiter = _AckWaiter()
        seq = None
        for _ in range(3):  # seq 冲突极小概率，重取
            seq = self._next_seq()
            with self._ack_lock:
                if seq not in self._ack_waiters:
                    self._ack_waiters[seq] = waiter
                    break
            seq = None
        if seq is None:
            return {"ok": False, "error": "seq 冲突"}
        try:
            self._ser.write(encode(seq, msg_type, payload))
        except Exception as e:
            with self._ack_lock:
                self._ack_waiters.pop(seq, None)
            return {"ok": False, "error": f"发送失败: {e}"}
        if not waiter.event.wait(ACK_TIMEOUT_S):
            with self._ack_lock:
                self._ack_waiters.pop(seq, None)
            return {"ok": False, "error": "ACK 超时"}
        if waiter.result is None:
            return {"ok": False, "error": "串口已关闭"}
        status, slot = waiter.result
        return {
            "ok": status == 0,
            "status": ACK_STATUS.get(status, f"unknown({status})"),
            "slot": slot,
        }

    def ping(self):
        if not self.is_open:
            return None
        t0 = time.monotonic()
        seq = self._send(T_PING)
        # Pong 无 waiter 机制：直接测往返（下一个 Pong 帧）
        # 简化：以命令 ACK 通道测 RTT —— Ping 也回 Ack? 不，Ping 只回 Pong。
        # 使用事件等待由 _reader 置位。
        ev = threading.Event()
        self._pong_event = (seq, ev)
        ok = ev.wait(ACK_TIMEOUT_S)
        self._pong_event = None
        if not ok:
            return None
        self.rtt_ms = round((time.monotonic() - t0) * 1000, 1)
        return self.rtt_ms

    _pong_event = None  # (seq, Event) 仅供 ping 测量

    # ---------- 业务封装 ----------

    def set_mode(self, slot, mode):
        return self.command(T_MODE, bytes([slot, mode]))

    def set_model(self, slot, model):
        return self.command(T_MODEL, bytes([slot, model]))

    def set_enable(self, slot, on):
        return self.command(T_ENABLE, bytes([slot, 1 if on else 0]))

    def set_param(self, slot, key, value):
        if key not in KEYS:
            return {"ok": False, "error": f"未知参数键 {key}"}
        if isinstance(value, bool):
            value = float(value)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return {"ok": False, "error": "参数值必须是数字"}
        return self.command(T_CFG_WRITE,
                            bytes([slot, KEYS[key]]) + struct.pack("<f", value))

    def set_target(self, slot, kind, value):
        key = {"rpm": "target_rpm", "deg": "target_deg",
               "torque": "target_torque"}[kind]
        return self.set_param(slot, key, value)

    def apply(self, slot):
        return self.command(T_CFG_APPLY, bytes([slot]))

    def estop(self):
        return self.command(T_ESTOP)

    def fault_clear(self, slot):
        return self.command(T_FAULT_CLEAR, bytes([slot]))

    # ---------- 后台线程 ----------

    def _keepalive_loop(self):
        """持续心跳：维持链路 up（MCU 看门狗 500ms，取 300ms 周期）。"""
        while not self._stop.is_set():
            if self.is_open:
                self._send(T_PING)
            self._stop.wait(KEEPALIVE_S)

    def _reader_loop(self, gen):
        buf = b""
        while not self._stop.is_set():
            try:
                data = self._ser.read(max(1, self._ser.in_waiting))
            except Exception:
                break
            if not data:
                continue
            buf += data
            buf = self._process(buf)
        self._broadcast({"type": "sys", "data": "发射调试串口连接已断开"})
        if gen == self._gen:        # 代次一致才清理（防止旧线程清掉新连接）
            self._ser = None
        # 串口死亡即清除陈旧遥测：否则 status 返回 open=False 但 telemetry
        # 仍是最后一帧，前端会继续渲染过期数据（在线电机不显示离线的根因）
        self.telemetry = None

    def _process(self, buf: bytes) -> bytes:
        while True:
            if len(buf) < 7:
                return buf
            if buf[0] != HEAD0 or buf[1] != HEAD1:
                buf = buf[1:]
                continue
            plen = buf[4] | (buf[5] << 8)
            if plen > 128:
                buf = buf[1:]
                continue
            if len(buf) < 7 + plen:
                return buf
            frame, buf = buf[:7 + plen], buf[7 + plen:]
            if crc8(frame[6:6 + plen]) != frame[6 + plen]:
                continue
            self._dispatch(frame[2], frame[3], frame[6:6 + plen])

    def _dispatch(self, msg_type, seq, payload):
        if msg_type == T_PONG:
            ev = self._pong_event
            if ev and ev[0] == seq:
                ev[1].set()
            self.last_pong = time.time()
            return
        if msg_type == T_ACK and len(payload) >= 3:
            with self._ack_lock:
                waiter = self._ack_waiters.pop(payload[0], None)
            if waiter:
                waiter.result = (payload[1], payload[2])
                waiter.event.set()
            self._broadcast({"type": "ack", "seq": payload[0],
                             "status": ACK_STATUS.get(payload[1], payload[1]),
                             "slot": payload[2]})
            return
        if msg_type == T_TELEM and len(payload) == 74:
            slots, link = decode_telemetry(payload)
            self.telemetry = {"slots": slots, "link": link}
            self.telemetry_count += 1
            self._telem_history.append(("t", time.time()))
            self._broadcast({"type": "telemetry", **self.telemetry})


