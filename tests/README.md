# 端到端测试基建（阶段 0 固化）

## 结构

```
tests/
  requirements-dev.txt   # 开发依赖（playwright）
  e2e/
    harness.py           # 服务启停 / mock 注入助手
    test_api.py          # REST 契约（无需浏览器）
    test_ui.py           # 浏览器端到端（boot/下拉/滑杆/预设/离线/排版/波形）
    run_all.py           # 一键执行：起服务 → 跑全部 → 汇总 → 退出码
```

## 首次准备

```bash
.venv/bin/pip install -r tests/requirements-dev.txt
.venv/bin/python -m playwright install chromium   # 浏览器缓存在 ~/.cache/ms-playwright
```

## 运行

```bash
.venv/bin/python tests/e2e/run_all.py
```

- 每次运行在临时目录生成独立 FIRMWARE_DIR / 预设文件，不污染真实数据
- 前端测试读的是 `static/app/` 的构建产物：改完 `web/` 先 `npm run build`。
  并行回归可用环境变量隔离：`E2E_PORT`（测试服务端口，默认 8090）、
  `FORAY_APP_DIR`（指向另一份构建产物目录，配合构建时的 `FORAY_OUT_DIR`）
- 输出逐项 PASS/FAIL 与汇总；任何 FAIL → 退出码 1
- UI 套件通过路由拦截注入"有状态 mock"模拟固件（50Hz 遥测语义、ACK、
  模式/型号回显），无硬件即可回归业务逻辑；硬件相关项另见
  docs/multi-sta-progress.md 的"待实测"清单

## 与阶段的关系

- 阶段 0 起为所有阶段的回归门槛：改动后必须 `run_all.py` 全绿再进入下一步
- 新增能力（设备注册 / TCP 传输 / 多任务烧录）随对应阶段补充用例，
  软件通过与实机通过在进度文档中分列
