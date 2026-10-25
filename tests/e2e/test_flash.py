"""阶段 3 专项：假 OpenOCD 进程验证成功/挂死超时/绝对游标/BIN 地址/TCP 命令构造。"""
import os
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from harness import REPO
sys.path.insert(0, REPO)

from app import config, flashing

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def make_fake_openocd(script):
    """创建假 OpenOCD 可执行脚本。"""
    path = os.path.join(tempfile.mkdtemp(), "openocd")
    with open(path, "w") as f:
        f.write(f"#!/bin/bash\n{script}\n")
    os.chmod(path, 0o755)
    return path


def run():
    print("== 阶段 3：烧录任务管理 ==")

    # 准备测试固件
    fw_dir = tempfile.mkdtemp()
    fw_elf = os.path.join(fw_dir, "test.elf")
    open(fw_elf, "w").write("fake elf")
    fw_bin = os.path.join(fw_dir, "test.bin")
    open(fw_bin, "w").write("fake bin")
    config.FIRMWARE_DIR = type(config.FIRMWARE_DIR)(fw_dir)

    # 1) 成功烧录（假 OpenOCD 输出成功标志）
    fake_ok = make_fake_openocd(
        'echo "** programming finished"; echo "** verified ok"; exit 0')
    orig_bin = config.OPENOCD_BIN
    config.OPENOCD_BIN = fake_ok
    config.FLASH_TIMEOUT_SEC = 5

    fm = flashing.FlashManager({"id": "test", "transport": "usb"})
    fm._loop = None
    try:
        job_id = fm.start("test.elf", "stm32h723vg")
        check("任务启动", job_id is not None)
        time.sleep(2)   # 等完成
        check("任务成功", fm.status()["state"] == "success")
    except Exception as e:
        check("任务启动", False, str(e))
    config.OPENOCD_BIN = orig_bin

    # 2) 绝对游标：创建 >2000 行的日志，验证游标不失效
    fm2 = flashing.FlashManager({"id": "test", "transport": "usb"})
    fm2._loop = None
    job = flashing.FlashJob("test", "test", "t", "stm32h723vg", "auto")
    for i in range(2500):
        job.seq += 1
        job.lines.append({"seq": job.seq, "t": time.time(), "s": "out", "line": f"line{i}"})
    fm2._job = job
    r1 = fm2.log_since(0)
    check("绝对游标 total", r1["next"] == 2500, str(r1["next"]))
    r2 = fm2.log_since(2400)
    check("绝对游标 since=2400 → 100 行", len(r2["lines"]) == 100,
          str(len(r2["lines"])))
    check("绝对游标 deque 满后 seq 连续",
          r2["lines"][0]["seq"] == 2401 and r2["lines"][-1]["seq"] == 2500)

    # 3) 挂死超时（假 OpenOCD 不输出不退出）
    fake_hang = make_fake_openocd("exec sleep 999")
    config.OPENOCD_BIN = fake_hang
    config.FLASH_TIMEOUT_SEC = 2     # 2 秒超时便于测试

    fm3 = flashing.FlashManager({"id": "test", "transport": "usb"})
    fm3._loop = None
    try:
        fm3.start("test.elf", "stm32h723vg")
        for _ in range(40):   # 轮询最多 20s
            time.sleep(0.5)
            st = fm3.status()
            if st["state"] != "running":
                break
        check("挂死超时→失败", st["state"] == "failed",
              f"state={st['state']} exit={st['exit_code']}")
    except Exception as e:
        check("挂死超时→失败", False, str(e))
    config.OPENOCD_BIN = orig_bin
    config.FLASH_TIMEOUT_SEC = 180

    # 4) BIN 地址：无地址 + 无默认 → 报错
    config.TARGETS["notarget"] = {"label": "x", "cfg": "x.cfg",
                                   "adapter_khz": 1000}   # 无 bin_base
    fm4 = flashing.FlashManager({"id": "t", "transport": "usb"})
    try:
        fm4.build_command(fw_bin, "notarget", "auto")
        check("BIN 无地址且无默认 → 拒绝", False)
    except ValueError:
        check("BIN 无地址且无默认 → 拒绝", True)
    del config.TARGETS["notarget"]

    # 5) BIN 地址：有默认 → 命令含地址
    cmd = fm4.build_command(fw_bin, "stm32h723vg", "daplink")
    check("BIN 默认地址 0x08000000", "0x08000000" in " ".join(cmd))

    # 6) BIN 地址：显式地址 → 命令含指定地址
    cmd = fm4.build_command(fw_bin, "stm32h723vg", "daplink", address=0x08010000)
    check("BIN 显式地址 0x08010000", "0x08010000" in " ".join(cmd))

    # 7) TCP-DAP 命令构造
    fm5 = flashing.FlashManager({"id": "sta-a", "transport": "vllink_tcp",
                                  "host": "192.168.1.100", "dap_port": 4441})
    cmd = fm5.build_command(fw_elf, "stm32h723vg", "vllink")
    cmd_str = " ".join(cmd)
    check("TCP 含 cmsis-dap backend", "cmsis-dap backend tcp" in cmd_str)
    check("TCP 含 host+port", "192.168.1.100" in cmd_str and "4441" in cmd_str)

    # 8) ELF 不需要地址
    cmd_usb = fm4.build_command(fw_elf, "stm32h723vg", "daplink")
    check("ELF 无地址", "0x08" not in cmd_usb[-1])


if __name__ == "__main__":
    run()
    print(f"\n烧录: {len(PASS)} 通过, {len(FAIL)} 失败")
    raise SystemExit(1 if FAIL else 0)
