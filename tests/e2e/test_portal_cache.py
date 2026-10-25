# -*- coding: utf-8 -*-
"""缓存库第 3 步：网页写入走本地队列 + 推送线程（端到端，假飞书）。

覆盖：
1. cache 模式下新建立即返回临时 ID，推送线程推到飞书且只建一条；
2. 用临时 ID 继续修改，推送时自动换成飞书 ID；
3. 同列两边都改 → 飞书优先，冲突留痕；删除推到飞书；
4. 飞书写入暂时失败（500）→ 接口照样立即成功，退避重试后写入；
   飞书明确拒绝 → 置为失败、不无限重试；
5. 排队中的改动跨服务重启不丢；
6. 考勤签到/签退立即生效（本地明细），飞书侧随后写入；
7. direct 模式回归：直写飞书、不排队；
8. 第 4 步：写完立即出现在列表/详情（不等推送与同步）；飞书不可写时页面照样显示
   本地改动；临时 ID 的详情页推送后仍能打开；direct 模式写完也立即可见。
9. 第 5 步：记录带同步标记（待同步 / 同步失败 / 冲突）；推送队列接口；浏览器里
   列表徽标、详情说明、侧栏提示、「数据质量」页的重试 / 放弃 / 确认冲突。
10. 组长在事项详情页标记已完成：服务端记完成时间、写成验收通过；重复完成、
    不存在的事项、非组长都被拒；组员不能改已完成（已归档）的事项。
11. 物资采购编号：新建与编辑都会在 赛季 + 一级分类 齐全时自动取号；已有编号与手填编号不动。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness
from harness import api
from test_portal_sync import fake_data, trigger_and_wait

PASS, FAIL = [], []
TASKS = "tblFixPortalSync2"
MEMBERS = "tblFixPortalSync5"
ME = "ou_e2e_fake_member_001"

CACHE_ENV = {
    "PORTAL_WRITE_MODE": "cache",
    "PORTAL_LOCAL_LEADERS": ME,               # 测试成员按组长处理（写事项需要）
    "ATTENDANCE_CIDRS": "127.0.0.0/8",        # 回环可打卡
    "ATTENDANCE_MIN_SESSION": "1",
    "PORTAL_PUSH_IDLE": "0.5",
    "PORTAL_PUSH_MAX_BACKOFF": "3",           # 退避封顶 3 秒，测试不必久等
    "PORTAL_SYNC_GLOBAL_MIN_GAP": "1",
    "PORTAL_SYNC_RETRIES": "1",
    "PORTAL_SYNC_BACKOFF": "0.2",
}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def cache_status():
    _, st = api("/api/portal/sync-status", auth="member")
    return (st or {}).get("cache") or {} if isinstance(st, dict) else {}


def wait_queue(timeout=20, until=None):
    """等队列推完（pending/pushing 都为 0），或等 until(cache) 为真；返回最后一次概况。"""
    deadline = time.time() + timeout
    c = {}
    while time.time() < deadline:
        c = cache_status()
        if until(c) if until else (c.get("pending") == 0 and c.get("pushing") == 0):
            return c
        time.sleep(0.3)
    return c


def C_resolved(record_id):
    """临时 ID 若已推出去，列表里显示的就是飞书 ID：两者都算"出现了"。"""
    import sqlite3
    try:
        c = sqlite3.connect(harness._tmpdir + "/portal.db")
        row = c.execute("SELECT remote_id FROM id_map WHERE local_id=?", (record_id,)).fetchone()
        c.close()
        return row[0] if row else None
    except Exception:
        return None


def mark_of(path):
    _, d = api(path, auth="member")
    return ((d or {}).get("data") or {}).get("sync") or {}


def wait_mark(path, state, timeout=8, text=""):
    """等记录的同步标记变成 state（None 表示没有标记），且说明里含 text；返回最后一次标记。"""
    deadline = time.time() + timeout
    m = {}
    while time.time() < deadline:
        m = mark_of(path)
        if (m.get("state") if m else None) == state and text in (m or {}).get("text", ""):
            return m
        time.sleep(0.2)
    return m


def queue_idle(c):
    return c.get("failed") == 0 and c.get("pending") == 0 and c.get("pushing") == 0


async def ui_checks():
    """浏览器里看同步标记与「数据质量」页的处理动作（此时有 1 条推送失败、1 条冲突）。"""
    from playwright.async_api import async_playwright
    harness.ensure_browser_env()
    fk = harness._fake_feishu
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 880})
        await page.context.add_cookies([harness.auth_cookie("member")])

        await page.goto(harness.BASE + "/tasks?poll_ms=1000", wait_until="domcontentloaded")
        await page.wait_for_selector("#task-body tr")
        if await page.locator("#accepted-toggle").count():
            await page.click("#accepted-toggle")        # recT3 在测试数据里是已完成（已归档），默认折叠
        failed = await page.locator('#task-body tr:has(a[href="/tasks/recT3"]) .sync-mark').get_attribute("data-sync")
        conflict = await page.locator('#task-body tr:has(a[href="/tasks/recT1"]) .sync-mark').get_attribute("data-sync")
        check("列表：推送失败与冲突的记录带徽标", (failed, conflict) == ("failed", "conflict"),
              f"{failed} {conflict}")
        line = await page.text_content("#sync-line")
        check("侧栏同步行显示推送失败与冲突条数",
              "1 条推送失败" in (line or "") and "1 条冲突待确认" in (line or ""), line)
        check("侧栏给出处理入口", await page.locator("#sync-queue-link").is_visible())

        await page.goto(harness.BASE + "/tasks/recT3", wait_until="domcontentloaded")
        await page.wait_for_selector(".sync-note")
        note = await page.text_content(".sync-note")
        check("详情页显示整句说明（失败原因 + 去哪处理）",
              "同步失败" in note and "数据质量" in note, note)

        await page.goto(harness.BASE + "/quality?poll_ms=1000", wait_until="domcontentloaded")
        await page.wait_for_selector("#queue-failed tbody tr")
        row = await page.text_content("#queue-failed tbody tr")
        check("数据质量页列出推送失败的改动（记录名、改动的列）",
              "推送失败" in row and "具体事项" in row, row)
        crow = await page.text_content("#conflict-list tbody tr")
        check("数据质量页列出冲突（网页上的改动 vs 飞书现行值）",
              "网页改名" in crow and "飞书改名" in crow, crow)

        await page.click("#conflict-list tbody tr button:has-text('确认')")
        await page.wait_for_selector("#conflict-list", state="detached", timeout=5000)
        check("组长确认冲突后列表清空", True)
        check("确认后记录上的冲突徽标消失", not wait_mark("/api/portal/tasks/recT1", None))

        fk.write_fail = {"mode": None, "left": 0}
        await page.click("#queue-failed tbody tr button:has-text('重试')")
        c = wait_queue(until=queue_idle)
        check("组长重试后推到飞书",
              fk._find(TASKS, "recT3")["fields"].get("具体事项") == "会被拒", str(c))
        await page.wait_for_selector("#queue-failed", state="detached", timeout=5000)
        check("重试成功后失败列表清空", True)
        await browser.close()


async def ui_complete():
    """组长在事项详情页点两次「标记已完成」（两步确认），写入成功后给出提示。"""
    from playwright.async_api import async_playwright
    harness.ensure_browser_env()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 880})
        await page.context.add_cookies([harness.auth_cookie("member")])
        await page.goto(harness.BASE + "/tasks/recT1", wait_until="domcontentloaded")
        await page.wait_for_selector("#task-complete button")
        check("详情页不再有「提交验收时间 + 保存」",
              await page.locator('#task-detail input[type="datetime-local"]').count() == 0)
        btn = page.locator("#task-complete button")
        check("组长看到「标记已完成」按钮", (await btn.text_content() or "").strip() == "标记已完成")
        await btn.click()
        armed = (await btn.text_content() or "").strip()
        check("第一次点击只进入待确认", armed.startswith("再点一次"), armed)
        await btn.click()
        await page.wait_for_selector("#toast .toast", timeout=8000)
        note = await page.text_content("#toast .toast-text") or ""
        check("第二次点击后提示写入成功", "已写入飞书" in note or "正在推送" in note, note)
        await browser.close()


def restart(env):
    ok = harness.restart_service(env_extra=env)
    if ok:
        harness.login_member()
    return ok


def run():
    print("== 缓存库第 3 步：队列模式写入（PORTAL_WRITE_MODE=cache）==")
    harness.start_fake_feishu(tenant_key="tenant-test")
    fk = harness._fake_feishu
    fk.bitable = fake_data(with_bad=False)
    check("以 cache 模式重启服务", restart(CACHE_ENV))
    st = trigger_and_wait(None)
    check("首轮同步成功（镜像就绪）", not st.get("last_error"), str(st.get("last_error")))
    c = cache_status()
    check("同步状态带出写入模式、走队列的表与推送线程状态",
          c.get("write_mode") == "cache" and c.get("pusher_alive") is True
          and "tasks" in (c.get("cache_tables") or []), str(c))

    print("-- 新建与临时 ID --")
    code, r = api("/api/portal/write/tasks",
                  {"name": "缓存新建", "owner_group": "电控", "priority": "P1"}, auth="member")
    lid = (r or {}).get("record_id", "") if isinstance(r, dict) else ""
    check("新建立即返回本地临时 ID（queued=true）",
          code == 200 and r.get("queued") is True and lid.startswith("loc_"), f"{code} {r}")
    wait_queue()
    created = [w for w in fk.writes if w[0] == "POST" and w[1] == TASKS]
    check("推送线程把新建推到飞书，且只建一条",
          len(created) == 1 and (created[0][3] or {}).get("事项名称") == "缓存新建", str(created))
    rid = created[0][2] if created else ""
    code, r = api(f"/api/portal/write/tasks/{lid}", {"status": "进行中"}, method="PATCH",
                  auth="member")
    wait_queue()
    rec = fk._find(TASKS, rid) or {}
    check("用临时 ID 修改：推送时自动换成飞书 ID",
          code == 200 and rec.get("fields", {}).get("执行状态") == "进行中", f"{code} {rec}")

    print("-- 写完立即可见（第 4 步：本地视图）--")
    code, r = api("/api/portal/write/projects", {"name": "即时可见", "owner_group": "电控"},
                  auth="member")
    plid = (r or {}).get("record_id", "") if isinstance(r, dict) else ""
    _, lst = api("/api/portal/projects?page_size=200&include_archived=true", auth="member")
    names = {p["name"]: p["id"] for p in (lst.get("data") or [])}
    check("新建后立刻出现在列表里（不等推送与同步）",
          code == 200 and names.get("即时可见") in (plid, C_resolved(plid)), str(names)[:120])
    wait_queue()
    code, d = api(f"/api/portal/projects/{plid}", auth="member")
    check("临时 ID 的详情页推送后仍能打开（自动换成飞书 ID）",
          code == 200 and str((d.get("data") or {}).get("id", "")).startswith("rec"),
          f"{code} {str(d)[:100]}")
    fk.write_fail = {"mode": "500", "left": 10 ** 6}
    code, r = api("/api/portal/write/tasks/recT4", {"name": "飞书不可写也可见"},
                  method="PATCH", auth="member")
    _, d = api("/api/portal/tasks/recT4", auth="member")
    check("飞书不可写时页面照样显示本地改动",
          code == 200 and (d.get("data") or {}).get("name") == "飞书不可写也可见",
          str(d)[:120])
    m = wait_mark("/api/portal/tasks/recT4", "pending", text="自动重试")
    check("排队中的记录带「待同步」标记，重试时带上次失败原因",
          m.get("label") == "待同步" and "自动重试" in m.get("text", ""), str(m))
    check("写入响应的提示不再说「同步后更新」",
          r.get("hint") == "已保存，正在推送到飞书（通常几秒内完成）", str(r.get("hint")))
    fk.write_fail = {"mode": None, "left": 0}
    wait_queue(timeout=30)
    check("飞书恢复后改动推上去", fk._find(TASKS, "recT4")["fields"].get("事项名称")
          == "飞书不可写也可见", str(fk._find(TASKS, "recT4")))

    print("-- 冲突与删除 --")
    fk._find(TASKS, "recT1")["fields"]["事项名称"] = "飞书改名"
    code, r = api("/api/portal/write/tasks/recT1", {"name": "网页改名"}, method="PATCH",
                  auth="member")
    c = wait_queue()
    check("同列两边都改：以飞书为准",
          code == 200 and fk._find(TASKS, "recT1")["fields"]["事项名称"] == "飞书改名",
          str(fk._find(TASKS, "recT1")))
    d = {}
    for _ in range(25):                   # 操作完成与快照重建之间有毫秒级先后：轮询
        _, d = api("/api/portal/tasks/recT1", auth="member")
        if (d.get("data") or {}).get("name") == "飞书改名":
            break
        time.sleep(0.2)
    check("冲突后页面回到飞书的值", (d.get("data") or {}).get("name") == "飞书改名",
          str(d.get("data"))[:120])
    check("冲突留痕（未确认冲突 1 条）", c.get("open_conflicts") == 1, str(c))
    code, r = api("/api/portal/write/tasks/recT2", method="DELETE", auth="member")
    wait_queue()
    check("删除推到飞书", code == 200 and fk._find(TASKS, "recT2") is None, f"{code} {r}")

    print("-- 飞书写入故障 --")
    fk.write_fail = {"mode": "500", "left": 3}
    code, r = api("/api/portal/write/tasks/recT3", {"description": "重试后写入"},
                  method="PATCH", auth="member")
    check("飞书暂时不可写时接口照样立即成功", code == 200 and r.get("queued") is True, f"{code} {r}")
    wait_queue(timeout=30)
    check("退避重试后写入成功",
          fk._find(TASKS, "recT3")["fields"].get("具体事项") == "重试后写入",
          str(fk._find(TASKS, "recT3")))
    fk.write_fail = {"mode": "reject", "left": 0}
    api("/api/portal/write/tasks/recT3", {"description": "会被拒"}, method="PATCH", auth="member")
    c = wait_queue(until=lambda c: c.get("failed") == 1)
    check("飞书明确拒绝 → 置为失败，不无限重试",
          c.get("failed") == 1 and c.get("pending") == 0, str(c))

    print("-- 同步标记与推送队列（第 5 步）--")
    m = wait_mark("/api/portal/tasks/recT3", "failed")
    check("推送失败的记录带「同步失败」标记", m.get("label") == "同步失败", str(m))
    m = mark_of("/api/portal/tasks/recT1")
    check("冲突的记录带「冲突」标记并点名冲突列",
          m.get("state") == "conflict" and "「事项名称」" in m.get("text", ""), str(m))
    _, lst = api("/api/portal/tasks?page_size=200", auth="member")
    marks = {t["id"]: (t.get("sync") or {}).get("state") for t in (lst.get("data") or [])}
    check("列表接口也带标记，没有标记的记录不带 sync 键",
          marks.get("recT3") == "failed" and marks.get("recT1") == "conflict"
          and "sync" not in next(t for t in lst["data"] if t["id"] == "recT4"), str(marks))
    code, q = api("/api/portal/sync-queue", auth="member")
    qd = (q or {}).get("data") or {}
    fo = (qd.get("failed") or [{}])[0]
    co = (qd.get("conflicts") or [{}])[0]
    check("推送队列接口：失败的改动（记录、列、原因）",
          code == 200 and fo.get("record_id") == "recT3" and fo.get("columns") == ["具体事项"]
          and fo.get("error"), str(fo))
    check("推送队列接口：冲突两边的值与组长可处理标记",
          (co.get("field"), co.get("local"), co.get("remote")) == ("事项名称", "网页改名", "飞书改名")
          and qd.get("can_manage") is True, str(co))
    code, r = api("/api/portal/conflicts/999999/ack", {}, auth="member")
    check("确认不存在的冲突 → 404", code == 404, str(code))
    try:
        asyncio.run(ui_checks())
    except Exception as e:
        check("浏览器检查套件异常", False, repr(e))
    fk.write_fail = {"mode": "reject", "left": 0}
    api("/api/portal/write/tasks/recT3", {"description": "要放弃的改动"}, method="PATCH",
        auth="member")
    c = wait_queue(until=lambda c: c.get("failed") == 1)
    fid = ((api("/api/portal/sync-queue", auth="member")[1] or {}).get("data") or {}).get(
        "failed", [{}])[0].get("id")
    code, r = api(f"/api/portal/sync-queue/{fid}/discard", {}, auth="member")
    c = cache_status()
    m = mark_of("/api/portal/tasks/recT3")
    desc = fk._find(TASKS, "recT3")["fields"].get("具体事项")
    check("组长放弃失败的改动：队列清空、标记消失、飞书上不出现被放弃的值",
          code == 200 and queue_idle(c) and not m and desc == "会被拒", f"{code} {c} {m} {desc}")
    code, r = api(f"/api/portal/sync-queue/{fid}/discard", {}, auth="member")
    check("重复放弃 → 409", code == 409, str(code))

    print("-- 跨重启不丢 --")
    fk.write_fail = {"mode": "500", "left": 10 ** 6}
    api("/api/portal/write/tasks/recT3", {"description": "跨重启"}, method="PATCH", auth="member")
    time.sleep(1.5)
    check("飞书持续不可写时重启服务", restart(CACHE_ENV))
    fk.write_fail = {"mode": None, "left": 0}
    wait_queue(timeout=40)
    check("排队中的改动跨服务重启不丢，恢复后推送成功",
          fk._find(TASKS, "recT3")["fields"].get("具体事项") == "跨重启",
          str(fk._find(TASKS, "recT3")))

    print("-- 考勤（本地明细）--")
    code, r = api("/api/portal/attendance/check-in", {}, auth="member")
    check("签到立即返回（queued）", code == 200 and r.get("queued") is True, f"{code} {r}")
    _, st = api("/api/portal/attendance/status", auth="member")
    check("签到后状态立即为签到中（不等同步）", st.get("checked_in") is True, str(st))
    code, r = api("/api/portal/attendance/check-in", {}, auth="member")
    check("重复签到被拒（409）", code == 409, str(code))
    wait_queue()
    mine = [x for x in fk.bitable[MEMBERS]["records"]
            if (x["fields"].get("文本") or [{}])[0].get("id") == ME]
    check("新成员行推到飞书（含最近签到）",
          len(mine) == 1 and isinstance(mine[0]["fields"].get("最近签到"), int), str(mine))
    time.sleep(1.3)
    code, r = api("/api/portal/attendance/check-out", {}, auth="member")
    check("签退立即返回（queued）", code == 200 and r.get("queued") is True, f"{code} {r}")
    _, st = api("/api/portal/attendance/status", auth="member")
    check("签退后状态立即为未签到", st.get("checked_in") is False, str(st))
    wait_queue()
    f = mine[0]["fields"] if mine else {}
    check("签退推到飞书（最近签退 + 同步说明 + 时长按增量写）",
          isinstance(f.get("最近签退"), int) and str(f.get("同步说明", "")).startswith("门户签退")
          and isinstance(f.get("有效时长（小时）"), (int, float)), str(f))

    print("-- 请假（组长在考勤页设置）--")
    _, ap = api("/api/portal/attendance", auth="member")
    rows = (ap.get("data") or {}).get("members") or []
    row = next((r for r in rows if r.get("week_ok") is not True), rows[0] if rows else {})
    who = (row.get("member") or {}).get("id")
    code, r = api("/api/portal/attendance/leave", {"record_id": row.get("id"), "on": True},
                  auth="member")
    _, ap = api("/api/portal/attendance", auth="member")
    mine = next((x for x in ((ap.get("data") or {}).get("members") or [])
                 if (x.get("member") or {}).get("id") == who), {})
    check("组长标本周请假：立即显示请假（没达标 → 请假）",
          code == 200 and mine.get("on_leave") is True and mine.get("leave") is True
          and (ap.get("data") or {}).get("summary", {}).get("leave") == 1, f"{code} {r} {mine}")
    wait_queue()
    rec = fk._find(MEMBERS, row.get("id")) or {}
    check("请假推到飞书：「请假周」= 本周一",
          (rec.get("fields") or {}).get("请假周") == harness.week_key(), str(rec.get("fields")))
    code, r = api("/api/portal/attendance/leave", {"record_id": row.get("id"), "on": False},
                  auth="member")
    wait_queue()
    check("取消请假：飞书「请假周」清空",
          code == 200 and not (fk._find(MEMBERS, row.get("id")) or {}).get("fields", {}).get("请假周"),
          str((fk._find(MEMBERS, row.get("id")) or {}).get("fields")))
    code, _ = api("/api/portal/attendance/leave", {"record_id": row.get("id")}, auth="member")
    check("缺 on 参数 → 422", code == 422, str(code))

    print("-- direct 模式回归 --")
    env = dict(CACHE_ENV, PORTAL_WRITE_MODE="direct")
    check("以 direct 模式重启服务", restart(env))
    n = len(fk.writes)
    code, r = api("/api/portal/write/tasks",
                  {"name": "直写新建", "owner_group": "电控", "priority": "P2"}, auth="member")
    check("direct 模式直写飞书（不排队、返回飞书 ID）",
          code == 200 and r.get("queued") is False
          and str(r.get("record_id", "")).startswith("rec")
          and any(w[0] == "POST" for w in fk.writes[n:]), f"{code} {r}")
    c = cache_status()
    check("direct 模式下推送线程照常在跑（排空遗留队列用）", c.get("pusher_alive") is True, str(c))
    code, r = api("/api/portal/write/projects", {"name": "直写即时可见", "owner_group": "机械"},
                  auth="member")
    _, lst = api("/api/portal/projects?page_size=200&include_archived=true", auth="member")
    check("direct 模式写完也立即出现在列表里（镜像写回 + 本地重建）",
          code == 200 and any(p["name"] == "直写即时可见" for p in (lst.get("data") or [])),
          str([p["name"] for p in (lst.get("data") or [])])[:120])


    print("-- 组长标记已完成 --")
    before = int(time.time() * 1000)
    try:
        asyncio.run(ui_complete())
    except Exception as e:
        check("标记已完成浏览器检查异常", False, repr(e))
    f = (fk._find(TASKS, "recT1") or {}).get("fields") or {}
    done_at = f.get("验收时间")
    check("完成写成 待验收 + 验收结论通过（归一为已完成）",
          (f.get("执行状态"), f.get("验收结论")) == ("待验收", "通过"), str(f))
    check("完成时间由服务端自动记为此刻",
          isinstance(done_at, int) and before - 1000 <= done_at <= int(time.time() * 1000) + 1000,
          f"{before} {done_at}")
    check("提交验收时间没填过的同填完成时间",
          isinstance(done_at, int) and f.get("提交验收时间") == done_at, str(f))
    code, r = api("/api/portal/write/tasks/recT3/complete", {}, auth="member")
    check("已完成的事项再标记 → 409", code == 409, f"{code} {r}")
    code, r = api("/api/portal/write/tasks/recNOPE/complete", {}, auth="member")
    check("不存在的事项 → 404", code == 404, f"{code} {r}")

    print("-- 采购编号自动生成 --")
    PROC = "tblFixPortalSync7"

    def proc_code(rid):
        return ((fk._find(PROC, rid) or {}).get("fields") or {}).get("采购编号")

    code, r = api("/api/portal/write/procurement",
                  {"name": "编号-齐全", "season": "2027赛季", "category1": "标准件"}, auth="member")
    rid = r.get("record_id")
    check("新建填了赛季与一级分类：按同前缀最大号 +1 取号，并在提示里给出编号",
          code == 200 and proc_code(rid) == "2027-BZJ-0003" and "2027-BZJ-0003" in r.get("hint", ""),
          f"{code} {proc_code(rid)} {r}")
    code, r = api("/api/portal/write/procurement", {"name": "编号-先建后补"}, auth="member")
    rid = r.get("record_id")
    check("缺赛季或一级分类：不取号，提示补上后自动生成",
          code == 200 and proc_code(rid) is None and "补上后自动生成" in r.get("hint", ""),
          f"{code} {proc_code(rid)} {r}")
    code, r = api(f"/api/portal/write/procurement/{rid}",
                  {"season": "2027赛季", "category1": "电机"}, method="PATCH", auth="member")
    check("编辑时补上赛季与一级分类 → 自动补编号（此前只在新建时取号）",
          code == 200 and proc_code(rid) == "2027-DJ-0001", f"{code} {proc_code(rid)} {r}")
    code, r = api(f"/api/portal/write/procurement/{rid}", {"category1": "标准件"},
                  method="PATCH", auth="member")
    check("已有编号的记录改分类不重新取号", code == 200 and proc_code(rid) == "2027-DJ-0001",
          f"{code} {proc_code(rid)}")
    code, r = api(f"/api/portal/write/procurement/{rid}", {"purchased": True},
                  method="PATCH", auth="member")
    check("与编号无关的修改不带编号说明", code == 200 and "编号" not in r.get("hint", ""), str(r))
    code, r = api("/api/portal/write/procurement",
                  {"name": "编号-手填", "season": "2027赛季", "category1": "标准件", "code": "自定义-01"},
                  auth="member")
    check("手填编号原样保留", code == 200 and proc_code(r.get("record_id")) == "自定义-01",
          f"{code} {r}")

    harness.restart_service()                 # 恢复默认环境，不影响后续套件
    harness.login_member()
    n = len(fk.writes)
    code, r = api("/api/portal/write/tasks/recT4/complete", {}, auth="member")
    check("非组长标记已完成 → 403，且不写飞书",
          code == 403 and len(fk.writes) == n, f"{code} {r}")
    code, r = api("/api/portal/write/tasks/recT3", {"status": "进行中"}, method="PATCH",
                  auth="member")
    check("组员不能把已完成（已归档）的事项改回进行中 → 403，且不写飞书",
          code == 403 and len(fk.writes) == n, f"{code} {r}")
    code, r = api("/api/portal/write/tasks/recT1", {"status": "进行中"}, method="PATCH",
                  auth="member")
    check("组员改未完成事项的执行状态照常可以",
          code == 200 and len(fk.writes) == n + 1, f"{code} {r}")


if __name__ == "__main__":
    if not harness.start_service():
        print("✗ 测试服务未就绪")
        raise SystemExit(1)
    try:
        run()
    finally:
        harness.stop_service()
    print(f"\n缓存库: {len(PASS)} 通过, {len(FAIL)} 失败")
    raise SystemExit(1 if FAIL else 0)
