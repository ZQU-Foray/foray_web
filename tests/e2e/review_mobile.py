"""使用隔离数据检查手机、平板和桌面页面的横向溢出并保存截图。"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import harness
import test_portal
from playwright.async_api import async_playwright


async def run():
    harness.ensure_browser_env()
    assert harness.start_service(with_member=True), "隔离测试服务未就绪"
    out = Path(os.environ.get("MOBILE_REVIEW_OUT", "/tmp/shootweb-mobile-before"))
    out.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        harness._fake_feishu.bitable = test_portal.fake_data()
        test_portal.trigger_sync()
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            for width in (320, 390, 480, 768, 1024, 1440):
                ctx = await browser.new_context(viewport={"width": width, "height": 844},
                                                reduced_motion="reduce")
                await ctx.add_cookies([harness.auth_cookie("member")])
                page = await ctx.new_page()
                for path in ("/", "/projects", "/tasks", "/schedule", "/quality",
                             "/projects/rP1", "/tasks/rT1", "/debug/", "/login"):
                    await page.goto(harness.BASE + path, wait_until="networkidle")
                    await page.wait_for_timeout(250)
                    data = await page.evaluate("""() => {
                        const vw = document.documentElement.clientWidth;
                        return {width: vw, scroll: document.documentElement.scrollWidth,
                            overflow: [...document.querySelectorAll('body *')].filter(el => {
                                const r = el.getBoundingClientRect();
                                return r.width && r.right > vw + 1 &&
                                    !el.closest('.p-scroller, .p-gantt, .motor-table-wrap, .dd-menu');
                            }).slice(0, 18).map(el => ({tag: el.tagName, id: el.id,
                                cls: el.className, right: Math.round(el.getBoundingClientRect().right)}))};
                    }""")
                    key = path.strip('/').replace('/', '-') or 'overview'
                    if width in (390, 1440):
                        await page.screenshot(path=str(out / f"{key}-{width}.png"), full_page=True)
                    results.append({"path": path, "viewport": width, **data})
                    print(json.dumps(results[-1], ensure_ascii=False), flush=True)
                await ctx.close()
            await browser.close()
    finally:
        harness.stop_service()
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(any(r["scroll"] > r["width"] + 1 for r in results))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
