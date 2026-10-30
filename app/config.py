"""全局配置。所有路径/参数均可用环境变量覆盖。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------- 服务 ----------
HOST = os.environ.get("WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "8000"))

# ---------- 固件目录 ----------
FIRMWARE_DIR = Path(os.environ.get("FIRMWARE_DIR", str(BASE_DIR / "firmware")))
FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTS = {".elf", ".hex", ".bin"}
MAX_UPLOAD_BYTES = 64 * 1024 * 1024  # 64MB

# ---------- 预设指令 ----------
PRESETS_FILE = Path(os.environ.get("PRESETS_FILE", str(BASE_DIR / "config" / "presets.json")))

# ---------- 电机调参预设（按 槽位×型号 命名空间，Web 侧存储） ----------
MOTOR_PRESETS_FILE = Path(os.environ.get(
    "MOTOR_PRESETS_FILE", str(BASE_DIR / "config" / "motor_presets.json")))

# ---------- 设备注册表（多 STA 改造阶段 1） ----------
DEVICES_FILE = Path(os.environ.get(
    "DEVICES_FILE", str(BASE_DIR / "config" / "devices.json")))

# ---------- OpenOCD ----------
OPENOCD_BIN = os.environ.get("OPENOCD_BIN", "openocd")
# VLLINK fork（TCP-DAP 后端），服务器部署路径
OPENOCD_TCP_BIN = os.environ.get("OPENOCD_TCP_BIN", "/opt/openocd-vllink/bin/openocd")
OPENOCD_SCRIPTS = os.environ.get("OPENOCD_SCRIPTS", "/usr/share/openocd/scripts")
FLASH_TIMEOUT_SEC = int(os.environ.get("FLASH_TIMEOUT", "180"))

# ---------- 串口 ----------
SERIAL_BAUD_DEFAULT = int(os.environ.get("SERIAL_BAUD", "115200"))
SERIAL_PORT_DEFAULT = os.environ.get("SERIAL_PORT", "auto")  # auto = 自动选择

# ---------- 目标芯片（key 即前端选项值） ----------
TARGETS = {
    "stm32f407igh": {
        "label": "STM32F407IGHx (1MB Flash)",
        "cfg": "target/stm32f4x.cfg",
        "adapter_khz": 4000,
        "bin_base": 0x08000000,
    },
    "stm32h723vg": {
        "label": "STM32H723VGTx (1MB Flash)",
        "cfg": "target/stm32h7x.cfg",
        "adapter_khz": 4000,
        "bin_base": 0x08000000,
    },
}

# ---------- 调试器通道 ----------
# vllink/daplink 同走 CMSIS-DAP 接口，仅按设备序列号区分
PROBES = {
    "auto":    {"label": "自动 (优先 Vllink)", "interface": None, "kind": None},
    "vllink":  {"label": "Vllink Basic2 (无线)", "interface": "interface/cmsis-dap.cfg", "kind": "vllink"},
    "daplink": {"label": "DAPLink (USB)",        "interface": "interface/cmsis-dap.cfg", "kind": "daplink"},
    "jlink":   {"label": "J-Link (USB)",         "interface": "interface/jlink.cfg",    "kind": "jlink",
                # J-Link OB（尤其克隆版）在 2MHz+ 下 SWD 偶发 halt 超时，降速到 1MHz；
                # 烧录前先 reset init（板上 NRST 已接线）避免撞上运行中固件的低功耗/屏蔽调试窗口。
                "adapter_khz": 1000, "reset_init": True},
}

# ---------- 认证与访问控制（门户阶段 3，D1） ----------
AUTH_DIR = Path(os.environ.get("AUTH_DIR", str(BASE_DIR / "config" / "auth")))
AUTH_SESSIONS_FILE = Path(os.environ.get(
    "AUTH_SESSIONS_FILE", str(AUTH_DIR / "sessions.json")))
AUTH_EMERGENCY_FILE = Path(os.environ.get(
    "AUTH_EMERGENCY_FILE", str(AUTH_DIR / "emergency_users.json")))
# D2（阶段 9）：本地账号库与审计日志
AUTH_ACCOUNTS_FILE = Path(os.environ.get(
    "AUTH_ACCOUNTS_FILE", str(AUTH_DIR / "accounts.json")))
AUTH_AUDIT_FILE = Path(os.environ.get(
    "AUTH_AUDIT_FILE", str(AUTH_DIR / "audit.log")))
# 本地密码登录失败限频（与应急账号参数同量级，独立可调）
AUTH_LOCAL_MAX_FAIL = int(os.environ.get("AUTH_LOCAL_MAX_FAIL",
                                         os.environ.get("AUTH_EMERGENCY_MAX_FAIL", "5")))
AUTH_LOCAL_LOCK_SEC = int(os.environ.get("AUTH_LOCAL_LOCK_SEC",
                                         os.environ.get("AUTH_EMERGENCY_LOCK_SEC", "60")))
# 应急账号（D1 遗留）：D2 起**默认关闭**（无应急账号，维护者口径 5）。
# 置 1 仅供测试基建与历史用例过渡，生产不得开启。
AUTH_EMERGENCY_ENABLED = os.environ.get("AUTH_EMERGENCY_ENABLED", "0") not in (
    "0", "false", "no")
# 开通/重置流程的临时授权时长（秒）：飞书再验证后允许改密的窗口
AUTH_RESET_TTL_SEC = int(os.environ.get("AUTH_RESET_TTL", "600"))
SESSION_COOKIE = "dsh_session"
# HTTPS（阶段 9）：证书存在即认为走 TLS；Cookie 自动加 Secure（可显式覆盖）
TLS_CERT_FILE = os.environ.get("TLS_CERT_FILE", "")
TLS_KEY_FILE = os.environ.get("TLS_KEY_FILE", "")
SESSION_COOKIE_SECURE = os.environ.get(
    "SESSION_COOKIE_SECURE",
    "1" if (TLS_CERT_FILE and os.path.exists(TLS_CERT_FILE)) else "0"
) not in ("0", "false", "no")
SESSION_MEMBER_TTL_SEC = int(os.environ.get("AUTH_SESSION_MEMBER_TTL", str(12 * 3600)))
SESSION_EMERGENCY_TTL_SEC = int(os.environ.get("AUTH_SESSION_EMERGENCY_TTL", str(8 * 3600)))
RECHECK_INTERVAL_SEC = int(os.environ.get("AUTH_RECHECK_INTERVAL", "300"))
RECHECK_GRACE_SEC = int(os.environ.get("AUTH_RECHECK_GRACE", "300"))
WS_RECHECK_SEC = int(os.environ.get("AUTH_WS_RECHECK", "30"))
EMERGENCY_LOGIN_MAX_FAIL = int(os.environ.get("AUTH_EMERGENCY_MAX_FAIL", "5"))
EMERGENCY_LOGIN_LOCK_SEC = int(os.environ.get("AUTH_EMERGENCY_LOCK_SEC", "60"))
# 飞书接入（env 优先；否则读 FEISHU_CONFIG_FILE 指向的 json；均无 = 未配置）
FEISHU_API_BASE = os.environ.get("FEISHU_API_BASE", "https://open.feishu.cn")
FEISHU_CONFIG_FILE = Path(os.environ.get(
    "FEISHU_CONFIG_FILE", str(BASE_DIR / "config" / "feishu.json")))
OAUTH_STATE_TTL_SEC = int(os.environ.get("AUTH_OAUTH_STATE_TTL", "600"))

# ---------- 设备访问策略（审计 P1-5） ----------
# 越界 host 是否直接拒绝；设 0 时只记录告警放行，用于灰度观察
DEVICE_HOST_ENFORCE = os.environ.get("DEVICE_HOST_ENFORCE", "1").strip() not in ("0", "false", "False")
# 额外允许的网段，逗号分隔，例如 192.168.1.0/24,10.0.0.0/16
DEVICE_HOST_ALLOWLIST = os.environ.get("DEVICE_HOST_ALLOWLIST", "").strip()
# 局域网扫描冷却秒数（组长手动触发也要限频，避免被当成内网探测器）
SCAN_COOLDOWN_SEC = int(os.environ.get("SCAN_COOLDOWN_SEC", "60"))

# ---------- 门户同步与快照（阶段 4） ----------
PORTAL_DB = Path(os.environ.get("PORTAL_DB", str(BASE_DIR / "config" / "portal_snapshot.db")))
PORTAL_SYNC_INTERVAL_SEC = int(os.environ.get("PORTAL_SYNC_INTERVAL", "60"))
# 分页大小：飞书列表/搜索接口 page_size 上限 500，用满可把请求数降到 1/5
# （记录与字段元数据共用；回退到旧行为用 PORTAL_SYNC_PAGE_SIZE=100）
PORTAL_SYNC_PAGE_SIZE = min(500, max(1, int(os.environ.get("PORTAL_SYNC_PAGE_SIZE", "500"))))
PORTAL_SYNC_TIMEOUT_SEC = float(os.environ.get("PORTAL_SYNC_TIMEOUT", "10"))
# 字段元数据缓存时长（秒）：列名→field_id 对照几乎不变，缓存后每轮不再请求；
# 配置字段在缓存里缺失时会强制重拉一次再判定，所以改字段名不会被缓存挡住
PORTAL_FIELDS_CACHE_TTL_SEC = int(os.environ.get("PORTAL_FIELDS_CACHE_TTL", "21600"))
# 同步失败后的轮级退避：第 n 次连续失败等 interval*2^n，封顶本值（秒）
PORTAL_SYNC_BACKOFF_MAX_SEC = int(os.environ.get("PORTAL_SYNC_BACKOFF_MAX", "600"))
PORTAL_SYNC_RETRIES = int(os.environ.get("PORTAL_SYNC_RETRIES", "2"))
PORTAL_SYNC_BACKOFF_SEC = float(os.environ.get("PORTAL_SYNC_BACKOFF", "1.0"))
PORTAL_SYNC_STALE_SEC = int(os.environ.get("PORTAL_SYNC_STALE", "180"))
PORTAL_MANUAL_MIN_GAP_SEC = int(os.environ.get("PORTAL_MANUAL_MIN_GAP", "10"))
# 全局下限（阶段 8）：无论谁来点刷新，两次真实同步之间至少间隔这么久，
# 保证"前端人数不放大飞书请求"（按用户限频之外的兜底）
PORTAL_SYNC_GLOBAL_MIN_GAP_SEC = int(
    os.environ.get("PORTAL_SYNC_GLOBAL_MIN_GAP",
                   os.environ.get("PORTAL_MANUAL_MIN_GAP", "10")))
# 记录链接（阶段 5 源记录跳转）：官方 batch_get(with_shared_url=true) → shared_url
PORTAL_LINKS_ENABLED = os.environ.get("PORTAL_LINKS_ENABLED", "1") not in ("0", "false", "no")
PORTAL_LINK_BATCH = int(os.environ.get("PORTAL_LINK_BATCH", "100"))
# 同步开关（回退用）：False = 不启动后台同步、管理 API 只读既有快照
PORTAL_SYNC_ENABLED = os.environ.get("PORTAL_SYNC_ENABLED", "1") not in ("0", "false", "no")

# ---------- 本地缓存库写入（2026-10-21 第 3 步，设计见 docs/portal-cache-design.md） ----------
# cache（默认）= 网页写入先落本地队列、由推送线程推到飞书（页面不等飞书 RTT）；
# direct = 退回到直写飞书（旧行为，回滚用：Environment=PORTAL_WRITE_MODE=direct）
PORTAL_WRITE_MODE = (os.environ.get("PORTAL_WRITE_MODE") or "cache").strip().lower()
# 别名归一：规划/运维文档里"排队"模式也叫 queue，与 cache 同义
if PORTAL_WRITE_MODE == "queue":
    PORTAL_WRITE_MODE = "cache"
# cache 模式下走队列的表（逗号分隔），用于按表逐步切换。
# members 不在默认列表：考勤签到/签退与零点结算写的是 members 列，属关键路径，
# 推送失败会让工时账目不一致——这些写入保持直写飞书（仍可显式加进来）。
PORTAL_CACHE_TABLES = tuple(
    t.strip() for t in (os.environ.get("PORTAL_CACHE_TABLES")
                        or "tasks,projects,schedule,procurement").split(",")
    if t.strip())
# 推送线程开关：默认常开（direct 模式下队列通常为空，只空转查询）；关掉后队列不再推送
PORTAL_PUSH_ENABLED = os.environ.get("PORTAL_PUSH_ENABLED", "1") not in ("0", "false", "no")
# 队列空闲时多久查一次（秒）；有新写入会立即唤醒，不靠这个间隔
PORTAL_PUSH_IDLE_SEC = float(os.environ.get("PORTAL_PUSH_IDLE", "5"))
# 临时失败（网络/限流）的退避上限（秒）
PORTAL_PUSH_MAX_BACKOFF_SEC = float(os.environ.get("PORTAL_PUSH_MAX_BACKOFF", "300"))

# ---------- 前端（Vue 3 构建产物，2026-10 起） ----------
# web/ 是前端工程源码，构建产物（portal/debug/auth 三个入口 + assets/）入库到 static/app/；
# 服务器上没有 node，不在服务器构建。FORAY_APP_DIR 可指向别的产物目录（并行测试用）。
APP_DIR = Path(os.environ.get("FORAY_APP_DIR", str(BASE_DIR / "static" / "app")))
# 回退开关：PORTAL_FRONTEND=legacy 改回旧版原生前端（static/portal/、static/index.html），
# 只需改环境变量并重启服务，不用重新部署。
PORTAL_FRONTEND = os.environ.get("PORTAL_FRONTEND", "vue").strip().lower()
