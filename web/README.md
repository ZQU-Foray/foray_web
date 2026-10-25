# Foray 前端（Vue 3 + Vite + TypeScript）

门户（`/` 等）、调试台（`/debug/`）、登录（`/login`、`/onboarding`、`/reset`）三个入口共用一套设计系统（v5）。

```bash
npm install        # 首次，需联网
npm run dev        # http://localhost:5173 ，接口代理到 FORAY_API（默认 http://127.0.0.1:8000）
npm run build      # 产物 → ../static/app/（入库，随代码部署；服务器不装 node）
npm run typecheck  # vue-tsc
```

- 设计令牌只在 `src/shared/styles/tokens.css` 定义，页面与组件只引用变量，不另起调色板。
- 代码注释只用简体中文（见仓库根目录 `AGENTS.md`）。
- 新增门户页面：`src/portal/routes.ts` 登记路由 + `app/main.py` 加页面路由 + `app/auth/guard.py` 的 `PORTAL_PAGES` 登记。
- 门户接口全部在 `src/portal/api.ts`，写入后的刷新策略在 `src/portal/store.ts` 的 `afterWrite()`。
- 端到端回归：仓库根目录 `tests/e2e/run_all.py`（测试读 `static/app/` 的产物，改完先构建）。

架构记录、回退开关与测试口径变化见 `../docs/frontend-vue.md`。
