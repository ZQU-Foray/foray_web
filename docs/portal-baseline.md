# 门户改造阶段 0 基线：当前多 STA 系统真实行为

> 编制日期：2026-09-08（晚间）· 执行者：阶段 0 AI（依据 `docs/team-portal-migration-plan.md` §8 阶段 0 与 §8.1 全部规则）
> 目的：固化现状供后续阶段引用；不实现门户功能、不改业务代码、不触硬件。
>
> **证据分级**（本文所有论断均标注来源）：
> - 📜 **历史文档**：`docs/current-behavior.md`（2026-09-06，单设备时代）、`docs/multi-sta-progress.md`（2026-09-06~09-08）。可能过时，仅作背景。
> - 🔍 **当前检查**：本次直接阅读当前源码逐行核对（文件与行号以当前工作区为准）。
> - ✅ **当前测试证据**：本次在隔离环境实际运行取得（见 §10 与 `docs/portal-progress.md`）。

---

## 1. 环境与版本控制现状

| 项 | 现状 | 证据 |
| --- | --- | --- |
| Git | **不可用**。本仓库及全部父目录（至 `~/RM`）均无 `.git`；`git status` 返回 not a git repository | 🔍 `git -C ~/RM/shoot-web status` → `fatal: not a git repository` |
| AGENTS.md | 本仓库无 AGENTS.md；最近一份位于 `~/RM/controller_code/AGENTS.md`，属另一项目，不适用 | 🔍 find 核查 |
| Python | `.venv` Python 3.13.5；fastapi 0.141.1 / uvicorn 0.52.4 / starlette 1.6.0 / pyserial 3.5 / pyudev 0.24.4 / pyusb 1.3.1 / websockets 17.1 / playwright 1.62.0 / paramiko 5.0.0 | 🔍 `.venv/bin/pip list` |
| 无头浏览器 | chromium-1234 + headless_shell 缓存于 `~/.cache/ms-playwright`；仓库自带运行库 `tests/e2e/browser-libs/`（16MB，libnss3/libX11/字体等） | 🔍 目录核查 |
| 父目录遗留备份 | `~/RM/shoot-web-backup-full-20260908-205950.tar.gz`、`shoot-web.zip`（维护者手工全量备份，非本阶段产物） | 🔍 |

### 1.1 阶段 0 文件快照（Git 不可用的回退依据）

| 文件 | 内容 | 校验 |
| --- | --- | --- |
| `backups/snapshot-phase0-baseline-20260908-210304.tar.gz` | 源码+文档+配置+部署+顶层文件，排除 `__pycache__`、`tests/e2e/browser-libs`（可再生依赖） | sha256 见同目录 `.sha256` 文件 |
| `backups/snapshot-phase0-baseline-20260908-210304.manifest` | 44 个文件的逐文件 sha256 清单 | `sha256sum -c` 全部 OK（本次已验证） |

**恢复方法**：`cd ~/RM/shoot-web && tar -xzf backups/snapshot-phase0-baseline-20260908-210304.tar.gz -C <目标目录>`（先校验 `sha256sum -c` manifest 与 tar 的 sha256）；需要精确回退单个文件时按 manifest 中的路径与哈希核对后单独覆盖。**不做全目录覆盖式回退**（§8.1 规则 3：不能递归删除不明目录）。

更早快照（📜 历史存在，本次未改动）：`backups/snapshot-pre-milestone1-20260906-212140.tar.gz`、`backups/snapshot-pre-single-sta-20260908.tar.gz`。

---

## 2. 系统架构现状（🔍 当前检查）

- 入口 `app/main.py`：FastAPI，lifespan 内完成 `sessions.init(loop)`（预热 legacy-default 会话）→ `presets.load()`（缺省生成默认文件）→ `device_registry.bootstrap_legacy()`（首启生成 legacy-default 设备）→ 启动 2 秒周期的 USB 设备轮询协程 `_device_poller`（pyudev 探针 + pyserial 串口枚举，结果存进程内 `_devices_cache`）。
- **会话模型**：`app/sessions.py` 的 `SessionStore` 是进程内字典（`device_id → DeviceSession`）。每个 `DeviceSession` 持有独立的 `SerialManager`（串口桥）、`ShootManager`（发射调试）、`FlashManager`（烧录）和一个预留的维护锁 `maint_lock`。**硬件状态驻留单进程内存——这是必须单 uvicorn worker 的根本原因**（计划 §2.1 已确认，代码注释亦明示）。
- **设备注册表**：`app/device_registry.py`，持久化 `config/devices.json`（原子写 `.tmp` → replace，写前备份 `.json.bak`）。稳定逻辑 ID（小写字母/数字/连字符 ≤32）；transport ∈ {usb, vllink_tcp}；USB 以 `probe_serial` 绑定物理端点、重复注册拒绝；`legacy-default` 首启自动生成且**禁止删除**；PATCH 白名单字段，id 与 transport 不可改。
- **传输抽象**：`app/transports.py`，USB 走 pyserial（`usb_serial_opener`），TCP 走自实现 `TcpStream`（socket + 接收线程模拟 pyserial 接口；断线 read 抛 OSError）。`opener_for_device(dev)` 按注册表选 opener；`make_loop_opener()` 供测试。
- **互斥**：同一设备内 `open_serial()` 先关 shoot、`open_shoot()` 先关 serial（REST 与 WS 共用 `DeviceSession` 统一入口）；不同设备互不影响。烧录另有全局信号量 `_FLASH_SEM`（默认 1，env `FLASH_CONCURRENCY` 可调）。
- **静态资源**：`app.mount("/", StaticFiles(static, html=True))` 挂载在**根路径**且必须最后挂载。中间件对非 `/api`、`/ws` 路径强制 `Cache-Control: no-cache`（ETag 304 协商）。

---

## 3. HTTP API 完整清单（🔍 逐行核对 `app/main.py`）

**鉴权现状：全部端点无任何鉴权**——无登录、无会话 Cookie、无 API Key、无 CSRF/Origin 校验、无 CORS 限制（服务绑定 `0.0.0.0`，systemd 部署监听 80，局域网内任意主机匿名拥有全部读写与硬件控制权）。阶段 3（D1）之前的现状即如此。

### 3.1 旧版（legacy-default 设备，前端 `deviceCtx` 在当前设备为 legacy-default 时使用）

| 方法 | 路径 | 行为 | 硬件/数据副作用 |
| --- | --- | --- | --- |
| GET | `/api/status` | 设备缓存、legacy 串口/烧录状态、目标芯片与调试器选项、固件目录 | 无（读） |
| GET | `/api/firmware` | 固件列表（全局，不按设备） | 无 |
| POST | `/api/firmware/upload` | 上传固件（multipart，≤64MB，白名单 .elf/.hex/.bin，防目录穿越） | 写服务器 `firmware/` |
| DELETE | `/api/firmware/{name}` | 删除固件（幂等） | 删服务器 `firmware/` 文件 |
| POST | `/api/flash` | 启动烧录 `{firmware,target,probe,address}`；busy 原子检查；3 次自动重试；180s 看门狗 | **强**：启动 OpenOCD 子进程烧写 MCU Flash 并 reset 运行 |
| GET | `/api/flash/log?since=` | 日志轮询（绝对单调 seq 游标，deque 2000 行） | 无 |
| GET/PUT | `/api/presets` | 指令预设读写（`config/presets.json`） | PUT 写配置文件 |
| POST | `/api/serial/open` | 打开串口（会先关闭本设备 shoot 会话） | 占用 USB/TCP 串口 |
| POST | `/api/serial/close` | 关闭串口 | 释放串口 |
| POST | `/api/serial/send` | 发送字节（b64/text） | **向目标 MCU 发任意字节** |
| GET | `/api/serial/history?since=` | 串口事件轮询（时间戳游标，内存 1000 条） | 无 |
| POST | `/api/shoot/open` / `close` | 发射调试串口开关（open 先关本设备 serial） | 占用串口 + 启动 300ms 保活心跳 |
| GET | `/api/shoot/status` | 状态、型号档案、模式表、默认参数、最近遥测 | 无 |
| POST | `/api/shoot/ping` | RTT 测量（Pong 事件等待 1s） | 向 MCU 发 Ping 帧 |
| POST | `/api/shoot/mode` / `model` / `enable` / `param` / `apply` | 电机控制命令（等 ACK） | **强**：`enable` 上电输出、`apply` 下发全部参数——真实电机可能立即动作 |
| POST | `/api/shoot/estop` | 急停帧（全部槽位闭锁输出） | 安全相关：断输出 |
| POST | `/api/shoot/fault_clear` | 清故障锁存 | 改变 MCU 故障状态 |
| GET/PUT | `/api/shoot/presets` | 电机预设（槽位×型号 ≤5 套，参数键白名单 20 键、有限数字校验） | PUT 写 `config/motor_presets.json` |

### 3.2 设备注册表 v2

| 方法 | 路径 | 行为 | 副作用 |
| --- | --- | --- | --- |
| GET | `/api/v2/devices` | 注册表列表 | 无 |
| POST | `/api/v2/devices` | 注册/按 id 覆盖（全字段校验） | 写 `config/devices.json` |
| GET | `/api/v2/devices/{id}` | 单设备档案 | 无 |
| PATCH | `/api/v2/devices/{id}` | 白名单字段更新（id/transport 不可改） | 写注册表 |
| DELETE | `/api/v2/devices/{id}` | 删除（legacy-default 拒绝） | 写注册表 |
| POST | `/api/v2/devices/{id}/probe` | 可达性探测：TCP 设备对 host 的 dap(4441)/uart(20010) 双端口 1.5s 连接测试；USB 设备查串口存在 | 发起出站 TCP 连接 |
| GET | `/api/v2/devices/{id}/vllink-config` | 读 Basic2 USB 配置（密码脱敏）+ 表单 schema | USB 控制传输读 |
| PATCH | `/api/v2/devices/{id}/vllink-config` | 字段合并写入 + commit + 回读验证（≤4096 字节） | **强**：改写 Basic2 设备持久配置（Mode/Wireless 变更需重启生效）。📜 实机曾返回 Pipe error，协议待调试（未验证） |
| POST | `/api/v2/devices/scan` | 扫描本机所在 /24（50 并发 × 0.5s 超时探测 4441+20010 双开 IP），返回未注册项 | 向 254 个内网 IP 发起 TCP 连接（网络扫描行为） |

路由匹配注意：`/api/v2/devices/scan` 为 POST，不被在先注册的 `GET/PATCH/DELETE /api/v2/devices/{device_id}` 吞掉（方法不同）；但 `GET /api/v2/devices/scan` 会被 `{device_id}` 路径参数匹配而返回 404「设备不存在」——前端未这样使用，属已知无害怪癖。

### 3.3 v2 按设备业务端点（`{id}` 换成具体设备；前端非 legacy-default 设备走这里）

| 方法 | 路径 | 行为 | 与旧版差异 |
| --- | --- | --- | --- |
| GET | `/api/v2/devices/{id}/status` | 同旧 status 但按设备会话 | — |
| POST | `/api/v2/devices/{id}/flash` + `GET .../flash/log?since=` | 同旧烧录，按设备 FlashManager；TCP 设备用 VLLINK fork OpenOCD（`cmsis-dap backend tcp`） | 日志游标同旧版 |
| POST | `/api/v2/devices/{id}/serial/open|close|send` + `GET .../serial/history` | 同旧串口，按设备 opener | TCP 设备 baud 仅作期望值记录，实际波特率由 Basic2 配置决定 |
| POST | `/api/v2/devices/{id}/shoot/open|close|ping` | 同旧发射调试 | — |
| POST | `/api/v2/devices/{id}/shoot/{action}` | action ∈ mode/model/enable/param/apply/estop/fault_clear（单条通配路由） | 未知 action → 404 |
| GET | `/api/v2/devices/{id}/shoot/status` | 同旧 | — |
| GET/PUT | `/api/v2/devices/{id}/shoot/presets` | 电机预设，**非默认设备存 `config/device-presets/<id>/motor_presets.json`** | 命名空间按设备隔离 |
| GET/PUT | `/api/v2/devices/{id}/presets` | 指令预设，同上按设备分文件 | 同上 |

固件管理（3.1 的 firmware 三条）**无设备维度**，全局共享——迁移时保持此契约。

## 4. WebSocket 清单（🔍）

均无鉴权（匿名可连）。服务端→客户端 JSON；客户端仅 `ws_serial` 可发指令。

| 路径 | 语义 | 客户端→服务端 | 服务端→客户端 |
| --- | --- | --- | --- |
| `/ws/flash` | legacy 烧录日志流；连接回放最近 ≤200 行 | 忽略（仅探测断连） | `{type:"log",stream,line,job,seq}` |
| `/ws/serial` | legacy 串口双向；订阅回放历史 ≤200 条 | `{type:"open"\|"close"\|"tx",port?,b64?}`；open/close 走会话层统一互斥 | `{type:"sys"\|"rx"\|"tx",b64?,data?,ts}` |
| `/ws/shoot` | legacy 遥测流；连接即推最新帧 | 忽略 | `{type:"telemetry"\|"ack"\|"sys",...}`（50Hz） |
| `/ws/v2/devices/{id}/flash` | 同上按设备 | 同上 | 同上 |
| `/ws/v2/devices/{id}/serial` | 同上按设备 | 同上（可触发 open/close/tx → 硬件副作用） | 同上 |
| `/ws/v2/devices/{id}/shoot` | 同上按设备 | 忽略 | 同上 |

**注意（阶段 3 设计输入）**：`/ws/serial` 与其 v2 版是仅有的客户端可发指令的 WS；烧录/遥测 WS 客户端只收。全部 WS 握手无 token、无 Origin 校验。

## 5. CLI 能力清单：`wsctl.py`（🔍 当前代码 + ✅ 冒烟证据）

纯标准库 HTTP 客户端（Python 3.8+），服务器地址取 `--server` 或环境变量 `WS_SERVER`（默认 `http://0.0.0.0` 仅本机）。

| 命令 | 后端端点 | 说明 |
| --- | --- | --- |
| `status [--json]` | GET /api/status | 人读/JSON 输出 |
| `flash <固件名> --target --probe [--no-wait] [--timeout 秒]` | POST /api/flash + GET /api/flash/log | 退出码 0=成功 1=失败 2=参数错 3=超时；日志实时回显 |
| `firmware list\|upload <file>\|rm <name>` | firmware 三端点 | upload 手工构造 multipart |
| `serial open [--port] [--baud]` / `close` | serial 端点 | — |
| `serial send (--text\|--hex\|--b64) [--nl crlf\|lf\|none]` | POST /api/serial/send | — |
| `serial read [--follow] [--timeout 秒]` | GET /api/serial/history | rx→stdout、sys/tx→stderr（可管道） |
| `presets get\|set [file]` | GET/PUT /api/presets | 不带 action 时 exit 2（argparse 用法错误，符合契约） |

**关键限制（本次确证的重要事实）**：
- `wsctl.py` **只调用旧版 `/api/*` 端点，只能操作 legacy-default 设备**。无 `--device` 参数、无 v2 调用。
- 📜 `docs/multi-sta-progress.md` 阶段 4（2026-09-07）记载「`wsctl.py`：--device 参数支持 v2 端点」，但当前代码及两份历史快照（`snapshot-pre-milestone1-20260906`、`snapshot-pre-single-sta-20260908`）中的 wsctl.py 均无该功能——**该记载与代码不符（从未落地或已丢失），以代码为准**。多设备 CLI 支持是阶段 3 的实际待办（计划 §6.6 已列）。
- ✅ 冒烟证据（本次隔离服务 127.0.0.1:8090 + 临时数据目录）：`status --json` exit 0；`firmware list` exit 0（临时目录空，输出「(无固件)」）；`presets get` exit 0（返回默认 JSON）。

## 6. 硬件副作用分级（阶段 3 访问矩阵与阶段 6 验收的基础）

| 级别 | 操作 | 说明 |
| --- | --- | --- |
| **强（写固件/动电机/改设备）** | POST flash（旧+全部 v2）；shoot enable/apply/mode/model/param/fault_clear；PATCH vllink-config | OpenOCD 烧写 MCU 并 reset；电机命令驱动真实输出；Basic2 配置持久化。**D1 落地前匿名即可触发** |
| **安全相关** | POST estop | 急停（闭锁输出）——授权模型里应保持低门槛可用（断电级安全兜底） |
| **中（占用链路/发字节）** | serial/shoot open/close、serial send、shoot ping、probe、scan | 占用串口/TCP 连接、向 MCU 发任意字节、扫描内网 |
| **弱（写服务器文件）** | firmware upload/delete、presets PUT、devices CRUD | 只动服务器本地文件，不动硬件 |
| **只读** | 全部 GET | — |

## 7. 会话与状态边界（必须保留的语义，计划 §2.2）

- `app/sessions.py` 管理的是**硬件设备会话**（串口/发射/烧录实例），与未来门户的**用户登录会话是两个概念**（计划 §2.1 明确；阶段 3 的用户会话不得混入本模块）。
- 同设备 serial↔shoot 互斥（REST/WS 对称，经由 `DeviceSession.open_serial/open_shoot`）。
- 连接代次（`_gen`）守卫：close→open 快速切换时旧读线程不得清理新连接。
- shoot 会话：300ms 保活心跳（MCU 看门狗 500ms）、命令 ACK 关联（1s 超时、seq 复用 1..255）、50Hz 遥测（74B 载荷/帧 `[A5 5A][type][seq][len16][payload][crc8]` 小端）、串口死亡即清空陈旧遥测。
- 前端滑杆 150ms 节流推送（UART 帧间空闲约束）。
- 烧录：同设备单任务（原子 busy 检查）、全局并发 1、3 次重试、180s 全程超时看门狗 kill、绝对 seq 日志游标、BIN 需显式/默认基地址（0x08000000）。
- 预设：指令预设（旧格式 `{port,baudrate,commands[]}`）与电机预设（`{motor_presets:{slot:{model:{presets[≤5],default}}}}`，参数键白名单 20 键）格式不变；非默认设备分文件存 `config/device-presets/<id>/`。

## 8. 静态资源与前端依赖（阶段 2 迁移 `/debug/` 的输入）

- 文件：`static/index.html`（单页四页签：固件烧录/串口调试/发射调试/设置）、`app.js`（671 行，deviceCtx + 烧录/串口/设置逻辑）、`shoot.js`（881 行，发射调试）、`chart.js`（490 行波形/CSV）、`dropdown.js`（196 行自绘下拉）、`style.css`、`favicon.png`。**无构建工程、无 CDN、无外部字体**——离线自足（计划 §6.6 的前提成立）。
- 相对引用：`href="style.css?v=20260908"`、`src="dropdown.js?v=..."` 等——**迁移到 `/debug/` 前缀时相对路径自动跟随，但 API/WS 地址是绝对路径 `/api/...`、`/ws/...`，不受影响；需核查刷新、直达链接与 `?v=` 缓存查询串**（计划 §2.1）。
- 设备上下文（`app.js` deviceCtx）：当前设备 `legacy-default` → 前缀 `/api`、`/ws`；其他设备 → `/api/v2/devices/<id>/`、`/ws/v2/devices/<id>/`。设备选择状态存 sessionStorage（`connection-mode`、`current-device`）。切设备时前端主动 close 旧设备 shoot 会话、清波形、过滤迟到响应。
- 版本查询串当前为 `?v=20260908`（📜 current-behavior.md 记载的 v=20250928 已过时）。

## 9. 配置目录、数据文件与部署

**配置/数据文件**（🔍 全部经 env 可覆盖，测试基建即依赖此）：

| 文件 | 用途 | env 覆盖 |
| --- | --- | --- |
| `config/presets.json` | 指令预设（legacy-default） | `PRESETS_FILE` |
| `config/motor_presets.json` | 电机预设（legacy-default） | `MOTOR_PRESETS_FILE` |
| `config/device-presets/<id>/*.json` | 非默认设备两类预设 | 随上两项路径派生 |
| `config/devices.json` | 设备注册表 | `DEVICES_FILE` |
| `firmware/` | 固件库（当前仅 .gitkeep） | `FIRMWARE_DIR` |
| 其他 env | `WEB_HOST/WEB_PORT`、`OPENOCD_BIN`、`OPENOCD_TCP_BIN`（默认 /opt/openocd-vllink）、`OPENOCD_SCRIPTS`、`FLASH_TIMEOUT`、`FLASH_CONCURRENCY`、`SERIAL_BAUD/SERIAL_PORT`、`SCAN_SUBNET` | `app/config.py` |

**启动与部署**：
- `run.sh`：单 uvicorn 进程（首次自动建 venv）。**不得为网页并发改多 worker**（§2.2）。
- `deploy/webserver.service`：root 用户、`/opt/stm32-webserver`、端口 80、`Restart=on-failure`。
- `deploy.py`（paramiko SSH 部署）：更新模式 `cp -a /tmp/stm32-extract/. {APP_DIR}/` 合并覆盖 + 重启服务。**🔍 与 📜 文档不一致**：`multi-sta-progress.md` 阶段 7 称「deploy.py：防覆盖设备注册表/预设/固件/配置快照」，但当前 `deploy.py` 的 `EXCLUDE_DIRS` 仅排除 `.venv/.git/__pycache__/.mypy_cache`，**不排除 `config/` 与 `firmware/`**——本地 `config/presets.json` 会打进包覆盖服务器版本；`devices.json`/`motor_presets.json`/`device-presets/` 因本地不存在而不被打包，且 `cp -a` 合并不删除服务器多出的文件（实际风险： presets.json 覆盖 + 理论上 firmware 同名覆盖）。2026-09-08 部署记载「配置哈希保持不变」是当时人工核对结果，非脚本保证。**阶段 7 必须实际修复 deploy.py 排除规则**（历史待办「顺手修复」不在本阶段范围）。

## 10. 当前测试证据（✅ 本次实际运行）

- 命令：`.venv/bin/python tests/e2e/run_all.py`（在仓库根执行；harness 自动使用临时目录的 FIRMWARE_DIR/PRESETS_FILE/MOTOR_PRESETS_FILE/DEVICES_FILE，绑定 127.0.0.1:8090，**不触碰真实配置与设备**；模拟设备 = 假 TCP 固件服务器 + playwright 路由拦截的有状态 mock）。
- 结果：**exit 0，97 项通过 / 0 失败**。分套件：API 契约 32、传输/会话 14、烧录 12、单STA 17、UI 22。
- 完整日志存档：`backups/phase0-e2e-run-20260908-210550.log`（sha256 `5547dfe4…d6f2a2d`）。
- 与 📜 历史记录对照：`multi-sta-progress.md`（2026-09-08）记载「97 项通过」——**本次复现实测一致**，故当前工作区代码与该历史验证时点的行为等价（至少在测试覆盖范围内）。
- CLI 冒烟（✅ 见 §5）：3 条命令 exit 0。
- 依赖状况：**无缺失**（requirements.txt 与 tests/requirements-dev.txt 全部已装，chromium 缓存在位），无需补装。

## 11. 已知问题与风险清单（阶段 0 发现，供后续阶段排期）

| # | 问题 | 等级 | 出处 | 处置建议 |
| --- | --- | --- | --- | --- |
| R1 | 全部 HTTP/WS/CLI 匿名可用，含烧录与电机使能 | 高（安全） | 🔍 | 阶段 3 D1 主任务；改造前服务仅靠局域网隔离 |
| R2 | `wsctl.py` 无多设备支持，📜 阶段 4 文档记载与代码不符 | 中 | 🔍+📜+快照核对 | 阶段 3 CLI 登录改造时一并补 `--device`；本文以代码为准 |
| R3 | `deploy.py` 无 config/firmware 防覆盖排除，📜 阶段 7 记载与代码不符 | 中 | 🔍+📜 | 阶段 7 实修；此前人工部署需避开 |
| R4 | 📜 `docs/current-behavior.md` 描述单设备时代架构（全局单例、「REST 全部单设备语义」、v=20250928），**已过时** | 低（文档债） | 🔍 | 后续阶段以本文与代码为准；不修改旧文（历史记录） |
| R5 | VLLINK USB 配置读写实机 Pipe error（端点 0 厂商传输被拒） | 中（硬件协议，**非本改造范围**） | 📜 2026-09-07 实测 | 保持待实测；阶段 6 单列，不顺手修 |
| R6 | TCP-DAP 烧录曾连上 STA 但无 MCU 时 `cannot read IDR`；接 MCU 全链路未验 | 中（待实机） | 📜 | 阶段 6 实机清单 |
| R7 | `/api/v2/devices/scan` 会主动扫描 /24 内网 | 低 | 🔍 | 引入鉴权后收敛；文档标注 |
| R8 | 进程内会话状态 = 单点；重启丢运行态（预设/注册表持久化不受影响） | 低（已知设计） | 🔍 | 保持单 worker；阶段 4 快照同理单进程 |
| R9 | 烧录与串口并发共享无线链路（TCP 场景），烧录期间建议暂停串口（代码已有提示日志） | 低 | 🔍 | 保持现状与提示语义 |

## 12. 下一阶段（阶段 1）输入

1. 本基线 §3/§4/§5 的路由全集即阶段 2「检查旧 /api、设备级 /api/v2、全部 WS 及 wsctl 路径」的核对底表；阶段 3 访问矩阵以 §6 副作用分级 + §4 WS 握手/指令通道为设计输入。
2. R2/R3 两处文档-代码不一致已定锚（以代码为准），阶段 3/7 分别处置。
3. 回归门槛：`tests/e2e/run_all.py` 97 项全绿为各阶段入口基线（`tests/README.md` 亦如此约定）。
4. 快照纪律：无 Git 环境下每阶段先做明确范围快照 + sha256 manifest（参照 §1.1 格式）。
5. 外部依赖待办（阶段 1 需维护者配置）：飞书自建应用、Base（当时四表，现为 8 张）、局域网回调地址——均未开始，属计划 §10.1 范围。
