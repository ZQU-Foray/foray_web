# 前端迁移到 Vue 3（架构记录 · 2026-10-20）

> 对应 `docs/team-portal-migration-plan.md` §6.1 的约定：「第一版无需引入 React/Vue；若实施中证据表明需要调整，先在架构记录说明原因和迁移代价」。本文就是这份记录，同时是新前端的开发与运维说明。

## 1. 决定

门户、调试台、登录三处前端从原生 JS 改为 **Vue 3 + Vite + TypeScript**，视觉落地 `design-v5-preview/`（设计稿只在本地，不入库）（v4 配色 + 非颜色优化）。
构建产物入库到 `static/app/`，服务器不装 node；旧前端文件原样保留，用环境变量即可回退。

## 2. 为什么改

1. **v5 回填本来就要重写渲染层**：v5 用到的 142 个类名与旧门户只有 5 个重合，旧 `portal.js` 的 DOM 拼装代码几乎要全部重写，换框架的增量成本小。
2. **旧代码在手工补框架能力**：轮询刷新后手动保存/恢复焦点、关闭入场动画防抖、自制软导航（fetch 整页再替换主区）、服务端与前端两处同构的状态文案、靠改写 `select.value` 实现的自绘下拉（10-19 的 P0 就出在这里）。这些在 Vue 里是默认行为：数据变了只修补变化的节点，焦点、滚动、动画都不受影响。
3. **页面结构天然组件化**：侧栏、顶栏、页头、看板、徽标、表格在每页重复出现。

## 3. 代价与约束（以及怎么处理的）

| 约束 | 处理 |
| --- | --- |
| 服务器没有 node | 本地构建，产物 `static/app/` 入库；`deploy.py` 打包前校验三个入口与 assets 都在，缺失即中止；`node_modules` 不进包 |
| 不得引 CDN、离线自足 | Vite 把脚本样式全部打包到本地；字体继续由 `/ui/fonts/` 自托管（不进打包） |
| 新路径必须登记鉴权 | `app/auth/guard.py` 新增 `/app/` 分类：带扩展名的资源公开，其余按管理页鉴权；入口 HTML 不经 `/app/` 暴露，只由页面路由返回 |
| 401 跳登录不能破坏 | `web/src/shared/http.ts` 统一处理；登录页自身不跳（防重定向环，`t_login_no_loop` 覆盖） |
| DOM 契约 | 功能性 id 全部保留（`#sync-line`、`#task-body`、`#group-filter`…）；外壳沿用 `.app-shell/.app-sidebar/.side-nav-item/.side-toggle` 与 `html[data-nav]`、`html[data-mobile-nav]`；设计变化涉及的断言在同一次改动里改写（见 §8） |
| 首帧不闪 | 服务端把身份与同步状态注入 `window.__FORAY_BOOT__`；侧栏收纳态、页间过场、开机引导遮底由内联首帧脚本 `web/src/shared/head-early.js` 在 `<head>` 同步处理 |
| 字体 | 中文统一 Foray Sans（姓名生僻字覆盖，`test_typography` 校验）；v5 的 HarmonyOS Sans 不采用 |

## 4. 目录与职责

```
web/                              前端工程（源码）
  portal.html / debug.html / auth.html   三个入口（Vite 多页）
  vite.config.ts                  构建配置（产物 → ../static/app，资源 → /app/assets/）
  src/shared/                     三个应用共用
    styles/                       设计系统：tokens（唯一变量来源）/ base / shell / components / boot
    components/                   AppShell（侧栏收纳 + 移动抽屉）/ TopBar / PageHead / ToastStack（右侧通知框）
    head-early.js                 首帧脚本（构建时内联进 <head>）
    http.ts router.ts format.ts handoff.ts bootCover.ts toast.ts（通知框状态与计时）
  src/portal/                     门户单页应用
    api.ts                        全部门户接口（读写集中于此）
    store.ts                      全局状态：身份 / 元数据 / 同步信封 / 跨页筛选 / 轮询 / afterWrite
    usePageData.ts                页面数据加载（generation 闸门、错误提示、无快照）
    forms.ts                      写入表单定义（与旧 WRITE_FORMS 一一对应）
    nav.ts                        侧栏导航表与页面编号（页头编号统一从这里取，增删页面只改一处）
    pages/ components/            页面与组件
  src/debug/                      调试台
  src/auth/                       登录 / 开通 / 重设密码
static/app/                       构建产物（入库，勿手改）
```

后端改动：`app/config.py`（`APP_DIR`、`PORTAL_FRONTEND`）、`app/main.py`（`_serve_app_page` 注入首帧状态、页面路由按开关返回新旧前端、挂载 `/app/assets`、该目录长缓存）、`app/auth/guard.py`（`/app/` 分类）、`deploy.py`（排除 node_modules、校验产物）。

## 5. 开发与构建

```bash
cd web
npm install            # 首次（需联网）
npm run dev            # 开发服务器 :5173，/api /auth /ws /ui 代理到 FORAY_API（默认 http://127.0.0.1:8000）
npm run build          # 产物写到 ../static/app/（会清空该目录）
npm run typecheck      # vue-tsc 全量类型检查
```

- 开发时先起后端（`uvicorn app.main:app --port 8000`），在 :5173 登录即可（Cookie 走代理，同源校验不受影响）。开发服务器没有首帧注入，页面会自己拉 `/api/auth/me` 与 `sync-status`。
- **改完前端必须重新 `npm run build` 并把 `static/app/` 一起提交/打包**，否则服务器上还是旧产物。
- 环境变量 `FORAY_OUT_DIR` 可把产物写到别处（并行测试用），后端用 `FORAY_APP_DIR` 指向它。

### 离线环境说明（本次迁移的实际情况）

本次在无外网的 WSL 上完成：`web/node_modules` 是从本机 npm 缓存与 pnpm 存储离线拼出来的（Vite 8.0.16 + rolldown 原生库版本不一致，打了一个只影响 `.wasm` 导入的桩补丁），装不上 `vue-router` 与 `vue-tsc`。因此：

- 路由用 `web/src/shared/router.ts` 自带的轻量实现，接口与 vue-router 子集一致（`RouterLink`/`RouterView`/`useRoute`/`router.push`），联网后可直接换成 vue-router；
- 类型检查只覆盖了 `.ts` 文件（`tsc --noEmit`），模板未经 vue-tsc 检查；联网后建议执行一次 `npm install && npm run typecheck && npm run build`，确认官方工具链构建结果一致；
- `package.json` 声明的是标准依赖（`@vitejs/plugin-vue ^6`、`vite ^8`、`vue-tsc ^3`）；离线拼装用的是 plugin-vue 5.2.4，两者对源码无差别。

## 6. 运行与回退

- 默认 `PORTAL_FRONTEND=vue`。出问题时在 `/opt/portal/.env`（portal.service 的 EnvironmentFile）里设 `PORTAL_FRONTEND=legacy` 并 `systemctl restart portal.service`，即回到旧版原生前端（`static/portal/`、`static/index.html`，未改动），无需重新部署。
- 产物缺失时页面返回 503 并提示构建或回退，不会给出半截页面。
- 缓存：入口 HTML `no-store`；`/app/assets/*`（文件名带内容哈希）`max-age=31536000, immutable`。

## 7. 行为口径（与旧版一致）

- 门户每 15 秒（`?poll_ms=` 可加速）只比对快照版本与陈旧标记，变化才局部刷新；筛选条件跨页保留；倒计时本地每秒计算。
- 总览（2026-10-21 布局）：只放摘要，每块有条数上限并给「查看全部」，格子里不套滚动——计数条（逾期 / 72 小时内到期可点击筛选下方列表）、重要节点矮卡横排、「需要关注的事项」一张列表（逾期 → 临期 → 受阻 → P0，组别芯片切换，最多 8 条）、近期日程按天分组（最多 3 天 8 条）、工时排行前 10 名 + 我（整行两列）。数据来自 `/api/portal/overview` 的 `attention` 与 `kpi`（`compute.attention_tasks`）；旧字段 `groups` 保留给旧前端回退用。
- 事项状态（2026-10-21 改）：组长在事项详情页点「标记已完成」（两步确认，`POST /api/portal/write/tasks/{id}/complete`），服务端记完成时间、写成 待验收 + 验收结论通过，归一为「已完成」即归档（`portal/taskStatus.ts`）；事项页与项目详情默认把它们折叠在末尾，点「展开已归档的事项」才显示；状态筛选选「已完成」时直接列出。
- 侧栏顺序（2026-10-21）：01 总览、02 考勤、03 项目、04 事项、05 物资采购、06 日程、07 数据质量（`portal/nav.ts`）。品牌区标题「信息面板 / INFO PANEL」，去掉了版本号。
- 通知（2026-10-21 改）：三个应用的通知一律从画面右侧弹出通知框（`shared/toast.ts` 的 `toast(文案, 语气)`，容器 `shared/components/ToastStack.vue` 在各应用根组件挂一次），不再用页面顶部的通知条。语气 info / ok / error 对应黄 / 绿 / 红色条与 NOTICE / DONE / ERROR 标签；停留 4–12 秒（按字数与语气），悬停或聚焦时暂停，可点 × 关闭，同一文案不重复堆叠，最多 4 条。页面上描述记录或页面状态的说明（如「※ 已逾期」「尚无可用同步数据」）仍是页内 `.note`，不属于通知。
- 写入成功统一走 `store.afterWrite(res)`：按响应的 `queued` 弹出「已写入飞书」或「已保存，正在推送到飞书」通知，然后立即刷新当前页（后端响应前已把改动并进页面数据）。记录上的「待同步 / 同步失败 / 冲突」徽标用 `components/SyncMark.vue`，推送失败与冲突在「数据质量」页查看和处理。详见 `docs/portal-cache-design.md` §8。
- 组长专属入口按 `/api/auth/me` 的 `is_leader` 显隐，权威校验仍在服务端。

## 8. 测试改动（设计变化导致的断言改写）

| 套件 | 原断言 | 新断言 | 理由 |
| --- | --- | --- | --- |
| test_portal | 选择器 `.p-count/.p-card/.p-row/.p-gantt*/.p-cal*/.p-src-link` | `.cd-card/.group-cell/.rank-row/.sched-row/.gantt*/.cal*/.src-link` 等 | 类名换成 v5 组件名，检查内容不变 |
| test_portal | 倒计时：改 DOM 属性模拟精确时刻 | 测试数据增加一个精确时刻节点，校验「天数 + 剩余时分秒」并每秒变化 | Vue 由数据驱动，直接改 DOM 属性不再代表真实行为 |
| test_portal | 排行文案 `29.11 小时` / `无数据` | `29.11 h` / `未统计打卡` | v5 文案 |
| test_portal | 日期含 `-` | 日期形如 `2026.10.30` | v5 文案 |
| test_portal | 登录卡黄顶条 #FFF44F + 黑黄硬阴影 | 墨色 4px 顶边 + 无柔和投影 + 黄带标题区 #FFFA00 | v5 去投影、配色沿用 v4 |
| test_portal | 侧栏宽 248 | 220（收纳 64 不变） | v5 导轨宽度 |
| test_portal | `/ui/tokens.css`、`portal.css` 的 @import | 门户与调试台引用同一份共享样式；产物可匿名加载；共享样式含 v5 令牌 | 设计系统改由构建产物提供 |
| test_portal | 登录队标 `foray-robomaster.svg` | 登录队标为 v5 R1 削角版 SVG | v5 队徽 |
| test_mobile | `.p-logout` / 旧网格选择器 | `#logout-btn` / v5 网格选择器 | 类名变化 |
| test_mobile | 回到桌面立即读 `data-nav` | 最多等 2 秒 matchMedia 事件生效 | 旧版此处在 320px 偶发失败（基线即失败），属时序问题 |

调试台相关断言的改写见 §9。

## 9. 调试台

### 9.1 文件与职责（`web/src/debug/`）

协议与交互逻辑放在 `lib/*.ts`（命令式模块，逐段移植自旧版，可被 `tsc` 检查）；组件只负责结构与慢变化的界面态。

| 文件 | 职责 | 对应旧代码 |
| --- | --- | --- |
| `main.ts` | 入口：样式 → 挂载 → 首启动画 → `startDebugConsole()`；`window.deviceCtx` 仍挂在全局 | `app.js` 末尾 |
| `lib/start.ts` | 启动编排：设备切换处理（顺序同旧版）、探测、日志 WS、3 秒状态轮询、固件库、发射调试 | `app.js`「初始化」 |
| `lib/api.ts` | `dfetch`（401 跳登录）、base64 / 文本编解码、日志追加（≤2000 行） | `app.js`「会话失效拦截」「工具」 |
| `lib/device.ts` | 设备上下文、候选与自动选中第一台、5 秒 `/probe`、扫描 / 手动添加 STA | `app.js`「设备上下文」「在线探测」 |
| `lib/status.ts` | 状态轮询：服务在线、设备清单、目标芯片 / 调试器 / 串口候选（未变化不替换）、烧录与串口徽标 | `app.js`「状态轮询」 |
| `lib/flash.ts` `serial.ts` `presets.ts` `firmware.ts` | 烧录 WS 与发起烧录；串口 WS、RX 按行合并（350ms 冲刷）、文本 / HEX 发送；串口预设；固件库 | `app.js` 同名段 |
| `lib/shoot.ts` `shoot-types.ts` | 发射调试全部协议逻辑：型号 / 模式 / PID / 目标滑杆 150ms 节流 / 预设三级命名空间 / 使能流 / 急停 / RTT / 离线渲染 / 链路恢复重推默认预设 | `shoot.js` |
| `lib/chart.ts` | 60s@50Hz 环形缓冲、分图 Canvas、FLIP 补间、列数记忆、CSV 导出（文件名含设备 ID）、数值总览 | `chart.js` |
| `lib/dropdown.ts` + `components/DdSelect.vue` | 自绘下拉（保留 10-19 修复语义：先落 fixed 再显示、视口真变化 >1px 才关、交互期赋值暂存、点标签展开） | `dropdown.js` |
| `lib/identity.ts` `toast.ts` | 侧栏身份与退出；`toast(文案, 是否错误)` 转到共享的右侧通知框 | 新增 / `shoot.js` toast |
| `App.vue` `components/*Tab.vue` `DeviceStrip.vue` `MotorCard.vue` `DetailPanel.vue` | 外壳、设备条、四个页签（始终挂载、按 `.panel.active` 显隐，切页不断 WS 与录制）、电机卡、波形区 | `index.html` |
| `styles/debug.css` | 调试台专属样式（只用 `tokens.css` 变量；曲线数据色沿用旧 `chart.js`） | `style.css` |

50Hz 遥测按 rAF 合帧后命令式写入数值节点，原始帧与波形缓冲都不进 Vue 响应式；电机卡的输入框是非受控的（逻辑层按 id 读写，Vue 不覆盖用户正在输入的内容）。

### 9.2 与旧版的差异

- **侧栏新增身份与退出**（旧调试台没有）。应急会话的显示名在服务端已带「（应急·仅调试）」，前端不再重复追加。
- **v5 视觉**：主区新增页签条（与侧栏导航等价）、页头显示当前设备与 RTT；徽标改用设计系统的 `ok / danger / warn / neutral` 样式。
- **401 处理**：旧版改写全局 `window.fetch`；新版所有请求走 `dfetch`，语义相同，不再污染全局。
- **外部字符串一律按文字渲染**：旧 `chart.js` 的图例与标题用 `innerHTML` 拼接，新版改为文本节点（内容不变）。
- **在线探测的设备切换判断**：旧版用一个与定时器共享的变量判断“设备已切换”，新版在每次探测前记下 generation 再比对，意图相同、不会误判。
- 协议与时序（WS 地址、请求载荷、节流、确认文案、自动失能、互斥）**没有改动**。

### 9.3 测试

调试台相关用例**无需改写断言**：`test_ui` 45 项、`test_mobile` 调试台段全部按原断言通过，`test_typography` 的 `/debug/` 检查通过。

迁移中顺带修了共享样式的一处时序问题：窄屏抽屉打开时，侧栏标签与底部区会继承抽屉的可见性变化并触发桌面「收纳淡出」用的 0.2 秒 visibility 过渡，打开后第一帧它们仍算不可见（读屏与自动化偶发读到不可见，`test_mobile` 的「菜单有可见文字 / 同步与退出入口可达」因此偶发失败）。现在窄屏与减少动态效果时关闭这组过渡（`shell.css` 末尾）。

### 9.4 只能真机验证的项

隔离测试用的是模拟遥测与假设备，以下需要在接上 STA / 电机后过一遍：真实 50Hz 遥测下的流畅度、滑杆拖动下发与固件 ACK、急停与急停锁定后的使能流程、MCU 重启后默认预设重推、TCP 串口与 USB 串口的切换和互斥、真实烧录日志。

## 10. 布局约定（避免内容跳动与裁切）

2026-10-20 预览反馈修过两类问题，新增页面或组件时按下面三条做：

1. **页面纵向滚动条常驻**（`shell.css` 的 `html { overflow-y: scroll; }`，登录类页面在 `auth.css` 改回自动）。否则页面高度落在视口高度附近时，展开 / 收起卡片、筛选、切页签都会让滚动条反复出现又消失，整页左右挤动一个滚动条宽度（如 1920×950 下展开发射调试的电机卡）。
2. **内容会增减的内部滚动容器加 `scrollbar-gutter: stable`**：已加在概览列表 `.scroll-list`、日志控制台 `.console`、写入弹窗 `.modal-body`。不加的话，内容跨过容器高度时容器宽度会少一个滚动条宽，长行整体重新折行。内容固定的浮层（下拉面板、人员浮层）不需要。
3. **悬停整行右移（`translateX(6px)`）的行，右内边距不小于位移量**：行尾贴右的按钮（如「飞书 ↗」）否则会被外层列表裁掉一截；放这类行的滚动容器横向用 `overflow-x: hidden`，避免悬停瞬间冒出横向滚动条。

## 11. 新增页面清单（以 2026-10-20 新增「02 考勤」为例）

新增一个门户页面要同时改 5 处，缺一处就会 404 或落到默认公开：

1. `web/src/portal/nav.ts`：导航项与编号（其后页面顺延，页头编号自动跟随）；
2. `web/src/portal/routes.ts`：路由；
3. `app/main.py`：页面路由（`_serve_app_page("portal", ...)`）；
4. `app/auth/guard.py`：`PORTAL_PAGES` 登记，按管理页鉴权；
5. `web/vite.config.ts`：开发服务器的路径改写。

考勤页（`pages/Attendance.vue`，接口 `GET /api/portal/attendance`）只在新版前端存在；`PORTAL_FRONTEND=legacy` 回退旧前端时访问 `/attendance` 会跳回总览。打卡工时排行抽成了 `components/RankList.vue`，总览与考勤页共用。
