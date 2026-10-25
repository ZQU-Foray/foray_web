# Foray 战队管理门户 + STM32 局域网调试台

> **接手先读 `接续开发说明.md`**（运行、测试、部署与最近状态）；2026-10-25 源码抢救记录见 `docs/rescue-20261025.md`。
>
> **部署形态**：门户 + 调试台一体，部署在实验室 Linux 服务器上，域名端口转发到服务器 **80 端口**（无 HTTPS）；
> 调试台在 **`/debug/`**，需登录后访问，`/debug`（无尾斜杠）会 307 跳到 `/debug/`。另有一个预发/测试实例跑在 8000 端口。
>
> 仓库是公开的：真实域名、SSH 端口、服务器地址和飞书各类 ID 都不入库。文档里统一写作 `portal.example.com`、
> `<SSH端口>`、`192.168.1.x`、`<正式Base app_token>`、`<表ID>` 等占位，真实值见服务器 `/opt/portal/.env`、
> `config/feishu.json` 和本地 `config/deploy_server.json`。

在实验室局域网内通过浏览器完成：

1. **战队管理门户**：总览（重要节点倒计时、五组紧急事项、7 天日程、打卡工时排行）、
   项目/事项/日程管理与只读甘特、物资采购面板——数据同步自飞书多维表格
   （60 秒，失败保留旧快照；同步 7 张表：项目/事项/重要节点/公共日程/成员数据/
   花名册/物资采购）。物资采购（物资明细）含赛季/一二级分类/采购编号/采购日期/单价/
   数量/总价（飞书公式＝单价×数量）/采购人/发票状态与发票文件，可按赛季与分类筛选并
   合计金额；除「组长批复」仅组长可改外全员可编辑；写入即时回传飞书；字段口径与维护
   见 `docs/procurement-detail.md`；
2. **固件烧录**：服务器 USB 插入 Vllink Basic2（无线）/ DAPLink / J-Link，Web 页选择固件（ELF/HEX/BIN）一键烧录，实时日志回显；
3. **无线串口调试**：Basic2 的"USB CDC串口-无线"模式对主机透明 —— AP 端插服务器，STA 端 TXD/RXD 接 STM32，Web 页即可收发串口数据、执行预设指令。

## 登录（阶段 3 起，全部功能需登录）

- **飞书成员登录**：门户登录页「飞书登录」→ 组织成员自动核验（tenant_key 白名单），
  会话 12 小时，拥有管理与调试全部权限。
- **本地应急登录（仅调试）**：断网/飞书不可用时使用；凭据由维护者在服务器本地创建：
  `.venv/bin/python scripts/emergency_ctl.py create <用户名>`（交互输密码；轮换 `create`、
  撤销 `disable`；详见 `docs/portal-operations.md`）。应急身份**只能调试，不能查看管理数据**。
- **CLI**：`python wsctl.py login --method emergency`（密码交互输入）或
  `login --method feishu`；`logout`；业务命令自动携带会话；`--device <设备ID>` 走 v2 端点。

## 运维入口

- 本地缓存库与飞书双向同步（2026-10-21 起，进行中）：冲突规则、库版本 2 的表结构、迁移与后续步骤见
  `docs/portal-cache-design.md`；旧库升级用 `scripts/portal_migrate.py`（部署时自动执行）；
  写入模式开关 `PORTAL_WRITE_MODE`（默认 direct）与队列运维 `scripts/portal_queue.py` 见运维手册 §8.2；
- 前端（2026-10 起 Vue 3）：开发、构建、回退与架构记录见 `docs/frontend-vue.md`；
  **改前端后必须在 `web/` 执行 `npm run build` 并连同 `static/app/` 一起部署**。
- 运维手册：`docs/portal-operations.md`（上线形态/部署升级/备份恢复/凭据轮换/监控巡检/应急账号/
  飞书配置/回退与故障排查）；部署差异与发布清单：`docs/portal-deploy.md`；验收报告：
  `docs/portal-acceptance.md`；飞书配置指南：`docs/feishu-setup.md`；物资明细字段口径与
  维护：`docs/procurement-detail.md`；改造进度：`docs/portal-progress.md`。
- 部署与退回：`python deploy.py --dry-run` 自检；实际部署 `python deploy.py`（服务器地址、端口、账号与口令
  默认读 `config/deploy_server.json`，样例 `config/deploy_server.example.json`；也可用 `--host/--ssh-port/--user/--password`）；
  `python deploy.py --list-backups` 列出可退回的版本，`python deploy.py --restore <时间戳>` 退回到该次升级之前
  （旧调试台已不存在，`--rollback` 会拒绝执行）。
- 备份/恢复：`scripts/portal_backup.py --data-dir /opt/portal --out <目录> --keep 14 --tar` 与
  `scripts/portal_restore.py --bundle <目录|tar.gz> --target <目录>`（含逐文件 sha256 校验与
  快照库一致性副本；隔离演练见 `tests/e2e/test_backup_restore.py`）。
- 定时运维（阶段 8）：`portal-backup.timer` 每日 03:30 备份（保留 14 份）、`portal-health.timer`
  每 5 分钟巡检（退出码即告警契约：3 不可达 / 4 schema 不匹配 / 5 同步连续失败 / 6 超时未同步）；
  手册见 `docs/portal-operations.md` §F/§G。
- 回退开关：同步故障可 `PORTAL_SYNC_ENABLED=0` 停同步（调试与既有快照不受影响）。
- 运行全部端到端回归：`.venv/bin/python tests/e2e/run_all.py`（含 API/传输/烧录/单 STA/UI/门户/
  门户同步/认证/阶段 6 验收/阶段 7 备份恢复演练；0 失败 + SKIP 项独立计数，门槛见 `docs/portal-progress.md`）；
  门户截图可复现：`.venv/bin/python tests/e2e/screenshots.py`。

## 默认连接方式：单STA

页面默认进入「单STA」。服务器通过路由器直接访问一只 Basic2 STA，无需电脑侧 Basic2 AP。
在顶部扫描 STA，或填写 STA 的 IPv4 地址后点击「添加 / 选择」；已有多只 STA 时明确选择当前设备。
手动添加使用默认烧录端口 4441、串口端口 20010，已登记设备保留其端口设置。
USB 有线、AP 转发、DAPLink 与 J-Link 可在「连接方式」中切换至「USB / AP」。

> 顶部「扫描」调用 `POST /api/v2/devices/scan`：它会**主动探测服务器所在 /24 网段**的 4441/20010 端口（可用环境变量
> `SCAN_SUBNET` 限定网段，例如 `SCAN_SUBNET=192.168.1`）。该接口**必须登录**（匿名 401，仅调试身份可用），
> 已登记设备会在结果里标为 `registered`。

进入「发射调试」后点击「连接串口」。TCP 串口建立后仍需收到 MCU 遥测，才能显示链路正常。
Basic2 必须启用 TCP-UART，实际 UART 波特率需在 Basic2 上配置并与 MCU 一致；
网页在单STA下隐藏本地 USB 串口和波特率控件，避免误认为它能改写硬件参数。

当前设备同时决定烧录、串口、发射调试、RTT 和预设的操作目标。切换设备时清空旧波形，
CSV 文件名包含设备 ID。非默认设备的预设保存到对应预设文件旁的
`device-presets/<device-id>/` 子目录；旧 USB 默认设备沿用原文件。

## 架构

```
浏览器 ──HTTP/WebSocket──► FastAPI (Linux 服务器)
  烧录:  OpenOCD ──USB──► AP端(Basic2, CMSIS-DAP v2) ──无线──► STA端 ──SWD──► STM32
         OpenOCD ──USB──► DAPLink / J-Link（有线兼容，仅 interface cfg 不同）
  串口:  pyserial ──/dev/ttyACMx──► AP端 ──无线──► STA端 TXD/RXD ──► STM32 UART
  检测:  pyudev + pyserial（2s 轮询热插拔）
```

Basic2 的无线链路（5GHz 直连或经 WiFi 路由器的 LAN 模式）对服务器完全透明，
**两种拓扑服务端代码零差别**，按官方文档在两端配置即可。

## 目录结构

```
app/
  main.py           FastAPI 入口（REST + WebSocket + 静态页面）
  config.py         全部配置（可用环境变量覆盖）
  devices.py        USB 设备探测（pyudev / pyserial）
  flashing.py       OpenOCD 烧录任务管理（单任务、日志广播、环形缓冲回放）
  serial_bridge.py  串口桥（pyserial ↔ WebSocket，历史回放）
  firmware.py       固件列表/上传/删除（防目录穿越）
  presets.py        预设指令 JSON 配置
app/portal/         门户后端：同步 / 快照 / 只读 API / 计算
app/auth/           认证：飞书 OAuth / 会话 / 守卫 / 应急账号
web/                前端工程源码（Vue 3 + Vite + TS：门户 / 调试台 / 登录三个入口，设计稿 v5）
static/app/         前端构建产物（web/ 下 npm run build 生成，入库；服务器不装 node）
static/ui/          自托管字体与旧前端外壳（/ui/fonts 等，新前端继续使用；第三方字体与素材不入库，见文末）
static/、static/portal/  旧版原生前端（保留作回退：PORTAL_FRONTEND=legacy）
app/transports.py   统一字节流传输（USB / TCP UART）
app/sessions.py     按设备会话管理
app/device_registry.py 设备注册表
app/vllink_config.py VLLINK USB 配置读写
scripts/emergency_ctl.py  应急账号管理（创建/轮换/撤销）
scripts/feishu_verify.py  飞书配置自检
scripts/feishu_procurement_setup.py  物资采购表结构初始化（幂等补建明细字段）
scripts/99-vllogic.rules   udev 权限规则
tests/e2e/          端到端回归（run_all.py；含 test_acceptance.py 验收套件、screenshots.py 截图）
deploy/webserver.service    systemd 单元
run.sh              启动脚本（自动建 venv）
firmware/           固件存放目录（默认）
config/presets.json 预设指令（首次启动自动生成）
```

## 服务器部署（Ubuntu/Debian）

```bash
# 1. 依赖
sudo apt update
sudo apt install -y openocd python3-venv python3-pip
# 建议 OpenOCD >= 0.12；TCP-DAP 需 VLLINK fork（部署至 /opt/openocd-vllink）；Ubuntu 22.04 自带 0.11 可用但速度较慢，
# 更快可选官方 release 或 vllogic fork: https://github.com/vllogic/openocd_cmsis-dap_v2

# 2. 拷贝项目并安装 Python 依赖
sudo mkdir -p /opt/stm32-webserver && sudo cp -r . /opt/stm32-webserver/
cd /opt/stm32-webserver && sudo bash run.sh   # 或手动:
# python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. udev 权限（调试器 + CDC 串口）
sudo cp scripts/99-vllogic.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
# 重新插拔设备

# 4. 启动（二选一）
sudo bash run.sh                                              # 前台
sudo cp deploy/webserver.service /etc/systemd/system/         # systemd 常驻
sudo systemctl daemon-reload && sudo systemctl enable --now webserver

# 5. 浏览器访问 http://<服务器IP>（systemd 服务默认 80 端口，无需端口号）
```

环境变量（可选）：`WEB_PORT`、`FIRMWARE_DIR`、`PRESETS_FILE`、
`OPENOCD_BIN`、`OPENOCD_SCRIPTS`（默认 `/usr/share/openocd/scripts`）、
`FLASH_TIMEOUT`、`SERIAL_BAUD`、`SERIAL_PORT`。

## Vllink Basic2 配置要点

1. **配对**：两个 Basic2 分别双击按键切到 AP（通电 LED 闪两下）与 STA（闪三下）模式，
   靠近自动配对；AP 蓝灯常亮、STA 绿灯常亮即连接成功。
   恢复出厂：长按按键 10 秒。
2. **无线串口参数**：Chrome 打开官方配置页
   https://vllogic.com/_static/tools/web_config_basic2/ ，连接 AP 端，
   设置 `UART_Bridge_Host=115200.1p0.none`，同步设置。
3. **无线烧录**：STA 端 SWDIO/SWCLK/GND 接 STM32（VRef 与目标电平匹配），
   AP 端插服务器 USB，Web 页选 "Vllink Basic2" 直接烧录。
4. **无线串口**：STA 端 TXD/RXD/GND 接 STM32 串口，AP 端插服务器，
   Web 页"打开串口"即可收发。
5. **LAN 模式**（可选，覆盖更远）：按官方文档
   https://vllogic.github.io/example/over_local_area_network.html
   在两端配置 WiFi 接入点与 `Mode3_SERVER_IPV4`。服务端无需任何改动。

## 命令行控制（wsctl.py，供 agent/脚本自动化）

无需 SSH 登录服务器，本机通过 HTTP 即可控制（纯标准库单文件，Python 3.8+）：

```bash
python wsctl.py status                                          # 设备/服务状态（--json 输出原始 JSON）
python wsctl.py firmware list                                   # 固件列表
python wsctl.py firmware upload ./mc02.elf                      # 上传固件
python wsctl.py firmware rm mc02.elf                            # 删除固件
python wsctl.py flash mc02.elf --target stm32h723vg --probe jlink  # 烧录并实时回显日志
python wsctl.py serial open                                     # 打开无线串口（--port/--baud 可覆盖）
python wsctl.py serial send --text start --nl crlf              # 发送文本（--hex/--b64 可发字节）
python wsctl.py serial read --follow --timeout 10               # 持续读取（rx 数据走 stdout）
python wsctl.py serial close                                    # 关闭串口
python wsctl.py presets get / set <file.json>                   # 预设指令读写
```

- 服务器地址：`--server URL` 或环境变量 `WS_SERVER`（默认 `http://0.0.0.0` 仅指本机；远程使用需显式指定，如 `http://192.168.1.130`）
- 烧录退出码：`0`=成功，`1`=烧录失败，`2`=参数/接口错误，`3`=等待超时
- `serial read` 的接收数据输出到 stdout、系统消息输出到 stderr，方便脚本管道处理

## 使用说明

- **烧录**：`固件烧录`页选择目标芯片（F407IGHx / H723VGx）、调试器（自动/Vllink/DAPLink/J-Link）、
  固件，点开始烧录。日志区实时显示 OpenOCD 输出，成功后自动 reset 运行。
  固件可在 `设置`页上传，或直接放入服务器 `firmware/` 目录。
- **串口调试**：`串口调试`页打开串口后，可点预设指令按钮或手动输入发送；
  绿色=收到的数据，蓝色=已发送，灰色=系统消息。勾选 HEX 可发送十六进制字节。
- **预设指令**：`设置`页编辑 JSON（或服务器上直接改 `config/presets.json`）：
  ```json
  {
    "port": "auto",
    "baudrate": 115200,
    "commands": [
      {"id": "start", "name": "启动", "data": "start", "encoding": "text", "newline": "crlf"},
      {"id": "setp",  "name": "设参数", "data": "AA 01 64", "encoding": "hex", "newline": "none"}
    ]
  }
  ```
- **发射调试**：`发射调试`页连接串口后可调试 3 个发射机构电机（M2006/M3508/GM6020C）：
  型号/模式切换、PID 参数下发、目标转速/角度/力矩、使能与急停；
  遥测 50Hz 实时刷新，顶栏显示链路状态与 RTT 往返延时。与`串口调试`页互斥占用串口。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 设备/串口/烧录状态、目标与调试器选项 |
| GET | `/api/firmware` | 固件列表 |
| POST | `/api/firmware/upload` | 上传固件（multipart, ≤64MB） |
| DELETE | `/api/firmware/{name}` | 删除固件 |
| POST | `/api/flash` | 启动烧录 `{firmware, target, probe}` |
| GET/PUT | `/api/presets` | 读取/保存预设指令 |
| WS | `/ws/flash` | 烧录日志流（重连自动回放） |
| WS | `/ws/serial` | 串口双向流（`open`/`close`/`tx{b64}`） |

## 常见问题

- **OpenOCD 打不开设备**：检查 udev rules 是否重载、设备是否重新插拔；
  `lsusb` 应能看到 `1209:6666`（Basic2）或 `0d28:0204`（DAPLink）或 `1366:xxxx`（J-Link）。
- **找不到 scripts**：确认 `OPENOCD_SCRIPTS` 指向 openocd 安装的 scripts 目录
  （`dpkg -L openocd | grep stm32f4x.cfg` 可查）。
- **H7 芯片擦除/烧写失败**：H723 双 bank，若用非官方 OpenOCD 版本建议升级 0.12+。
- **Windows 开发机**：无 pyudev 时自动降级为仅串口枚举，前端可正常调试；
  指定 `OPENOCD_BIN` 指向本地 openocd.exe 也能走通烧录流程。

## 许可证

本项目代码以 [MIT 许可证](LICENSE) 发布，版权归肇院Foray战队。

MIT 只覆盖本项目自己的代码与文档，不覆盖仓库里随附的第三方字体（各自的授权见 `static/ui/fonts/FONTS.md`）：

| 字体 | 用途 | 授权 |
| --- | --- | --- |
| Foray Sans（由 Noto Sans SC 分包改名） | 中文与正文，全站主字体 | SIL OFL 1.1 |
| Space Grotesk | 技术标签；数字的后备字体 | SIL OFL 1.1 |
| JetBrains Mono | 日志、串口与代码 | SIL OFL 1.1 |

以下第三方字体与素材**不入库**（授权不允许公开分发，或版权属于他方），只在本地工作区和生产服务器上保留：
Gilroy、Novecento Wide、HarmonyOS Sans SC 子集（`static/ui/fonts/`）、终末地官方纹理（`static/ui/assets/`）、
设计预览稿 `design-v3/v4/v5-preview/` 与设计参考 `参考/`。`deploy.py` 按工作区打包，本地有这些文件时照常部署；
从新克隆的仓库部署时，数字与宽体标签自动回退到 Space Grotesk / Foray Sans，旧前端少几处背景纹理，功能不受影响。
