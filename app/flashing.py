"""OpenOCD 烧录任务管理（阶段 3：按设备实例 + 绝对游标 + 全程超时 + TCP-DAP + BIN 基地址）。

修复计划 §2.3：
- #3 busy 检查与任务创建原子化（threading.Lock）；日志绝对序号游标
- #4 全程超时看门狗（Timer 线程 kill 进程，stdout 阻塞不再导致挂死）
- #8 TCP 后端命令构造（OpenOCD ≥0.12 文档语法，标记待实测）
- #9 BIN 文件需显式基地址或使用目标默认地址
"""
import asyncio
import logging
import subprocess
import threading
import time
import uuid
from collections import deque

from . import config, devices, firmware as firmware_store


def _tcl_arg(value):
    """将外部参数限制为单个字面量，防止进入调试器命令语法。"""
    value = str(value)
    if any(c in value for c in '{}\\') or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("调试器参数包含非法字符")
    return "{" + value + "}"

log = logging.getLogger(__name__)

SUCCESS_MARKS = ("** verified ok", "** programming finished")

# 全局并发信号量：默认 1，可通过环境变量 FLASH_CONCURRENCY 调整
_FLASH_SEM = threading.Semaphore(int(config.__dict__.get("FLASH_CONCURRENCY", 1)))


class FlashJob:
    def __init__(self, job_id, device_id, firmware, target_key, probe_key, address=None):
        self.id = job_id
        self.device_id = device_id
        self.firmware = firmware
        self.target_key = target_key
        self.probe_key = probe_key
        self.address = address
        self.state = "running"  # running | success | failed
        self.exit_code = None
        self.started = time.time()
        self.seq = 0                   # 绝对日志序号（单调递增，deque 满后不失效）
        self.lines = deque(maxlen=2000)


class FlashManager:
    """按设备实例；同设备单任务，全局并发受限。"""

    def __init__(self, device=None):
        self.device = device            # 注册表设备记录（含 transport/host 等）
        self._loop = None
        self._job = None
        self._lock = threading.Lock()   # 原子 busy 检查 + 任务创建（§2.3 #3）
        self._subscribers = set()

    def attach_loop(self, loop):
        self._loop = loop

    @property
    def busy(self):
        return self._job is not None and self._job.state == "running"

    def status(self):
        j = self._job
        if not j:
            return {"state": "idle"}
        return {
            "state": j.state, "firmware": j.firmware,
            "target": j.target_key, "probe": j.probe_key,
            "exit_code": j.exit_code,
            "elapsed": round(time.time() - j.started, 1),
            "tail": [l["line"] for l in list(j.lines)[-50:]],
        }

    def _push(self, job, stream, line):
        job.seq += 1
        entry = {"seq": job.seq, "t": time.time(), "s": stream, "line": line}
        job.lines.append(entry)
        msg = {"type": "log", "stream": stream, "line": line,
               "job": job.id, "seq": job.seq}
        for q in list(self._subscribers):
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, msg)
            except Exception:
                pass

    # ---------- 命令构造 ----------

    def build_command(self, firmware_path, target_key, probe_key, address=None):
        target = config.TARGETS[target_key]
        probe = config.PROBES[probe_key]
        is_tcp = (self.device and self.device.get("transport") == "vllink_tcp")

        khz = probe.get("adapter_khz") or target["adapter_khz"]
        # TCP 用 VLLINK fork 二进制；USB 用系统 OpenOCD
        openocd_bin = (config.OPENOCD_TCP_BIN if is_tcp else config.OPENOCD_BIN)
        cmd = [openocd_bin, "-s", config.OPENOCD_SCRIPTS,
               "-f", probe["interface"],
               "-c", "transport select swd"]

        if is_tcp:
            # TCP-DAP（OpenOCD ≥0.12 文档语法，待实机验证——docs/vllink-capabilities.md）
            host = self.device.get("host", "")
            dap_port = self.device.get("dap_port", 4441)
            if type(dap_port) is not int or not 1 <= dap_port <= 65535:
                raise ValueError("调试器端口必须为 1 至 65535 的整数")
            # 实测语法（2026-09-07 VLLINK fork OpenOCD 0.12.0+dev 连接成功）
            cmd += ["-c", f"cmsis-dap backend tcp",
                    "-c", f"cmsis-dap tcp host {_tcl_arg(host)}",
                    "-c", f"cmsis-dap tcp port {dap_port}",
                    "-c", f"adapter speed {khz}"]
        else:
            # USB（现行路径）
            pre_init = [f"adapter speed {khz}"]
            dev = devices.find_probe(probe.get("kind"))
            if probe_key in ("vllink", "daplink") and dev and dev.get("serial"):
                pre_init.append(f"cmsis_dap_serial {_tcl_arg(dev['serial'])}")
            elif probe_key == "jlink" and dev and dev.get("serial"):
                pre_init.append(f"jlink serial {_tcl_arg(dev['serial'])}")
            cmd += ["-c", "; ".join(pre_init)]

        cmd += ["-f", target["cfg"]]

        if probe.get("reset_init"):
            cmd += ["-c", "init", "-c", "reset init", "-c", f"adapter speed {khz}"]

        # BIN 需显式基地址（§2.3 #9）；ELF/HEX 由文件自带地址
        prog = f"program {_tcl_arg(firmware_path)}"
        if firmware_path.lower().endswith(".bin"):
            if address is not None and (type(address) is not int or not 0 <= address <= 0xFFFFFFFF):
                raise ValueError("固件基地址必须为有效的无符号整数")
            addr = address or target.get("bin_base")
            if not addr:
                raise ValueError(
                    f"BIN 文件需要显式基地址（目标 {target_key} 未定义默认地址）")
            prog += f" 0x{addr:08X}"
        prog += " verify reset exit"
        cmd += ["-c", prog]
        return cmd

    # ---------- 任务启动 ----------

    def start(self, firmware, target_key, probe_key="auto", address=None):
        with self._lock:                          # 原子 busy + 创建（§2.3 #3）
            if self.busy:
                raise RuntimeError("已有烧录任务在进行中")
            if probe_key == "auto":
                is_tcp = (self.device and self.device.get("transport") == "vllink_tcp")
                if is_tcp:
                    probe_key = "vllink"
                else:
                    dev = devices.find_probe(None)
                    probe_key = dev["kind"] if dev else "daplink"
            if target_key not in config.TARGETS or probe_key not in config.PROBES:
                raise ValueError("未知的目标芯片或调试器类型")
            path = firmware_store._safe_path(firmware)
            if not path.is_file():
                raise ValueError(f"固件不存在: {firmware}")
            cmd = self.build_command(str(path), target_key, probe_key, address)
            job = FlashJob(uuid.uuid4().hex[:8],
                           (self.device or {}).get("id", "legacy-default"),
                           firmware, target_key, probe_key, address)
            self._job = job

        self._push(job, "sys", f"$ {' '.join(cmd)}")
        self._push(job, "sys", "提示: 无线烧录与串口共用无线链路，烧录期间建议暂停串口收发")
        threading.Thread(target=self._run, args=(job, cmd), daemon=True).start()
        return job.id

    def _run(self, job, cmd):
        ok = False
        proc = None
        for attempt in range(1, 4):
            if attempt > 1:
                self._push(job, "sys", f"自动重试 第 {attempt}/3 次（1.5 秒后）...")
                time.sleep(1.5)
            start_seq = job.seq

            _FLASH_SEM.acquire()
            try:
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, errors="replace", bufsize=1)
                except Exception as e:
                    self._push(job, "sys", f"启动 OpenOCD 失败: {e}")
                    continue

                # 全程超时看门狗（§2.3 #4）：Timer 线程 kill 进程，
                # stdout 阻塞读收到 EOF 后迭代自然退出
                def _kill():
                    try:
                        proc.kill()
                        self._push(job, "sys", "烧录超时，进程已终止")
                    except Exception:
                        pass
                killer = threading.Timer(config.FLASH_TIMEOUT_SEC, _kill)
                killer.start()
                try:
                    for line in proc.stdout:
                        self._push(job, "stdout", line.rstrip("\r\n"))
                    proc.wait()
                finally:
                    killer.cancel()
            finally:
                _FLASH_SEM.release()

            joined = "\n".join(l["line"].lower()
                               for l in list(job.lines) if l["seq"] > start_seq)
            ok = (proc is not None and proc.returncode == 0
                  and any(m in joined for m in SUCCESS_MARKS))
            if ok:
                break
            if attempt < 3:
                self._push(job, "sys",
                           f"本次尝试失败 (exit={proc.returncode if proc else -1})")

        job.exit_code = proc.returncode if proc else -1
        job.state = "success" if ok else "failed"
        self._push(job, "sys",
                   f"=== 烧录{'成功' if ok else '失败'} "
                   f"(exit={job.exit_code}, 用时 {time.time() - job.started:.1f}s) ===")

    # ---------- 日志（绝对游标） ----------

    def log_since(self, since=0):
        """返回 seq > since 的行（绝对游标，deque 满后不失效——§2.3 #3 修复）。"""
        j = self._job
        if not j:
            return {"state": "idle", "lines": [], "next": 0,
                    "exit_code": None, "elapsed": None}
        lines = [{"seq": l["seq"], "s": l["s"], "line": l["line"]}
                 for l in list(j.lines) if l["seq"] > since]
        return {"state": j.state, "exit_code": j.exit_code,
                "elapsed": round(time.time() - j.started, 1),
                "lines": lines, "next": j.seq}

    # ---------- WebSocket ----------

    def subscribe(self):
        q = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        self._subscribers.discard(q)
