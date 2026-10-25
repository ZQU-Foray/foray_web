# 多 STA 改造进度（里程碑一）

> 依据 `~/RM/shoot-web-multi-sta-plan.md` 分阶段推进；
> 软件通过与实机通过分列；快照：`backups/snapshot-pre-milestone1-*.tar.gz`。

## 阶段 0：基线与测试基建 ✅（2026-09-06）

### 变更
- `docs/current-behavior.md`：现行系统行为基线（API/UI/CLI/部署/协议契约/缺陷清单）
- `docs/vllink-capabilities.md`：VLLINK 能力核对表（证据分级，TCP 项标记未验证）
- `tests/`：端到端测试基建固化（阶段 0 评审补充意见 #1）
  - `tests/e2e/run_all.py` 一键回归：临时数据目录起独立服务 → API 契约 22 项 + UI 27 项
  - 有状态固件 mock（harness.FakeTelemetry：命令→遥测回显闭环）
  - 无头浏览器运行库入库（`tests/e2e/browser-libs/`，Debian .deb 解包，免系统安装）
- 修复计划 §2.3 #1：WS open 串口未关 shoot 会话（评审补充意见 #2，提前至阶段 0）

### 顺带修复（测试基建暴露的真实缺陷）
- 折叠容器 `.mcol-body/.subcol-body` 缺 `grid-template-columns: minmax(0,1fr)`：
  不可收缩分组撑出 min-content，480px 窄屏横向溢出
- `.shoot-body` 右列缺 `minmax(0,1fr)`：数值总览 `min-width:560` 撑宽整条网格轨道
- 测试基建自身：fonts.conf 换行转义、PUT 请求体未解析（页面逻辑无恙）

### 验证
- `tests/e2e/run_all.py` 全绿（API 22 + UI 27 = 49 项，0 失败）
- 未动任何生成文件；未触碰线上设备

### 待实测（不阻塞）
- TCP-DAP/TCP-UART 全部条目（见 vllink-capabilities.md 等级列）

## 阶段 1：设备注册与兼容路由 ✅（2026-09-06）

### 变更
- 新增 `app/device_registry.py`：稳定逻辑 ID 注册表（config/devices.json，
  原子写 + 备份）；id 正则校验；transport/target/probe_kind/capabilities 白名单；
  USB 以 probe_serial 绑定物理端点，重复注册拒绝；legacy-default 首启自动生成且不可删
- `app/config.py`：DEVICES_FILE 路径（env 可覆盖）
- `app/devices.py`：**choose_port 显式端口缺失 → 明确失败**（计划 §2.3 #2 修复，
  杜绝多目标环境误发）
- `app/main.py`：/api/v2/devices CRUD（GET 列表/GET 单个/POST 注册/PATCH 更新/
  DELETE 移除）；PATCH 仅放行 name/target/host/端口/能力/绑定，id 与 transport 不可改；
  lifespan 首启 bootstrap_legacy
- 测试：阶段 1 用例并入 run_all（注册/IP 更新 id 不变/非法 id/重复序列号/
  legacy 不可删/正常删/PATCH 白名单/choose_port 显式缺失失败）

### 验证
- run_all 全绿（API 30 + UI 27 = 57 项）；legacy API 行为不变（回归套件）

## 阶段 2：传输抽象与按设备会话 ✅（2026-09-06）

### 变更
- 新增 `app/transports.py`：统一字节流接口（usb_serial_opener / TcpStream / tcp_opener / loop_opener）
  - TcpStream.in_waiting 为 @property（对齐 pyserial 接口）
  - TCP 断线 read 抛 OSError（对齐 SerialException 语义驱动读线程退出）
- 新增 `app/sessions.py`：按设备会话（DeviceSession / SessionStore / 统一互斥 / 维护锁预留）
- 重构 serial_bridge.py / shoot.py：注入 opener、连接代次守卫、有界广播队列、移除全局单例
- 重构 main.py：全局管理器全部替换为 sessions.store().get("legacy-default")，互斥 REST/WS 统一

### 验证
- run_all 全绿 71 项（API 30 + 传输/会话 14 + UI 27）
- 假 TCP 固件：遥测解码 / Ping RTT / 命令 Ack / 互斥 / 隔离 / 代次守卫 / 断线检测 ✓

## 阶段 3：多设备烧录与任务隔离 ✅（2026-09-06）

### 变更
- 重构 `app/flashing.py`：
  - FlashManager 按设备实例化（由 DeviceSession 持有），移除全局单例
  - 原子 busy 检查 + 任务创建（threading.Lock，§2.3 #3）
  - 绝对日志游标：每行带单调递增 seq；`/api/flash/log?since=N` 返回 seq>N 的行，
    deque 满后游标不失效（§2.3 #3 修复）
  - 全程超时看门狗：Timer 线程 kill 进程，stdout 阻塞读收到 EOF 自然退出（§2.3 #4）
  - TCP-DAP 命令构造：cmsis_dap_backend tcp + cmsis_dap_uri tcp://IP:PORT（§2.3 #8，
    文档语法待实测）
  - BIN 基地址：显式 address 参数或目标默认 bin_base，缺失则拒绝（§2.3 #9）
  - 全局并发信号量（默认 1，FLASH_CONCURRENCY 环境变量可调）
- `app/config.py`：TARGETS 增加 bin_base（F407/H723 均为 0x08000000）
- `app/sessions.py`：DeviceSession 增加 flash = FlashManager(device)
- `app/main.py`：全部烧录端点走会话层；flash/log 使用绝对游标

### 验证
- run_all 全绿 83 项（API 30 + 传输/会话 14 + 烧录 12 + UI 27）
- 假 OpenOCD 覆盖：成功 / 挂死超时 kill / 绝对游标 deque 满 / BIN 地址 /
  TCP-DAP 命令构造 / ELF 无需地址

## 里程碑一完成 ✅

## 阶段 4-7 批量推进 ✅（2026-09-07）

### 阶段 4：前端设备上下文
- `static/app.js`：deviceCtx 模块（sessionStorage 持久化 / onChange 监听 / prefix 生成）
- `static/shoot.js`：全部 fetch/WS 绑定 deviceCtx.prefix；设备切换自动断开旧会话
- `static/index.html`：顶栏设备选择器（多设备时显示）
- `wsctl.py`：--device 参数支持 v2 端点
- `app/main.py`：v2 按设备全量端点（serial/shoot/flash/presets/WS）

### 阶段 5：VLLINK USB 配置
- `app/vllink_config.py`：PyUSB 控制传输 + 配置解析/合并/schema
- `app/main.py`：GET/PATCH vllink-config 端点（读→patch→写→回读验证）
- `requirements.txt`：+pyusb

### 阶段 7：部署与文档
- README：频段修正（5GHz）、目录结构补全、TCP-DAP fork 说明
- `deploy.py`：防覆盖设备注册表/预设/固件/配置快照
- 全量部署至 192.168.1.130

### 实测结果
- ✅ TcpStream → STA TCP-UART (20010)：连接/写入/断开
- ✅ OpenOCD TCP-DAP (4441)：VLLINK fork 编译成功、连接成功、识别 V00.54
- ⚠️ SWD 目标检测：`cannot read IDR`（无 MCU，预期失败）
- ❌ VLLINK USB 配置读写：端点 0 厂商传输返回 Pipe error
  → 设备拒绝端点 0 控制传输，协议可能走 vendor-specific 接口的 bulk 端点
  → 需对照 vllink_console 源码逐字节调试（标记待实测）

### 验证
- run_all 全绿 80 项（API 30 + 传输 14 + 烧录 12 + UI 24）
- 远程 v2 API：设备列表/shoot/flash/presets 按设备隔离 ✓

## 待完成
- VLLINK USB 配置协议调试（对照 vllink_console.js 源码）
- 接 MCU 后全链路实机验收（§9 验收矩阵）


## 单STA默认入口及连接修复（2026-09-08）

### 变更
- 页面默认「单STA」，保留「USB / AP」切换；扫描入口始终可达，支持手动登记 IPv4。
- 统一设备 API / WS 地址生成，修复错误路径中重复的 `/api/shoot`、`/ws/shoot`。
  原错误 POST 被静态资源路由处理，返回 405 Method Not Allowed。
- 烧录、普通串口、发射调试、RTT、状态、指令预设与电机预设绑定当前设备。
- 修复 STA 电机预设把设备 ID 当槽位校验而导致保存失败；独立文件复用原有槽位校验，
  默认 USB 文件格式保持不变。
- 单STA显示地址、端口与遥测等待说明，隐藏 USB 串口、波特率及调试器类型控件；
  实际 UART 参数明确由 Basic2 配置决定。
- 切设备过滤迟到响应与旧 WS，取消旧滑杆定时器，重置遥测和图表；
  已由本页连接的旧发射会话请求断开，查看新设备不自动应用默认预设。
- CSV 文件名带设备 ID；窄屏连接区换行，页签两列显示。

### 验证
- `tests/e2e/run_all.py`：全量 97 项回归通过，0 失败；其中新增 17 项单STA接口回归。
- 真实 HTTP / WebSocket 配合本机假 TCP 固件，验证打开、三槽遥测、RTT、断开、
  USB/另一STA隔离、电机预设读写与非法数据拒绝。
- 浏览器实际点击单STA「连接串口」：链路正常、遥测显示、RTT返回，未出现405或控制台错误。
- 浏览器检查480像素单STA布局无横向溢出，验证USB切换和非法IPv4提示。
- 原UI套件明确指定USB默认设备，原滑杆/预设/离线/图表与多尺寸排版回归全部通过。

### 边界与恢复
- 未部署线上服务、连接真实Basic2、执行真实电机动作或修改固件/CubeMX生成内容。
- 实机联调仍待验证；本轮不把模拟通过作为硬件验收通过。
- 修改前源码快照：`backups/snapshot-pre-single-sta-20260908.tar.gz`。

## 部署至130（2026-09-08）

- 目标：192.168.1.130，服务 webserver，目录 /opt/stm32-webserver。
- 已核对8个待更新文件与修改前快照一致，仅部署对应代码和静态资源。
- 重启成功；部署时配置哈希保持不变，固件、虚拟环境和systemd单元未覆盖。
- 按需求将 sta-bench 的显示名称改为「单STA」，地址192.168.1.142和端口4441/20010保持不变。
- 线上5份静态资源逐字节与本地一致，设备状态、发射状态和POST连接路由核验通过。
- 浏览器默认单STA、目标地址、连接按钮及控制台检查通过；未打开真实硬件控制会话。
- 回滚备份：/opt/stm32-webserver-backups/single-sta-20260908-154653/before.tar.gz。

## 线上修复：多设备下拉点不开 / 串口接收排障（2026-10-19）

### 现象
1. 电脑端“当前设备”有多个时下拉点不开（连接方式/目标芯片/调试器同样点不开）；
2. 串口调试页显示“串口已打开”但收不到任何 RX 数据，设备健康徽章仍显示在线。

### 根因
1. **自绘下拉组件回归（P0）**：`dropdown.js` 的 `openPanel()` 在面板仍为
   `position:absolute; top:calc(100%+4px)`（且挂在 `document.body` 下）时就 `hidden=false`，
   会把本无滚动条的页面瞬时撑出纵向滚动条 → `visualViewport.resize` 事件在点击后 2~3ms
   被派发 → `pinFixed()` 里注册的 `visualViewport resize → closePanel` 立刻把面板关掉。
   Playwright 无头默认 `--hide-scrollbars`，故既有 e2e 一直抓不到；真机/有头浏览器必现。
2. **多设备半选状态**：`deviceCtx.choose()` 仅在“唯一候选”或 sessionStorage 命中时自动选中；
   多台且无有效 `current-device` 时 `id=null`，页面标签仍显示第一台，但串口/烧录/发射按钮全禁用、
   不建立 WS——用户感知为“多个设备时用不了”。
3. **3 秒轮询清空标签**：`id=null` 时 `refreshStatus()` 每 3s 重建设备下拉，暂存值 `''`
   在关闭面板时被写回，触发器标签变空白/显示与真实值脱节。
4. **探测语义**：前端每 5s 的 `/probe` 会同时对 `uart_port(20010)` 发起 TCP 连接；
   对单客户端型 TCP-UART 设备存在顶掉/干扰正在使用的串口会话的风险，“在线”也只代表任一端口通。
5. 串口事件循环未绑定时 `_emit` 静默丢推送、异常分支无日志、`TcpStream` 无 `.port`，
   导致日志显示“串口已打开: auto”、现场无法判断数据到底有没有到服务器。

### 修复
- `static/dropdown.js`：展开前先落 `position:fixed` 再显示；`resize/visualViewport` 改为
  “尺寸真变化(>1px)才关闭”；`flushPending()` 丢弃已不存在的暂存值；点 `<label>` 文字也展开面板。
- `static/app.js`：`choose()` 无有效保存值时回退第一台并写入 sessionStorage；候选列表未变化
  不再每 3s 重建（新增 `sameSelectOptions`）；健康徽章按 DAP/会话状态描述；串口 RX 按行合并显示。
- `app/main.py`：`/probe` 只探 `dap_port`，UART 状态改由进程内会话状态回答；设备忙
  （串口/发射会话或烧录中）时不发起任何新连接；`/scan` 对活跃会话设备只探 4441。
- `app/serial_bridge.py` / `app/transports.py` / `app/sessions.py`：串口打开/关闭/异常/断线
  写服务端日志并显示真实端点（如 `192.168.1.227:20010`）；`status` 增加 `rx_bytes/last_rx_at`；
  `SessionStore.peek()` 供只读探测；事件循环未绑定首次告警。
- `static/index.html`：静态资源版本 `app.js/dropdown.js → ?v=20261019fix1`（生产缓存失效）。
- 测试：`tests/e2e/test_ui.py` 新增“多设备自动选中 / 无滚动条下拉不误关”用例（用
  `ignore_default_args=["--hide-scrollbars"]` 复现真实滚动条）；`test_single_sta.py` 新增
  “探测只连 DAP、不碰 UART、忙时会话不新建连接”用例；`harness.py` 改用
  `sys.executable -m uvicorn`，Windows 开发机可直接跑回归。

### 验证
- 本地回归子集（Windows + Edge）：API 32 + 传输/会话 16 + 单STA 26 + UI 45 = **119 项全部通过**。
- 生产部署（备份 `/opt/portal-backups/pre-20261019-fix-20261019-200416/`）后浏览器端到端
  **11/11 通过**：多设备自动选中、无滚动条下拉开合正常、可切到 STA·227、probe 只探 DAP、
  串口打开显示真实端点且跨过 5s 探测周期不断开。

### 现场结论（串口“收不到”）
生产实测：`192.168.1.142` 离线；`192.168.1.227` 的 TCP-UART 端口可连，但服务器直读 6s
**0 字节**，发射调试 Ping 超时、遥测计数不再增长——数据没有到达服务器，问题在
**MCU ↔ Basic2 上游**（MCU 未运行/未上电、UART 接线或波特率、Basic2 的 TCP-UART 转发配置；
固件 500ms 主机失联看门狗也会闭锁输出）。网页侧修复后可正常打开/选择设备，并在
`/api/v2/devices/<id>/status` 的 `serial.rx_bytes/last_rx_at` 直接判断数据是否到达。
