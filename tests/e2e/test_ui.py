"""浏览器端到端套件（阶段 0 固化）：boot / 品牌 / 下拉 / 滑杆 / 预设 / 离线 / 排版 / 波形。

全部走有状态 mock（harness.FakeTelemetry），无硬件依赖。
"""
import asyncio
import os

from playwright.async_api import async_playwright

from harness import BASE, FakeTelemetry, install_mock, ensure_browser_env, auth_cookie
import harness

ensure_browser_env()

# 浏览器通道可选覆盖（Windows 开发机只有 Edge 时：PLAYWRIGHT_CHANNEL=msedge）
CHANNEL = os.environ.get("PLAYWRIGHT_CHANNEL") or None


def _launch_kwargs():
    return {"channel": CHANNEL} if CHANNEL else {}

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


async def new_page(browser, **kw):
    page = await browser.new_page(viewport=kw.get("viewport", {"width": 1400, "height": 880}),
                                  device_scale_factor=kw.get("dpr", 1))
    # 阶段 3：受控测试身份（应急，仅调试）注入会话 Cookie
    await page.context.add_cookies([auth_cookie()])
    await page.add_init_script("sessionStorage.setItem('connection-mode', 'usb'); sessionStorage.setItem('current-device', 'legacy-default')")
    page.on("dialog", lambda d: asyncio.ensure_future(
        d.accept(kw["prompt_text"]) if d.type == "prompt" and "prompt_text" in kw
        else d.accept()))
    return page


async def t_boot(browser):
    print("== 引导与品牌 ==")
    page = await new_page(browser)
    await page.goto(BASE + "/debug/", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("#loading-cover", state="detached", timeout=6000)
        gone = True
    except Exception:
        gone = False
    check("引导盖按时移除", gone)
    check("标题 Foray", (await page.title()).startswith("Foray"))
    # 终末地 v2：旧跑马水印已移除，改为侧边栏品牌区（终末地式导航外壳）
    brand = await page.text_content(".side-brand-text small")
    check("侧边栏品牌标识（LAN DEBUG TERMINAL）",
          brand and "LAN DEBUG TERMINAL" in (brand or ""), str(brand))
    dot = await page.evaluate(
        "() => { const d = document.getElementById('conn-dot');"
        "return d.className + '|' + getComputedStyle(d).backgroundColor; }")
    check("服务在线绿点", "dot ok" in dot and "18, 138, 69" in dot, dot)
    await page.close()


async def t_dropdown(browser):
    print("== 自绘下拉 ==")
    fake = FakeTelemetry()
    page = await new_page(browser)
    await install_mock(page, fake)
    await page.goto(BASE + "/debug/", wait_until="domcontentloaded")
    await page.wait_for_timeout(2400)
    await page.click('.tab[data-tab="shoot"]')
    await page.click(".motor-card[data-slot='1'] .mcol-head")   # 展开 Motor 2
    await page.wait_for_timeout(400)
    n = await page.evaluate(
        "() => ({wrapped: document.querySelectorAll('.dd-native').length,"
        "        triggers: document.querySelectorAll('.dd-trigger').length})")
    check("全部 select 已包裹", n["wrapped"] == n["triggers"] and n["triggers"] >= 8, str(n))
    await page.click("#mode1 ~ .dd-trigger")
    # 面板挂在 document.body 上（避免被带 transform 的祖先错位或裁掉），
    # 按 data-for 归属查找，不再依赖 DOM 兄弟关系
    opts = await page.evaluate(
        "() => [...document.querySelector('.dd-panel[data-for=\"mode1\"]').children]"
        ".map(o => o.textContent)")
    check("模式选项中文", opts[:2] == ["停止（无输出）", "转速 (rpm)"], str(opts))
    await page.click('.dd-panel[data-for="mode1"] .dd-option:nth-child(2)')
    await page.wait_for_timeout(400)
    v = await page.evaluate(
        "() => document.getElementById('mode1').value + '|' +"
        "document.querySelector('#mode1 ~ .dd-trigger .dd-label').textContent")
    check("选择生效并同步", v.startswith("1|"), v)

    # 空选择（无选项）：面板要有实体高度与说明，且占位不可选、不改值
    await page.evaluate("() => { document.getElementById('model1').innerHTML = ''; }")
    await page.wait_for_timeout(300)
    await page.click("#model1 ~ .dd-trigger")
    await page.wait_for_timeout(300)
    empty = await page.evaluate("""() => {
        const pn = document.querySelector('.dd-panel[data-for="model1"]');
        const h = pn.querySelector('.dd-option-empty');
        return {h: Math.round(pn.getBoundingClientRect().height),
                text: h ? h.textContent.trim() : null,
                selectable: h ? h.dataset.value !== undefined : null,
                active: pn.querySelectorAll('.dd-active').length,
                label: document.querySelector('#model1 ~ .dd-trigger .dd-label')
                         .textContent.trim()};
    }""")
    check("空选择：面板有实体高度并给出说明（不是 2px 细线）",
          empty["h"] >= 36 and empty["text"] == "（无可选项）", str(empty))
    check("空选择：占位不可选、不高亮、收起态标签同步说明",
          empty["selectable"] is False and empty["active"] == 0
          and empty["label"] == "（无可选项）", str(empty))
    v0 = await page.evaluate("() => document.getElementById('model1').value")
    await page.evaluate("""() => document.querySelector(
        '.dd-panel[data-for="model1"] .dd-option-empty').click()""")
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(250)
    v1 = await page.evaluate("() => document.getElementById('model1').value")
    check("空选择：点占位或回车都不改值", v0 == v1, "%r → %r" % (v0, v1))
    await page.close()


async def t_slider(browser):
    print("== 目标滑杆 ==")
    fake = FakeTelemetry()
    page = await new_page(browser)
    await install_mock(page, fake)
    await page.goto(BASE + "/debug/", wait_until="domcontentloaded")
    await page.wait_for_timeout(2400)
    await page.click('.tab[data-tab="shoot"]')
    await page.click("#shoot-open")
    await page.wait_for_timeout(1500)
    await page.select_option("#mode1", "1")      # 转速模式（经 mock 回显）
    await page.wait_for_timeout(800)
    lim = await page.evaluate(
        "() => ({min: document.getElementById('tslider1').min,"
        "        max: document.getElementById('tslider1').max,"
        "        disp: document.getElementById('tgtrow1').style.display})")
    check("M2006 转速限额 ±500", lim["min"] == "-500" and lim["max"] == "500", str(lim))
    await page.evaluate("""() => {
        const s = document.getElementById('tslider1');
        s.value = 200;
        s.dispatchEvent(new Event('input', {bubbles:true}));
        s.dispatchEvent(new Event('change', {bubbles:true})); }""")
    await page.wait_for_timeout(400)
    # 推送为 150ms 节流 + 网络往返：整机负载高时固定 400ms 会偶发误判，
    # 改为轮询等待目标命令落地（最多 ~2.5s），断言本身不变
    pushed = []
    for _ in range(5):
        pushed = [c for c in fake.calls
                  if c[0] == "param" and c[1].get("key") == "target_rpm"]
        if any(abs(c[1]["value"] - 200) < 1 for c in pushed):
            break
        await page.wait_for_timeout(500)
    check("拖动推送目标", any(abs(c[1]["value"] - 200) < 1 for c in pushed), str(pushed[-1:]))
    await page.evaluate("""() => {
        const t = document.getElementById('tval1');
        t.value = 777;
        t.dispatchEvent(new Event('change', {bubbles:true})); }""")
    await page.wait_for_timeout(300)
    clamped = await page.evaluate("() => document.getElementById('tval1').value")
    check("越界钳制 500", clamped == "500", clamped)
    await page.close()


async def t_preset(browser):
    print("== 电机预设 ==")
    fake = FakeTelemetry()
    page = await new_page(browser, prompt_text="回归预设")
    await install_mock(page, fake)
    await page.goto(BASE + "/debug/", wait_until="domcontentloaded")
    await page.wait_for_timeout(2400)
    await page.click('.tab[data-tab="shoot"]')
    await page.wait_for_timeout(500)
    await page.click(".motor-card[data-slot='0'] .mcol-head")
    await page.click(".motor-card[data-slot='0'] .subcol-head")  # 展开 控制参数
    await page.wait_for_timeout(300)
    await page.fill("#kp_speed0", "0.08")
    fake.calls.clear()
    await page.click("button[data-act='psave']")
    await page.wait_for_timeout(800)
    doc = await page.evaluate(
        "() => window.fetch('/api/shoot/presets')"
        ".then(r => r.json()).then(d => JSON.stringify(d))")
    puts = [c for c in fake.calls if c[0] == "presets"]
    check("保存入库", "回归预设" in doc and "kp_speed" in doc,
          f"doc={doc[:60]} puts={len(puts)}")
    note = await page.text_content("#toast .toast-text") or ""
    box = await page.locator("#toast .toast").first.bounding_box()
    check("保存结果从右侧通知框弹出（调试台与门户共用）",
          "已保存预设" in note and bool(box) and 1400 - (box["x"] + box["width"]) <= 40,
          f"{note} {box}")
    await page.evaluate("""() => {
        const s = document.getElementById('preset0');
        s.value = '0';
        s.dispatchEvent(new Event('change', {bubbles:true})); }""")
    await page.wait_for_timeout(600)
    v = await page.evaluate("() => document.getElementById('kp_speed0').value")
    check("应用回填", v == "0.08", v)
    await page.close()


async def t_offline(browser):
    print("== 离线渲染 ==")
    fake = FakeTelemetry()
    page = await new_page(browser)
    await install_mock(page, fake)
    await page.goto(BASE + "/debug/", wait_until="domcontentloaded")
    await page.wait_for_timeout(2400)
    await page.click('.tab[data-tab="shoot"]')
    await page.wait_for_timeout(500)
    fake.open = False            # 模拟断开（status telemetry=None）
    await page.wait_for_timeout(1600)
    tags = await page.evaluate(
        "[0,1,2].map(i => document.getElementById('faults'+i).textContent)")
    check("断开全显离线", tags == ["离线"] * 3, str(tags))
    fake.open = True             # 恢复
    await page.wait_for_timeout(1600)
    tags2 = await page.evaluate(
        "[0,1,2].map(i => document.getElementById('faults'+i).textContent)")
    check("恢复交回遥测", tags2 == ["", "", ""], str(tags2))
    await page.close()


async def t_layout(browser):
    print("== 排版审计 ==")
    fake = FakeTelemetry()
    for w, h, name in [(1400, 880, "1400"), (1097, 700, "1097"), (700, 900, "700"),
                       (480, 800, "480")]:
        page = await new_page(browser, viewport={"width": w, "height": h})
        await install_mock(page, fake)
        await page.goto(BASE + "/debug/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2300)
        if w <= 900:
            await page.click('.side-toggle')
        await page.click('.tab[data-tab="shoot"]')
        await page.wait_for_timeout(300)
        probs = await page.evaluate("""() => {
            const de = document.documentElement, out = [];
            if (de.scrollWidth > de.clientWidth + 1)
                out.push('横向溢出');
            const inScroller = (el) => {
                // 位于横向滚动容器内的内容（如数值总览）允许越界显示
                for (let a = el.parentElement; a; a = a.parentElement) {
                    const cs = getComputedStyle(a);
                    if ((cs.overflowX === 'auto' || cs.overflowX === 'scroll') &&
                        a.scrollWidth > a.clientWidth) return true;
                }
                return false;
            };
            for (const c of document.querySelectorAll('.panel.active .card')) {
                const cr = c.getBoundingClientRect();
                for (const el of c.querySelectorAll('*')) {
                    if (inScroller(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width && r.height &&
                        (r.right > cr.right + 2 || r.left < cr.left - 2)) {
                        out.push('越界:' + (el.id || el.className)); break;
                    }
                }
            }
            return [...new Set(out)].slice(0, 3); }""")
        check(f"[{name}] 零溢出", not probs, str(probs))
        await page.close()


async def t_chart(browser):
    print("== 波形区 ==")
    fake = FakeTelemetry()
    page = await new_page(browser)
    await install_mock(page, fake)
    await page.goto(BASE + "/debug/", wait_until="domcontentloaded")
    await page.wait_for_timeout(2400)
    await page.click('.tab[data-tab="shoot"]')
    await page.wait_for_timeout(500)
    num = await page.evaluate(
        "() => document.querySelector('.numval .u') && " 
        "document.querySelector('.numval .v') !== null")
    check("数值总览存在", num)
    charts0 = await page.evaluate(
        "() => document.querySelectorAll('.dchart').length")
    check("未开始零图表", charts0 == 0, str(charts0))
    await page.click("#dc-start")
    await page.wait_for_timeout(900)
    charts1 = await page.evaluate(
        "() => [...document.querySelectorAll('.dchart')].map(b => b.dataset.field)")
    check("开始后出图", set(charts1) == {"rpm", "current"}, str(charts1))
    await page.evaluate("""() => [...document.querySelectorAll('.dchart')]
        .find(b => b.dataset.field === 'rpm').click()""")
    await page.wait_for_timeout(120)
    zoom = await page.evaluate(
        "() => [...document.querySelectorAll('.dchart')]"
        ".filter(b => getComputedStyle(b).transform !== 'none').length")
    check("FLIP 放大补间", zoom >= 1, str(zoom))

    print("== CSV 导出（勾选序列） ==")
    # 只勾选 1 号电机的转速列，便于逐列核对导出内容
    await page.evaluate("""() => {
        for (const m of [1, 2, 3]) {
            const el = document.querySelector('#dc-m' + m); if (el) el.checked = (m === 1);
        }
        for (const k of ['rpm', 'deg', 'current', 'torque', 'temperature', 'freq']) {
            const el = document.querySelector('#dc-' + k); if (el) el.checked = (k === 'rpm');
        }
        const t = document.querySelector('#dc-target'); if (t) t.checked = false;
        // 无头环境下不依赖 download 事件：拦截 Blob 与下载点击，改在页面内取回内容。
        // 仍然走真实的 exportCsv()（读勾选、拼列名、生成 Blob、设定文件名）。
        window.__csvBlob = null; window.__csvName = null; window.__csvAlerts = [];
        window.alert = function (m) { window.__csvAlerts.push(String(m)); };
        const origCreate = URL.createObjectURL.bind(URL);
        URL.createObjectURL = function (blob) { window.__csvBlob = blob; return origCreate(blob); };
        const origClick = HTMLAnchorElement.prototype.click;
        HTMLAnchorElement.prototype.click = function () {
            if (this.download && this.download.indexOf('.csv') >= 0) {
                window.__csvName = this.download;
                return;                     // 阻止真实下载，内容由 __csvBlob 提供
            }
            return origClick.apply(this, arguments);
        };
    }""")
    # 导出要求已绘制点 ≥2（按绘图节奏增长），轮询重试直到成功，避免时序假失败
    got, alerts = None, []
    for _ in range(6):
        await page.wait_for_timeout(900)
        await page.click("#dc-export")
        await page.wait_for_timeout(250)
        got = await page.evaluate("""async () => {
            if (!window.__csvBlob) return null;
            return {name: window.__csvName, text: await window.__csvBlob.text()};
        }""")
        alerts = await page.evaluate("() => window.__csvAlerts || []")
        if got:
            break
    check("CSV 导出被触发（Blob 已生成）", bool(got),
          f"alerts={alerts}")
    if got:
        lines = [ln for ln in got["text"].splitlines() if ln.strip()]
        header = lines[0] if lines else ""
        check("CSV 文件名含设备与时间戳",
              (got["name"] or "").startswith("shoot_legacy-default_")
              and (got["name"] or "").endswith(".csv"), got["name"])
        check("CSV 表头为勾选序列（t_s + Motor1 转速）",
              '"t_s"' in header and "Motor1_" in header and "Motor2_" not in header,
              header[:90])
        check("CSV 含 ≥2 行数据且列数与表头一致",
              len(lines) >= 3 and all(len(ln.split(",")) == 2 for ln in lines[1:]),
              f"{len(lines)} 行，表头 {len(header.split(','))} 列")
        check("CSV 数据行为相对秒 + 数值",
              len(lines) > 1 and lines[1].split(",")[0].replace(".", "").replace("-", "").isdigit(),
              lines[1][:40] if len(lines) > 1 else "")


async def t_nav_collapse(browser):
    """侧边栏收纳（v2.1）：默认展开 / 点击收缩 / 过渡动画 / 刷新持久化。"""
    page = await new_page(browser)
    await page.goto(BASE + "/debug/", wait_until="domcontentloaded")
    try:                       # 等引导盖移除，确保侧栏外壳已挂载
        await page.wait_for_selector("#loading-cover", state="detached", timeout=6000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    async def w():
        return await page.evaluate(
            "() => Math.round(document.querySelector('.app-sidebar')"
            ".getBoundingClientRect().width)")

    check("调试台有收纳开关", await page.query_selector(".side-toggle") is not None)
    check("调试台默认不收纳", not await page.evaluate(
        "() => document.querySelector('.app-shell').classList.contains('is-collapsed')"))
    w0 = await w()
    check("展开态侧栏宽度≥200px", w0 >= 200, str(w0))

    await page.click(".side-toggle")
    await page.wait_for_timeout(420)
    w1 = await w()
    check("收纳态侧栏宽度≈64px", abs(w1 - 64) <= 3, str(w1))
    check("收纳态标签淡出隐藏", await page.evaluate(
        "() => { const el = document.querySelector('.side-nav-item > span:not(.nav-no)');"
        " if (!el) return true; const s = getComputedStyle(el);"
        " return s.display === 'none' || (s.visibility === 'hidden' && +s.opacity < .05); }"))
    dur = await page.evaluate(
        "() => getComputedStyle(document.querySelector('.app-sidebar')).transitionDuration")
    check("侧栏带宽度过渡（动画）", dur and dur.split(",")[0].strip() != "0s", dur)

    await page.reload(wait_until="domcontentloaded")
    await page.wait_for_timeout(400)
    check("刷新后保持收纳（localStorage）", await page.evaluate(
        "() => document.querySelector('.app-shell').classList.contains('is-collapsed')"))
    await page.click(".side-toggle")
    await page.wait_for_timeout(420)
    check("再次点击恢复展开", abs(await w() - w0) <= 2, str(await w()))
    await page.evaluate("() => localStorage.removeItem('foray.nav.collapsed')")
    await page.close()


async def t_multi_device_dropdown(browser):
    """多设备默认选中 + 无滚动条页面下拉不被 resize 误关（2026-10-19 回归）。

    背景：自绘下拉展开瞬间面板仍是 absolute 且挂在 body 下，会把无滚动条的
    页面瞬时撑出滚动条 → visualViewport resize → 面板被 close_on_resize 关闭，
    真机表现为“点下拉框没反应”。Playwright headless 默认 --hide-scrollbars
    掩盖了这个问题，这里显式取消该默认参数以守住回归。
    """
    print("== 多设备默认选中 / 无滚动条下拉 ==")
    ids = ["ui-multi-0", "ui-multi-1"]
    for i, dev_id in enumerate(ids):
        code, _ = harness.api("/api/v2/devices", {
            "id": dev_id, "name": f"UI 多设备 {i}", "transport": "vllink_tcp",
            "host": f"192.168.1.{240 + i}", "dap_port": 4441, "uart_port": 20010})
        check(f"注册多设备用例设备 {dev_id}", code == 200, str(code))
    try:
        # 复用同一浏览器类型，但取消 --hide-scrollbars（经典滚动条参与布局）
        launch_kw = dict(_launch_kwargs())
        launch_kw["ignore_default_args"] = ["--hide-scrollbars"]
        b = await browser.browser_type.launch(**launch_kw)
        try:
            ctx = await b.new_context(viewport={"width": 1440, "height": 900})
            await ctx.add_cookies([auth_cookie()])
            page = await ctx.new_page()
            errs = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            await page.goto(BASE + "/debug/", wait_until="domcontentloaded")
            await page.wait_for_timeout(2400)
            st = await page.evaluate("""() => ({
                id: window.deviceCtx && deviceCtx.id,
                saved: sessionStorage.getItem('current-device'),
                opts: [...document.querySelectorAll('#device-select option')].map(o => o.value),
                enabled: !document.getElementById('serial-open').disabled,
                vScroll: document.documentElement.scrollHeight
                         > document.documentElement.clientHeight + 1,
            })""")
            check("多设备时自动选中第一台（不再半选）",
                  st["id"] == ids[0], str(st))
            check("多设备时串口/烧录按钮可用", st["enabled"] is True, str(st))
            await page.click("#device-select ~ .dd-trigger")
            await page.wait_for_timeout(300)
            panel = await page.evaluate("""() => {
                const pn = document.querySelector('.dd-panel[data-for="device-select"]');
                if (!pn) return null;
                const r = pn.getBoundingClientRect();
                return {hidden: pn.hidden, n: pn.children.length,
                        h: Math.round(r.height)};
            }""")
            check("无滚动条页面点开下拉后面板保持展开",
                  bool(panel) and panel["hidden"] is False and panel["n"] >= 2,
                  str(panel))
            if panel and not panel["hidden"]:
                await page.click('.dd-panel[data-for="device-select"] .dd-option:nth-child(2)')
                await page.wait_for_timeout(700)
                after = await page.evaluate(
                    "() => ({id: deviceCtx.id, label: document.querySelector('#device-select')"
                    "?.closest('.dd')?.querySelector('.dd-label')?.textContent})")
                check("下拉可切换到第二台设备", after["id"] == ids[1], str(after))
            check("多设备下拉无 JS 异常", not errs, str(errs[:2]))
            await ctx.close()
        finally:
            await b.close()
    finally:
        for dev_id in ids:
            harness.api("/api/v2/devices/" + dev_id, method="DELETE")


SUITES = [t_boot, t_nav_collapse, t_dropdown, t_multi_device_dropdown,
          t_slider, t_preset, t_offline, t_layout, t_chart]


async def run_all():
    async with async_playwright() as p:
        browser = await p.chromium.launch(**_launch_kwargs())
        for s in SUITES:
            try:
                await s(browser)
            except Exception as e:
                check(f"{s.__name__} 套件异常", False, repr(e))
        await browser.close()
    return len(PASS), len(FAIL)


if __name__ == "__main__":
    npass, nfail = asyncio.run(run_all())
    print(f"\nUI: {npass} 通过, {nfail} 失败")
    raise SystemExit(1 if nfail else 0)
