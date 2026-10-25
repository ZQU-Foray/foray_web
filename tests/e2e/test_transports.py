"""阶段 2 测试矩阵：传输抽象 + 按设备会话隔离 + 互斥 + 代次守卫。

全部软件级（假 TCP 固件服务器 / loop:// 环回），实机 TCP 行为见待实测清单。
"""
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, __import__("harness").REPO if "harness" in dir() else ".")

from harness import REPO
sys.path.insert(0, REPO)

from app import shoot as shoot_mod
from app import sessions, transports

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def fake_frame(msg_type, seq, payload=b""):
    return bytes([0xA5, 0x5A, msg_type, seq,
                  len(payload) & 0xFF, len(payload) >> 8]) + payload + \
           bytes([shoot_mod.crc8(payload)])


def make_telemetry(link=1):
    payload = b""
    for _ in range(3):
        # 4f(16)+h(2)+H(2)+B(1)=21B + model(1)+enabled(1)+填充(1) = 24B/槽
        payload += struct.pack("<4fhHB", 100.0, 5.0, 0.5, 0.2,
                               30, 0, 1) + bytes([0, 0, 0])
    payload += struct.pack("<H", link)
    return payload


class FakeFirmwareServer:
    """假 TCP 固件：响应 Ping→Pong、命令→Ack(ok)、持续推遥测帧。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.running = True
        self.received = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
            self.conn = conn
            # 遥测推送线程（20Hz，测试足够）
            def push():
                while self.running:
                    try:
                        conn.sendall(fake_frame(shoot_mod.T_TELEM, 0, make_telemetry()))
                    except Exception:
                        break
                    time.sleep(0.05)
            threading.Thread(target=push, daemon=True).start()
            # 命令响应
            buf = b""
            while self.running:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data
                while len(buf) >= 7:
                    plen = buf[4] | (buf[5] << 8)
                    if len(buf) < 7 + plen:
                        break
                    frame, buf = buf[:7 + plen], buf[7 + plen:]
                    mtype, seq = frame[2], frame[3]
                    payload = frame[6:6 + plen]
                    self.received.append((mtype, payload))
                    if mtype == shoot_mod.T_PING:
                        conn.sendall(fake_frame(shoot_mod.T_PONG, seq))
                    else:
                        # Ack: [seq, status=0 ok, slot]
                        ack = bytes([seq, 0, payload[0] if payload else 0])
                        conn.sendall(fake_frame(shoot_mod.T_ACK, seq, ack))
        except Exception:
            pass

    def stop(self):
        self.running = False
        # 必须也关闭已接受的连接，否则客户端收不到 EOF
        conn = getattr(self, "conn", None)
        if conn:
            try: conn.close()
            except Exception: pass
        try:
            self.sock.close()
        except Exception:
            pass


def run():
    print("== 传输抽象 ==")

    # loop:// 环回
    op = transports.make_loop_opener()
    s = op(None, 115200)
    s.write(b"hello")
    time.sleep(0.1)
    check("loop 写读回环", s.read(64) == b"hello")

    # TcpStream 读写 + EOF
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(1)
    tport = srv.getsockname()[1]
    def echo():
        c, _ = srv.accept()
        try:
            while True:
                d = c.recv(1024)
                if not d: break
                c.sendall(d.upper())
        except Exception: pass
    threading.Thread(target=echo, daemon=True).start()
    ts = transports.TcpStream("127.0.0.1", tport)
    ts.write(b"abc")
    time.sleep(0.2)
    check("TCP 写读（大写回显）", ts.read(64) == b"ABC")
    check("TCP in_waiting", ts.in_waiting == 0)
    ts.close()
    time.sleep(0.1)
    check("TCP close → is_open False", not ts.is_open)

    print("== 假 TCP 固件全链路 ==")
    fw = FakeFirmwareServer()
    time.sleep(0.1)

    sess_a = sessions.DeviceSession(
        {"id": "test-a", "transport": "vllink_tcp", "host": "127.0.0.1",
         "uart_port": fw.port}, loop=None)
    check("TCP 打开 shoot", sess_a.open_shoot())
    time.sleep(0.5)   # 等遥测
    st = sess_a.shoot.status()
    check("遥测解码（TCP）", st["telemetry"] is not None and
          st["telemetry"]["slots"][0]["rpm"] == 100.0,
          f"count={st['telemetry_count']}")
    rtt = sess_a.shoot.ping()
    check("TCP ping RTT", rtt is not None and rtt > 0, str(rtt))
    r = sess_a.shoot.set_mode(0, 1)
    check("TCP 命令 Ack ok", r["ok"] and r["status"] == "ok", str(r))
    # 急停属于安全语义：必须既有 ACK，又确实下发 T_ESTOP 帧（不是只返回成功）
    r = sess_a.shoot.estop()
    check("急停命令 Ack ok（安全相关）", r.get("ok") and r.get("status") == "ok", str(r))
    check("急停帧已下发到固件（T_ESTOP）",
          any(mt == shoot_mod.T_ESTOP for mt, _ in fw.received),
          str([mt for mt, _ in fw.received][-5:]))

    print("== 会话互斥与隔离 ==")
    # 同设备互斥：open serial（loop opener）→ shoot 被关闭
    sess_b = sessions.DeviceSession(
        {"id": "test-b", "transport": "usb", "probe_serial": "TEST-B"}, loop=None)
    loop_op = transports.make_loop_opener()
    sess_b.serial._opener = loop_op
    sess_b.shoot._opener = loop_op
    check("B 打开 shoot", sess_b.open_shoot())
    check("B shoot 在线", sess_b.shoot.is_open)
    # open_serial 应关闭 shoot
    ok = sess_b.open_serial()
    time.sleep(0.2)
    check("同设备互斥：serial 开 → shoot 关", not sess_b.shoot.is_open)

    # 不同设备隔离：A 不受 B 影响
    check("A 隔离不受影响", sess_a.shoot.is_open)

    # 代次守卫：快速 close→open，旧线程不清理新连接
    sess_c = sessions.DeviceSession(
        {"id": "test-c", "transport": "usb", "probe_serial": "TEST-C"}, loop=None)
    sess_c.shoot._opener = transports.make_loop_opener()
    sess_c.open_shoot()
    sess_c.shoot.close()
    sess_c.open_shoot()
    time.sleep(0.3)
    check("代次守卫：reopen 后仍在线", sess_c.shoot.is_open)

    fw.stop()
    time.sleep(1.5)   # TcpStream 的 rx 超时 0.05s + 读线程需要轮询周期
    # A 断线后状态
    check("固件断开 → A 失联", not sess_a.shoot.is_open or
          sess_a.shoot.telemetry is None)


if __name__ == "__main__":
    run()
    print(f"\n传输/会话: {len(PASS)} 通过, {len(FAIL)} 失败")
    raise SystemExit(1 if FAIL else 0)
