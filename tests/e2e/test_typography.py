"""字体回归：检查真实渲染字库、动态姓名、字重、移动布局及加载失败恢复。"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
import harness
import test_portal

PASS, FAIL = [], []

def check(name, condition, detail=''):
    (PASS if condition else FAIL).append(name)
    print(('  ✓ ' if condition else '  ✗ ') + name + ('' if condition else ' ' + str(detail)), flush=True)

async def rendered_fonts(ctx, page, selector):
    session = await ctx.new_cdp_session(page)
    await session.send('DOM.enable')
    await session.send('CSS.enable')
    root = await session.send('DOM.getDocument')
    node = await session.send('DOM.querySelector', {'nodeId': root['root']['nodeId'], 'selector': selector})
    result = await session.send('CSS.getPlatformFontsForNode', {'nodeId': node['nodeId']})
    await session.detach()
    return result['fonts']

def unified(fonts):
    return bool(fonts) and all(f['isCustomFont'] and f['familyName'].startswith('Foray Sans') for f in fonts)

async def run_all():
    PASS.clear(); FAIL.clear()
    harness.ensure_browser_env()
    check('字体隔离服务启动', harness.start_service(with_member=True))
    harness._fake_feishu.bitable = test_portal.fake_data()
    test_portal.trigger_sync()
    output = Path('/tmp/shootweb-typography')
    output.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for width in (390, 1440):
            ctx = await browser.new_context(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
            await ctx.add_cookies([harness.auth_cookie('member')])
            page = await ctx.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for path in ('/', '/projects', '/projects/rP1', '/tasks', '/tasks/rT1', '/schedule', '/quality', '/debug/', '/login', '/onboarding', '/reset'):
                await page.goto(harness.BASE + path, wait_until='networkidle')
                await page.evaluate('document.fonts.ready')
                family = await page.locator('body').evaluate('e => getComputedStyle(e).fontFamily')
                check(f'{width}px {path} 使用统一界面字体', family.startswith('"Foray Sans"'), family)
                check(f'{width}px {path} 页面无横向溢出', await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
                who = page.locator('#who')
                if await who.count() and path not in ('/login', '/onboarding', '/reset'):
                    if width == 390:
                        await page.locator('.side-toggle').click()
                    await who.evaluate("e => e.textContent = '张三龚睿 · Foray（成员）'")
                    await page.evaluate('document.fonts.ready')
                    fonts = await rendered_fonts(ctx, page, '#who')
                    check(f'{width}px {path} 姓名与身份未混用系统字体', unified(fonts), fonts)
                    check(f'{width}px {path} 身份字重固定为500', await who.evaluate('e=>getComputedStyle(e).fontWeight') == '500')
                    if path == '/':
                        await page.screenshot(path=str(output / f'overview-{width}.png'))
            await page.goto(harness.BASE + '/', wait_until='networkidle')
            await page.evaluate("""() => {
                const el = document.createElement('p');
                el.id = 'font-sample';
                el.textContent = '张三鑫龚蕾彭睿邓昊龘喆（成员）Foray 0123456789';
                el.style.cssText = 'position:fixed;left:0;top:100px;z-index:99999;background:white;font-size:16px';
                document.body.append(el);
            }""")
            for weight in (400, 500, 600, 700, 800, 900):
                await page.locator('#font-sample').evaluate('(e,w)=>e.style.fontWeight=String(w)', weight)
                await page.evaluate('document.fonts.ready')
                fonts = await rendered_fonts(ctx, page, '#font-sample')
                check(f'{width}px {weight}字重的常用字、生僻字及英文均为本地字体', unified(fonts), fonts)
            check(f'{width}px 没有脚本异常', not errors, errors)
            await ctx.close()
        ctx = await browser.new_context(reduced_motion='reduce')
        failed_once = []
        async def fail_first(route):
            if not failed_once:
                failed_once.append(route.request.url)
                await route.abort()
            else:
                await route.continue_()
        await ctx.route('**/ui/fonts/ForaySans-common.*.woff2', fail_first)
        await ctx.add_cookies([harness.auth_cookie('member')])
        page = await ctx.new_page()
        await page.goto(harness.BASE + '/', wait_until='networkidle')
        await page.locator('#who').evaluate("e=>e.textContent='张三龚睿（成员）'")
        await page.wait_for_function("""document.fonts.check('500 16px "Foray Sans"', "张三龚睿（成员）")""")
        fonts = await rendered_fonts(ctx, page, '#who')
        check('字库首次加载失败后仍恢复正确分包', bool(failed_once) and unified(fonts), fonts)
        await browser.close()
    print(f'字体回归：通过 {len(PASS)}，失败 {len(FAIL)}', flush=True)
    return len(PASS), len(FAIL)

if __name__ == '__main__':
    try:
        _, failures = asyncio.run(run_all())
    finally:
        harness.stop_service()
    raise SystemExit(bool(failures))
