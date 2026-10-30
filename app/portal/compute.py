"""§5 统一计算口径（阶段 4）。

时区：Asia/Shanghai（zoneinfo，系统 tzdata）。纯日期截止的排他边界 =
次日 00:00（上海）；精确时刻按真实时间比较。所有计算只读快照记录，
不触碰网络。
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .normalize import OPEN_STATUSES, PRIORITIES

TZ = ZoneInfo("Asia/Shanghai")
DUE_SOON_HOURS = 72


def _dt(ts):
    return datetime.fromtimestamp(ts, TZ)


def datetime_now():
    return datetime.now(TZ)


def due_boundary(task, now=None):
    """截止边界（排他）。纯日期 = 上海次日 00:00；精确时刻 = ts 本身。

    返回 datetime 或 None（无截止）。
    """
    d = task.get("due")
    if not d:
        return None
    ts = d["ts"]
    if task.get("due_precision") == "date":
        day = _dt(ts).astimezone(TZ)
        return (day + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
    return _dt(ts)


def is_overdue(task, now=None):
    """§5.2：逾期 = **未完成**（未开始/进行中/受阻）且已过截止边界。

    已完成/已取消不参与逾期判定（此前缺陷：视图层对每条任务无条件暴露
    该标志，导致已完成任务显示"逾期"）。
    """
    if task.get("status") not in OPEN_STATUSES:
        return False
    b = due_boundary(task, now)
    if not b:
        return False
    return (now or datetime.now(TZ)) >= b


def is_due_soon(task, now=None):
    """未完成、未逾期且距边界 ≤ 72h。"""
    if task.get("status") not in OPEN_STATUSES:
        return False
    b = due_boundary(task)
    if not b:
        return False
    now = now or datetime.now(TZ)
    return now < b and (b - now) <= timedelta(hours=DUE_SOON_HOURS)


def valid_tasks_for_project(tasks, project_id):
    """§5.1 有效事项 = 关联该项目且状态 ≠ 已取消（排除质量异常记录）。

    关联列可能同时指向多个项目：只看 project_id 会让「1/2 完成」被算成「1/1」，
    所以按 project_ids 集合判断，单值记录仍命中（审计 P2-7）。
    """
    out = []
    for t in tasks:
        if t.get("status") == "已取消" or t.get("_invalid"):
            continue
        ids = t.get("project_ids")
        if ids:
            if project_id in ids:
                out.append(t)
        elif t.get("project_id") == project_id:
            out.append(t)
    return out


def project_progress(tasks, project_id):
    """完成率：round(已完成/有效×100) + 分子分母；零分母 → None（暂无事项）。"""
    valid = valid_tasks_for_project(tasks, project_id)
    if not valid:
        return {"done": 0, "total": 0, "percent": None}
    done = sum(1 for t in valid if t.get("status") == "已完成")
    pct = round(done * 100 / len(valid))
    return {"done": done, "total": len(valid), "percent": pct}


def urgent_tasks(tasks, group=None, limit=None, now=None):
    """§5.2 紧急列表：未完成 + 排序（P→逾期→截止→稳定 ID）。"""
    now = now or datetime.now(TZ)

    def p_index(t):
        p = t.get("priority")
        return PRIORITIES.index(p) if p in PRIORITIES else 99

    open_tasks = [t for t in tasks
                  if t.get("status") in OPEN_STATUSES and not t.get("_invalid")
                  and (group is None or t.get("owner_group") == group)]
    open_tasks.sort(key=lambda t: (
        p_index(t),
        0 if is_overdue(t, now) else 1,
        due_boundary(t, now) or datetime.max.replace(tzinfo=TZ),
        t.get("id") or ""))
    out = []
    for t in open_tasks:
        out.append({
            "id": t["id"], "name": t["name"], "priority": t["priority"],
            "status": t["status"], "owner_group": t["owner_group"],
            "owners": t.get("owners") or [],
            "due": t.get("due"), "due_precision": t.get("due_precision"),
            "overdue": is_overdue(t, now), "due_soon": is_due_soon(t, now),
            "url": t.get("url"),
            **({"sync": t["sync"]} if t.get("sync") else {}),
        })
        if limit and len(out) >= limit:
            break
    return out


def milestone_boundary(target):
    """节点排他边界（§5.2 同一原则）：纯日期 = 上海次日 00:00；精确时刻 = ts。

    纯日期节点当天必须仍算"未来"（§4.3「纯日期节点当天显示今天」），
    因此不能用原始 ts（当天 00:00）与 now 直接比较。
    """
    if not target:
        return None
    dt = _dt(target["ts"])
    if target.get("precision") == "date":
        return (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                microsecond=0)
    return dt


def future_milestones(milestones, limit=None, now=None):
    """未来节点升序（过去进历史不显示负倒计时）。

    可见性放宽：只有显式 `隐藏`/`已取消` 才不展示，**留空按展示处理**——
    重要节点表由人工维护，忘选展示状态不应导致节点整条消失。
    """
    now = now or datetime.now(TZ)
    items = []
    for m in milestones:
        if m.get("_invalid") or m.get("visibility") not in (None, "展示"):
            continue
        tgt = m.get("target")
        if not tgt:
            continue
        b = milestone_boundary(tgt)
        if b is None or b <= now:
            continue
        items.append((_dt(tgt["ts"]), m))
    items.sort(key=lambda x: (x[0], x[1]["id"]))
    out = []
    for dt, m in items[:limit]:
        out.append({"id": m["id"], "name": m["name"], "kind": m["kind"],
                    "target": m["target"], "project_id": m.get("project_id"),
                    "url": m.get("url"),
                    "countdown_days": _countdown(dt, m["target"]["precision"], now)})
    return out


def _countdown(dt, precision, now):
    """倒计时展示语义（§5.3）：纯日期给天数/今天；精确时刻给到秒。"""
    if precision == "date":
        days = (dt.date() - now.date()).days
        return {"days": days}
    delta = dt - now
    total_sec = max(0, int(delta.total_seconds()))
    return {"days": delta.days, "hours": total_sec % 86400 // 3600,
            "minutes": total_sec % 3600 // 60, "seconds": total_sec % 60}


def upcoming_schedule(schedule, days=7, now=None):
    """未来 N 天公共安排（含正在进行；**不返回已结束的历史安排**）。

    此前缺陷：只判 `start <= until` 无下界，两个月前的日程也会进"近期日程"。
    """
    now = now or datetime.now(TZ)
    until = now + timedelta(days=days)
    out = []
    for s in schedule:
        if s.get("_invalid"):
            continue
        st = s.get("start")
        if not st:
            continue
        sdt = _dt(st["ts"])
        end = s.get("end")
        edt = _dt(end["ts"]) if end else None
        if edt is None and s.get("all_day"):
            # 全天且无结束时间：按当天 24 点结束（§4.4 全天按上海时区解释）
            edt = (sdt + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                    microsecond=0)
        ongoing = bool(edt and sdt <= now <= edt)
        if not ongoing and not (now <= sdt <= until):
            continue
        out.append({"id": s["id"], "name": s["name"], "start": st, "end": end,
                    "all_day": s.get("all_day"), "kind": s.get("kind"),
                    "groups": s.get("groups") or [],
                    "location": s.get("location"),
                    "project_id": s.get("project_id"),
                    "url": s.get("url"),
                    "status": s.get("status"), "ongoing": ongoing,
                    **({"sync": s["sync"]} if s.get("sync") else {})})
    out.sort(key=lambda x: (x["start"]["ts"], x["id"]))
    return out


# 「需要关注」的分档：逾期 → 72 小时内到期 → 受阻 → P0（其余未完成事项不进总览）
ATTENTION_LEVELS = ("overdue", "due_soon", "blocked", "p0")


def attention_tasks(tasks, now=None):
    """总览「需要关注的事项」（2026-10-21 布局）：全部组别一张列表，最急的在前。

    每条带 level（见 ATTENTION_LEVELS）；同档内按 P 等级、截止边界、ID 排序。
    返回全量，页面只显示前几条并给出「查看全部」。
    """
    now = now or datetime.now(TZ)
    picked = []
    for t in tasks:
        if t.get("_invalid") or t.get("status") not in OPEN_STATUSES:
            continue
        if is_overdue(t, now):
            level = 0
        elif is_due_soon(t, now):
            level = 1
        elif t.get("status") == "受阻":
            level = 2
        elif t.get("priority") == "P0":
            level = 3
        else:
            continue
        p = t.get("priority")
        picked.append(((level, PRIORITIES.index(p) if p in PRIORITIES else 99,
                        due_boundary(t, now) or datetime.max.replace(tzinfo=TZ),
                        t.get("id") or ""), level, t))
    picked.sort(key=lambda x: x[0])
    out = []
    for _, level, t in picked:
        out.append({
            "id": t["id"], "name": t["name"], "priority": t.get("priority"),
            "status": t.get("status"), "owner_group": t.get("owner_group"),
            "owners": t.get("owners") or [],
            "due": t.get("due"), "due_precision": t.get("due_precision"),
            "overdue": level == 0, "due_soon": level == 1,
            "level": ATTENTION_LEVELS[level], "url": t.get("url"),
            **({"sync": t["sync"]} if t.get("sync") else {}),
        })
    return out


def group_urgent_summary(tasks, limit_per_group=5):
    """首页五组紧急：每组最多 N 条 + 组级统计。"""
    from .normalize import GROUPS
    out = []
    for g in GROUPS:
        items = urgent_tasks(tasks, group=g, limit=limit_per_group)
        group_open = [t for t in tasks
                      if t.get("owner_group") == g
                      and t.get("status") in OPEN_STATUSES
                      and not t.get("_invalid")]
        out.append({
            "group": g,
            "open_total": len(group_open),
            # 逾期数按全量未完成统计（此前只统计被截断的前 N 条 → 少报）
            "overdue": sum(1 for t in group_open if is_overdue(t)),
            "items": items,
        })
    return out


def attendance_rank(members, limit=None):
    """打卡工时排行：按有效时长降序，时长相同按姓名稳定排序。

    无考勤数据的成员（`有效时长` 为空，如尚未打卡或同步未覆盖）排在末尾，
    不参与名次比较，但仍列出——避免"没数据显示不出来"被误读为已排除。
    """
    rows = [m for m in members if not m.get("_invalid") and m.get("member")]

    def sort_key(m):
        hours = m.get("work_hours")
        return (1 if hours is None else 0, -(hours or 0),
                (m.get("member") or {}).get("name") or "")

    rows.sort(key=sort_key)
    out = []
    for m in (rows[:limit] if limit else rows):
        out.append({
            "id": m["id"],
            "member": m.get("member") or {},
            "work_hours": m.get("work_hours"),
            "last_check_in": m.get("last_check_in"),
            "attendance_anomalies": m.get("attendance_anomalies"),
            # 同步元数据随行返回，便于页面标注"截止哪一轮"，不额外拉接口
            "synced_at": m.get("synced_at"),
            "sync_note": m.get("sync_note"),
            # 记录链接：页面行尾的「飞书 ↗」（此前未下发，新旧前端都显示不出来）
            "url": m.get("url"),
        })
    return out


def overview(snap):
    """总览数据（§3.2）：倒计时、五组紧急、近期日程、打卡工时排行。

    项目摘要已按 2026-09-12 布局调整移除（Base 无项目表，该卡长期为空）；
    `projects` 键不再返回，`/projects` 页面仍由 project_progress 单独支撑。
    """
    tasks = snap["tables"].get("tasks", [])
    now = datetime.now(TZ)
    schedule = upcoming_schedule(snap["tables"].get("schedule", []), days=7, now=now)
    attention = attention_tasks(tasks, now)
    today = now.date()
    return {
        "milestones": future_milestones(
            snap["tables"].get("milestones", []), limit=3),
        # groups 是旧版五组格子用的（旧前端回退时仍要），新版总览用 attention
        "groups": group_urgent_summary(tasks),
        "attention": attention,
        "kpi": {
            "overdue": sum(1 for t in attention if t["level"] == "overdue"),
            "due_soon": sum(1 for t in attention if t["level"] == "due_soon"),
            "attention": len(attention),
            "today_schedule": sum(1 for s in schedule
                                  if s.get("ongoing") or _dt(s["start"]["ts"]).date() == today),
        },
        "schedule": schedule,
        "members": attendance_rank(snap["tables"].get("members", [])),
        "quality_issues": len(snap.get("quality") or []),
    }
