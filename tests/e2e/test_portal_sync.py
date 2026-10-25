"""阶段 4 同步与只读 API 验收（模拟 API + 真实服务进程）。

覆盖提示词全部场景：多页、令牌过期、429、部分表失败、记录删除、冷启动、
错误字段、跨日边界（compute 直测）、手动限频合并、API 认证与 envelope、
同步不阻塞 WS 遥测。
"""
import asyncio
import datetime
import json
import time

import websockets

import harness
from harness import BASE, api

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def note(key, value):
    """定量观察（与验收套件同名，便于报告统一引用）。"""
    print(f"  · {key}: {value}")


# ---------------- 假数据 ----------------

def _ms(y, m, d, hh=0, mm=0):
    return int(datetime.datetime(y, m, d, hh, mm,
                                  tzinfo=datetime.timezone(
                                      datetime.timedelta(hours=8))).timestamp() * 1000)


FAKE_META = {
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


def _link(rid):
    return [{"record_ids": [rid], "table_id": "tblFixPortalSync1",
             "text": "x", "text_arr": ["x"], "type": "text"}]


def fake_data(with_bad=True):
    """4 任务（多页基础）+ 2 项目 + 2 节点 + 2 日程；with_bad 追加脏数据。"""
    tasks = [
        {"record_id": "recT1", "fields": {"事项名称": "打样", "所属项目": _link("recP1"),
          "主责组": "硬件", "P等级": "P0", "状态": "进行中",
          "截止时间": _ms(2026, 9, 8), "截止时间精度": "仅日期"}},
        {"record_id": "recT2", "fields": {"事项名称": "联调", "所属项目": _link("recP1"),
          "主责组": "算法", "P等级": "P1", "状态": "受阻",
          "截止时间": _ms(2026, 9, 20), "截止时间精度": "仅日期",
          "阻塞说明": "等电机"}},
        {"record_id": "recT3", "fields": {"事项名称": "预约", "主责组": "运营",
          "P等级": "P2", "状态": "已完成",
          "截止时间": _ms(2026, 9, 5), "截止时间精度": "仅日期",
          "完成时间": _ms(2026, 9, 4)}},
        {"record_id": "recT4", "fields": {"事项名称": "归档", "所属项目": _link("recP2"),
          "主责组": "运营", "P等级": "P3", "状态": "已取消"}},
    ]
    if with_bad:
        tasks.append({"record_id": "recT9", "fields": {
            "事项名称": "脏数据-未知枚举", "所属项目": _link("recP1"),
            "主责组": "神秘组", "P等级": "P9", "状态": "躺平",
            "截止时间": _ms(2026, 9, 10), "截止时间精度": "大概"}})
        tasks.append({"record_id": "recT8", "fields": {
            "事项名称": "脏数据-失效关联", "所属项目": _link("recGONE"),
            "主责组": "电控", "P等级": "P1", "状态": "未开始"}})
    return {
        "tblFixPortalSync1": {"meta": FAKE_META["tblFixPortalSync1"], "records": [
            {"record_id": "recP1", "fields": {"项目名称": "哨兵", "主责组": "电控",
              "协作组": ["机械", "算法"], "项目状态": "进行中",
              "负责人": [{"id": "ou_fixa", "name": "成员甲"}],
              "计划开始": _ms(2026, 9, 1), "计划结束": _ms(2026, 12, 1)}},
            {"record_id": "recP2", "fields": {"项目名称": "官网", "主责组": "运营",
              "项目状态": "筹备中"}}]},
        "tblFixPortalSync2": {"meta": FAKE_META["tblFixPortalSync2"], "records": tasks},
        "tblFixPortalSync3": {"meta": FAKE_META["tblFixPortalSync3"], "records": [
            {"record_id": "recM1", "fields": {"节点名称": "区域赛", "节点类型": "比赛",
              "目标日期时间": _ms(2026, 12, 10), "时间精度": "仅日期",
              "展示状态": "展示"}},
            {"record_id": "recM2", "fields": {"节点名称": "检查", "节点类型": "关键检查",
              "目标日期时间": _ms(2026, 11, 8, 14, 0), "时间精度": "精确时刻",
              "展示状态": "展示", "所属项目": _link("recP1")}}]},
        "tblFixPortalSync4": {"meta": FAKE_META["tblFixPortalSync4"], "records": [
            {"record_id": "recS1", "fields": {"日程名称": "例会",
              "开始时间": _ms(2026, 9, 20, 19, 30), "结束时间": _ms(2026, 9, 20, 21, 0),
              "全天": False, "活动类型": "例会", "状态": "正常"}},
            {"record_id": "recS2", "fields": {"日程名称": "联调",
              "开始时间": _ms(2026, 9, 22), "结束时间": _ms(2026, 9, 23),
              "全天": True, "活动类型": "联调", "相关组": ["电控"],
              "状态": "正常"}}]},
        # 成员数据：数字字段刻意用字符串（真实返回形状），末行留空模拟用户保留的空行
        "tblFixPortalSync5": {"meta": FAKE_META["tblFixPortalSync5"], "records": [
            {"record_id": "recMem1", "fields": {
              "成员": [{"id": "ou_fixa", "name": "成员甲"}],
              "有效时长（小时）": "29.11", "到期事项数": "3",
              "准时完成率": 0.333333333333333, "最近签到": _ms(2026, 9, 12, 9, 4),
              "异常考勤数": "0", "同步时间": _ms(2026, 9, 12, 18, 5),
              "统计说明": [{"text": "按到期事项统计", "type": "text"}]}},
            {"record_id": "recMem2", "fields": {
              "成员": [{"id": "ou_fixb", "name": "成员乙"}],
              "有效时长（小时）": "26.1", "异常考勤数": "2",
              "同步说明": "事项：已同步；考勤：已读取原生考勤"}},
            {"record_id": "recMem3", "fields": {
              "成员": [{"id": "ou_fixc", "name": "成员丙"}]}},
            {"record_id": "recMem4", "fields": {}}]},
        "tblFixPortalSync6": {"meta": FAKE_META["tblFixPortalSync6"], "records": [
            {"record_id": "recR1", "fields": {
              "职责名称": "机械组", "组别": "机械",
              "组长": [{"id": "ou_fixa", "name": "成员甲"}],
              "组员": [{"id": "ou_fixb", "name": "成员乙"}]}},
            {"record_id": "recR2", "fields": {
              "职责名称": "电控组", "组别": "电控",
              "组长": [{"id": "ou_fixb", "name": "成员乙"}],
              "组员": [{"id": "ou_fixa", "name": "成员甲"}]}}]},
        # 物资明细：覆盖真实返回形状（数字=字符串、公式=数值、日期=毫秒、
        # 复选=布尔、多选=数组、附件=[{name,file_token,url}]）
        "tblFixPortalSync7": {"meta": FAKE_META["tblFixPortalSync7"], "records": [
            {"record_id": "recU1", "fields": {
              "采购物资名称": "碳纤维管 ⌀12×1mm", "申请组别": ["机械"],
              "购物链接": "https://example.com/cf", "用途说明": "发射管材",
              "赛季": "2027赛季", "一级分类": "加工件", "二级分类": "管材",
              "采购编号": "2027-JGJ-0001", "采购日期": _ms(2026, 9, 28),
              "单价（元）": "42.5", "数量": "6", "总价（元）": 255,
              "采购人": [{"id": "ou_fixa", "name": "成员甲"}],
              "组长批复": True, "已采购": True, "发票已开": True,
              "发票文件": [{"name": "发票-碳纤维管.pdf", "file_token": "ft_inv1"}],
              "图片补充": [{"name": "实物.jpg", "file_token": "ft_p1",
                            "url": "https://example.com/p1.jpg"}]}},
            {"record_id": "recU2", "fields": {
              "采购物资名称": "M3×6 不锈钢螺丝", "申请组别": ["机械", "硬件"],
              "赛季": "2027赛季", "一级分类": "标准件", "二级分类": "螺丝",
              "采购编号": "2027-BZJ-0002", "采购日期": _ms(2026, 10, 5),
              "单价（元）": "0.35", "数量": "200", "总价（元）": 70,
              "采购人": [{"id": "ou_fixb", "name": "成员乙"}],
              "用途说明": "结构固定", "已采购": True, "发票已开": False}},
            {"record_id": "recU3", "fields": {
              "采购物资名称": "STM32H723 开发板", "申请组别": ["电控"],
              "赛季": "2026赛季", "一级分类": "电子元器件", "二级分类": "主控板",
              "采购编号": "2026-DZ-0007", "采购日期": _ms(2026, 8, 20),
              "单价（元）": "128", "数量": "2", "总价（元）": 256,
              "用途说明": "备用主控板"}}]},
        # 值日表：本套件不展示，给出字段齐全的空表（结构性校验要求配置的字段都存在）
        "tblFixPortalSync8": {"meta": FAKE_META["tblFixPortalSync8"], "records": []},
    }


def trigger_and_wait(fake, timeout=25):
    """触发同步并等待完成（last_attempt_at 变化且非进行中）。"""
    import urllib.request
    before = api("/api/portal/sync-status", auth="member")[1].get("last_attempt_at")
    time.sleep(1.2)                       # 越过测试限频间隔（1s）
    for attempt in range(3):
        req = urllib.request.Request(
            BASE + "/api/portal/sync", data=b"{}", method="POST",
            headers={"Authorization": "Bearer " + harness._auth["member"],
                     "Content-Type": "application/json"})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if r.get("accepted"):
                break
        except Exception:
            pass
        time.sleep(1.2)
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, st = api("/api/portal/sync-status", auth="member")
        att = st.get("last_attempt_at")
        if att and att != before and not st.get("syncing"):
            time.sleep(0.5)
            return api("/api/portal/sync-status", auth="member")[1]
        time.sleep(0.6)
    return api("/api/portal/sync-status", auth="member")[1]


def _reset_db():
    """清空快照库（本套件冷启动断言自包含；test_portal 可能已建快照）。"""
    import sqlite3
    c = sqlite3.connect(harness._tmpdir + "/portal.db")
    c.execute("DELETE FROM snapshot")
    c.execute("DELETE FROM quality")
    c.execute("DELETE FROM remote_records")
    c.execute("DELETE FROM meta WHERE key != 'schema_version'")
    c.commit()
    c.close()


def _make_bad_schema_db(path, version="99"):
    """造一个"未知版本但含业务行"的快照库（用于"零写入"对照）。"""
    import sqlite3
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS snapshot(version INTEGER, table_name TEXT,"
              " record_id TEXT, data TEXT, PRIMARY KEY(version, table_name, record_id))")
    c.execute("CREATE TABLE IF NOT EXISTS quality(version INTEGER, table_name TEXT,"
              " record_id TEXT, issue TEXT)")
    c.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                  [("schema_version", version), ("current_version", "7"),
                   ("counts", '{"projects": 1}'), ("last_error", "")])
    c.execute("INSERT OR REPLACE INTO snapshot(version, table_name, record_id, data)"
              " VALUES(7, 'projects', 'recX', '{\"id\":\"recX\"}')")
    c.commit()
    c.close()


def _db_fingerprint(path):
    """库指纹：**文件字节 sha256** + 行数 + 版本（证明未知版本库零写入）。"""
    import hashlib
    import sqlite3
    raw = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
    c = sqlite3.connect(path)
    try:
        rows = {k: c.execute("SELECT COUNT(*) FROM " + k).fetchone()[0]
                for k in ("meta", "snapshot", "quality")}
        ver = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    finally:
        c.close()
    return rows, (ver[0] if ver else None), raw


def t_token_cache():
    """阶段 8：令牌按响应 expire 复用（修复前每轮同步都取一次 token）。"""
    print("== 令牌缓存（跨轮复用） ==")
    harness._fake_feishu.bitable = fake_data(with_bad=False)
    check("重启（5s 定时 + 长复核间隔，隔掉复核噪声）",
          harness.restart_service(env_extra={
              "PORTAL_SYNC_INTERVAL": "5", "PORTAL_MANUAL_MIN_GAP": "1",
              "AUTH_RECHECK_INTERVAL": "3600"}))
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    tok0 = harness._feishu_calls("token") or 0
    bt0 = harness._feishu_calls("bitable") or 0
    deadline = time.time() + 20
    rounds = 0
    last = None
    while time.time() < deadline and rounds < 3:
        time.sleep(1)
        _, st = api("/api/portal/sync-status", auth="member")
        att = st.get("last_attempt_at")
        if att and att != last and not st.get("last_error"):
            last = att
            rounds += 1
    d_tok = (harness._feishu_calls("token") or 0) - tok0
    d_bt = (harness._feishu_calls("bitable") or 0) - bt0
    check("至少完成 3 轮同步（基准）", rounds >= 3, f"rounds={rounds}")
    check("多轮同步只取一次令牌（缓存生效，修复前=每轮 1 次）",
          rounds >= 3 and d_tok <= 1, f"rounds={rounds} token+{d_tok} bitable+{d_bt}")
    note("令牌缓存", f"{rounds} 轮同步期间 token 请求 +{d_tok}、bitable 请求 +{d_bt}")
    # 收尾：把服务恢复成常规测试态（复核 2s），避免 AUTH_RECHECK_INTERVAL=3600
    # 泄漏给后续套件（曾导致 test_auth 的"复核不可用"用例超时）
    harness.restart_service(env_extra={"PORTAL_SYNC_INTERVAL": "3600",
                                       "PORTAL_MANUAL_MIN_GAP": "1"})
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()


def t_manual_per_user():
    """阶段 8：手动刷新限频——全局下限 + 按用户窗口（全部走 HTTP 验证）。

    顺序（restart 后调度器刚跑完启动轮，全局下限处于生效期）：
      ① 立刻触发 → 被"全局下限"拒绝（避免与刚发生的同步重复）；
      ② 等过全局窗口 → 接受；
      ③ 立刻再触发 → 被"按用户窗口"拒绝；
      ④ 等过用户窗口 → 再次接受。
    """
    print("== 手动刷新：全局下限 + 按用户限频 ==")
    check("重启（同步开、限频 3s、全局下限 3s）", harness.restart_service(env_extra={
        "PORTAL_SYNC_INTERVAL": "3600", "PORTAL_MANUAL_MIN_GAP": "3",
        "PORTAL_SYNC_GLOBAL_MIN_GAP": "3"}))
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()

    def trigger():
        _, r = api("/api/portal/sync", body={}, method="POST", auth="member")
        return r.get("accepted"), r.get("detail", "")

    a1 = trigger()
    check("启动轮刚结束即触发 → 被全局下限拒绝（不重复拉飞书）",
          a1[0] is False and "刚刚已刷新" in a1[1], str(a1))
    time.sleep(3.4)
    a2 = trigger()
    check("全局窗口过后 → 接受", a2[0] is True, str(a2))
    time.sleep(0.4)
    a3 = trigger()
    check("同一用户窗口内再触发 → 按用户拒绝", a3[0] is False and "频繁" in a3[1], str(a3))
    time.sleep(3.4)
    a4 = trigger()
    check("用户窗口过后 → 再次接受", a4[0] is True, str(a4))
    # 三次同步之间至少隔了全局下限（用 bitable 增量间接核对没有被放大）
    note("手动限频", "全局下限 3s + 按用户 3s：上述 4 次调用最终只放行 2 次")


def t_schema_mismatch():
    """阶段 7 条款：快照库版本不可识别时必须"故障关闭"——不降级读、不改写。"""
    print("== 快照库 schema 版本不匹配（故障关闭） ==")
    bad = harness._tmpdir + "/bad-schema.db"
    _make_bad_schema_db(bad)
    rows0, ver0, fp0 = _db_fingerprint(bad)
    check("服务带未知版本快照库仍能启动（不阻塞启动）",
          harness.restart_service(env_extra={
              "PORTAL_DB": bad, "PORTAL_SYNC_INTERVAL": "3600",
              "PORTAL_MANUAL_MIN_GAP": "1"}))
    # 基线放在重启**之后**取：旧进程在途的请求会落进同一个假飞书计数器，
    # 放在重启前取会把它误算到新进程头上（本用例只关心新进程是否读了上游）
    bt0 = harness._feishu_calls("bitable")
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    code, r = api("/api/portal/overview", auth="member")
    body = r if isinstance(r, dict) else {}
    check("管理数据接口 → 503 且 code=schema_version_mismatch",
          code == 503 and body.get("code") == "schema_version_mismatch",
          f"{code} {str(r)[:80]}")
    check("503 明确给出库内版本与本程序期望版本",
          str(body.get("schema_found")) == "99"
          and str(body.get("schema_expected")) == "2",
          f"found={body.get('schema_found')} expected={body.get('schema_expected')}")
    check("503 提示指向运维手册与快照路径",
          "portal-operations" in str(body.get("hint", ""))
          and str(body.get("snapshot_path", "")).endswith("bad-schema.db"),
          str(body.get("hint"))[:70])
    code, _ = api("/api/portal/sync-status", auth="member")
    check("同步状态接口同样 503（不假装无错误）", code == 503, str(code))
    code, _ = api("/api/v2/devices")
    check("调试 API 不受影响（门户存储故障不拖垮调试台）", code == 200, str(code))
    # 手动触发一轮同步：必须在读上游之前失败，且一行都不写
    api("/api/portal/sync", body={}, method="POST", auth="member")
    time.sleep(6)
    rows1, ver1, fp1 = _db_fingerprint(bad)
    check("未知版本库零写入（文件字节 sha256 与行数均不变）",
          fp1 == fp0 and ver1 == ver0 and rows0 == rows1,
          f"sha {fp0}->{fp1} rows {rows0}->{rows1} ver={ver1}")
    check("失败发生在读上游之前（未知版本库进程零 bitable 请求）",
          (harness._feishu_calls("bitable") or 0) == bt0,
          f"{bt0}->{harness._feishu_calls('bitable')}")
    check("恢复常规快照库后重启", harness.restart_service(env_extra={
        "PORTAL_DB": harness._tmpdir + "/portal.db",
        "PORTAL_SYNC_INTERVAL": "3600", "PORTAL_MANUAL_MIN_GAP": "1"}))
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    code, r = api("/api/portal/overview", auth="member")
    check("恢复后管理数据接口回到 200 且含 envelope",
          code == 200 and "snapshot_version" in r, f"{code} {str(r)[:60]}")


def run():
    _reset_db()
    print("== 冷启动（无快照状态区分） ==")
    harness.start_fake_feishu(tenant_key="tenant-test")   # 无 bitable 数据
    harness.login_member()
    code, st = api("/api/portal/sync-status", auth="member")
    check("冷启动 has_snapshot=false", code == 200 and st["has_snapshot"] is False)
    code, r = api("/api/portal/projects", auth="member")
    check("冷启动 data=null + 明确消息",
          r.get("data") is None and "尚无可用同步数据" in r.get("message", ""), str(r)[:80])
    # 空表 ≠ 无快照：同步"字段齐全但零记录"的合法空表成功后 data=[]
    harness._fake_feishu.bitable = {tid: {"meta": meta, "records": []}
                                    for tid, meta in FAKE_META.items()}
    st = trigger_and_wait(None)
    code, r = api("/api/portal/projects", auth="member")
    check("空表同步后 data=[] 且 has_snapshot=true",
          r.get("data") == [] and r.get("has_snapshot") is True, str(r)[:90])

    print("== 结构性字段缺失 → 本次快照失败（保留旧版，§6.4.6） ==")
    ver_before = api("/api/portal/sync-status", auth="member")[1]["snapshot_version"]
    harness._fake_feishu.bitable = {
        tid: {"meta": [m for m in meta if m[1] != "fldTstatus"], "records": []}
        for tid, meta in FAKE_META.items()}
    st = trigger_and_wait(None)
    check("配置字段在源表不存在 → 同步失败（last_error=config）",
          st.get("last_error") == "config", str(st.get("last_error")))
    check("失败保留旧快照版本", st.get("snapshot_version") == ver_before,
          f"{st.get('snapshot_version')} vs {ver_before}")
    harness._fake_feishu.bitable = {tid: {"meta": meta, "records": []}
                                    for tid, meta in FAKE_META.items()}
    trigger_and_wait(None)

    print("== 多页 / 翻页对账 ==")
    harness._fake_feishu.bitable = fake_data(with_bad=True)
    st = trigger_and_wait(None)
    check("同步成功（五表计数 2/6/2/2/3）",
          [st.get("counts", {}).get(t) for t in
           ("projects", "tasks", "milestones", "schedule", "members")] == [2, 6, 2, 2, 3],
          str(st.get("counts")))
    code, r = api("/api/portal/tasks", auth="member")
    check("任务 6 条", len(r["data"]) == 6, str(len(r.get("data") or [])))

    print("== 飞书镜像（本地缓存库的合并基准，2026-10-21 起）==")
    import sqlite3
    c = sqlite3.connect(harness._tmpdir + "/portal.db")
    try:
        per = dict(c.execute("SELECT table_name, COUNT(*) FROM remote_records "
                             "GROUP BY table_name").fetchall())
        row = c.execute("SELECT fields FROM remote_records WHERE table_name='tasks' "
                        "AND record_id='recT3'").fetchone()
        mirror_err = c.execute("SELECT value FROM meta WHERE key='remote_mirror_error'"
                               ).fetchone()
    finally:
        c.close()
    check("同步顺带写入镜像（各表行数与快照一致，脏数据也在）",
          per.get("tasks") == 6 and per.get("projects") == 2, str(per))
    check("镜像按飞书列名保存原始值（写入接口用同一套列名）",
          row is not None and json.loads(row[0]).get("事项名称") == "预约",
          str(row)[:80])
    check("镜像刷新无错误", not (mirror_err and mirror_err[0]), str(mirror_err))

    print("== 物资明细（2026-10-12 新增字段）==")
    from urllib.parse import quote
    code, r = api("/api/portal/procurement", auth="member")
    check("物资明细 3 条", len(r["data"]) == 3, str(len(r.get("data") or [])))
    by_id = {p["id"]: p for p in r["data"]}
    u1 = by_id.get("recU1") or {}
    check("明细字段回读（赛季/分类/编号/日期/金额/发票）",
          u1.get("season") == "2027赛季" and u1.get("category1") == "加工件"
          and u1.get("category2") == "管材" and u1.get("code") == "2027-JGJ-0001"
          and (u1.get("purchase_date") or {}).get("raw_ms") == _ms(2026, 9, 28)
          and u1.get("total_price") == 255 and u1.get("invoice_done") is True,
          str(u1))
    check("数字字符串已强转数值（\"42.5\"→42.5、\"6\"→6）",
          u1.get("unit_price") == 42.5 and u1.get("quantity") == 6,
          str((u1.get("unit_price"), u1.get("quantity"))))
    check("采购人归一为 [{id,name}]",
          (u1.get("purchaser") or [{}])[0].get("name") == "成员甲",
          str(u1.get("purchaser")))
    check("发票文件与图片附件分列归一",
          (u1.get("invoice_files") or [{}])[0].get("name") == "发票-碳纤维管.pdf"
          and (u1.get("attachments") or [{}])[0].get("name") == "实物.jpg",
          str((u1.get("invoice_files"), u1.get("attachments"))))
    check("汇总：3 条合计 581",
          (r.get("stats") or {}).get("count") == 3
          and (r.get("stats") or {}).get("amount") == 581, str(r.get("stats")))
    opts = r.get("options") or {}
    check("下拉可选项 = 常量清单 ∪ 快照实际值",
          "2027赛季" in (opts.get("seasons") or [])
          and "其他" in (opts.get("category1") or []), str(opts))
    code, r = api("/api/portal/procurement?season=" + quote("2026赛季"), auth="member")
    check("按赛季筛选（2026赛季 → 仅 STM32）",
          [p["id"] for p in r["data"]] == ["recU3"]
          and (r.get("stats") or {}).get("amount") == 256,
          str([p["id"] for p in r["data"]]))
    code, r = api("/api/portal/procurement?invoice=done", auth="member")
    check("按开票状态筛选（已开 → 仅 recU1）",
          [p["id"] for p in r["data"]] == ["recU1"],
          str([p["id"] for p in r["data"]]))
    code, r = api("/api/portal/procurement?category1=" + quote("标准件")
                  + "&category2=" + quote("螺丝"), auth="member")
    check("按一二级分类筛选（标准件/螺丝 → 仅 recU2）",
          [p["id"] for p in r["data"]] == ["recU2"],
          str([p["id"] for p in r["data"]]))
    code, r = api("/api/portal/procurement?q=STM32", auth="member")
    check("关键词搜索命中物资名称",
          [p["id"] for p in r["data"]] == ["recU3"],
          str([p["id"] for p in r["data"]]))
    code, r = api("/api/portal/procurement?sort=amount", auth="member")
    check("按总价降序排序（飞书公式列）",
          [p["id"] for p in r["data"]] == ["recU3", "recU1", "recU2"],
          str([(p["id"], p.get("total_price")) for p in r["data"]]))
    code, r = api("/api/portal/quality", auth="member")
    check("物资明细不影响质量异常计数（仍 2 条）",
          (r.get("total") if isinstance(r, dict) else None) == 2, str(r)[:120])

    print("== 总览：倒计时 / 项目摘要移除 / 打卡工时排行 ==")
    code, r = api("/api/portal/overview", auth="member")
    ov = r.get("data") or {}
    check("总览不再返回项目摘要（projects 键已移除）", "projects" not in ov,
          str(sorted(ov)))
    check("倒计时 2 条且按目标时间升序",
          [m["name"] for m in ov.get("milestones", [])] == ["检查", "区域赛"],
          str([m.get("name") for m in ov.get("milestones", [])]))
    dt_nodes = [m for m in ov.get("milestones", [])
                if (m.get("target") or {}).get("precision") == "datetime"]
    check("精确时刻倒计时给到秒（键含 seconds）",
          bool(dt_nodes) and "seconds" in dt_nodes[0]["countdown_days"],
          str([m.get("countdown_days") for m in ov.get("milestones", [])]))
    rank_rows = ov.get("members", [])
    check("打卡工时排行：按时长降序、无数据成员排末位、空行不入榜",
          [m["member"]["name"] for m in rank_rows] ==
          ["成员甲", "成员乙", "成员丙"],
          str([(m.get("member") or {}).get("name") for m in rank_rows]))
    check("排行里数字字符串已转数值（29.11 而非 \"29.11\"）",
          bool(rank_rows) and rank_rows[0].get("work_hours") == 29.11,
          repr([m.get("work_hours") for m in rank_rows]))
    check("排行带同步说明与最近签到",
          len(rank_rows) >= 2
          and rank_rows[1].get("sync_note") == "事项：已同步；考勤：已读取原生考勤"
          and rank_rows[0].get("last_check_in") is not None,
          str([(m.get("sync_note"), bool(m.get("last_check_in")))
               for m in rank_rows]))

    print("== 错误字段（未知枚举 / 失效关联） ==")
    check("质量异常计数 = 2（未知枚举串合并 + 失效关联）",
          st["counts"].get("quality_issues") == 2, str(st["counts"]))
    code, r = api("/api/portal/tasks", auth="member")
    dirty = [t for t in r["data"] if t.get("invalid")]
    check("脏记录标注 invalid", len(dirty) == 2, str(len(dirty)))
    urg = r["urgent"]
    check("紧急列表排除脏记录", all(t["id"] not in ("recT9", "recT8") for t in urg))
    code, r = api("/api/portal/overview", auth="member")
    check("overview 报告质量异常数", r["data"]["quality_issues"] == 2)
    check("overview 不再返回项目摘要（2026-09-12 布局调整后由 /projects 承担）",
          "projects" not in r["data"], str(sorted(r["data"])))
    # 完成率口径改由 /projects 校验：哨兵排除已取消与脏数据 → 分母 2
    code, rp = api("/api/portal/projects?include_archived=true", auth="member")
    check("哨兵完成率排除已取消 + 脏数据（1/2）",
          next(p for p in rp["data"] if p["name"] == "哨兵")
          ["progress"]["total"] == 2)

    print("== 部分表失败（保留旧快照） ==")
    ver_before = api("/api/portal/sync-status", auth="member")[1]["snapshot_version"]
    harness._fake_feishu.sync_fail = {"table": "tblFixPortalSync4", "mode": "500",
                                      "left": 99}
    st = trigger_and_wait(None)
    check("部分表失败 → last_error=network", st.get("last_error") == "network",
          str(st.get("last_error")))
    check("失败保留旧版本", st["snapshot_version"] == ver_before)
    code, r = api("/api/portal/schedule/recS1", auth="member")
    check("旧快照数据仍在（详情可读；列表空因 7 天窗口过滤）",
          code == 200 and r["data"]["id"] == "recS1", f"code={code}")
    harness._fake_feishu.sync_fail = {"table": None, "mode": None, "left": 0}

    print("== 429 退避 / 令牌过期重取 ==")
    harness._fake_feishu.sync_fail = {"table": "tblFixPortalSync2", "mode": "429",
                                      "left": 1}
    st = trigger_and_wait(None)
    check("429 重试后成功", st["counts"].get("tasks") == 6 and not st.get("last_error"), str(st.get("last_error")))
    harness._fake_feishu.sync_fail = {"table": "tblFixPortalSync2",
                                      "mode": "token_expire", "left": 1}
    st = trigger_and_wait(None)
    check("token 过期重取后成功", st["counts"].get("tasks") == 6 and not st.get("last_error"),
          str(st.get("last_error")))
    # 内容相同的同步不会递增版本，失败应保留故障注入前的实际版本。
    version_before_incomplete = st["snapshot_version"]
    harness._fake_feishu.sync_fail = {"table": "tblFixPortalSync2",
                                      "mode": "bad_total", "left": 0}
    st = trigger_and_wait(None)
    check("分页不完整 → 失败且旧版保留",
          st["last_error"] == "parse" and st["snapshot_version"] == version_before_incomplete,
          f"{st.get('last_error')} v={st['snapshot_version']}")
    harness._fake_feishu.sync_fail = {"table": None, "mode": None, "left": 0}

    print("== 记录删除对账 ==")
    harness._fake_feishu.bitable["tblFixPortalSync2"]["records"] = [
        r for r in harness._fake_feishu.bitable["tblFixPortalSync2"]["records"]
        if r["record_id"] != "recT3"]
    st = trigger_and_wait(None)
    code, r = api("/api/portal/tasks", auth="member")
    ids = [t["id"] for t in r["data"]]
    check("删除的记录从快照消失", "recT3" not in ids and len(ids) == 5, str(ids))

    print("== API 认证与 envelope ==")
    check("匿名 401", api("/api/portal/overview", auth=None)[0] == 401)
    check("应急 403", api("/api/portal/overview")[0] == 403)
    code, r = api("/api/portal/overview", auth="member")
    check("envelope 字段齐全",
          all(k in r for k in ("snapshot_version", "last_success_at",
                               "server_time", "stale")))
    check("stale=false（刚同步）", r["stale"] is False)
    code, r = api("/api/portal/projects/{bad}".format(bad="recXXX"), auth="member")
    check("详情不存在 → 404", code == 404)

    print("== 手动刷新限频 / 合并 ==")
    import urllib.request as _u
    def _post_sync():
        req = _u.Request(BASE + "/api/portal/sync", data=b"{}", method="POST",
                         headers={"Authorization": "Bearer " + harness._auth["member"],
                                  "Content-Type": "application/json"})
        try:
            return json.loads(_u.urlopen(req, timeout=10).read())
        except Exception as e:
            return {"accepted": None, "detail": str(e)[:60]}
    r1 = _post_sync()
    r2 = _post_sync()
    ok2 = (r2.get("accepted") is False
           or (r2.get("accepted") is True and "合并" in r2.get("detail", "")))
    check("连续手动刷新：第二次被限频或合并（不放大飞书请求）", ok2,
          f"{r1.get('detail')} | {r2.get('detail')}")

    print("== §5 跨日边界（compute 直测，上海时区） ==")
    from app.portal import compute as C
    D = datetime.timedelta(days=1)

    def task_due(days_offset, precision, hour=0, status="进行中"):
        base = datetime.datetime(2026, 9, 10, hour, tzinfo=C.TZ) + \
            datetime.timedelta(days=days_offset)
        return {"due": {"ts": base.timestamp()},
                "due_precision": precision, "id": "t", "status": status}

    now = datetime.datetime(2026, 9, 10, 12, 0, tzinfo=C.TZ)
    check("纯日期·昨天 → 逾期", C.is_overdue(task_due(-1, "date"), now))
    check("纯日期·今天 → 未逾期（边界=次日00:00）",
          not C.is_overdue(task_due(0, "date"), now))
    check("纯日期·今天 → 临期（≤72h）", C.is_due_soon(task_due(0, "date"), now))
    check("纯日期·2 天后 → 临期", C.is_due_soon(task_due(2, "date"), now))
    check("纯日期·4 天后 → 不临期", not C.is_due_soon(task_due(4, "date"), now))
    check("精确时刻·1h 后 → 未逾期", not C.is_overdue(task_due(0, "datetime", 13), now))
    check("精确时刻·30 分钟前 → 逾期", C.is_overdue(
        {"due": {"ts": (now - datetime.timedelta(minutes=30)).timestamp()},
         "due_precision": "datetime", "id": "t", "status": "进行中"}, now))
    # UTC→上海跨日：UTC 16:00 = 上海次日 00:00（排他边界）
    utc_edge = datetime.datetime(2026, 9, 10, 16, 0,
                                 tzinfo=datetime.timezone.utc)
    t = {"due": {"ts": utc_edge.timestamp()}, "due_precision": "date",
         "id": "t", "status": "进行中"}
    sh_now = datetime.datetime(2026, 9, 10, 23, 0, tzinfo=C.TZ)
    check("UTC16:00 纯日期按上海解释为次日零点（当晚未逾期）",
          not C.is_overdue(t, sh_now))
    check("上海次日 01:00 仍未逾期（该日期边界=第三日 00:00）",
          not C.is_overdue(t, sh_now + datetime.timedelta(hours=2)))
    check("上海第三日 01:00 → 逾期",
          C.is_overdue(t, sh_now + datetime.timedelta(hours=26)))
    # 完成率零分母
    check("零分母 → percent=None", C.project_progress([], "x")["percent"] is None)
    check("已完成/已取消不计逾期（§5.2 未完成才逾期）",
          not C.is_overdue(task_due(-1, "date", status="已完成"), now)
          and not C.is_overdue(task_due(-1, "date", status="已取消"), now))
    check("受阻计入逾期", C.is_overdue(task_due(-1, "date", status="受阻"), now))

    print("== 阶段 4 审计修复：日程下界 / 节点当天 / 缺必填 ==")
    D70 = datetime.timedelta(days=70)
    s_past = {"id": "s1", "name": "两月前", "all_day": False, "status": "正常",
              "start": {"ts": (now - D70).timestamp()}}
    s_ended = {"id": "s2", "name": "刚结束", "all_day": False, "status": "正常",
               "start": {"ts": (now - datetime.timedelta(hours=2)).timestamp()},
               "end": {"ts": (now - datetime.timedelta(hours=1)).timestamp()}}
    s_today_all = {"id": "s3", "name": "今天全天", "all_day": True, "status": "正常",
                   "start": {"ts": now.replace(hour=0, minute=0,
                                               second=0).timestamp()}}
    s_soon = {"id": "s4", "name": "两天后", "all_day": False, "status": "正常",
              "start": {"ts": (now + datetime.timedelta(days=2)).timestamp()}}
    got = [x["name"] for x in C.upcoming_schedule(
        [s_past, s_ended, s_today_all, s_soon], 7, now)]
    check("日程：历史与刚结束不进『近期』，今天全天算进行中",
          got == ["今天全天", "两天后"], str(got))

    m_today = {"id": "m1", "name": "今天节点", "kind": "比赛", "visibility": "展示",
               "target": {"ts": now.replace(hour=0, minute=0, second=0).timestamp(),
                          "precision": "date"}}
    m_yest = {"id": "m2", "name": "昨天节点", "kind": "比赛", "visibility": "展示",
              "target": {"ts": (now - datetime.timedelta(days=1)).replace(
                  hour=0, minute=0, second=0).timestamp(), "precision": "date"}}
    fut = C.future_milestones([m_today, m_yest], now=now)
    check("纯日期节点当天仍算未来、倒计时 0 天",
          [x["name"] for x in fut] == ["今天节点"]
          and fut[0]["countdown_days"]["days"] == 0, str(fut))

    from app.portal.normalize import Normalizer
    recs, issues = Normalizer({}).tasks([
        {"record_id": "x1", "fields": {"name": "缺主责组", "priority": "P0",
                                       "status": "进行中"}},
        {"record_id": "x2", "fields": {"name": "缺精度", "owner_group": "硬件",
                                       "priority": "P0", "status": "进行中",
                                       "due_time": _ms(2026, 9, 10)}},
        {"record_id": "x3", "fields": {"name": "正常", "owner_group": "硬件",
                                       "priority": "P1", "status": "未开始"}}])
    by = {r["id"]: r for r in recs}
    check("缺必填主责组 → invalid + 质量异常",
          by["x1"].get("_invalid") is True and any(i[0] == "x1" for i in issues),
          str(issues))
    # 阶段口径修正：新数据源的日期列自带时刻，缺精度不再判异常，缺省为"精确时刻"
    check("有截止但缺精度 → 缺省精确时刻且不再判异常",
          by["x2"].get("_invalid") is None
          and by["x2"].get("due_precision") == "datetime" and not issues[1:],
          f"{by['x2'].get('due_precision')} / {issues}")
    check("合法记录不受影响", by["x3"].get("_invalid") is None)

    # 换算：成员数据表的数字字段飞书返回字符串、公式文本返回富文本数组
    recs, issues = Normalizer({}).members([
        {"record_id": "m1", "fields": {
            "member": [{"id": "ou_x", "name": "成员甲"}],
            "work_hours": "29.11", "attendance_anomalies": "0",
            "summary_note": [{"text": "按到期事项统计", "type": "text"}],
            "last_check_in": _ms(2026, 9, 12, 9, 4)}},
        {"record_id": "m2", "fields": {"member": [{"id": "ou_y", "name": "成员乙"}]}},
        {"record_id": "m3", "fields": {}}])
    mb = {r["id"]: r for r in recs}
    check("数字字符串强转为数值", mb["m1"]["work_hours"] == 29.11
          and mb["m1"]["attendance_anomalies"] == 0,
          f"{mb['m1']['work_hours']!r} / {mb['m1']['attendance_anomalies']!r}")
    check("公式富文本取到纯文本", mb["m1"]["summary_note"] == "按到期事项统计",
          repr(mb["m1"]["summary_note"]))
    check("无考勤数据的成员保留（时长 None）", mb["m2"]["work_hours"] is None)
    check("用户空行跳过且不计质量异常",
          "m3" not in mb and not issues, str(issues))

    # 重要节点：无时间精度字段的表也能用（缺省精确时刻），无展示状态按展示处理
    recs, issues = Normalizer({}).milestones([
        {"record_id": "n1", "fields": {"name": "完整形态考核",
                                       "target_time": _ms(2027, 1, 18, 9, 0)}}])
    check("节点缺省时间精度为精确时刻且不判异常",
          recs[0]["target"]["precision"] == "datetime" and not issues, str(issues))
    now_n = datetime.datetime(2026, 9, 12, 12, 0, tzinfo=C.TZ)
    shown = C.future_milestones(recs, now=now_n)
    check("节点展示状态留空仍进倒计时", len(shown) == 1, str(shown))
    hidden = C.future_milestones(
        [dict(recs[0], visibility="隐藏")], now=now_n)
    check("显式隐藏的节点不展示", hidden == [], str(hidden))

    print("== 阶段 4 审计修复：分页 / 排序白名单 / 同源校验 ==")
    harness._fake_feishu.bitable = fake_data(with_bad=True)
    trigger_and_wait(None)
    code, r = api("/api/portal/tasks?page_size=1&page=1", auth="member")
    check("分页：page_size=1 且 total/has_more 正确",
          code == 200 and len(r["data"]) == 1 and r["total"] == 6
          and r["has_more"] is True,
          str({k: r.get(k) for k in ("total", "page", "has_more")}))
    code, r = api("/api/portal/tasks?page_size=5&page=2", auth="member")
    check("分页：末页 has_more=false", code == 200 and r["has_more"] is False,
          str(r.get("has_more")))
    code, _ = api("/api/portal/tasks?page_size=999", auth="member")
    check("非法 page_size → 422", code == 422, str(code))
    code, _ = api("/api/portal/tasks?page=0", auth="member")
    check("非法 page → 422", code == 422, str(code))
    code, r = api("/api/portal/tasks?sort=due", auth="member")
    check("排序白名单：sort=due 首条为最早截止",
          code == 200 and r["data"][0]["id"] == "recT3",
          str([x["id"] for x in r["data"]][:3]))
    code, _ = api("/api/portal/tasks?sort=evil", auth="member")
    check("未知排序字段 → 422（不静默忽略）", code == 422, str(code))
    code, _ = api("/api/portal/schedule?days=0", auth="member")
    check("非法 days → 422", code == 422, str(code))

    import urllib.error
    import urllib.request as _u

    def post_sync(origin=None):
        hdrs = {"Authorization": "Bearer " + harness._auth["member"],
                "Content-Type": "application/json"}
        if origin:
            hdrs["Origin"] = origin
        req = _u.Request(BASE + "/api/portal/sync", data=b"{}", method="POST",
                         headers=hdrs)
        try:
            with _u.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    host = BASE.split("//", 1)[1]
    check("跨站 Origin POST /sync → 403",
          post_sync("http://evil.example.com") == 403)
    check("同源 Origin POST /sync → 200", post_sync("http://" + host) == 200)
    check("无 Origin（CLI/Bearer）POST /sync → 200", post_sync() == 200)

    print("== 阶段 5 前置：记录链接 / 归档筛选 / 质量明细 ==")
    code, r = api("/api/portal/tasks?page_size=1", auth="member")
    check("任务带官方记录链接（shared_url）",
          code == 200 and str(r["data"][0].get("url", "")).startswith(
              "http://fake.feishu.local/record/"), str(r["data"][0].get("url")))

    data = fake_data(with_bad=False)
    data["tblFixPortalSync1"]["records"].append({
        "record_id": "recP9", "fields": {"项目名称": "归档项目", "主责组": "运营",
                                         "项目状态": "已归档"}})
    harness._fake_feishu.bitable = data
    trigger_and_wait(None)
    code, r = api("/api/portal/projects", auth="member")
    names = [p["name"] for p in r["data"]]
    check("归档项目默认隐藏（§5.1）", "归档项目" not in names, str(names))
    code, r = api("/api/portal/projects?include_archived=true", auth="member")
    names = [p["name"] for p in r["data"]]
    check("include_archived=true 可查看归档项目", "归档项目" in names, str(names))

    harness._fake_feishu.bitable = fake_data(with_bad=True)
    trigger_and_wait(None)
    code, r = api("/api/portal/quality", auth="member")
    check("质量明细：返回被排除记录与原因",
          code == 200 and r["total"] >= 2
          and all("record_id" in x and "issue" in x for x in r["data"]),
          str(r)[:120])
    check("质量明细：按表汇总计数", r.get("by_table", {}).get("tasks", 0) >= 2,
          str(r.get("by_table")))

    print("== 调度器：启动即首轮 / 定时循环 / stop 退出（单元） ==")
    from app.portal import sync as S
    calls = {"n": 0}
    orig_run, orig_mark = S.run_sync, S.portal_store.mark_attempt
    orig_interval = S.config.PORTAL_SYNC_INTERVAL_SEC
    S.run_sync = lambda: (calls.__setitem__("n", calls["n"] + 1), {"ok": 1})[1]
    S.portal_store.mark_attempt = lambda *a, **k: None
    S.config.PORTAL_SYNC_INTERVAL_SEC = 1
    sch = S.Scheduler()
    try:
        sch.start()
        time.sleep(1.0)
        first = calls["n"]
        time.sleep(1.6)
        second = calls["n"]
        sch.stop(timeout=3)
        alive = bool(sch._thread and sch._thread.is_alive())
        time.sleep(1.6)
        after = calls["n"]
    finally:
        S.run_sync, S.portal_store.mark_attempt = orig_run, orig_mark
        S.config.PORTAL_SYNC_INTERVAL_SEC = orig_interval
    check("启动即触发首轮（不空等一个周期）", first >= 1, str(first))
    check("定时循环按间隔再次触发", second > first, f"{first}->{second}")
    check("stop() 后线程退出且不再触发", (not alive) and after == second,
          f"alive={alive} after={after} second={second}")

    print("== 配置合并：env 覆盖单字段、不丢文件里的表映射 ==")
    import os as _os
    import pathlib
    import tempfile
    from app import config as AC
    tmpcfg = pathlib.Path(tempfile.mkdtemp()) / "feishu.json"
    tmpcfg.write_text(json.dumps({
        "app_id": "file-app-id", "app_secret_ref": "env:UNUSED_SECRET",
        "base": {"app_token": "file-base-token"},
        "tables": {"tasks": {"table_id": "tblX", "fields": {"name": "fldX"}}}}),
        encoding="utf-8")
    old_cfg = AC.FEISHU_CONFIG_FILE
    saved = {k: _os.environ.get(k) for k in
             ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_BASE_APP_TOKEN",
              "FEISHU_TABLES_JSON")}
    AC.FEISHU_CONFIG_FILE = tmpcfg
    try:
        _os.environ["FEISHU_APP_ID"] = "env-app-id"
        _os.environ["FEISHU_APP_SECRET"] = "env-secret"
        _os.environ["FEISHU_BASE_APP_TOKEN"] = "env-base-token"
        _os.environ.pop("FEISHU_TABLES_JSON", None)
        cfg = S._load_sync_cfg()
        check("env 覆盖 app_id/secret/token",
              cfg["app_id"] == "env-app-id" and cfg["app_secret"] == "env-secret"
              and cfg["app_token"] == "env-base-token", str(cfg)[:100])
        check("只给 env 凭据时仍保留配置文件里的表映射（部署缺陷回归）",
              bool((cfg.get("tables") or {}).get("tasks")),
              str(cfg.get("tables"))[:80])
    finally:
        AC.FEISHU_CONFIG_FILE = old_cfg
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v

    print("== 同步不阻塞 WS 遥测 ==")
    async def ws_throughput():
        from test_transports import FakeFirmwareServer
        fw = FakeFirmwareServer()
        try:
            for name in ("telemetry-sta",):
                api("/api/v2/devices", {"id": name, "name": name,
                                        "transport": "vllink_tcp",
                                        "host": "127.0.0.1",
                                        "uart_port": fw.port})
            c_open, b_open = api("/api/v2/devices/telemetry-sta/shoot/open", {})
            assert c_open == 200, f"shoot open {c_open} {b_open}"
            time.sleep(0.6)
            counts = {"n": 0}
            async with websockets.connect(
                    BASE.replace("http", "ws")
                    + "/ws/v2/devices/telemetry-sta/shoot",
                    additional_headers=harness.ws_headers()) as ws:
                async def count():
                    try:
                        while True:
                            m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                            if m.get("type") == "telemetry":
                                counts["n"] += 1
                    except Exception as e:
                        counts["err"] = repr(e)[:120]
                ct = asyncio.create_task(count())
                # 触发一次同步（含多请求）——放到线程，避免阻塞本事件循环
                harness._fake_feishu.bitable = fake_data(with_bad=True)
                t0 = time.time()
                await asyncio.to_thread(trigger_and_wait, None)
                dur = time.time() - t0
                await asyncio.sleep(2.0)
                ct.cancel()
                return counts, dur
        finally:
            api("/api/v2/devices/telemetry-sta/shoot/close", {}, "POST")
            api("/api/v2/devices/telemetry-sta", method="DELETE")
            fw.stop()
    counts, dur = asyncio.run(ws_throughput())
    n = counts["n"]
    # 假固件 50Hz；同步期间（dur 秒 + 2s 采集）遥测应持续到达
    check(f"同步（{dur:.1f}s）期间遥测流不断（{n} 帧）", n >= max(3, int(dur * 10)),
          f"n={n} dur={dur:.1f} err={counts.get('err')}")

    # 阶段 8：令牌缓存 + 手动刷新按用户限频
    t_token_cache()
    t_manual_per_user()

    # 阶段 7：快照库版本不匹配 → 故障关闭（放最后：会重启服务换库）
    t_schema_mismatch()


if __name__ == "__main__":
    def _ensure():
        import urllib.request as _u
        try:
            _u.urlopen(BASE + "/login", timeout=2)
            return True
        except Exception:
            return harness.start_service()
    if not _ensure():
        print("✗ 测试服务未就绪")
        raise SystemExit(1)
    try:
        run()
    finally:
        harness.stop_service()
    print(f"\n同步: {len(PASS)} 通过, {len(FAIL)} 失败")
    raise SystemExit(1 if FAIL else 0)
