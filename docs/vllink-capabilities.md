# VLLINK Basic2 能力核对表（阶段 0 产出）

> 核对日期：2026-09-06 · 证据等级：**公开证实**（官方文档/源码）、**本地模拟**（假设备测试）、
> **硬件通过**（实机验证）、**未验证**（含推断成分，实施阶段必须落地）。
> 本表不修改任何设备；固件升级属于硬件操作，仅在用户授权的对应阶段执行。

## 1. 固件与传输能力

| 能力 | 证据 | 等级 | 说明 |
| --- | --- | --- | --- |
| Basic2 正式固件含 TCP-DAP（默认端口 4441） | 官方固件更新页/2026 特性页 | 公开证实 | V00.50 起；正式列表已见 V00.54 |
| Basic2 正式固件含 TCP-UART（默认端口 20010） | 同上 | 公开证实 | 需启用 `Mode3_TCP_UART` |
| STA 模式下 USB 通信关闭 | 官方文档 | 公开证实 | 配置须在 USB 有线模式或经 AP 转发完成 |
| WebUSB 厂商配置协议（≥V00.40） | spec_dap_protocal.md + vllink.js | 公开证实 | 浏览器侧；本改造在**服务器侧**以 libusb 复刻同一端点 0 传输 |
| AP 最多 8 STA | 官方描述 | 公开证实 | **不是**本服务 TCP 直连设备数上限（服务端逐 IP 独立连接） |
| Basic2 硬件为 5GHz（非 2.4G） | 硬件对比页 | 公开证实 | README 措辞待阶段 7 修正 |
| 官方配置示例密码字段误写为 SSID | spec 示例 | 公开证实 | 实现使用 `Wireless_Device_Password` 并以回读验证 |
| OpenOCD USB 路径（CMSIS-DAP/J-Link/DAPLink） | 本项目长期使用 | 硬件通过 | 现行基线 |
| **TCP-DAP 经 OpenOCD 烧录**（`cmsis_dap_backend tcp` + uri） | OpenOCD 文档 + 官方示例 | **未验证** | 阶段 3 软件实现 + 标记待实测；语法以实施时二进制实测为准 |
| **同一 STA 烧录+串口并行** | — | **未验证** | 默认互斥（烧录前暂停串口）；实测达标后再开放 |
| **TCP-DAP 通道接受厂商 0x91 配置命令** | — | **未验证** | 阶段 6 专项验证；不支持则 USB 表单主线不受影响 |
| AP 远端配置转发（选择/索引语义） | — | **未验证** | 阶段 5 后单列实测 |

## 2. 阶段 3 烧录命令构造基线（待实测确认）

USB（现行）：
```
openocd -s <scripts> -f interface/cmsis-dap.cfg -c "transport select swd" \
  -c "adapter speed N; [cmsis_dap_serial SN]" -f target/<chip>.cfg \
  -c "program {file} verify reset exit"
```

TCP（阶段 3 实现，OpenOCD ≥0.12 文档语法，**未经实机**）：
```
openocd -s <scripts> -f interface/cmsis-dap.cfg -c "transport select swd" \
  -c "cmsis_dap_backend tcp; cmsis_dap_uri tcp://<ip>:4441; adapter speed N" \
  -f target/<chip>.cfg -c "program {file} verify reset exit"
```

## 3. 配置字段映射（阶段 5 表单，实施时按实际固件生成 schema）

见计划 §6.2。要点：字段白名单来自实机读取；未知键/电压/VRef 原样保留；
密码仅返回“已设置”布尔；UTF-8 字节数按设备容量分块写入，超长拒绝不截断。

## 4. 版本记录要求

各阶段实施时记录：Basic2 实际固件版本、OpenOCD 二进制版本与来源、
vllink_console 源码提交标识；不以 master 分支漂移内容为实现依据。
