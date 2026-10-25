"""门户截图采集脚本（阶段 5 查漏补缺：让截图证据可复现）。

用法（仓库根目录）：
    .venv/bin/python tests/e2e/screenshots.py [输出目录]
默认输出到 backups/phase5-screenshots/。脚本自起隔离测试服务 + 假飞书数据，
不触碰真实设备与线上服务。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness

harness.ensure_browser_env()

import test_portal as tp                       # 复用其假数据与页面助手
from playwright.async_api import async_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    harness.REPO, "backups", "phase5-screenshots")

SHOTS = [
    ("overview.png", "/?poll_ms=60000", 1400, 900),
    ("overview-480.png", "/?poll_ms=60000", 480, 900),
    ("procurement.png", "/procurement", 1400, 900),
    ("tasks.png", "/tasks", 1400, 900),
    ("project-gantt.png", "/projects/rP1", 1400, 1100),
    ("project-480.png", "/projects/rP1", 480, 900),
    ("schedule.png", "/schedule", 1400, 900),
    ("schedule-calendar.png", "/schedule", 1400, 900),   # 切到日历视图后截图
    ("quality.png", "/quality", 1400, 900),
]


async def main():
    os.makedirs(OUT, exist_ok=True)
    if not harness.start_service():
        print("✗ 测试服务未就绪")
        return 1
    try:
        harness.start_fake_feishu(tenant_key="tenant-test")
        harness.login_member()
        harness._fake_feishu.bitable = tp.fake_data()
        tp.trigger_sync()
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            for name, path, w, h in SHOTS:
                page = await tp.new_page(browser, path,
                                         viewport={"width": w, "height": h})
                if name == "schedule-calendar.png":
                    await page.click("#view-toggle button:nth-child(2)")
                await page.wait_for_timeout(1600)
                await page.screenshot(path=os.path.join(OUT, name), full_page=True)
                await page.close()
                print("  已截图", name)
            await browser.close()
    finally:
        harness.stop_service()
    print("截图输出:", OUT)
    return 0


raise SystemExit(asyncio.run(main()))
