"""考勤页的飞书侧结构初始化（幂等）：成员数据表补建周结算列 + 建值日表 + 首次换周。

需要联网，凭据只从环境变量读（FEISHU_APP_ID / FEISHU_APP_SECRET），密钥不进仓库：
  .venv/bin/python scripts/feishu_attendance_setup.py --dry-run        # 只看计划，不写飞书
  .venv/bin/python scripts/feishu_attendance_setup.py                  # 建列 / 建表 / 首次换周
  .venv/bin/python scripts/feishu_attendance_setup.py --write-config   # 同上，并把 field_id 回填 config/feishu.json
  .venv/bin/python scripts/feishu_attendance_setup.py --verify         # 只回读现状并打印映射

做的事（口径见 app/portal/attendance.py）：
1. 成员数据表补建：周初时长 / 结算周 / 上周时长（门户换周时写入）与
   本周时长 / 本周达标 / 上周达标（飞书公式：本周时长 = 有效时长 − 周初时长，时长 > 30 为达标），
   以及请假周（组长在考勤页设置）。
2. 值日表：没有就新建（值日安排 / 日期 / 星期 / 值日人员）；已存在（例如先导入了表格）
   就只补缺的列。每行填「日期」或「星期」之一加值日人员，按日期的行优先。
3. 首次换周：「结算周」不是本周的成员行，把周初时长设为当前有效时长、结算周设为本周一，
   本周时长从执行这一刻开始累计（之前的累计时长无法拆分到周，不计入本周）。
之后每周一 00:00 由门户的零点任务换周，签退时也会逐行兜底。

门户按 field_id 认列，建完必须回填到服务器上的 config/feishu.json（--write-config 自动回填；
config/ 不随部署包发布，请在服务器上执行本脚本或手工合并）。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from feishu_procurement_setup import ApiError, Client  # noqa: E402  复用同一套最小客户端
from app.portal import attendance  # noqa: E402  与门户共用换周逻辑，口径只有一份

CONFIG = ROOT / "config" / "feishu.json"
THRESHOLD = attendance.WEEK_THRESHOLD_HOURS

# 成员数据表：语义键 → (列名, 类型, 属性)；20 = 公式（表达式在 ensure_formulas 里按真实 field_id 生成）
MEMBER_FIELDS = [
    ("week_base", attendance.COL_WEEK_BASE, 2, {"formatter": "0.00"}),
    ("week_key", attendance.COL_WEEK_KEY, 1, None),
    ("last_week_hours", attendance.COL_LAST_WEEK, 2, {"formatter": "0.00"}),
    ("week_hours", "本周时长", 20, {"formatter": "0.00"}),
    ("week_ok", "本周达标", 20, None),
    ("last_week_ok", "上周达标", 20, None),
    # 请假周（2026-10-21 起）：组长在考勤页设置，存请假那周的周一日期（逗号分隔）
    ("leave_week", attendance.COL_LEAVE, 1, None),
]

DUTY_TABLE_NAME = "值日表"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# 值日表：语义键 → (列名, 类型, 属性)；第一列为主字段（飞书要求主字段是文本类）
DUTY_FIELDS = [
    (None, "值日安排", 1, None),
    ("date", "日期", 5, {"date_formatter": "yyyy/MM/dd", "auto_fill": False}),
    ("weekday", "星期", 3, {"options": [{"name": x} for x in WEEKDAYS]}),
    ("people", "值日人员", 11, {"multiple": True}),
]


def ref(table, fid):
    return "bitable::$table[%s].$field[%s]" % (table, fid)


def load_cfg():
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    base = os.environ.get("FEISHU_BASE_APP_TOKEN") or (raw.get("base") or {}).get("app_token", "")
    members = ((raw.get("tables") or {}).get("members") or {})
    if not base or not members.get("table_id"):
        raise SystemExit("config/feishu.json 缺 base.app_token 或 tables.members.table_id")
    return raw, base, members


def fields_of(client, base, table):
    rows = client.items("/bitable/v1/apps/%s/tables/%s/fields" % (base, table))
    return {f["field_name"]: f for f in rows}


def ensure_fields(client, base, table, specs, dry_run, label):
    """补建缺失列；类型不符只报告不改（避免误伤已有数据）。"""
    have = fields_of(client, base, table)
    for _, name, kind, prop in specs:
        cur = have.get(name)
        if cur is not None:
            if cur.get("type") != kind:
                print("  [注意] %s『%s』类型为 %s（期望 %s），保持不动——"
                      % (label, name, cur.get("type"), kind)
                      + ("值日人员为文本时门户按姓名匹配" if name == "值日人员" else "请人工核对"))
            else:
                print("  [跳过] %s『%s』已存在" % (label, name))
            continue
        body = {"field_name": name, "type": kind}
        if prop:
            body["property"] = prop
        if kind == 20:
            # 公式列先建成占位表达式，随后按真实 field_id 重写
            body["property"] = {**(prop or {}), "formula_expression": "0"}
        if dry_run:
            print("  [计划] %s 新建『%s』类型=%s" % (label, name, kind))
            continue
        fid = client.call("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (base, table),
                          body)["data"]["field"]["field_id"]
        print("  [完成] %s 新建『%s』field_id=%s" % (label, name, fid))


def ensure_formulas(client, base, table, work_fid, dry_run):
    have = fields_of(client, base, table)
    need = ("本周时长", "本周达标", "上周达标", attendance.COL_WEEK_BASE, attendance.COL_LAST_WEEK)
    if not all(n in have for n in need):
        print("  [跳过] 公式列：周结算列未建齐（dry-run 时属正常）")
        return
    w = ref(table, work_fid)
    b = ref(table, have[attendance.COL_WEEK_BASE]["field_id"])
    t = ref(table, have["本周时长"]["field_id"])
    lw = ref(table, have[attendance.COL_LAST_WEEK]["field_id"])
    exprs = {
        # 未结算过（周初时长为空）时返回累计值；门户以「结算周 = 本周」为准才展示，不会误读
        "本周时长": "IF(ISBLANK(%s), 0, IF(ISBLANK(%s), %s, ROUND(%s - %s, 2)))" % (w, b, w, w, b),
        "本周达标": 'IF(%s > %d, "达标", "未达标")' % (t, THRESHOLD),
        "上周达标": 'IF(ISBLANK(%s), "", IF(%s > %d, "达标", "未达标"))' % (lw, lw, THRESHOLD),
    }
    for name, expr in exprs.items():
        cur = have[name]
        prop = dict(cur.get("property") or {})
        # 飞书回读的表达式会去掉空格（2026-10-21 实测），比较时忽略空格，避免每次重跑都重写公式
        if (prop.get("formula_expression") or "").replace(" ", "") == expr.replace(" ", ""):
            print("  [跳过] 公式『%s』已是目标表达式" % name)
            continue
        prop["formula_expression"] = expr
        if dry_run:
            print("  [计划] 公式『%s』→ %s" % (name, expr))
            continue
        client.call("PUT", "/bitable/v1/apps/%s/tables/%s/fields/%s" % (base, table, cur["field_id"]),
                    {"field_name": name, "type": 20, "property": prop})
        print("  [完成] 公式『%s』" % name)


def find_or_create_duty(client, base, duty_cfg, dry_run):
    tid = (duty_cfg or {}).get("table_id", "")
    tables = client.items("/bitable/v1/apps/%s/tables" % base)
    by_id = {t["table_id"]: t for t in tables}
    if tid and tid in by_id:
        print("  [跳过] 值日表已映射：%s（%s）" % (by_id[tid].get("name"), tid))
        return tid
    for t in tables:
        if t.get("name") == DUTY_TABLE_NAME:
            print("  [跳过] 找到已有的『%s』：%s" % (DUTY_TABLE_NAME, t["table_id"]))
            return t["table_id"]
    if dry_run:
        print("  [计划] 新建数据表『%s』" % DUTY_TABLE_NAME)
        return ""
    body = {"table": {"name": DUTY_TABLE_NAME, "default_view_name": "值日安排",
                      "fields": [{"field_name": n, "type": k, **({"property": p} if p else {})}
                                 for _, n, k, p in DUTY_FIELDS]}}
    tid = client.call("POST", "/bitable/v1/apps/%s/tables" % base, body)["data"]["table_id"]
    print("  [完成] 新建数据表『%s』：%s" % (DUTY_TABLE_NAME, tid))
    return tid


def first_rollover(client, base, members_cfg, dry_run):
    """首次换周：与门户零点任务同一套逻辑（attendance.rollover_updates），只写结算周不是本周的行。"""
    table = members_cfg["table_id"]
    have = fields_of(client, base, table)
    id2name = {f["field_id"]: n for n, f in have.items()}
    member_col = id2name.get((members_cfg.get("fields") or {}).get("member", ""), "")
    if not all(n in have for n in (attendance.COL_WORK, attendance.COL_WEEK_BASE,
                                    attendance.COL_WEEK_KEY, attendance.COL_LAST_WEEK)):
        print("  [跳过] 首次换周：周结算列未建齐（dry-run 时属正常）")
        return
    _, _, this_key, last_key = attendance.week_bounds()
    rows = client.items("/bitable/v1/apps/%s/tables/%s/records?user_id_type=open_id" % (base, table))
    n = 0
    for row in rows:
        f = row.get("fields") or {}
        if member_col and not f.get(member_col):
            continue                                  # 用户保留的空行
        key = f.get(attendance.COL_WEEK_KEY)
        if isinstance(key, list):
            key = "".join(x.get("text", "") for x in key if isinstance(x, dict))
        num = lambda v: float(v) if v not in (None, "") else None  # noqa: E731
        upd = attendance.rollover_updates(num(f.get(attendance.COL_WORK)),
                                          num(f.get(attendance.COL_WEEK_BASE)),
                                          (key or None), this_key, last_key)
        if upd is None:
            continue
        n += 1
        if dry_run:
            print("  [计划] %s → 本周起点 %.2f h" % (row["record_id"], upd[attendance.COL_WEEK_BASE]))
            continue
        client.call("PUT", "/bitable/v1/apps/%s/tables/%s/records/%s?user_id_type=open_id"
                    % (base, table, row["record_id"]), {"fields": upd})
        time.sleep(0.12)                              # 留出余量，避开写接口限频
    print("  [%s] 首次换周：%d 行（本周 %s 起）" % ("计划" if dry_run else "完成", n, this_key))


def mapping(client, base, members_table, duty_table):
    mh = fields_of(client, base, members_table)
    mem = {k: mh[n]["field_id"] for k, n, _, _ in MEMBER_FIELDS if n in mh}
    duty = {}
    if duty_table:
        dh = fields_of(client, base, duty_table)
        duty = {k: dh[n]["field_id"] for k, n, _, _ in DUTY_FIELDS if k and n in dh}
    return mem, duty


def write_config(raw, mem_map, duty_table, duty_map):
    tables = raw.setdefault("tables", {})
    tables["members"].setdefault("fields", {}).update(mem_map)
    tables["duty"] = {"table_id": duty_table, "display_name": DUTY_TABLE_NAME,
                      "_note": "维护者导入、门户只读：每行填日期或星期之一 + 值日人员；按日期的行优先（见 app/portal/attendance.py）",
                      "fields": duty_map}
    bak = CONFIG.with_suffix(".json.bak-attendance")
    bak.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    CONFIG.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("  [完成] 已回填 %s（原文件备份为 %s）" % (CONFIG, bak.name))


def main():
    ap = argparse.ArgumentParser(description="考勤页飞书侧结构初始化（幂等）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写飞书")
    ap.add_argument("--verify", action="store_true", help="只回读现状并打印映射")
    ap.add_argument("--write-config", action="store_true", help="把 field_id 回填到 config/feishu.json")
    args = ap.parse_args()
    app_id = os.environ.get("FEISHU_APP_ID", "")
    secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not secret:
        raise SystemExit("缺 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量")
    raw, base, members_cfg = load_cfg()
    client = Client(app_id, secret)
    mt = members_cfg["table_id"]
    print("Base=%s 成员数据表=%s" % (base, mt))
    try:
        if not args.verify:
            have = fields_of(client, base, mt)
            if attendance.COL_WORK not in have:
                raise SystemExit("成员数据表没有『%s』列，无法结算周时长" % attendance.COL_WORK)
            print("\n1. 成员数据表周结算列：")
            ensure_fields(client, base, mt, MEMBER_FIELDS, args.dry_run, "成员数据")
            ensure_formulas(client, base, mt, have[attendance.COL_WORK]["field_id"], args.dry_run)
            print("\n2. 值日表：")
            dt = find_or_create_duty(client, base, (raw.get("tables") or {}).get("duty"), args.dry_run)
            if dt:
                ensure_fields(client, base, dt, DUTY_FIELDS[1:], args.dry_run, "值日表")
            print("\n3. 首次换周：")
            first_rollover(client, base, members_cfg, args.dry_run)
        else:
            dt = ((raw.get("tables") or {}).get("duty") or {}).get("table_id", "")
        mem_map, duty_map = mapping(client, base, mt, dt)
        print("\n回填片段 tables.members.fields（追加）：")
        print(json.dumps(mem_map, ensure_ascii=False, indent=2, sort_keys=True))
        print("tables.duty：")
        print(json.dumps({"table_id": dt, "fields": duty_map}, ensure_ascii=False, indent=2))
        if args.write_config and not args.dry_run:
            if len(mem_map) != len(MEMBER_FIELDS) or not dt or len(duty_map) != 3:
                raise SystemExit("列未建齐，不回填配置；请先检查上面的输出")
            write_config(raw, mem_map, dt, duty_map)
    except ApiError as e:
        raise SystemExit("飞书接口失败：%s" % e)
    print("\n完成。门户每 60 秒同步一次，回填配置后无需重启即可在「考勤」页看到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
