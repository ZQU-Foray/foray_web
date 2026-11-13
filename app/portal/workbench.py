# -*- coding: utf-8 -*-
"""工作台：卡片目录、布局存储与卡片数据组装。

分工：
- **布局**按人保存在快照库 workbench_layouts 表（open_id 主键，schema 版本 3），
  这里是唯一的读写入口；布局只是「卡片 id + 宽度」的有序列表，不含任何业务数据，
  卡片数据的口径仍由各页面视图与 compute 决定，换布局不会带来新的权限面。
- **卡片目录**（CATALOG）是服务端唯一事实源：前端只渲染服务端给的清单，
  新增卡片只改这一个文件（并在 _BUILDERS 里登记数据构造函数）。
- **数据组装**只在 GET 时按当前布局与本人在快照里的身份算一次，复用既有视图，
  不新开数据源、不额外拉飞书。
"""
import json
import time

from . import store

# 卡片宽度：half = 半屏（两列网格里占一格），full = 整屏
WIDTHS = ("half", "full")
MAX_CARDS = 20

# 卡片目录：card_id / 标题 / 提示 / 默认宽度 / 需要的数据表（仅用于排障与文档）
CATALOG = (
    {"id": "my_attendance", "title": "我的考勤", "width": "half",
     "hint": "本周时长、达标情况与今日签到状态", "needs": ("members",)},
    {"id": "my_tasks", "title": "我的事项", "width": "full",
     "hint": "名下未完成的事项，按截止时间排序", "needs": ("tasks",)},
    {"id": "urgent_tasks", "title": "临期与逾期", "width": "full",
     "hint": "全队最紧急的未完成事项", "needs": ("tasks",)},
    {"id": "project_progress", "title": "项目进度", "width": "half",
     "hint": "在研项目的完成率", "needs": ("projects", "tasks")},
    {"id": "milestones", "title": "近期里程碑", "width": "half",
     "hint": "接下来要交付的节点", "needs": ("milestones",)},
    {"id": "duty", "title": "今日值日", "width": "half",
     "hint": "今天负责值日的成员", "needs": ("duty",)},
    {"id": "attendance_rank", "title": "工时排行", "width": "half",
     "hint": "本周打卡时长前五名", "needs": ("members",)},
    {"id": "my_procurement", "title": "我的采购", "width": "full",
     "hint": "我发起或经手的采购申请", "needs": ("procurement",)},
    {"id": "data_quality", "title": "数据质量", "width": "half",
     "hint": "被排除记录的数量与主要原因", "needs": ()},
    {"id": "sync_status", "title": "同步状态", "width": "half",
     "hint": "最近一次同步时间、推送积压与冲突", "needs": ()},
)

CATALOG_BY_ID = {c["id"]: c for c in CATALOG}

# 首次进入 / 恢复默认时的布局：先放最常看的两张，其余从卡片库添加
DEFAULT_LAYOUT = (
    {"card_id": "my_attendance", "width": "half"},
    {"card_id": "my_tasks", "width": "full"},
    {"card_id": "urgent_tasks", "width": "full"},
    {"card_id": "project_progress", "width": "half"},
    {"card_id": "milestones", "width": "half"},
)


class WorkbenchLayoutError(ValueError):
    """布局不合法（未知卡片、重复、超量、宽度非法）。"""


def normalize_layout(raw):
    """校验并规整一份布局，返回 [{card_id, width}]（新列表，不含额外键）。

    接受 {"cards": [...]} 或直接数组；未知卡片、重复卡片、超过 MAX_CARDS、
    宽度不在 WIDTHS 里都抛 WorkbenchLayoutError，不做静默裁剪。
    """
    if isinstance(raw, dict):
        raw = raw.get("cards")
    if raw is None:
        msg = "布局缺少 cards 数组"
        raise WorkbenchLayoutError(msg)
    if not isinstance(raw, (list, tuple)):
        msg = "cards 必须是数组"
        raise WorkbenchLayoutError(msg)
    if len(raw) > MAX_CARDS:
        raise WorkbenchLayoutError("卡片最多 %d 张" % MAX_CARDS)
    seen = set()
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise WorkbenchLayoutError("卡片项必须是对象")
        card_id = str(item.get("card_id") or "").strip()
        if card_id not in CATALOG_BY_ID:
            raise WorkbenchLayoutError("未知卡片：%s" % (card_id or "(空)"))
        if card_id in seen:
            raise WorkbenchLayoutError("卡片重复：%s" % card_id)
        seen.add(card_id)
        width = str(item.get("width") or CATALOG_BY_ID[card_id]["width"]).strip()
        if width not in WIDTHS:
            raise WorkbenchLayoutError("卡片宽度只能是 half 或 full：%s" % width)
        out.append({"card_id": card_id, "width": width})
    return out


def default_layout():
    return [dict(x) for x in DEFAULT_LAYOUT]


def _conn():
    return store._db()


def load_layout(open_id):
    """读某人保存的布局；没保存过或内容损坏时返回 None（由调用方用默认布局）。"""
    if not open_id:
        return None
    with store._lock:
        row = _conn().execute(
            "SELECT layout FROM workbench_layouts WHERE open_id=?", (open_id,)).fetchone()
    if not row:
        return None
    try:
        return normalize_layout(json.loads(row["layout"]))
    except (ValueError, TypeError):
        # 库里的旧值不合法（例如卡片下线后）：当作没保存过，页面用默认布局
        return None


def layout_updated_at(open_id):
    if not open_id:
        return None
    with store._lock:
        row = _conn().execute(
            "SELECT updated_at FROM workbench_layouts WHERE open_id=?",
            (open_id,)).fetchone()
    return float(row["updated_at"]) if row else None


def save_layout(open_id, raw):
    """保存某人布局（先校验再落库），返回规整后的布局。"""
    if not open_id:
        raise WorkbenchLayoutError("缺少成员身份，不能保存布局")
    layout = normalize_layout(raw)
    payload = json.dumps(layout, ensure_ascii=False)
    now = time.time()
    with store._lock, _conn() as c:
        c.execute(
            "INSERT INTO workbench_layouts(open_id, layout, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(open_id) DO UPDATE SET layout=excluded.layout, "
            "updated_at=excluded.updated_at", (open_id, payload, now))
    return layout


def reset_layout(open_id):
    """删除保存的布局（回到默认）。没保存过也返回成功，语义是幂等的。"""
    if not open_id:
        raise WorkbenchLayoutError("缺少成员身份，不能恢复默认布局")
    with store._lock, _conn() as c:
        c.execute("DELETE FROM workbench_layouts WHERE open_id=?", (open_id,))
    return default_layout()


def effective_layout(open_id):
    """页面要用的布局：保存过就用保存的，否则默认。"""
    return load_layout(open_id) or default_layout()


# ---------------- 卡片数据组装 ----------------
# 各卡只读快照，口径复用既有视图/计算；接口层已保证快照存在（否则返回无数据信封）。

def _api():
    """延迟导入 api：api 在模块顶层 import 本模块，函数内导入可避免循环。"""
    from . import api as portal_api
    return portal_api


def _open_tasks(tasks):
    from .compute import OPEN_STATUSES
    return [t for t in tasks if t.get("status") in OPEN_STATUSES and not t.get("_invalid")]


def _due_key(t):
    return ((t.get("due") or {}).get("ts") or float("inf"), t.get("id") or "")


def _my_tasks(snap, open_id):
    mine = [t for t in _open_tasks(snap["tables"].get("tasks", []))
            if open_id and open_id in [o.get("id") for o in (t.get("owners") or [])]]
    mine.sort(key=_due_key)
    api = _api()
    pnames = api._project_names(snap)
    items = [api._task_view(t, pnames) for t in mine[:8]]
    return {"items": items, "total": len(mine)}


def _urgent_tasks(snap, _open_id):
    from .compute import urgent_tasks
    return {"items": urgent_tasks(snap["tables"].get("tasks", []), limit=5)}


def _my_attendance(snap, open_id):
    if not open_id:
        return {"items": [], "note": "本地维护者账号没有花名册身份，无法汇总考勤"}
    from . import attendance
    api = _api()
    members = [m for m in snap["tables"].get("members", []) if not m.get("_invalid")]
    data = attendance.page_data(
        members, snap["tables"].get("duty", []),
        week_ready=api._week_cols_ready(read_only=True),
        duty_ready=api._duty_ready(), live=api._attendance_live(members),
        leave_ready=api._leave_ready())
    row = next((r for r in data["members"]
                if (r.get("member") or {}).get("id") == open_id), None)
    if not row:
        return {"items": [], "note": "花名册里找不到本人记录"}
    return {"item": row, "week": data["week"], "threshold": data["threshold"],
            "configured": data["configured"]}


def _project_progress(snap, _open_id):
    from .compute import project_progress
    tasks = snap["tables"].get("tasks", [])
    items = []
    for p in snap["tables"].get("projects", []):
        if p.get("_invalid") or p.get("archived") or p.get("status") in ("已结束", "已归档"):
            continue
        items.append({"id": p["id"], "name": p.get("name"),
                      "owner_group": p.get("owner_group"),
                      "status": p.get("status"), "url": p.get("url"),
                      "progress": project_progress(tasks, p["id"])})
    items.sort(key=lambda x: (x["status"] != "进行中", x["name"] or ""))
    return {"items": items[:6], "total": len(items)}


def _milestones(snap, _open_id):
    from .compute import future_milestones
    return {"items": future_milestones(snap["tables"].get("milestones", []), limit=5)}


def _duty(snap, _open_id):
    from . import attendance
    today_key, weekday, _ = attendance.today_info()
    people, source = attendance.duty_today(
        snap["tables"].get("duty", []), today_key, weekday)
    return {"items": people, "source": source, "date": today_key}


def _attendance_rank(snap, _open_id):
    from .compute import attendance_rank
    members = [m for m in snap["tables"].get("members", []) if not m.get("_invalid")]
    return {"items": attendance_rank(members, limit=5)}


def _my_procurement(snap, open_id):
    api = _api()
    rows = []
    for p in snap["tables"].get("procurement", []):
        if p.get("_invalid"):
            continue
        purchaser_ids = [x.get("id") for x in (p.get("purchaser") or [])]
        if open_id and open_id in purchaser_ids:
            rows.append(p)
    rows.sort(key=lambda p: (p.get("purchase_date") or {}).get("ts") or 0, reverse=True)
    return {"items": [api._procurement_view(p) for p in rows[:5]], "total": len(rows)}


def _data_quality(snap, _open_id):
    from collections import Counter
    issues = snap.get("quality") or []
    # 快照里的质量条目键是 table（store.current 的形状），兼容 table_name 的旧写法
    by_table = Counter(i.get("table") or i.get("table_name") for i in issues)
    kinds = Counter((i.get("issue") or "")[:40] for i in issues)
    return {"total": len(issues),
            "by_table": [{"table_name": k, "count": v} for k, v in by_table.most_common()],
            "top_issues": [{"issue": k, "count": v} for k, v in kinds.most_common(3)]}


def _sync_status(_snap, _open_id):
    from . import cache_store
    st = store.status()
    return {"last_success_at": st.get("last_success_at"),
            "last_attempt_at": st.get("last_attempt_at"),
            "last_duration_sec": st.get("last_duration_sec"),
            "stale": st.get("stale"), "last_error": st.get("last_error"),
            "snapshot_version": st.get("snapshot_version"),
            "counts": st.get("counts"),
            "backlog": cache_store.backlog()}


_BUILDERS = {
    "my_attendance": _my_attendance,
    "my_tasks": _my_tasks,
    "urgent_tasks": _urgent_tasks,
    "project_progress": _project_progress,
    "milestones": _milestones,
    "duty": _duty,
    "attendance_rank": _attendance_rank,
    "my_procurement": _my_procurement,
    "data_quality": _data_quality,
    "sync_status": _sync_status,
}


def build_cards(snap, open_id, layout):
    """按布局组装卡片：每张卡={id,title,hint,width,data}；单卡出错只标记该卡。"""
    out = []
    for item in layout:
        card_id = item["card_id"]
        meta = CATALOG_BY_ID[card_id]
        card = {"id": card_id, "title": meta["title"], "hint": meta["hint"],
                "width": item["width"]}
        try:
            card["data"] = _BUILDERS[card_id](snap, open_id)
        except Exception as e:                      # 单卡失败不拖垮整页
            card["error"] = "卡片数据加载失败：%s" % str(e)[:80]
        out.append(card)
    return out


def catalog_view():
    """给前端「卡片库」用的目录（不含 needs 之外的内部字段，全部为可序列化值）。"""
    return [{"id": c["id"], "title": c["title"], "hint": c["hint"], "width": c["width"]}
            for c in CATALOG]
