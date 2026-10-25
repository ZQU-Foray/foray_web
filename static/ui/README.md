# 统一设计资源（v2 · 终末地工业风 / 浅色主导）

门户与调试台**共用**这一套资源：配色、字号、间距、边框、阴影、动效都取自
`tokens.css`；`base.css` 提供外壳（侧边栏 / 系统条 / 页头）与组件基类。
v2 参考 grad-research-workbench（科研工作台）的终末地设计系统重做：
浅色主导、柠檬黄强调、黑黄双层硬阴影、切角、等高线纹理、入场动画。

## 文件

| 文件 | 内容 | 谁在用 |
| --- | --- | --- |
| `tokens.css` | **唯一**变量来源：基础色（纸白/墨黑/柠檬黄）、语义色、字号阶、间距阶、边框/切角、多层阴影、动效、字体、布局宽度 | `static/portal/portal.css` 以 `@import` 引入；`static/index.html`（调试台）以 `<link>` 引入 |
| `base.css` | @font-face 自托管字体 + 外壳（`.app-shell/.app-sidebar/.side-* / .sys-bar/.page-head/.app-main`）+ 组件（`.c-btn/.c-input/.c-card/.c-table/.c-badge/.c-console/.c-note/.c-empty`）+ 工具类（`.u-*`）+ 入场动画 keyframes + 页间入场动效（仅入场，纯 CSS） | 门户与调试台外壳、新组件 |
| `sidebar.js` | 侧边栏收纳开关（自动插入按钮、状态记 localStorage、`<head>` 同步加载防首帧闪烁、收纳态写到 `<html data-nav>` 作为 CSS 键） | 所有含 `.app-shell` 的页面 |
| `fonts/` | Foray Sans（100–900 可变字重、30,890 字符、按需分包）与日志用 JetBrains Mono；全部自托管。授权见 `fonts/FONTS.md`；Gilroy / Novecento / HarmonyOS 子集不入库 | `base.css` 的 @font-face |
| `assets/` | 终末地官方纹理素材（card-bg / title_bg / 按钮纹理等）。版权属于鹰角网络，**不入库**，只在本地工作区与生产服务器保留；缺失时旧前端只少几处背景纹理。页面等高线装饰一律用纯 CSS 圈层绘制 | `base.css` / `style.css` / `portal.css` 的 background |
| `foray-robomaster.svg` | FORAY 队标（品牌区 / 登录页 / 开机序列） | 门户与调试台侧边栏、登录页 |

命名约定：外壳 `app-*`/`side-*`/`sys-*`、组件 `c-*`、工具 `u-*`——与既有
`p-*`（门户）/ 调试台类名**并存不冲突**，便于分批迁移、随时回退。

## v2 设计规则

1. **浅色主导**：纸面画布（#EFEEEA）+ 白色信息面板；深色只用于局部锚点
   （系统条 / 工业按钮 / 控制台窗口）。
2. **柠檬黄 #FFF44F 唯一强调色**：黄底黑字标签、卡片黄顶条、页头黄块、
   激活态黄底；暗黄 #D9CD00 用于浅底上的黄字与黄边线。
3. **硬边工业语言**：1px 硬边、零圆角、右上切角（clip-path）、
   黑黄双层硬阴影（`--shadow-*`）。
4. **装饰回归**：等高线纹理、官方卡面纹理（灰度压暗）、顶部五色信号条、
   开机引导序列（调试台）、卡片入场动画；无跑马水印、无 HUD 视口角标。
5. **动效分级**：状态过渡（`--t-fast/--t-base`）、入场编排（pageIn /
   moduleEnter / panelDeploy）、语义闪烁（仅告警徽标/状态点）；
   `prefers-reduced-motion` 下全部归零。
6. **字体自托管**：全部 woff2 本地加载，无 CDN 依赖；中英文和数字统一使用 Foray Sans，日志与代码使用等宽字体。

## 约定

- 新增样式**不得**再定义新的 `:root` 调色板；需要新变量请加到 `tokens.css`。
- 新增页面路径必须登记 `app/auth/guard.py`（`/ui/` 已登记：资源公开、HTML 按管理页鉴权）。
- 改动后跑 `tests/e2e/test_portal.py`（含 `/ui` 资源断言）与全量回归。

## 字体覆盖与更新

Foray Sans 基于 Google Fonts 的 Noto Sans SC 可变字体，保留原始字库的全部 30,890 个字符和 100–900 字重范围，按拉丁字符、常用中文、扩展字符三个互斥范围分包。首屏预加载约 2 MB 的常用分包，扩展字符出现时按需请求对应分包，避免动态成员姓名落入系统字体。来源与文件摘要见 fonts/foray-sans-manifest.json，授权见 fonts/ForaySans-OFL.txt。

业务界面（姓名、身份、标题、表格、按钮、状态、图表）使用同一字体，保留字号与字重层级。#who 的姓名和身份统一为 12px / 500。字体修复按字体族、字重、样式、宽度和字符范围匹配，每个失败字体面最多重试一次。更新资源后必须同步页面及样式引用版本。

专项检查：.venv/bin/python tests/e2e/test_typography.py，通过浏览器实际字形来源检查常用姓名、生僻字、混排和真实字重，并覆盖手机、桌面和一次加载失败恢复。
