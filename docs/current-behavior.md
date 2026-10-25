# 现行系统行为基线（阶段 0 产出）

> 记录日期：2026-09-06 · 范围：shoot-web 全部现有能力，作为多 STA 改造的兼容基准。
> 本文档只描述现状，不含改造设计；改造设计见 `shoot-web-multi-sta-plan.md`。

## 1. 服务架构

- 单 uvicorn worker：`app/main:app`，`run.sh` 启动（默认 0.0.0.0:8000，`WEB_PORT` 覆盖）
- 全局单例：`flash_manager` / `serial_manager` / `shoot_manager`（设备会话互斥见 §5）
- 设备轮询：`_device_poller` 每 2s 刷新 USB 探针/串口缓存（pyudev + pyserial）
- 静态资源：`/` 挂载 `static/`，响应带 `Cache-Control: no-cache` 中间件（ETag 304 协商）

## 2. REST API（全部为单设备语义）

| 路径 | 行为 |
| --- | --- |
| GET /api/status | 设备列表、串口/烧录状态、目标芯片与调试器选项、固件目录 |
| GET /api/firmware · POST upload · DELETE /{name} | 固件库管理（ELF/HEX/BIN，≤64MB，防目录穿越） |
| POST /api/flash | 启动烧录 `{firmware, target, probe}`；单任务锁，3 次重试 |
| GET /api/flash/log?since= | 烧录日志轮询（游标=行号，deque 上限 2000 后游标语义退化——已知缺陷） |
| GET/PUT /api/presets | 串口指令预设（config/presets.json） |
| POST /api/serial/{open,close,send} · GET history | 原始串口（与 shoot 互斥，REST 侧） |
| POST /api/shoot/{open,close,status,ping,mode,model,enable,param,apply,estop,fault_clear} | 发射调试（与原始串口互斥） |
| GET/PUT /api/shoot/presets | 电机预设（槽位×型号×≤5 套 + 默认预设，服务端校验白名单 19 键） |

## 3. WebSocket

| 路径 | 行为 |
| --- | --- |
| /ws/flash | 烧录日志流 + 断线重连回放最近 200 行 |
| /ws/serial | 串口双向（open/close/tx 指令；订阅时回放历史 200 条） |
| /ws/shoot | 50Hz 遥测流（连接即推最新帧） |

已知不对称（阶段 0 修复）：REST open 串口会关闭 shoot 会话；**WS open 分支此前未做同等互斥**，已于阶段 0 补齐。

## 4. 前端（单页四页签，无框架，无暗色主题）

- **固件烧录**：芯片/调试器/固件选择 + 实时日志；**串口调试**：文本/HEX 收发、预设指令按钮、历史回放；**发射调试**：三电机折叠面板（型号/模式/目标滑杆 150ms 节流推送/预设三级命名空间/使能流/离线渲染/链路恢复重推默认预设）+ 波形区（按数据类型分图、FLIP 缩放、数值总览、CSV 导出、开始/停止录制）；**设置**：指令预设 JSON、固件管理
- 组件：自绘下拉（dropdown.js，包裹原生 select，value 拦截器含忙时暂存）、开机引导盖、跑马灯水印
- 静态资源版本化 URL（`?v=`），当前 v=20250928

## 5. 会话与互斥现状

- 原始串口与发射调试共享同一 USB CDC 端口：open 任一方先 close 另一方（REST 双向对称；WS 侧阶段 0 补齐）
- shoot 会话：300ms 保活心跳 + ACK 关联（1s 超时）+ 50Hz 遥测解码（74B 载荷）+ 链路位
- 串口异常断开时清空陈旧遥测（`telemetry=None`），前端据此渲染离线

## 6. CLI 与部署

- `wsctl.py`：纯标准库 HTTP 客户端（status/firmware/flash/serial/presets），退出码契约见 README
- systemd `webserver.service`：root 运行 uvicorn:80，`/opt/stm32-webserver`，重启保数据
- 数据文件：`config/presets.json`、`config/motor_presets.json`、`firmware/`（升级部署需防覆盖——阶段 7 处理）

## 7. 固件协议契约（不属本服务，作为约束）

- 帧格式 `[A5 5A][type][seq][len16][payload][crc8]` 小端；下行 8 类命令、上行 Pong/Telemetry/Ack
- USART10 @115200 8N1；主机失联 500ms 看门狗闭锁输出；UART 接收依赖帧间空闲（Web 端滑杆 150ms 节流即为此约束）

## 8. 已知缺陷清单（§2.3 对照，处置阶段）

| # | 缺陷 | 处置 |
| --- | --- | --- |
| 1 | WS open 串口未关 shoot 会话 | **阶段 0 已修复** |
| 2 | choose_port 显式端口缺失回退自动选择 | 阶段 1 |
| 3 | FlashManager busy 检查非原子；deque 满后游标失稳 | 阶段 3（绝对游标） |
| 4 | 烧录读 stdout 阻塞致挂死无超时 | 阶段 3（全程看门狗） |
| 5 | 读线程退出清新连接（可变 self._ser） | 阶段 2（连接代次） |
| 6 | shoot WS 订阅队列无上限 | 阶段 2（有界+遥测丢旧） |
| 7 | README Basic2 频段描述（2.4G→5GHz 待核） | 阶段 7 |
| 8 | OpenOCD TCP 后端版本门槛未检测 | 阶段 3/7 |
| 9 | BIN 烧录无基地址契约 | 阶段 3（显式地址或拒绝） |
