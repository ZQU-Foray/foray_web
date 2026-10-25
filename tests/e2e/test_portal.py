"""门户阶段 5 验收（浏览器端到端，API 驱动 UI）。

自包含：冷启动（无快照）断言 → 注入 fake bitable 数据并触发同步 → 全套
渲染/轮询/详情/甘特/状态提示验收。数据来自 /api/portal/*（生产无 fixture）。
"""
import asyncio
import datetime
import json
import time

import urllib.request

from playwright.async_api import async_playwright

import harness
from harness import BASE

harness.ensure_browser_env()

PASS, FAIL = [], []

POLL_URL = "/?poll_ms=1200"      # 测试加速轮询（1.2s）


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ---------------- 动态 fake 数据（相对 now，保证 7 天窗/逾期/未来节点都在） ----------------

def _ms(delta_days, hour=10, minute=0):
    tz = datetime.timezone(datetime.timedelta(hours=8))
    base = datetime.datetime.now(tz) + datetime.timedelta(days=delta_days)
    base = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int(base.timestamp() * 1000)


WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


def _today_weekday():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).isoweekday()


def _link(rid):
    return [{"record_ids": [rid], "table_id": "tblFixPortalSync1",
             "text": "x", "text_arr": ["x"], "type": "text"}]


def fake_data():
    tasks = [
        {"record_id": "rT1", "fields": {"事项名称": "逾期任务", "所属项目": _link("rP1"),
          "主责组": "硬件", "P等级": "P0", "状态": "进行中",
          "截止时间": _ms(-1), "截止时间精度": "仅日期",
          "负责人": [{"id": "ou_fix_a", "name": "成员甲"}]}},
        {"record_id": "rT2", "fields": {"事项名称": "临期任务", "所属项目": _link("rP1"),
          "主责组": "算法", "P等级": "P1", "状态": "受阻",
          "截止时间": _ms(1, 18), "截止时间精度": "精确时刻", "阻塞说明": "等件",
          "负责人": [{"id": "ou_fix_b", "name": "成员乙"}]}},
        {"record_id": "rT3", "fields": {"事项名称": "完成任务", "所属项目": _link("rP1"),
          "主责组": "运营", "P等级": "P2", "状态": "已完成",
          "截止时间": _ms(-2), "截止时间精度": "仅日期", "完成时间": _ms(-2)}},
        {"record_id": "rT4", "fields": {"事项名称": "未排期任务", "所属项目": _link("rP1"),
          "主责组": "电控", "P等级": "P1", "状态": "未开始"}},
        {"record_id": "rT5", "fields": {"事项名称": "甘特任务", "所属项目": _link("rP1"),
          "主责组": "机械", "P等级": "P2", "状态": "进行中",
          "开始时间": _ms(0), "截止时间": _ms(7), "截止时间精度": "仅日期"}},
    ]
    meta = {
        "tblFixPortalSync1": [("项目名称", "fldPname"), ("项目说明", "fldPdesc"),
                              ("主责组", "fldPown"), ("协作组", "fldPcol"),
                              ("负责人", "fldPowners"), ("计划开始", "fldPps"),
                              ("计划结束", "fldPpe"), ("项目状态", "fldPstatus")],
        "tblFixPortalSync2": [("事项名称", "fldTname"), ("所属项目", "fldTproj"),
                              ("主责组", "fldTown"), ("协作组", "fldTcol"),
                              ("负责人", "fldTowners"), ("P等级", "fldTpri"),
                              ("状态", "fldTstatus"), ("开始时间", "fldTstart"),
                              ("截止时间", "fldTdue"), ("截止时间精度", "fldTprec"),
                              ("完成时间", "fldTdone"), ("事项说明", "fldTdesc"),
                              ("阻塞说明", "fldTblk")],
        "tblFixPortalSync3": [("节点名称", "fldMname"), ("节点类型", "fldMkind"),
                              ("目标日期时间", "fldMtime"), ("时间精度", "fldMprec"),
                              ("所属项目", "fldMproj"), ("展示状态", "fldMvis"),
                              ("节点说明", "fldMdesc")],
        "tblFixPortalSync4": [("日程名称", "fldSname"), ("开始时间", "fldSstart"),
                              ("结束时间", "fldSend"), ("全天", "fldSaday"),
                              ("活动类型", "fldSkind"), ("相关组", "fldSgrp"),
                              ("参与人", "fldSpart"), ("地点", "fldSloc"),
                              ("所属项目", "fldSproj"), ("日程说明", "fldSdesc"),
                              ("状态", "fldSstatus")],
        # 必须与 harness.FAKE_TABLES_JSON 声明的字段一一对应，否则同步报 config
        "tblFixPortalSync5": [("成员", "fldMem"), ("有效事项数", "fldMvalid"),
                              ("已完成事项数", "fldMdone"), ("到期事项数", "fldMdue"),
                              ("准时完成数", "fldMok"), ("逾期未完成数", "fldMlate"),
                              ("交付时间待补数", "fldMmiss"),
                              ("未设截止事项数", "fldMnodue"),
                              ("总体完成率", "fldMrate"), ("准时完成率", "fldMotr"),
                              ("统计说明", "fldMnote"), ("最近签到", "fldMci"),
                              ("有效时长（小时）", "fldMhours"),
                              ("异常考勤数", "fldManom"), ("同步时间", "fldMsync"),
                              ("同步说明", "fldMsnote")] + harness.MEMBER_WEEK_META,
        "tblFixPortalSync8": harness.DUTY_META,
        # 与 harness.FAKE_TABLES_JSON 对齐：本套件不渲染这两张表，但配置里已存在，
        # 必须给出字段元数据——否则结构性校验（§6.4.6）会把整轮同步判为 config 失败，
        # 后续所有用例都拿不到数据。
        "tblFixPortalSync6": [("职责名称", "fldRduty"), ("组别", "fldRgrp"),
                              ("组长", "fldRlead"), ("组员", "fldRmem")],
        "tblFixPortalSync7": [("采购物资名称", "fldPrname"), ("申请组别", "fldPrgrp"),
                              ("购物链接", "fldPrlink"), ("用途说明", "fldPrpurp"),
                              ("图片补充", "fldPratt"), ("组长批复", "fldPrlead"),
                              ("已采购", "fldPrbuy"), ("赛季", "fldPrseason"),
                              ("一级分类", "fldPrcat1"), ("二级分类", "fldPrcat2"),
                              ("采购编号", "fldPrcode"), ("采购日期", "fldPrdate"),
                              ("单价（元）", "fldPrprice"), ("数量", "fldPrqty"),
                              ("总价（元）", "fldPrtotal"), ("采购人", "fldPrwho"),
                              ("发票已开", "fldPrinv"), ("发票文件", "fldPrinvf")],
    }
    return {
        "tblFixPortalSync1": {"meta": meta["tblFixPortalSync1"], "records": [
            {"record_id": "rP1", "fields": {"项目名称": "测试项目甲", "主责组": "电控",
              "项目状态": "进行中", "计划开始": _ms(-10), "计划结束": _ms(30)}},
            {"record_id": "rP9", "fields": {"项目名称": "已归档项目", "主责组": "运营",
              "项目状态": "已归档"}}]},
        "tblFixPortalSync2": {"meta": meta["tblFixPortalSync2"], "records": tasks},
        "tblFixPortalSync3": {"meta": meta["tblFixPortalSync3"], "records": [
            {"record_id": "rM1", "fields": {"节点名称": "未来检查点", "节点类型": "关键检查",
              "目标日期时间": _ms(10), "时间精度": "仅日期", "展示状态": "展示"}},
            # 精确时刻节点：倒计时给出「天数 + 剩余时分秒」并每秒刷新（Vue 版用真实数据验证）
            {"record_id": "rM2", "fields": {"节点名称": "精确时刻节点", "节点类型": "比赛",
              "目标日期时间": _ms(3, 18), "时间精度": "精确时刻", "展示状态": "展示"}}]},
        "tblFixPortalSync4": {"meta": meta["tblFixPortalSync4"], "records": [
            {"record_id": "rS1", "fields": {"日程名称": "跨日全天联调",
              "开始时间": _ms(2), "结束时间": _ms(3), "全天": True,
              "活动类型": "联调", "地点": "测试场", "状态": "正常"}},
            {"record_id": "rS2", "fields": {"日程名称": "今晚例会",
              "开始时间": _ms(1, 21), "结束时间": _ms(1, 22), "全天": False,
              "活动类型": "例会", "状态": "正常"}}]},
        "tblFixPortalSync5": {"meta": meta["tblFixPortalSync5"], "records": [
            # 周结算：甲本周已结算（公式数值 + 公式文本的真实返回形状）且今天签到中；
            # 乙的结算周还停在上周（待结算）；丙没有任何考勤数据
            {"record_id": "rMem1", "fields": {"成员": [{"id": "ou_fix_a", "name": "成员甲"}],
              "有效时长（小时）": "29.11", "最近签到": _ms(0, 0, 1),
              "异常考勤数": "0", "同步说明": "事项：已同步",
              "周初时长": "0", "结算周": [{"type": "text", "text": harness.week_key()}],
              "本周时长": 29.11, "本周达标": [{"type": "text", "text": "未达标"}],
              "上周时长": "31.5", "上周达标": [{"type": "text", "text": "达标"}]}},
            {"record_id": "rMem2", "fields": {"成员": [{"id": "ou_fix_b", "name": "成员乙"}],
              "有效时长（小时）": "12.5", "异常考勤数": "2",
              "周初时长": "2", "结算周": harness.week_key(-1), "本周时长": 10.5}},
            {"record_id": "rMem3", "fields": {"成员": [{"id": "ou_fix_c", "name": "成员丙"}]}}]},
        # 空表即可：字段齐全、零记录（本套件不渲染这两张表）
        "tblFixPortalSync6": {"meta": meta["tblFixPortalSync6"], "records": []},
        "tblFixPortalSync7": {"meta": meta["tblFixPortalSync7"], "records": []},
        # 值日表：今天按星期轮到乙和一位不在成员表里的「外援」（文本姓名）；明天按日期轮到丙
        "tblFixPortalSync8": {"meta": meta["tblFixPortalSync8"], "records": [
            {"record_id": "rD1", "fields": {"星期": WEEKDAY_CN[_today_weekday()],
              "值日人员": [{"id": "ou_fix_b", "name": "成员乙"}]}},
            {"record_id": "rD2", "fields": {"星期": WEEKDAY_CN[_today_weekday()],
              "值日人员": "外援"}},
            {"record_id": "rD3", "fields": {"日期": _ms(1, 0, 0),
              "值日人员": [{"id": "ou_fix_c", "name": "成员丙"}]}}]},
    }


def trigger_sync(expect_tasks=None):
    """注入数据并触发一次同步，等待完成。"""
    time.sleep(1.2)
    req = urllib.request.Request(
        BASE + "/api/portal/sync", data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + harness._auth["member"],
                 "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass
    want = expect_tasks if expect_tasks is not None else \
        len(harness._fake_feishu.bitable.get("tblFixPortalSync2", {}).get("records", []))
    _, st0 = harness.api("/api/portal/sync-status", auth="member")
    deadline = time.time() + 20
    while time.time() < deadline:
        _, st = harness.api("/api/portal/sync-status", auth="member")
        if (st.get("last_attempt_at") != st0.get("last_attempt_at")
                and not st.get("syncing")
                and st.get("counts", {}).get("tasks") == want):
            return st
        time.sleep(0.6)
    return harness.api("/api/portal/sync-status", auth="member")[1]


async def new_page(browser, path, role="member", viewport=None):
    page = await browser.new_page(
        viewport=viewport or {"width": 1400, "height": 880}, device_scale_factor=1)
    if role:
        await page.context.add_cookies([harness.auth_cookie(role)])
    reqs = []
    page.on("request", lambda r: reqs.append(r.url))
    page._reqs = reqs
    await page.goto(BASE + path, wait_until="domcontentloaded")
    return page


async def t_cold_start(browser):
    print("== 冷启动：无快照提示（区分空表） ==")
    page = await new_page(browser, POLL_URL)
    await page.wait_for_timeout(1200)
    note = await page.text_content("#no-snap-note")
    check("无快照 → 明确提示（≠空表）", bool(note) and "尚无可用同步数据" in note, str(note)[:60])
    line_cls = await page.evaluate(
        "() => document.getElementById('sync-line').className")
    check("同步条空状态样式", "is-empty" in line_cls, line_cls)
    await page.close()


async def t_data_render(browser):
    print("== API 数据渲染（无 fixture） ==")
    page = await new_page(browser, POLL_URL)
    await page.wait_for_timeout(1800)
    check("页面请求 /api/portal/*（API 驱动）",
          any("/api/portal/" in u for u in page._reqs))
    check("无 fixtures.js 请求（生产不混 fixture）",
          not any("fixtures.js" in u for u in page._reqs))
    counts = await page.evaluate("""() => ({
        counts: document.querySelectorAll('.cd-card[data-target]').length,
        groups: document.querySelectorAll('#ov-group-chips .chip').length,
        kpi: document.querySelectorAll('#ov-kpi [data-kpi]').length,
        rank: document.querySelectorAll('#rank-box .rank-row').length,
        sched: document.querySelectorAll('#schedule-box .sched-row').length })""")
    check("倒计时节点 2 个（仅日期 + 精确时刻，data-target 供本地计算）", counts["counts"] == 2, str(counts))
    check("需要关注：全部 + 五组芯片", counts["groups"] == 6, str(counts))
    check("计数条（逾期 / 临期 / 今日日程 / 签到中）", counts["kpi"] >= 4, str(counts))
    check("打卡工时排行 3 行（空行不入榜）", counts["rank"] == 3, str(counts))
    check("7 天日程 2 条", counts["sched"] == 2, str(counts))
    check("项目摘要卡已移除（#project-box 不存在）",
          await page.evaluate("() => !document.querySelector('#project-box')"))
    rank = await page.evaluate(
        "() => [...document.querySelectorAll('#rank-box .rank-row')].map(r => r.textContent)")
    check("排行按时长降序、首行标 #1、无数据成员在末位",
          len(rank) == 3 and "#1" in rank[0] and "29.11 h" in rank[0]
          and "未统计打卡" in rank[-1] and "成员丙" in rank[-1], str(rank)[:120])
    urg = await page.evaluate(
        "() => [...document.querySelectorAll('#urgent-box .task-row')].map(r => r.textContent)")
    check("紧急含逾期任务", any("逾期任务" in x for x in urg), str(urg)[:80])
    # 点「逾期」计数：列表只剩逾期的；再点一次恢复
    await page.click('#ov-kpi [data-kpi="overdue"]')
    levels = await page.evaluate(
        "() => [...document.querySelectorAll('#urgent-box .task-row')].map(r => r.dataset.level)")
    check("点逾期计数只看逾期事项", levels and all(x == "overdue" for x in levels), str(levels))
    await page.click('#ov-kpi [data-kpi="overdue"]')
    check("再点一次恢复全部",
          await page.evaluate("() => !document.querySelector('.ov-filter-note')"))
    check("总览格子里不再套滚动（事项 / 日程 / 排行不出现内部滚动条）",
          await page.evaluate("""() => ['#urgent-box', '#schedule-box', '#rank-box'].every(sel => {
              const el = document.querySelector(sel);
              return el && el.scrollHeight <= el.clientHeight + 1; })"""))
    await page.close()


async def t_countdown_local(browser):
    print("== 倒计时本地计算（秒级变化，无飞书轮询） ==")
    page = await new_page(browser, POLL_URL)
    await page.wait_for_timeout(2100)
    days = await page.text_content('.cd-card[data-precision="date"] .days')
    sub = await page.text_content('.cd-card[data-precision="date"] .cd-date')
    rem0 = await page.text_content('.cd-card[data-precision="date"] .cd-rem')
    check("纯日期节点只给天数/今天（不虚构小时分钟，§5.3）",
          bool(days) and ("天" in days or "今天" in days) and ":" not in days and not (rem0 or "").strip(),
          f"{days} | {rem0}")
    import re as _re
    check("倒计时卡显示目标日期（2026.10.30 格式）",
          bool(sub) and _re.search(r"\d{4}\.\d{2}\.\d{2}", sub or "") is not None, str(sub))
    r1 = await page.text_content('.cd-card[data-precision="datetime"] .cd-rem')
    await page.wait_for_timeout(1300)
    r2 = await page.text_content('.cd-card[data-precision="datetime"] .cd-rem')
    d2 = await page.text_content('.cd-card[data-precision="datetime"] .days')
    check("精确时刻节点给出天数 + 剩余时分秒，且每秒重算",
          bool(_re.match(r"剩余 \d\d:\d\d:\d\d$", (r1 or "").strip())) and r1 != r2
          and "天" in (d2 or ""), f"{d2} | {r1} -> {r2}")
    # 可证伪断言：①页面不得请求任何飞书域名；②假飞书服务器调用计数在 tick 窗口内不增长
    before = dict(harness._fake_feishu.calls_by_path)
    feishu_calls = [u for u in page._reqs if "feishu" in u.lower()]
    await page.wait_for_timeout(2600)          # ≥2 个 tick
    bitable_before = before.get("bitable", 0)
    bitable_after = harness._fake_feishu.calls_by_path.get("bitable", 0)
    check("页面零飞书域名请求（倒计时本地计算）", not feishu_calls, str(feishu_calls[:2]))
    check("tick 窗口内同步流量零增长（可证伪：bitable 接口调用数不变）",
          bitable_after == bitable_before,
          f"bitable {bitable_before}->{bitable_after}")
    await page.close()


async def t_poll_refresh(browser):
    print("== 15s 轮询局部刷新（保筛选/滚动） ==")
    page = await new_page(browser, "/tasks?poll_ms=1200")
    await page.wait_for_timeout(1500)
    rows0 = await page.evaluate("() => document.querySelectorAll('#task-body tr').length")
    check("任务表 5 行", rows0 == 5, str(rows0))
    await page.click("#group-filter button:nth-child(3)")
    await page.wait_for_timeout(900)
    rows_f = await page.evaluate("() => document.querySelectorAll('#task-body tr').length")
    check("筛选=硬件 → 1 行", rows_f == 1, str(rows_f))
    harness._fake_feishu.bitable["tblFixPortalSync2"]["records"].append(
        {"record_id": "rT6", "fields": {"事项名称": "新增硬件任务", "主责组": "硬件",
          "P等级": "P3", "状态": "未开始"}})
    trigger_sync()
    await page.wait_for_timeout(2600)
    rows_n = await page.evaluate("() => document.querySelectorAll('#task-body tr').length")
    pressed = await page.evaluate(
        "() => document.querySelector('#group-filter button[aria-pressed=\"true\"]').textContent")
    check("刷新后新数据出现（2 行）且筛选保持硬件", rows_n == 2 and pressed == "硬件",
          f"rows={rows_n} pressed={pressed}")
    # 切回"全部"（顺带验证筛选可切换），再撑高页面
    await page.click("#group-filter button:nth-child(1)")
    await page.wait_for_timeout(900)
    for i in range(30):
        harness._fake_feishu.bitable["tblFixPortalSync2"]["records"].append(
            {"record_id": f"rPad{i}", "fields": {"事项名称": f"撑高{i}", "主责组": "运营",
              "P等级": "P3", "状态": "未开始"}})
    trigger_sync()
    await page.wait_for_timeout(2600)
    await page.evaluate("() => window.scrollTo(0, 500)")
    await page.wait_for_timeout(200)
    sy0 = await page.evaluate("() => window.scrollY")
    check("页面已撑高可滚动", sy0 >= 450, str(sy0))
    # 修改既有任务内容（不减行，页面高度不变）触发版本变化 → 轮询局部刷新
    for r in harness._fake_feishu.bitable["tblFixPortalSync2"]["records"]:
        if r["record_id"] == "rT1":
            r["fields"]["事项名称"] = "逾期任务（已更新）"
    trigger_sync()
    await page.wait_for_timeout(2600)
    sy = await page.evaluate("() => window.scrollY")
    name = await page.evaluate(
        "() => [...document.querySelectorAll('#task-body a')].some(a => a.textContent.includes('已更新'))")
    check("刷新后滚动位置保持且内容更新", 450 <= sy <= 550 and name,
          f"sy={sy} updated={name}")

    # 阶段 8（8-6）：轮询重渲染必须把键盘焦点还给原控件（修复前焦点丢失）
    await page.evaluate("""() => {
        const btn = document.querySelector('#group-filter button:nth-child(2)');
        if (btn) btn.focus();
    }""")
    focus_before = await page.evaluate(
        "() => document.activeElement && document.activeElement.textContent")
    for r in harness._fake_feishu.bitable["tblFixPortalSync2"]["records"]:
        if r["record_id"] == "rT1":
            r["fields"]["事项名称"] = "逾期任务（焦点轮次）"
    trigger_sync()
    await page.wait_for_timeout(2600)
    focus_after = await page.evaluate(
        "() => document.activeElement && document.activeElement.textContent")
    check("轮询刷新后键盘焦点回到原控件（a11y）",
          bool(focus_before) and focus_after == focus_before,
          f"{focus_before!r} -> {focus_after!r}")
    # 焦点元素本身被内容替换（行内链接文本变化）时：允许焦点回落到 body，
    # 但不得抛出渲染异常、不得出现错误提示条——"恢复"只对稳定标识负责
    await page.evaluate("""() => {
        const a = document.querySelector('#task-body a');
        if (a) a.focus();
    }""")
    for r in harness._fake_feishu.bitable["tblFixPortalSync2"]["records"]:
        if r["record_id"] == "rT1":
            r["fields"]["事项名称"] = "逾期任务（内容替换轮次）"
    trigger_sync()
    await page.wait_for_timeout(2600)
    err = await page.evaluate(
        "() => { const n = document.querySelector('#toast .toast.is-error'); return n ? n.textContent : ''; }")
    rows_ok = await page.evaluate("() => document.querySelectorAll('#task-body tr').length")
    check("焦点元素被内容替换时不报错、列表仍正常渲染",
          err == "" and rows_ok >= 1, f"err={err!r} rows={rows_ok}")
    await page.close()


async def t_detail_and_gantt(browser):
    print("== 详情直达 + 只读甘特 ==")
    page = await new_page(browser, "/projects/rP1")
    await page.wait_for_timeout(1500)
    g = await page.evaluate("""() => ({
        bars: document.querySelectorAll('.gantt-bar').length,
        unsched: [...document.querySelectorAll('.unsched .list-row')].length,
        draggable: [...document.querySelectorAll('.gantt-bar')].map(b => b.getAttribute('draggable')),
        title: document.querySelector('#project-detail h2') &&
               document.querySelector('#project-detail h2').textContent })""")
    check("项目详情标题", g["title"] and "测试项目甲" in g["title"], str(g["title"]))
    check("甘特条 1 个（有日期任务）", g["bars"] == 1, str(g))
    check("未排期列表 4 条（缺开始或截止均不伪造排期）", g["unsched"] == 4, str(g))
    check("甘特无拖拽属性（只读）", all(d is None for d in g["draggable"]), str(g["draggable"]))
    src = await page.evaluate(
        "() => { const a = document.querySelector('#p-main .src-link'); return a && a.href; }")
    # 记录级链接来自快照的官方 shared_url（合法）；关键断言是**不得出现表级
    # 配置链接**（出现即说明测试读到了生产 config/feishu.json）。
    check("源链接为记录级官方链接（非生产表级配置链接）",
          bool(src) and "/record/" in src and "?table=" not in src, str(src))
    meta_links = await page.evaluate(
        "() => fetch('/api/portal/meta').then(r => r.json())"
        ".then(m => Object.keys(m.source_links || {}).length)")
    check("隔离环境无表级源链接（不读生产配置）", meta_links == 0, str(meta_links))
    await page.goto(BASE + "/tasks/rT1", wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)
    det = await page.text_content("#task-detail")
    check("事项详情（逾期标注 + 精度文字）",
          det and "已逾期" in det and "仅日期" in det, str(det)[:80])
    done_row = await page.text_content("#task-complete") or ""
    check("非组长在详情页只看到「由组长标记完成」，没有完成按钮与提交时间输入",
          "由组长标记完成" in done_row
          and await page.locator("#task-complete button").count() == 0
          and await page.locator('#task-detail input[type="datetime-local"]').count() == 0,
          done_row)
    await page.go_back()
    await page.wait_for_timeout(800)
    check("浏览器返回项目详情", page.url.endswith("/projects/rP1"), page.url)
    await page.goto(BASE + "/tasks/rT3", wait_until="domcontentloaded")
    await page.wait_for_selector("#task-status")
    st_row = await page.text_content("#task-status") or ""
    check("组员看已完成事项：没有执行状态按钮，只提示组长才能重新打开",
          "只有组长能重新打开" in st_row
          and await page.locator("#task-status button").count() == 0, st_row)
    await page.goto(BASE + "/schedule/rS1", wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)
    sd = await page.text_content("#schedule-detail")
    check("跨日全天显示 – 分隔", sd and "全天 " in sd and " – " in sd, str(sd)[:80])
    await page.close()


async def t_filled_gaps(browser):
    """阶段 5 查漏补缺验收：文案、记录级链接、负责人/项目筛选、失败提示。"""
    print("== 查漏补缺（文案 / 记录链接 / 筛选） ==")
    page = await new_page(browser, POLL_URL)
    body = await page.text_content("body") or ""
    check("页面不再宣称『模拟数据 / fixture / 未接飞书』",
          "fixture" not in body and "未接飞书" not in body
          and "DEV · 开发模式" not in body, body[:90])
    mode = await page.query_selector(".p-mode")
    check("已去掉『只读门户…』说明条", mode is None)
    sync = (await page.text_content("#sync-line")) or ""
    check("状态条只展示最新同步时间（无说明性长文案）",
          "同步于" in sync and "只读门户" not in sync, sync[:70])

    hrefs = await page.evaluate(
        "() => [...document.querySelectorAll('a.src-link, a.more-link')].map(a => a.href)")
    rec_links = [h for h in hrefs if "feishu" in h]
    check("首页出现记录级官方链接（官方 /record/ 共享链接）",
          len(rec_links) >= 2 and all("/record/" in h for h in rec_links),
          str(hrefs[:4]))
    check("首页有『查看全部节点与日程』入口（§3.2-2）",
          any(h.endswith("/schedule") for h in hrefs), str(hrefs[:4]))

    await page.goto(BASE + "/tasks", wait_until="domcontentloaded")
    await page.wait_for_timeout(1600)
    cols = await page.evaluate(
        "() => document.querySelectorAll('#task-body tr:first-child td').length")
    check("事项表新增『源』列（8 列）", cols == 8, str(cols))
    # 已完成的事项即归档：默认折叠，状态显示「已完成」
    body_txt = await page.text_content("#task-body") or ""
    check("已完成事项默认不展示，只给展开已归档按钮",
          "完成任务" not in body_txt and "展开已归档的事项（1 条）" in body_txt, body_txt[:120])
    await page.click("#accepted-toggle")
    row_txt = await page.text_content("#task-body tr.is-accepted") or ""
    check("展开后列出已归档事项，状态显示「已完成」",
          "完成任务" in row_txt and "已完成" in row_txt and "已验收" not in row_txt, row_txt)
    await page.click("#accepted-toggle")
    stat = await page.evaluate("() => [...document.querySelectorAll('#status-filter button')].map(b => b.textContent)")
    check("状态筛选项叫「已完成」（不再叫「已验收」）", "已完成" in stat and "已验收" not in stat, str(stat))
    brand = await page.text_content(".side-brand-text") or ""
    check("左上角品牌为「信息面板」且去掉 V5 版本号",
          "信息面板" in brand and "V5" not in brand and "战队门户" not in brand, brand)
    rows_all = await page.evaluate(
        "() => document.querySelectorAll('#task-body tr').length")
    owners = await page.evaluate(
        "() => [...document.querySelectorAll('#owner-filter button')].map(b => b.textContent)")
    check("负责人筛选项来自数据（全部+成员甲+成员乙）",
          len(owners) == 3 and "成员甲" in owners, str(owners))
    await page.click("#owner-filter button:nth-child(2)")
    await page.wait_for_timeout(900)
    rows_owner = await page.evaluate(
        "() => document.querySelectorAll('#task-body tr').length")
    check("按负责人筛选生效（行数减少）",
          0 < rows_owner < rows_all, f"{rows_all}->{rows_owner}")
    projects = await page.evaluate(
        "() => [...document.querySelectorAll('#project-filter button')].map(b => b.textContent)")
    check("项目筛选项来自数据（含测试项目甲，归档项目也在候选内）",
          len(projects) >= 2 and any("测试项目甲" in p for p in projects), str(projects))

    # 「项目」列显示项目名而非 record_id（§5.1）
    cells = await page.evaluate(
        "() => [...document.querySelectorAll('#task-body tr td:nth-child(6)')].map(c => c.textContent)")
    check("事项『项目』列显示项目名（非原始 record_id）",
          any("测试项目甲" in (c or "") for c in cells) and
          not any((c or "").startswith("rec") for c in cells), str(cells[:4]))

    # 逾期/临期文字（不只靠颜色，§3.3）
    body2 = await page.text_content("#task-body") or ""
    check("逾期/临期以文字标注（非仅颜色）", "逾期" in body2, body2[:60])

    # 分页消费：列表请求必须显式带 page_size（不再 50 条静默截断）
    list_reqs = [u for u in page._reqs if "/api/portal/tasks?" in u]
    check("列表请求显式带 page_size（消费后端分页）",
          bool(list_reqs) and all("page_size=200" in u for u in list_reqs),
          str(list_reqs[:2]))

    # 手动刷新按钮存在且能触发（§3.2-6）
    btn = await page.query_selector("#sync-refresh")
    check("同步状态条含『刷新』按钮", bool(btn), str(btn))
    await page.close()


async def t_toasts(browser):
    """通知统一从画面右侧的通知框弹出：停留一段时间后自动消失、悬停暂停、可手动关闭；
    页面里不再有顶部通知条。用打开不存在的记录触发（加载失败 → 错误通知）。"""
    print("== 右侧通知框 ==")
    page = await new_page(browser, "/tasks/rNOPE1")
    toast = page.locator("#toast .toast")
    await toast.first.wait_for(timeout=6000)
    box = await toast.first.bounding_box()
    check("通知框在画面右侧弹出（贴右边、位于顶栏下方）",
          bool(box) and 1400 - (box["x"] + box["width"]) <= 40 and box["x"] > 700
          and box["y"] >= 56, str(box))
    cls = await toast.first.get_attribute("class") or ""
    txt = await toast.first.text_content() or ""
    check("加载失败弹出错误通知（ERROR 标签 + 原因）",
          "is-error" in cls and "ERROR" in txt and "记录不存在" in txt, f"{cls} {txt}")
    check("页面里不再有顶部通知条", await page.locator("#data-note").count() == 0)
    check("通知带倒计时细条", await page.locator("#toast .toast-timer").count() == 1)
    await toast.first.hover()
    await page.wait_for_timeout(8500)            # 错误通知约 6.5 秒；悬停期间不该消失
    check("悬停时暂停计时（超过停留时长仍在）", await toast.count() == 1, str(await toast.count()))
    await page.mouse.move(5, 870)
    await toast.first.wait_for(state="detached", timeout=12000)
    check("移开后按剩余时间自动消失", True)

    await page.goto(BASE + "/tasks/rNOPE2", wait_until="domcontentloaded")
    await toast.first.wait_for(timeout=6000)
    await page.wait_for_timeout(2500)
    check("通知停留一段时间（2.5 秒后仍在）", await toast.count() == 1, str(await toast.count()))
    await toast.first.wait_for(state="detached", timeout=10000)
    check("停留时长到了自动消失", True)

    await page.goto(BASE + "/tasks/rNOPE3", wait_until="domcontentloaded")
    await toast.first.wait_for(timeout=6000)
    await page.click("#toast .toast-close")
    await toast.first.wait_for(state="detached", timeout=2000)
    check("点 × 立即关闭", True)
    await page.close()

    page = await browser.new_page(viewport={"width": 1400, "height": 880})
    await page.goto(BASE + "/login?reset=1", wait_until="domcontentloaded")
    await page.locator("#toast .toast").first.wait_for(timeout=6000)
    t = await page.text_content("#toast .toast") or ""
    cls = await page.locator("#toast .toast").first.get_attribute("class") or ""
    check("登录页的跳转说明改为通知框（密码已重设 → 完成语气）",
          "密码已重设" in t and "is-ok" in cls and await page.locator("#login-note").count() == 0,
          f"{cls} {t}")
    check("登录页不再有状态提示行", await page.locator("#lo-msg").count() == 0)
    await page.close()


async def t_proc_new_season(browser):
    """新建采购申请默认带当前赛季（每年 9 月起算下一赛季）：采购编号要 赛季 + 一级分类 才能自动生成。"""
    print("== 新建采购申请：默认当前赛季 ==")
    import datetime as _d
    now = _d.date.today()
    expect = f"{now.year + (1 if now.month >= 9 else 0)}赛季"
    page = await new_page(browser, "/procurement")
    await page.click("button:has-text('新建采购申请')")
    sel = page.locator('.modal select[data-k="season"]')
    await sel.wait_for(timeout=5000)
    val = await sel.input_value()
    opts = await sel.evaluate("s => [...s.options].map(o => o.value)")
    check("新建采购申请的赛季默认是当前赛季",
          val == expect if expect in opts else val == "", f"{val} 期望 {expect} 候选 {opts}")
    await page.close()


async def t_login_no_loop(browser):
    """P0 回归：匿名打开 /login 必须稳定，不得 401→跳转→401 死循环。"""
    print("== 登录页匿名无重定向环（P0 回归） ==")
    page = await browser.new_page(viewport={"width": 1400, "height": 880})
    navs = []
    page.on("framenavigated", lambda f: navs.append(f.url) if f == page.main_frame else None)
    await page.goto(BASE + "/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    check("匿名 /login 无重复导航（≤1 次）", len(navs) <= 1, str(navs[:4]))
    check("匿名 /login URL 未被 next 嵌套",
          page.url.rstrip("/").endswith("/login") and "next=" not in page.url, page.url)
    form = await page.query_selector("#lo-user")
    check("登录页为本站账号表单（用户名+密码，D2）", bool(form))
    pw = await page.query_selector("#lo-pass")
    check("登录页含密码输入框", bool(pw))
    fb = await page.query_selector("#feishu-btn")
    check("飞书登录入口在场（组织成员开通/登录）", bool(fb))
    old_em = await page.query_selector("#em-user")
    check("应急登录表单已移除（D2 无应急账号）", old_em is None)
    await page.close()


async def t_stale_and_empty(browser):
    print("== 陈旧缓存提示 ==")
    import sqlite3
    c = sqlite3.connect(harness._tmpdir + "/portal.db")
    c.execute("UPDATE meta SET value=? WHERE key='last_success_at'",
              (str(time.time() - 10000),))
    c.commit()
    c.close()
    page = await new_page(browser, POLL_URL)
    await page.wait_for_timeout(1500)
    cls = await page.evaluate("() => document.getElementById('sync-line').className")
    txt = await page.text_content("#sync-line")
    check("陈旧缓存 → is-stale 样式 + 文案", "is-stale" in cls and "陈旧" in (txt or ""),
          f"{cls} {txt}")
    await page.close()

    # 同步失败：last_error 必须在状态条可见（§3.2-6 失败提示）
    import sqlite3 as _sq
    c = _sq.connect(harness._tmpdir + "/portal.db")
    c.execute("UPDATE meta SET value='network' WHERE key='last_error'")
    c.commit()
    c.close()
    page = await new_page(browser, POLL_URL)
    await page.wait_for_timeout(1500)
    cls = await page.evaluate("() => document.getElementById('sync-line').className")
    txt = await page.text_content("#sync-line")
    check("同步失败 → is-failed 样式 + 失败文案",
          "is-failed" in cls and "失败" in (txt or ""), f"{cls} {txt}")
    c = _sq.connect(harness._tmpdir + "/portal.db")
    c.execute("UPDATE meta SET value='' WHERE key='last_error'")
    c.commit()
    c.close()
    await page.close()


async def t_auth_pages(browser):
    print("== 登录失效与页面守卫 ==")
    page = await browser.new_page(viewport={"width": 1400, "height": 880})
    await page.goto(BASE + "/", wait_until="domcontentloaded")
    check("匿名访问门户 → 到达登录页", "/login" in page.url, page.url)
    await page.close()
    page = await new_page(browser, "/", role="emergency")
    await page.wait_for_timeout(900)
    check("应急访问管理页 → 引导登录页（forbidden）",
          "/login" in page.url and "forbidden" in page.url, page.url)
    await page.close()


async def t_keyboard_contrast(browser):
    print("== 键盘操作与文字对比 ==")
    page = await new_page(browser, POLL_URL)
    await page.wait_for_timeout(1200)
    await page.keyboard.press("Tab")
    focused = await page.evaluate("""() => {
        const a = document.activeElement;
        if (!a) return {tag: null};
        const cs = getComputedStyle(a);
        return {tag: a.tagName, outline: cs.outlineStyle + ' ' + cs.outlineWidth}; }""")
    check("Tab 可达导航且焦点可见", focused["tag"] in ("A", "BUTTON") and
          focused["outline"].startswith("solid"), str(focused))
    ratios = await page.evaluate("""() => {
        function lum(c) {
            const m = c.match(/\\d+/g).map(Number);
            const f = (v) => { v /= 255; return v <= .03928 ? v/12.92 : Math.pow((v+.055)/1.055, 2.4); };
            return .2126*f(m[0]) + .7152*f(m[1]) + .0722*f(m[2]);
        }
        function ratio(fg, bg) {
            const l1 = lum(fg), l2 = lum(bg);
            return ((Math.max(l1,l2)+.05)/(Math.min(l1,l2)+.05)).toFixed(2);
        }
        function hex2rgb(h) {
            h = h.trim().replace('#', '');
            if (h.length === 3) h = h.split('').map(c => c + c).join('');
            return 'rgb(' + [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)).join(', ') + ')';
        }
        const doc = getComputedStyle(document.documentElement);
        const paperRaw = doc.getPropertyValue('--paper').trim() || '#EFEEEA';
        const paper = paperRaw.startsWith('#') ? hex2rgb(paperRaw) : paperRaw;
        const main = getComputedStyle(document.querySelector('.module-head h2'));
        const sub = getComputedStyle(document.querySelector('.row-meta'));
        return { main: ratio(main.color, paper), sub: ratio(sub.color, paper),
                 rawMain: main.color, rawSub: sub.color };
    }""")
    check(f"主文字对比度 ≥ 7（实际 {ratios['main']}:1）",
          float(ratios["main"]) >= 7, str(ratios))
    check(f"次级文字对比度 ≥ 4.5（实际 {ratios['sub']}:1，{ratios.get('rawSub')}）",
          float(ratios["sub"]) >= 4.5, str(ratios))
    await page.close()


async def t_layout_480(browser):
    print("== 480px 布局（含甘特） ==")
    for path in ("/?poll_ms=3000", "/tasks", "/projects/rP1", "/schedule"):
        page = await new_page(browser, path, viewport={"width": 480, "height": 800})
        await page.wait_for_timeout(1500)
        probs = await page.evaluate("""() => {
            const de = document.documentElement;
            return de.scrollWidth > de.clientWidth + 1
                ? ['溢出 ' + de.scrollWidth + '>' + de.clientWidth] : []; }""")
        check(f"[480] {path} 零溢出", not probs, str(probs))
        # 甘特必须在自身容器内滚动（而不是被裁切或撑破页面）
        g = await page.evaluate("""() => {
            const el = document.querySelector('.gantt');
            return el ? {sw: el.scrollWidth, cw: el.clientWidth,
                         ov: getComputedStyle(el).overflowX} : null; }""")
        if g:
            check(f"[480] {path} 甘特容器内滚动（scrollWidth>clientWidth）",
                  g["sw"] > g["cw"] and g["ov"] in ("auto", "scroll"), str(g))
        await page.close()


async def t_schedule_views(browser):
    print("== 日程 列表 / 日历 切换（§3.1） ==")
    page = await new_page(browser, "/schedule")
    await page.wait_for_timeout(1500)
    rows = await page.evaluate("() => document.querySelectorAll('#schedule-list .sched-row').length")
    check("默认列表视图有日程", rows >= 1, str(rows))
    await page.click("#view-toggle button:nth-child(2)")   # 日历视图
    await page.wait_for_timeout(1200)
    cal = await page.evaluate("""() => ({
        heads: document.querySelectorAll('.cal-head').length,
        cells: document.querySelectorAll('.cal-cell').length,
        evs: [...document.querySelectorAll('.cal-ev')].map(a => a.getAttribute('href')),
        today: document.querySelectorAll('.cal-cell.is-today').length,
        listHidden: document.querySelector('#schedule-list').hidden })""")
    check("日历视图：7 列表头 + 28 天单元", cal["heads"] == 7 and cal["cells"] == 28, str(cal))
    check("日历视图：日程以链接呈现", len(cal["evs"]) >= 1
          and all((h or "").startswith("/schedule/") for h in cal["evs"]), str(cal["evs"][:3]))
    check("日历视图：今日高亮且列表已隐藏", cal["today"] == 1 and cal["listHidden"] is True,
          str(cal))
    await page.click("#view-toggle button:nth-child(1)")   # 切回列表
    await page.wait_for_timeout(1200)
    back = await page.evaluate("""() => ({
        rows: document.querySelectorAll('#schedule-list .sched-row').length,
        calHidden: document.querySelector('#schedule-calendar').hidden })""")
    check("切回列表视图正常且日历隐藏", back["rows"] >= 1 and back["calHidden"] is True,
          str(back))
    await page.close()


async def t_project_filters(browser):
    print("== 项目页：主责组筛选 + 含归档开关（§3.1/§5.1） ==")
    page = await new_page(browser, "/projects")
    await page.wait_for_timeout(1500)
    names = await page.evaluate(
        "() => [...document.querySelectorAll('#project-list h2')].map(h => h.textContent)")
    check("归档项目默认隐藏", not any("已归档项目" in (x or "") for x in names), str(names))
    await page.click("#proj-archived-filter button")
    await page.wait_for_timeout(1200)
    names2 = await page.evaluate(
        "() => [...document.querySelectorAll('#project-list h2')].map(h => h.textContent)")
    check("开启「含归档」后可见归档项目",
          any("已归档项目" in (x or "") for x in names2), str(names2))
    # 主责组=运营 → 只剩归档项目（测试项目甲是电控）
    bars = await page.evaluate(
        "() => [...document.querySelectorAll('#proj-group-filter button')].map(b => b.textContent)")
    check("主责组筛选栏来自 /meta 五组", len(bars) == 6, str(bars))
    idx = bars.index("运营") + 1
    await page.click(f"#proj-group-filter button:nth-child({idx})")
    await page.wait_for_timeout(1200)
    names3 = await page.evaluate(
        "() => [...document.querySelectorAll('#project-list h2')].map(h => h.textContent)")
    check("按主责组筛选生效（运营→仅归档项目）",
          any("已归档项目" in (x or "") for x in names3)
          and not any("测试项目甲" in (x or "") for x in names3), str(names3))
    await page.close()


async def t_ui_kit_and_simplify(browser):
    """设计系统 v5（Vue 版）：共享样式由构建产物提供、门户与调试台共用同一份；
    自托管字体可匿名加载；无 HUD / 跑马等旧装饰残留。"""
    print("== 统一设计资源（v5 / Vue 构建产物） ==")
    import re as _re
    import urllib.request

    def get(path, cookie=None):
        req = urllib.request.Request(harness.BASE + path)
        if cookie:
            req.add_header("Cookie", "dsh_session=" + cookie)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except Exception as e:                      # noqa: BLE001
            return getattr(e, "code", None), ""

    for path in ("/ui/fonts/foray-sans.css?v=20260915font1", "/ui/favicon.png"):
        code, _ = get(path)
        check("匿名可加载 %s（200）" % path, code == 200, str(code))
    _, portal_html = get("/tasks", harness._auth["member"])
    _, debug_html = get("/debug/", harness._auth["member"])
    css_p = set(_re.findall(r'href="(/app/assets/[^"]+\.css)"', portal_html))
    css_d = set(_re.findall(r'href="(/app/assets/[^"]+\.css)"', debug_html))
    js_p = set(_re.findall(r'src="(/app/assets/[^"]+\.js)"', portal_html))
    shared = sorted(css_p & css_d)
    check("门户与调试台引用同一份共享样式（资源统一）", bool(shared), f"{css_p} / {css_d}")
    check("门户页面入口脚本来自 /app/assets/（构建产物）", bool(js_p), str(js_p))
    for path in sorted(css_p | js_p):
        code, _ = get(path)
        check("匿名可加载构建产物 %s（200）" % path.rsplit("/", 1)[-1], code == 200, str(code))
    css = "".join(get(path)[1] for path in shared)
    check("共享样式含 v5 设计令牌（--c-paper / --c-yellow / Foray Sans）",
          "--c-paper:" in css and "--c-yellow:" in css and "Foray Sans" in css)
    for bad in ("p-wm-marquee", "p-card-up", "hud::before"):
        check("共享样式无旧装饰规则（%s）" % bad, bad not in css)
    # 页面 HTML：无 HUD 角标、无跑马水印、侧边栏外壳在场
    page = await new_page(browser, "/tasks?poll_ms=1500")
    await page.wait_for_timeout(600)
    html = await page.content()
    check("门户页面无 HUD 角标", "hud hud-tl" not in html)
    check("门户页面无跑马水印", "p-wm-track" not in html)
    check("门户页面为侧边栏外壳", "app-sidebar" in html)
    await page.close()


async def t_v11_login_and_compact(browser):
    """设计 v2：登录页单框居中卡（黄顶条 + 黑黄硬阴影）；门户页头为大幅 h1 + 侧边栏外壳。"""
    print("== v2：居中单框登录 + 大幅页头 ==")
    # 1) 登录页结构：三件套在场，调试台入口/状态条/旧报头都不在
    page = await browser.new_page(viewport={"width": 1360, "height": 860})
    await page.goto(harness.BASE + "/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(500)
    for sel, name in (("#lo-user", "用户名输入"), ("#lo-pass", "密码输入"),
                      ("#feishu-btn", "飞书登录按钮")):
        check("登录页含 %s" % name, await page.query_selector(sel) is not None)
    check("登录页无调试台入口", await page.query_selector('a[href="/debug/"]') is None)
    check("登录页无状态条（同步状态）", await page.query_selector("#sync-line") is None)
    check("登录页无旧报头", await page.query_selector(".p-masthead") is None)
    box = await page.locator(".login-box").bounding_box()
    center = (box["x"] + box["width"] / 2) if box else 0
    check("登录卡居中（中心 x 在视口 35%–65%）",
          1360 * 0.35 < center < 1360 * 0.65, "center=%.0f" % center)
    # v5 特征：登录卡 4px 墨色顶边（看板页签）+ 去掉柔和投影（只用 1px 细线）+ 黄带标题区
    style = await page.evaluate("""() => {
        const el = document.querySelector('.login-box');
        const cs = getComputedStyle(el);
        const head = getComputedStyle(document.querySelector('.login-box-head'));
        return { top: cs.borderTopColor + ' ' + cs.borderTopWidth,
                 shadow: cs.boxShadow, head: head.backgroundImage }; }""")
    check("登录卡墨色顶边（#191919 · 4px）",
          "25, 25, 25" in style["top"] and "4px" in style["top"], style["top"])
    check("登录卡无柔和投影（v5 只用细线分区）", style["shadow"] == "none", style["shadow"][:80])
    check("登录卡标题区为 v5 黄带（#FFFA00）", "255, 250, 0" in style["head"], style["head"][:80])
    # 等高线装饰画布在场（v2 装饰回归）
    check("登录页等高线画布在场",
          await page.query_selector(".login-canvas") is not None)
    await page.close()

    # 门户现用筛选与内容卡片直接开场，页面身份由导航与浏览器标题表达。
    page = await new_page(browser, "/tasks?poll_ms=2000")
    await page.wait_for_timeout(900)
    check("门户页有侧边栏外壳", await page.query_selector(".app-sidebar") is not None)
    navs = await page.evaluate(
        "() => document.querySelectorAll('.side-nav-item').length")
    check("侧边导航 ≥ 5 项（含数据质量）", navs >= 5, str(navs))
    check("事项页当前导航有文字与选中语义",
          "事项" in await page.locator('.side-nav-item[aria-current="page"]').inner_text())
    check("事项页具有具名筛选入口",
          await page.locator('#group-filter[aria-label="按主责组筛选"]').is_visible())
    await page.close()


async def t_sidebar_collapse(browser):
    """阶段 11 收纳：门户侧边栏默认展开、可收纳、开关浮于脊线、调试台同源。"""
    print("== 侧边栏收纳（门户 + 调试台）==")
    page = await new_page(browser, "/?poll_ms=2000")
    await page.wait_for_timeout(900)

    async def w():
        return await page.evaluate(
            "() => Math.round(document.querySelector('.app-sidebar')"
            ".getBoundingClientRect().width)")

    check("门户有收纳开关", await page.query_selector(".side-toggle") is not None)
    check("开关挂在 .app-shell 上（不被侧栏裁剪）", await page.evaluate(
        "() => document.querySelector('.side-toggle')"
        ".parentElement.classList.contains('app-shell')"))
    check("门户默认不收纳（宽度 220）", abs(await w() - 220) <= 3, str(await w()))
    w0 = await w()
    await page.click(".side-toggle")
    await page.wait_for_timeout(420)
    check("收纳后宽度≈64", abs(await w() - 64) <= 3, str(await w()))
    check("收纳后品牌文字淡出隐藏", await page.evaluate(
        "() => { const el = document.querySelector('.side-brand-text'); if (!el) return true;"
        " const s = getComputedStyle(el);"
        " return s.display === 'none' || (s.visibility === 'hidden' && +s.opacity < .05); }"))
    check("收纳后导航文字淡出隐藏", await page.evaluate(
        "() => { const el = document.querySelector('.side-nav-item > .nav-name');"
        " if (!el) return true; const s = getComputedStyle(el);"
        " return s.display === 'none' || (s.visibility === 'hidden' && +s.opacity < .05); }"))
    await page.reload(wait_until="domcontentloaded")
    await page.wait_for_timeout(500)
    check("刷新后仍为收纳态", await page.evaluate(
        "() => document.querySelector('.app-shell').classList.contains('is-collapsed')"))
    await page.evaluate("() => localStorage.removeItem('foray.nav.collapsed')")
    await page.close()

    # 调试台同源（同一脚本/CSS，默认同为展开）
    page = await new_page(browser, "/debug/")
    await page.wait_for_timeout(900)
    check("调试台有收纳开关", await page.query_selector(".side-toggle") is not None)
    check("调试台默认不收纳", not await page.evaluate(
        "() => document.querySelector('.app-shell').classList.contains('is-collapsed')"))
    check("调试台侧栏宽≈220", abs(await w() - 220) <= 3, str(await w()))
    await page.evaluate("() => localStorage.removeItem('foray.nav.collapsed')")
    await page.close()


async def t_v12_links_strip_logo(browser):
    """阶段 10 v1.2：内容链接不用蓝色；状态条只留同步时间；登录页用上传队标（favicon 不变）。"""
    print("== v1.2：链接去蓝 / 状态条精简 / 队标 ==")
    # 1) 内容区链接为墨色（灰度），不是浏览器默认蓝
    page = await new_page(browser, "/?poll_ms=2000")
    await page.wait_for_timeout(1200)
    colors = await page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('#p-main .module a.lnk').forEach(a => {
            const c = getComputedStyle(a).color;
            out.push([a.textContent.trim().slice(0, 12), c]);
        });
        return out.slice(0, 6);
    }""")
    # 墨色是 #08090A（通道 8/9/10），不是严格灰度；这里判"无蓝偏、且足够深"
    ok = True
    for txt, c in colors:
        m = [int(x) for x in c.replace("rgb(", "").replace(")", "").split(",")]
        if len(m) == 3 and (m[2] - m[0] > 16 or sum(m) > 260):
            ok = False
    check("内容区链接为墨色（无蓝偏、非默认蓝）", bool(colors) and ok, str(colors[:3]))
    # 2) 状态条：只有时间（或时间+短后缀）
    sync = (await page.text_content("#sync-line")) or ""
    check("状态条文案精简（同步于 …）", sync.startswith("同步于") or sync.startswith("尚无"),
          sync[:60])
    check("状态条不再含说明性长文案",
          "只读门户" not in sync and "非实时" not in sync, sync[:60])
    # 3) 链接样式：有下划线（可点提示）且悬停变黄
    deco = await page.evaluate("""() => {
        const a = document.querySelector('#p-main .module a.lnk');
        return a ? getComputedStyle(a).textDecorationLine : '';
    }""")
    check("内容链接用下划线提示可点", "underline" in deco, deco)
    await page.close()

    # 4) 登录页：品牌用上传的队标；favicon 仍是原图（字节一致）
    page = await browser.new_page(viewport={"width": 1280, "height": 800})
    await page.goto(harness.BASE + "/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(400)
    logo = await page.query_selector('img.login-brand-mark')
    src = (await logo.get_attribute("src")) if logo else ""
    check("登录页品牌使用 v5 队徽（R1 削角版 SVG）",
          logo is not None and ("svg" in (src or "")), str(src)[:60])
    if logo:
        box = await logo.bounding_box()
        check("队标尺寸可见（≥40px）", box and box["width"] >= 40,
              "%.0f" % (box["width"] if box else 0))
    same = await page.evaluate("""async () => {
        const get = async (u) => (await fetch(u)).arrayBuffer();
        const a = new Uint8Array(await get('/ui/favicon.png'));
        const b = new Uint8Array(await get('/debug/favicon.png'));
        if (a.length !== b.length) return false;
        for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
        return true;
    }""")
    check("站点 favicon 保持原图（与 /debug/favicon.png 字节一致）", same is True)
    await page.close()


async def t_attendance_page(browser):
    print("== 考勤页：本周时长 / 达标 / 签到中 / 今日值日 / 排行（只读） ==")
    page = await new_page(browser, "/attendance")
    await page.locator("#att-week-body tr[data-member]").first.wait_for(timeout=8000)
    nav = await page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.side-nav-item')]
        .map(a => [a.getAttribute('href'), a.querySelector('.nav-no').textContent.trim()]))""")
    check("导航顺序：02 考勤、03 项目、04 事项、05 物资采购、06 日程、07 数据质量",
          nav.get("/attendance") == "02" and nav.get("/projects") == "03"
          and nav.get("/tasks") == "04" and nav.get("/procurement") == "05"
          and nav.get("/schedule") == "06" and nav.get("/quality") == "07", str(nav))
    check("考勤页页头编号 02", "02" in (await page.text_content(".page-no") or ""))
    check("考勤导航项为当前页",
          await page.locator('.side-nav-item[href="/attendance"]').get_attribute("aria-current") == "page")

    async def row(oid):
        return await page.locator(f'#att-week-body tr[data-member="{oid}"]').text_content() or ""
    a, b, c = await row("ou_fix_a"), await row("ou_fix_b"), await row("ou_fix_c")
    check("已结算成员：本周 29.11、未达标（取飞书公式）、上周 31.50 达标、签到中",
          "29.11" in a and "未达标" in a and "31.50" in a and "达标" in a.split("31.50")[1]
          and "签到中" in a, a)
    check("结算周停在上周的成员标「待结算」，不显示跨周累计", "待结算" in b and "10.50" not in b, b)
    check("从未签退的成员本周按 0 小时、未达标", "0.00" in c and "未达标" in c, c)
    check("今日按星期轮换：乙值日，明天按日期排的丙今天不值日", "值日" in b and "值日" not in c, b + " | " + c)
    duty = await page.text_content("#att-duty") or ""
    check("今日值日列出乙与不在成员表的「外援」", "成员乙" in duty and "外援" in duty
          and "不在成员表" in duty and "按星期轮换" in duty, duty)
    summary = await page.text_content("#att-summary") or ""
    check("统计卡：达标 0 / 3、签到中 1 人、今日值日姓名",
          "0 / 3" in summary and "1 人" in summary and "成员乙、外援" in summary, summary)
    ranks = await page.evaluate("() => [...document.querySelectorAll('#attend-rank-box .rank-row')].map(r => r.textContent)")
    check("考勤页含打卡工时排行（与总览同序）", len(ranks) == 3 and "#1" in ranks[0] and "29.11 h" in ranks[0], str(ranks)[:120])
    week = await page.evaluate("fetch('/api/portal/attendance').then(r => r.json()).then(j => j.data.week.key)")
    check("本周区间按北京时间周一起算", week == harness.week_key(), str(week))
    check("非组长看不到请假按钮（页面无编辑入口）",
          await page.locator("#att-week-body button, #att-duty button, .page-actions .btn").count() == 0)
    await page.close()

    # 变体：乙本周已结算且达标；今天另有按日期的值日行（甲）→ 以日期行为准，乙不再值日
    data = fake_data()
    mem = data["tblFixPortalSync5"]["records"][1]["fields"]
    mem.update({"结算周": harness.week_key(), "本周时长": 33.5,
                "本周达标": [{"type": "text", "text": "达标"}], "上周时长": "28"})
    data["tblFixPortalSync8"]["records"].append(
        {"record_id": "rD9", "fields": {"日期": _ms(0, 0, 0),
          "值日人员": [{"id": "ou_fix_a", "name": "成员甲"}]}})
    # 丙本周请假（没签到、没达标）：达标栏与今日栏都显示「请假」，不计入应达标人数
    data["tblFixPortalSync5"]["records"][2]["fields"]["请假周"] = harness.week_key()
    harness._fake_feishu.bitable = data
    trigger_sync()
    page = await new_page(browser, "/attendance")
    await page.locator("#att-week-body tr[data-member]").first.wait_for(timeout=8000)
    a, b = await row("ou_fix_a"), await row("ou_fix_b")
    check("达标成员显示「达标」且进度条为达标色",
          "33.50" in b and "达标" in b and "未达标" not in b.split("33.50")[1].split("28.00")[0]
          and await page.locator('#att-week-body tr[data-member="ou_fix_b"] .prog-fill.ok').count() == 1, b)
    check("上周未达标显示未达标", "28.00" in b and "未达标" in b.split("28.00")[1], b)
    check("按日期的值日行优先于星期轮换", "值日" in a and "值日" not in b, a + " | " + b)
    c = await row("ou_fix_c")
    check("请假成员：没达标显示请假、今日显示请假（不显示未达标）",
          c.count("请假") >= 2 and "未达标" not in c, c)
    summ = await page.text_content("#att-summary") or ""
    check("达标人数随数据更新为 1 / 2（请假的不计入），并注明请假 1 人",
          "1 / 2" in summ and "请假 1 人" in summ, summ)
    order = await page.evaluate("() => [...document.querySelectorAll('#att-week-body tr[data-member]')].map(r => r.dataset.member)")
    check("本周考勤按本周时长降序", order[:2] == ["ou_fix_b", "ou_fix_a"], str(order))
    await page.close()
    harness._fake_feishu.bitable = fake_data()
    trigger_sync()


async def t_quality_page(browser):
    print("== 数据质量明细页（§5.2） ==")
    # 注入两条脏记录（未知枚举 + 失效关联）后同步
    data = fake_data()
    data["tblFixPortalSync2"]["records"].append(
        {"record_id": "rBad1", "fields": {"事项名称": "脏数据-未知枚举",
          "主责组": "神秘组", "P等级": "P9", "状态": "躺平"}})
    data["tblFixPortalSync2"]["records"].append(
        {"record_id": "rBad2", "fields": {"事项名称": "脏数据-失效关联",
          "所属项目": _link("rGONE"), "主责组": "电控", "P等级": "P1",
          "状态": "未开始"}})
    harness._fake_feishu.bitable = data
    trigger_sync()
    page = await new_page(browser, "/quality")
    await page.wait_for_timeout(1600)
    summary = await page.text_content("#quality-summary")
    rows = await page.evaluate("() => document.querySelectorAll('#quality-list tbody tr').length")
    txt = await page.text_content("#quality-list")
    check("质量明细页显示汇总与计数", bool(summary) and "条异常记录" in summary, str(summary))
    check("质量明细页列出异常记录（≥2 条）", rows >= 2, str(rows))
    check("质量明细页含表名与原因",
          ("事项" in (txt or "")) and ("未知" in (txt or "") or "已移除" in (txt or "")),
          (txt or "")[:80])
    await page.close()


async def t_layout_desktop(browser):
    print("== 桌面 1400 排版审计（门户全页，恢复阶段 2 覆盖） ==")
    for path in ("/?poll_ms=3000", "/attendance", "/projects", "/tasks", "/schedule", "/quality",
                 "/login", "/projects/rP1", "/tasks/rT1"):
        page = await new_page(browser, path)
        await page.wait_for_timeout(1200)
        probs = await page.evaluate("""() => {
            const de = document.documentElement;
            return de.scrollWidth > de.clientWidth + 1
                ? ['溢出 ' + de.scrollWidth + '>' + de.clientWidth] : []; }""")
        check(f"[1400] {path} 零横向溢出", not probs, str(probs))
        await page.close()


SUITES = [t_ui_kit_and_simplify, t_v11_login_and_compact, t_cold_start, t_data_render, t_countdown_local, t_poll_refresh,
          t_detail_and_gantt, t_stale_and_empty, t_filled_gaps, t_toasts, t_proc_new_season,
          t_login_no_loop, t_auth_pages, t_schedule_views, t_project_filters,
          t_attendance_page, t_quality_page, t_keyboard_contrast, t_layout_480, t_layout_desktop,
          t_v12_links_strip_logo, t_sidebar_collapse ]   # 放在有数据之后（要渲染内容链接）


async def run_all():
    if not harness._fake_feishu:
        harness.start_fake_feishu(tenant_key="tenant-test")
    if not harness._auth.get("member"):
        if not harness.login_member():
            check("成员登录", False)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for s in SUITES:
            if s is t_data_render:
                harness._fake_feishu.bitable = fake_data()
                st = trigger_sync()
                check("测试数据同步（5 任务）",
                      st.get("counts", {}).get("tasks") == 5, str(st.get("counts")))
            try:
                await s(browser)
            except Exception as e:
                check(f"{s.__name__} 套件异常", False, repr(e))
        await browser.close()
    return len(PASS), len(FAIL)


if __name__ == "__main__":
    def _ensure():
        try:
            urllib.request.urlopen(BASE + "/login", timeout=2)
            return True
        except Exception:
            return harness.start_service()
    if not _ensure():
        print("✗ 测试服务未就绪")
        raise SystemExit(1)
    try:
        npass, nfail = asyncio.run(run_all())
    finally:
        harness.stop_service()
    print(f"\n门户: {npass} 通过, {nfail} 失败")
    raise SystemExit(1 if nfail else 0)
