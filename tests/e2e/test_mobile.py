"""移动端交互回归：导航、内容边界、调试页签与可滚动下拉框。"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright
import harness
import test_portal

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (" " + str(detail) if not cond else ""), flush=True)


async def bounds(page, label):
    bad = await page.evaluate("""() => {
      const out = [];
      if (document.documentElement.scrollWidth > innerWidth + 1) out.push('页面横向溢出');
      document.querySelectorAll('.grid-auto > .module, .ug-grid > .group-cell, .cd-strip > .card, .overview-grid > .module, .stack > .module, .grid2 > .card, .shoot-body > *, .dchart-grid > *, .att-stats > .card, .att-grid > *').forEach(el => {
        const r = el.getBoundingClientRect(), p = el.parentElement.getBoundingClientRect();
        if (r.width && (r.right > p.right + 1 || r.left < p.left - 1)) out.push(el.className);
      });
      return out;
    }""")
    check(label + " 页面与内部卡片不溢出", not bad, bad)


async def run_all():
    harness.ensure_browser_env()
    check("移动端隔离服务启动", harness.start_service(with_member=True))
    harness._fake_feishu.bitable = test_portal.fake_data()
    test_portal.trigger_sync()
    out = Path("/tmp/shootweb-mobile-interactions")
    out.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for width in (320, 390, 768, 1440):
            ctx = await browser.new_context(viewport={"width": width, "height": 844},
                                            has_touch=width <= 900, reduced_motion="reduce")
            await ctx.add_cookies([harness.auth_cookie("member")])
            await ctx.add_init_script("localStorage.setItem('foray.nav.collapsed', '1')")
            page = await ctx.new_page()
            for path in ("/", "/attendance", "/projects", "/tasks", "/schedule", "/quality", "/projects/rP1", "/tasks/rT1"):
                await page.goto(harness.BASE + path, wait_until="networkidle")
                await bounds(page, f"{width}px {path}")
            if width <= 900:
                check(f"{width}px 手机不继承桌面收纳态",
                      await page.locator(".side-toggle").get_attribute("aria-expanded") == "false")
                await page.locator(".side-toggle").click()
                await page.locator('.side-nav-item[href="/tasks"]').wait_for(state="visible")
                check(f"{width}px 菜单有可见文字", await page.locator('.side-nav-item[href="/tasks"] span:not(.nav-no)').is_visible())
                check(f"{width}px 同步与退出入口可达", await page.locator("#sync-refresh").is_visible()
                      and await page.locator("#logout-btn").is_visible())
                await page.locator("#logout-btn").scroll_into_view_if_needed()
                if width == 390:
                    await page.screenshot(path=str(out / "menu-390.png"))
                await page.keyboard.press("Escape")
                check(f"{width}px 关闭菜单恢复焦点", await page.locator(".side-toggle").evaluate("el => el === document.activeElement"))
                await page.locator(".side-toggle").click()
                await page.locator('.side-nav-item[href="/tasks"]').click()
                await page.wait_for_url("**/tasks")
                check(f"{width}px 选择页面后菜单关闭", not await page.locator("html").evaluate("el => el.hasAttribute('data-mobile-nav')"))
                await page.locator(".side-toggle").click()
                await page.locator(".side-backdrop").click(position={"x": width - 5, "y": 400})
                check(f"{width}px 点击遮罩关闭", await page.locator(".side-toggle").get_attribute("aria-expanded") == "false")
                await page.set_viewport_size({"width": 1440, "height": 844})
                # 视口变化经 matchMedia change 事件异步生效：最多等 2 秒（旧版此处偶发读到变化前的值）
                try:
                    await page.wait_for_function(
                        "document.documentElement.getAttribute('data-nav') === 'collapsed'", timeout=2000)
                except Exception:
                    pass
                check(f"{width}px 返回桌面恢复原偏好", await page.locator("html").get_attribute("data-nav") == "collapsed")
                await page.set_viewport_size({"width": width, "height": 844})

            hostile = '<img src=x onerror=window.__injected=1>.bin'
            async def firmware_route(route):
                await route.fulfill(json={"items": [{"name": hostile, "size": 4, "mtime": 0}] + [
                    {"name": f"测试固件-{i}-很长的描述.bin", "size": 4, "mtime": 0} for i in range(35)]})
            await page.route("**/api/firmware", firmware_route)
            await page.goto(harness.BASE + "/debug/", wait_until="networkidle")
            for tab in ("flash", "serial", "shoot", "settings"):
                if width <= 900:
                    await page.locator(".side-toggle").click()
                await page.locator(f'.side-nav-item[data-tab="{tab}"]').click()
                await page.locator(f"#tab-{tab}").wait_for(state="visible")
                await bounds(page, f"{width}px 调试/{tab}")
                if width == 390:
                    await page.screenshot(path=str(out / f"{tab}-390.png"), full_page=True)
            check(f"{width}px 固件名作为文字渲染", await page.locator("#fw-list .name").first.text_content() == hostile
                  and await page.locator("#fw-list img").count() == 0
                  and not await page.evaluate("Boolean(window.__injected)"))
            if width <= 900:
                await page.locator(".side-toggle").click()
            await page.locator('.side-nav-item[data-tab="flash"]').click()
            trigger = page.locator(".dd").filter(has=page.locator("#firmware-select")).locator(".dd-trigger")
            await trigger.scroll_into_view_if_needed()
            await trigger.click()
            panel = page.locator('.dd-panel[data-for="firmware-select"]')
            check(f"{width}px 下拉菜单在视口内", await panel.evaluate("el => { const r=el.getBoundingClientRect(); return r.top>=0 && r.bottom<=innerHeight && r.left>=0 && r.right<=innerWidth && r.height>=44; }"))
            await panel.evaluate("el => { el.scrollTop = el.scrollHeight; }")
            await page.wait_for_timeout(100)
            check(f"{width}px 滚动选项不会关闭下拉", await panel.is_visible())
            await panel.locator('[role="option"]').last.click()
            check(f"{width}px 可以选择末尾选项", await page.locator("#firmware-select").input_value() == "测试固件-34-很长的描述.bin")
            await ctx.close()
        await browser.close()
    print(f"移动端交互：通过 {len(PASS)}，失败 {len(FAIL)}", flush=True)
    return len(PASS), len(FAIL)


if __name__ == "__main__":
    try:
        _, failures = asyncio.run(run_all())
    finally:
        harness.stop_service()
    raise SystemExit(bool(failures))
