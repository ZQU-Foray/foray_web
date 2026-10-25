"""考勤页：周时长结算与当天值日判定（纯逻辑，不访问网络，便于单元测试）。

口径（维护者确认，2026-10-20）：
- 一周 = 北京时间周一 00:00 至周日 24:00；本周时长 **大于** 30 小时为达标（恰好 30 不算）。
- 周时长在飞书侧用公式结算：成员数据表「本周时长」= 有效时长 − 周初时长，
  「本周达标 / 上周达标」= 时长 > 30。门户只负责在换周时写起点：
  每周一 00:00 把「周初时长」改成当时的有效时长、「上周时长」记下刚结束那周的时长、
  「结算周」写本周一的日期（YYYY-MM-DD）。「结算周」= 本周 → 本周已换周，重复执行不再写。
- 值日表由维护者导入飞书，网页只读：每行填「日期」或「星期」之一加「值日人员」。
  当天有按日期的行就以它为准（调班、节假日），否则取同一星期几的轮换行。
- 请假（2026-10-21 起）：组长在考勤页给成员标「本周请假」，存在成员数据表「请假周」列
  （请假那周的周一日期，多周用逗号分隔，只保留本周与上周）。请假那周没达标、当天没签到
  都显示「请假」；达标了照常显示达标。下周自动恢复正常，不需要清理。
  飞书「本周达标 / 上周达标」公式不变，仍只按时长判定。
"""
import datetime as dt
import re
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
WEEK_THRESHOLD_HOURS = 30

# 成员数据表里由门户写入的列（scripts/feishu_attendance_setup.py 按这些列名建列）
COL_WORK = "有效时长（小时）"
COL_WEEK_BASE = "周初时长"
COL_WEEK_KEY = "结算周"
COL_LAST_WEEK = "上周时长"
COL_LEAVE = "请假周"
# 这三个语义键在 config/feishu.json 的 members.fields 里都映射了（field_id 非空），
# 门户才写周结算列；未建列时写入未知列名会让整次签退失败
WEEK_WRITE_KEYS = ("week_base", "week_key", "last_week_hours")

_WEEKDAY_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
               "日": 7, "天": 7, "七": 7}
_WEEKDAY_EN = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABEL = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


def _now(now_ts=None):
    return dt.datetime.fromtimestamp(time.time() if now_ts is None else now_ts, TZ)


def week_bounds(now_ts=None):
    """本周区间：(周一 00:00 时间戳, 下周一 00:00 时间戳, 本周键 YYYY-MM-DD, 上周键)。"""
    now = _now(now_ts)
    monday = (now - dt.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    nxt = monday + dt.timedelta(days=7)
    last = monday - dt.timedelta(days=7)
    return (monday.timestamp(), nxt.timestamp(),
            monday.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d"))


def today_info(now_ts=None):
    """北京时间的今天：(YYYY-MM-DD, 星期几 1-7, 今日 00:00 时间戳)。"""
    now = _now(now_ts)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.strftime("%Y-%m-%d"), now.isoweekday(), start.timestamp()


def date_key(ts):
    """秒级时间戳 → 北京时间日期 YYYY-MM-DD（飞书日期列存的是当地零点的毫秒数）。"""
    if ts is None:
        return None
    return _now(ts).strftime("%Y-%m-%d")


def parse_weekday(v):
    """星期列 → 1-7（周一=1）；认「周一 / 星期一 / 礼拜一 / 一 / 1 / Mon / Monday」，
    周日认「周日 / 周天 / 星期日 / 星期天 / 7」。无法识别返回 None。"""
    if isinstance(v, list):
        v = v[0] if v else None
        if isinstance(v, dict):
            v = v.get("text") or v.get("name")
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return n if 1 <= n <= 7 else None
    s = str(v).strip().lower()
    if not s:
        return None
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= 7 else None
    for i, en in enumerate(_WEEKDAY_EN):
        if s.startswith(en):
            return i + 1
    s = re.sub(r"^(星期|礼拜|周)", "", s)
    return _WEEKDAY_CN.get(s[:1]) if len(s) == 1 else None


def split_names(v):
    """值日人员不是飞书人员列时（如从表格导入的文本）：拆成姓名列表。"""
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, dict):
                parts.append(x.get("text") or x.get("name") or "")
            elif isinstance(x, str):
                parts.append(x)
        v = "、".join(parts)
    if not isinstance(v, str):
        return []
    return [n for n in (p.strip() for p in re.split(r"[,，、/;；\s]+", v)) if n]


def rollover_updates(work, base, key, this_key, last_key):
    """换周写入：本周已换周返回 None；否则返回要写入飞书的 {列名: 值}。

    上周时长只在「结算周 = 上周」时才算得准（= 换周时的有效时长 − 上周的周初时长）；
    结算周更早（服务停了一周以上）或从未结算过（刚建列）时上周未知，写空。
    """
    if key == this_key:
        return None
    w = round(float(work or 0), 2)
    last = None
    if key == last_key and base is not None:
        last = round(max(w - float(base), 0.0), 2)
    return {COL_WEEK_BASE: w, COL_WEEK_KEY: this_key, COL_LAST_WEEK: last}


def new_member_week_cols(this_key):
    """首次签到新建成员行时一并写入的周结算列（本周起点 0）。"""
    return {COL_WEEK_BASE: 0, COL_WEEK_KEY: this_key}


def leave_weeks(v):
    """「请假周」列 → 周键集合（YYYY-MM-DD）；认逗号、顿号、空白分隔，忽略写法不对的项。"""
    text = v if isinstance(v, str) else ""
    keys = set()
    for part in re.split(r"[,，、;；\s]+", text):
        part = part.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            keys.add(part)
    return keys


def leave_value(current, on, this_key, last_key):
    """组长设 / 取消本周请假后「请假周」应写的值：只保留本周与上周（上周的请假用于
    「上周」一栏的显示），更早的顺手清掉。全部清空时写空串。"""
    keys = {k for k in leave_weeks(current) if k >= last_key}
    if on:
        keys.add(this_key)
    else:
        keys.discard(this_key)
    return ",".join(sorted(keys))


def duty_today(rows, today_key, weekday):
    """当天值日：(人员列表, 来源 "date" | "weekday" | None)。

    人员项为 {"id": open_id | None, "name": 姓名}；同一人只出现一次。"""
    valid = [r for r in rows or [] if not r.get("_invalid")]
    chosen = [r for r in valid if r.get("date") == today_key]
    source = "date" if chosen else None
    if not chosen:
        chosen = [r for r in valid if not r.get("date") and r.get("weekday") == weekday]
        source = "weekday" if chosen else None
    people, seen = [], set()
    for r in chosen:
        for u in r.get("people") or []:
            k = "id:" + u["id"]
            if k not in seen:
                seen.add(k)
                people.append({"id": u["id"], "name": u.get("name") or ""})
        for n in r.get("names") or []:
            k = "name:" + n
            if k not in seen:
                seen.add(k)
                people.append({"id": None, "name": n})
    return people, source


def _bool_judge(text, hours):
    """达标判定：优先采用飞书公式列的结果（「达标」/「未达标」），公式列缺失时按时长 > 30 判定。"""
    if text in ("达标", "未达标"):
        return text == "达标"
    if hours is None:
        return None
    return hours > WEEK_THRESHOLD_HOURS


def is_checked_in(m, today_start, live=None):
    """是否签到中：门户进程内实时态优先；否则看快照（签到晚于签退且在今天零点之后）。"""
    oid = (m.get("member") or {}).get("id")
    if live is not None and oid in live:
        ts = live[oid]
        return bool(ts) and ts >= today_start, (ts if ts else None)
    ci = (m.get("last_check_in") or {}).get("ts")
    co = (m.get("last_check_out") or {}).get("ts")
    on = bool(ci) and (not co or co < ci) and ci >= today_start
    return on, (ci if on else None)


def page_data(members, duty_rows, now_ts=None, week_ready=False, duty_ready=False, live=None,
              leave_ready=False):
    """考勤页数据：本周区间 / 每人本周与上周时长及达标 / 签到中 / 当天值日。

    members 为快照成员记录（已排除 _invalid 与无人员行由调用方保证或在此过滤）。
    结算周不是本周的行（换周尚未落到快照、或从未结算）本周数值未知，标为待结算，
    不把跨周累计的公式结果当成本周时长展示。"""
    start, end, this_key, last_key = week_bounds(now_ts)
    today_key, weekday, today_start = today_info(now_ts)
    people, source = duty_today(duty_rows, today_key, weekday)
    duty_ids = {p["id"] for p in people if p["id"]}
    duty_names = {p["name"] for p in people if not p["id"]}

    rows = []
    for m in members or []:
        if m.get("_invalid") or not m.get("member"):
            continue
        person = m.get("member") or {}
        settled = week_ready and m.get("week_key") == this_key
        week_hours = m.get("week_hours") if settled else None
        last_week = m.get("last_week_hours") if settled else None
        if week_ready and not settled and m.get("work_hours") is None:
            # 从没签退过（累计时长为空）：本周时长确定是 0，不必等换周；上周无从得知
            settled, week_hours = True, 0
        on, since = is_checked_in(m, today_start, live)
        weeks = leave_weeks(m.get("leave_weeks"))
        week_ok = _bool_judge(m.get("week_ok"), week_hours) if settled else None
        last_week_ok = (_bool_judge(m.get("last_week_ok"), last_week)
                        if settled and last_week is not None else None)
        rows.append({
            "id": m.get("id"),
            "member": person,
            "work_hours": m.get("work_hours"),
            "week_hours": week_hours,
            "week_ok": week_ok,
            "last_week_hours": last_week,
            "last_week_ok": last_week_ok,
            "pending": week_ready and not settled,
            # 本周请假：没达标（含待结算）显示「请假」；今天没签到也显示「请假」
            "on_leave": this_key in weeks,
            "leave": this_key in weeks and week_ok is not True,
            "last_week_leave": last_key in weeks and last_week_ok is not True,
            "checked_in": on,
            "checked_in_since": since,
            "on_duty": person.get("id") in duty_ids or (person.get("name") or "") in duty_names,
            "url": m.get("url"),
        })

    def sort_key(r):
        h = r["week_hours"]
        return (1 if h is None else 0, -(h or 0), r["member"].get("name") or "")

    rows.sort(key=sort_key)
    member_ids = {r["member"].get("id") for r in rows}
    member_names = {r["member"].get("name") for r in rows}
    return {
        "threshold": WEEK_THRESHOLD_HOURS,
        "week": {"start": start, "end": end, "key": this_key},
        "today": {"date": today_key, "weekday": weekday, "label": WEEKDAY_LABEL[weekday]},
        "configured": {"week": bool(week_ready), "duty": bool(duty_ready),
                       "leave": bool(leave_ready)},
        "duty": {
            "source": source,
            "people": [{**p, "in_members": (p["id"] in member_ids) if p["id"]
                        else (p["name"] in member_names)} for p in people],
        },
        "summary": {
            "members": len(rows),
            "met": sum(1 for r in rows if r["week_ok"] is True),
            # 请假且没达标的人不计入应达标人数
            "leave": sum(1 for r in rows if r["leave"]),
            "required": sum(1 for r in rows if not r["leave"]),
            "pending": sum(1 for r in rows if r["pending"]),
            "checked_in": sum(1 for r in rows if r["checked_in"]),
        },
        "members": rows,
    }
