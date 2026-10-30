"""只读管理 API（阶段 4）：全部经认证中间件（/api/portal/* = member）。

Envelope（§6.3）：所有响应附 snapshot_version / last_success_at /
server_time / stale；无首快照（has_snapshot=false，data=null）≠ 空表
（data=[]）≠ 失败（last_error 见 /sync-status）。

§6.3 另外三条硬性要求（阶段 4 审计后补齐）：
- 列表提供**分页**（page/page_size + total/has_more）；
- 客户端传入的排序字段走**白名单**，未知值 422（不静默忽略）；
- **变更本地状态的接口校验 Origin/Referer**（跨站 403）。
"""
import os
import time
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from .. import config
from . import attendance, cache_store, compute, normalize, store, sync, writes

router = APIRouter(prefix="/api/portal")

MAX_PAGE_SIZE = 200
_PRIORITY_INDEX = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


# ---------------- envelope / 分页 / 排序 / 同源 ----------------

def _envelope(now=None):
    st = store.status(now)
    return {
        "snapshot_version": st["snapshot_version"],
        "last_success_at": st["last_success_at"],
        "server_time": now or time.time(),
        "stale": st["stale"],
        # §6.4.7「失败立即显示」：数据接口也带上错误类别（无错误为 None）
        "last_error": st["last_error"],
    }, st


def _sync_of(rec):
    """记录的同步标记（待同步 / 同步失败 / 冲突，快照生成时算好）；没有标记不带这个键。"""
    return {"sync": rec["sync"]} if rec.get("sync") else {}


def _require_snapshot():
    """无首快照 → 200 + 明确状态（区分空表）。"""
    snap = store.current()
    env, st = _envelope()
    if not snap:
        return None, {**env, "has_snapshot": False, "data": None,
                      "message": "尚无可用同步数据（首次同步未完成或未配置）"}
    return snap, {**env, "has_snapshot": True}


def _page_args(page, page_size):
    if page < 1:
        raise HTTPException(422, "page 必须 ≥ 1")
    if not (1 <= page_size <= MAX_PAGE_SIZE):
        raise HTTPException(422, f"page_size 必须在 1..{MAX_PAGE_SIZE}")


def _paginate(items, page, page_size):
    total = len(items)
    start = (page - 1) * page_size
    return items[start:start + page_size], {
        "total": total, "page": page, "page_size": page_size,
        "has_more": start + page_size < total}


def _sorter(sort, sorters):
    """排序白名单（§6.3）：未知字段 422，不静默忽略。"""
    if sort not in sorters:
        raise HTTPException(422, "不支持的排序字段 %r；可选: %s"
                            % (sort, ", ".join(sorted(sorters))))
    return sorters[sort]


def _check_same_origin(request):
    """§6.3：变更本地状态的接口校验 Origin/Referer，跨站 → 403。

    浏览器同源请求会带 Origin/Referer；CLI/服务端客户端（Bearer）通常不带，
    此时不拦截（Cookie 会话另有 SameSite=Lax 兜底）。
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return
    host = (request.headers.get("host") or "").lower()
    netloc = urlparse(origin).netloc.lower()
    if netloc and host and netloc != host:
        raise HTTPException(403, "跨站请求被拒绝（Origin 与 Host 不一致）")


# ---------------- 同步状态 / 手动刷新 ----------------

@router.get("/meta")
def api_meta():
    """非敏感元数据：五组顺序 + 源表链接（表级，与维护者 Base 链接同构；
    记录级直达待浏览器实测 record 参数后启用——不猜链接）。"""
    import json as _json
    from ..auth import feishu as _f
    env, _ = _envelope()
    links, readable = {}, False
    try:
        raw = _json.loads(_f.config.FEISHU_CONFIG_FILE.read_text(encoding="utf-8"))
        url = (raw.get("base") or {}).get("url")
        readable = True
        if url:
            for key, t in (raw.get("tables") or {}).items():
                # tables 里混有 "_comment" 等字符串说明键：跳过非映射项，
                # 否则 .get 抛异常被兜底吞掉，所有表的源链接都会缺失
                if isinstance(t, dict) and t.get("table_id"):
                    links[key] = url + "?table=" + t["table_id"]
    except FileNotFoundError:
        pass
    except Exception:
        readable = False                      # 读取失败要显式，不冒充"没有表"
    # 同一个人可能同时属于多个组别（花名册按组分行、组长/组员各自成条），
    # 因此按 open_id 聚合为一条：groups 保留全部组别，leader 取各组"是否组长"的并集。
    # 此前逐条下发、前端按 id 去重只留第一条 → 多组别的人只会显示一个组。
    roster_by_id = {}
    try:
        snap = store.current()
        for r in (snap.get("tables", {}).get("roster") or []):
            group = r.get("group")
            for key, is_leader in (("leaders", True), ("members", False)):
                for u in r.get(key) or []:
                    if not u.get("id"):
                        continue
                    rec = roster_by_id.setdefault(u["id"], {
                        "id": u["id"], "name": u.get("name") or "",
                        "groups": [], "leader": False})
                    if group and group not in rec["groups"]:
                        rec["groups"].append(group)
                    if is_leader:
                        rec["leader"] = True
                    if not rec["name"] and u.get("name"):
                        rec["name"] = u["name"]
    except Exception:
        pass
    roster_users = sorted(roster_by_id.values(),
                          key=lambda x: (x["name"], x["id"]))
    return {**env, "groups": ["机械", "硬件", "电控", "算法", "运营"],
            "source_links": links, "config_readable": readable,
            "roster_users": roster_users}


@router.get("/sync-status")
def sync_status():
    env, st = _envelope()
    return {**env, "has_snapshot": st["has_snapshot"],
            "last_attempt_at": st["last_attempt_at"],
            "last_duration_sec": st["last_duration_sec"],
            "counts": st["counts"], "last_error": st["last_error"],
            "syncing": sync.scheduler().is_running(),
            "sync_enabled": config.PORTAL_SYNC_ENABLED,
            "sync_interval_sec": config.PORTAL_SYNC_INTERVAL_SEC,
            "next_run_in_sec": sync.scheduler().next_run_in(),
            "fail_streak": sync.scheduler().fail_streak(),
            "cache": _cache_status()}


def _cache_status():
    """本地缓存库概况：写入模式、走队列的表、队列积压与未确认冲突（缓存库第 3 步）。"""
    from . import pusher
    out = {"write_mode": config.PORTAL_WRITE_MODE,
           "cache_tables": [t for t in config.PORTAL_CACHE_TABLES if writes.queued(t)],
           "pusher_alive": pusher.pusher().is_alive()}
    try:
        out.update(cache_store.backlog())
    except Exception:
        out["backlog_error"] = True
    return out


@router.get("/sync-queue")
def api_sync_queue(request: Request):
    """推送队列与冲突明细（「数据质量」页用，缓存库第 5 步）：全员可看，组长可处理。"""
    from . import queue_view
    snap = store.current()
    env, _ = _envelope()
    _, groups = leader_ctx(request)
    return {**env, "data": queue_view.page(snap, _cache_status(), can_manage=bool(groups))}


def _queue_op(op_id):
    op = cache_store.get_op(op_id)
    if op is None:
        raise HTTPException(404, "队列里没有这条操作")
    return op


@router.post("/sync-queue/{op_id}/retry")
def api_queue_retry(op_id: int, request: Request):
    """组长重试一条推送失败的操作（飞书侧问题修好之后）；新建连同其后续操作一起重试。"""
    _require_leader(request)
    op = _queue_op(op_id)
    ids = [op_id] + (cache_store.failed_after(op_id) if op["op"] == "create" else [])
    if not cache_store.retry_failed(op_id):
        raise HTTPException(409, "这条操作已不是失败状态（可能已被别人处理），请刷新")
    for i in ids[1:]:
        cache_store.retry_failed(i)
    _audit("portal_queue_retry", op=op_id, table=op["table_name"], record=op["record_id"],
           count=len(ids), by=_who(request))
    writes.changed(True)
    return {"ok": True, "count": len(ids)}


@router.post("/sync-queue/{op_id}/discard")
def api_queue_discard(op_id: int, request: Request):
    """组长放弃一条还没落到飞书的操作（失败或排队中）；页面回到飞书上的值。
    放弃新建时，同一记录上已失败的后续操作一并放弃。"""
    _require_leader(request)
    op = _queue_op(op_id)
    note = "组长在页面放弃：" + _who(request)
    ids = [op_id] + (cache_store.failed_after(op_id) if op["op"] == "create" else [])
    if not cache_store.discard(op_id, note=note):
        raise HTTPException(409, "这条操作正在推送或已经结束，不能放弃，请刷新")
    for i in ids[1:]:
        cache_store.discard(i, note=note)
    _audit("portal_queue_discard", op=op_id, table=op["table_name"], record=op["record_id"],
           count=len(ids), by=_who(request))
    writes.changed(False)
    return {"ok": True, "count": len(ids)}


@router.post("/conflicts/{conflict_id}/ack")
def api_conflict_ack(conflict_id: int, request: Request):
    """组长确认已知悉一条冲突（结果已按飞书为准，确认只是消掉提醒）。"""
    _require_leader(request)
    c = cache_store.get_conflict(conflict_id)
    if c is None:
        raise HTTPException(404, "没有这条冲突记录")
    if not cache_store.ack_conflict(conflict_id, by=_who(request)):
        raise HTTPException(409, "这条冲突已经确认过了")
    _audit("portal_conflict_ack", conflict=conflict_id, table=c["table_name"],
           record=c["record_id"], field=c.get("field"), by=_who(request))
    writes.changed(False)
    return {"ok": True}


@router.post("/sync")
def sync_now(request: Request):
    """手动刷新：合并定时任务 + 限频（前端人数不放大飞书请求）。"""
    _check_same_origin(request)
    if not config.PORTAL_SYNC_ENABLED:
        env, _ = _envelope()
        return {**env, "accepted": False, "detail": "同步已关闭（回退开关）"}
    # 按用户限频（阶段 8）：取会话里的用户标识；拿不到则退化为全局限频
    sess = getattr(request.state, "session", None) or {}
    user = str(sess.get("user") or "") or None
    accepted, detail = sync.scheduler().request_manual(user)
    env, _ = _envelope()
    return {**env, "accepted": accepted, "detail": detail}


# ---------------- 总览 ----------------

@router.get("/overview")
def api_overview():
    snap, env = _require_snapshot()
    if not snap:
        return env
    data = compute.overview(snap)
    # 签到中人数要用进程内实时态（与考勤页同一口径），快照里算不准
    members = snap["tables"].get("members", [])
    live = _attendance_live(members)
    _, _, today_start = attendance.today_info()
    data["kpi"]["checked_in"] = sum(
        1 for m in members
        if not m.get("_invalid") and m.get("member")
        and attendance.is_checked_in(m, today_start, live)[0])
    return {**env, "data": data}


@router.get("/attendance")
def api_attendance():
    """考勤页（只读）：本周时长与达标、上周结果、签到中、当天值日、打卡工时排行。

    周时长与达标取飞书公式列；成员数据表还没建周结算列、或值日表还没接入时，
    data.configured 标明未接入，页面给出说明而不是显示 0。"""
    snap, env = _require_snapshot()
    if not snap:
        return env
    members = snap["tables"].get("members", [])
    data = attendance.page_data(
        members, snap["tables"].get("duty", []),
        week_ready=_week_cols_ready(read_only=True), duty_ready=_duty_ready(),
        live=_attendance_live(members), leave_ready=_leave_ready())
    data["rank"] = compute.attendance_rank(members)
    return {**env, "data": data}


@router.get("/quality")
def api_quality(page: int = 1, page_size: int = 50):
    """数据质量明细（§5.2）：被排除的记录与原因，供页面显示异常说明。

    只暴露 表/record_id/原因文本（原因不含成员姓名）。
    """
    snap, env = _require_snapshot()
    if not snap:
        return env
    _page_args(page, page_size)
    items = sorted(snap["quality"],
                   key=lambda x: (x.get("table") or "", x.get("record_id") or ""))
    by_table = {}
    for q in snap["quality"]:
        by_table[q.get("table")] = by_table.get(q.get("table"), 0) + 1
    data, meta = _paginate(items, page, page_size)
    return {**env, "data": data, **meta, "by_table": by_table}


# ---------------- 项目 ----------------

_PROJECT_SORTS = {
    "default": lambda x: (0 if x.get("status") != "已归档" else 1,
                          x.get("name") or ""),
    "name": lambda x: x.get("name") or "",
    "status": lambda x: (x.get("status") or "", x.get("name") or ""),
    "plan_start": lambda x: ((x.get("plan_start") or {}).get("ts") or 0,
                             x.get("name") or ""),
}


@router.get("/projects")
def api_projects(sort: str = "default", page: int = 1, page_size: int = 50,
                 include_archived: bool = False, group: str = None):
    """§5.1：归档默认隐藏（可 include_archived=true 查看）；支持按主责组筛选。"""
    _page_args(page, page_size)
    _sorter(sort, _PROJECT_SORTS)             # 未知排序字段先 422
    snap, env = _require_snapshot()
    if not snap:
        return env
    tasks = snap["tables"].get("tasks", [])
    out = []
    for p in snap["tables"].get("projects", []):
        if not include_archived and p.get("status") == "已归档":
            continue
        if group and p.get("owner_group") != group:
            continue
        out.append(_project_view(p, tasks))
    out.sort(key=_sorter(sort, _PROJECT_SORTS))
    data, meta = _paginate(out, page, page_size)
    return {**env, "data": data, **meta, "include_archived": include_archived}


def _project_view(p, tasks):
    return {"id": p["id"], "name": p["name"],
            "description": p.get("description"),
            "owner_group": p.get("owner_group"),
            "collab_groups": p.get("collab_groups") or [],
            "owners": p.get("owners") or [],
            "plan_start": p.get("plan_start"), "plan_end": p.get("plan_end"),
            "status": p.get("status"),
            "progress": compute.project_progress(tasks, p["id"]),
            "url": p.get("url"),
            "invalid": bool(p.get("_invalid")), **_sync_of(p)}


@router.get("/projects/{project_id}")
def api_project_detail(project_id: str):
    snap, env = _require_snapshot()
    if not snap:
        return env
    tasks = snap["tables"].get("tasks", [])
    project_id = _resolve_local(project_id)
    for p in snap["tables"].get("projects", []):
        if p["id"] == project_id:
            tasks_of = [t for t in tasks if t.get("project_id") == project_id]
            return {**env, "data": {
                **_project_view(p, tasks),
                "tasks": [_task_view(t, _project_names(snap)) for t in tasks_of],
                "milestones": [{"id": m["id"], "name": m["name"],
                                "kind": m.get("kind"), "target": m.get("target"),
                                "url": m.get("url")}
                               for m in snap["tables"].get("milestones", [])
                               if m.get("project_id") == project_id
                               and not m.get("_invalid")]}}
    raise HTTPException(404, "项目不存在（或已从源表移除）")


# ---------------- 事项 ----------------

_TASK_SORTS = {
    "default": lambda x: (_PRIORITY_INDEX.get(x.get("priority"), 9),
                          x.get("id") or ""),
    "priority": lambda x: (_PRIORITY_INDEX.get(x.get("priority"), 9),
                           x.get("id") or ""),
    "due": lambda x: ((x.get("due") or {}).get("ts") or float("inf"),
                      x.get("id") or ""),
    "name": lambda x: x.get("name") or "",
    "status": lambda x: (x.get("status") or "", x.get("id") or ""),
}


def _project_names(snap):
    """项目 id → 名称。事项列表/详情据此显示项目名，避免前端拿不到项目清单时露出 record_id。"""
    return {p["id"]: p["name"] for p in snap["tables"].get("projects", [])}


def _task_view(t, project_names=None):
    return {"id": t["id"], "name": t["name"],
            "project_id": t.get("project_id"),
            "project_name": (project_names or {}).get(t.get("project_id")),
            "owner_group": t.get("owner_group"),
            "collab_groups": t.get("collab_groups") or [],
            "owners": t.get("owners") or [],
            "priority": t.get("priority"), "status": t.get("status"),
            "start": t.get("start"), "due": t.get("due"),
            "due_precision": t.get("due_precision"),
            "done_time": t.get("done_time"),
            "submit_time": t.get("submit_time"),
            "description": t.get("description"),
            "blocker_note": t.get("blocker_note"),
            "overdue": compute.is_overdue(t),
            "due_soon": compute.is_due_soon(t),
            "url": t.get("url"),
            "invalid": bool(t.get("_invalid")), **_sync_of(t)}


@router.get("/tasks")
def api_tasks(group: str = None, priority: str = None, status: str = None,
              project_id: str = None, owner: str = None,
              sort: str = "default", page: int = 1, page_size: int = 50):
    _page_args(page, page_size)
    _sorter(sort, _TASK_SORTS)
    snap, env = _require_snapshot()
    if not snap:
        return env
    out = []
    pnames = _project_names(snap)
    for t in snap["tables"].get("tasks", []):
        if group and t.get("owner_group") != group:
            continue
        if priority and t.get("priority") != priority:
            continue
        if status and t.get("status") != status:
            continue
        if project_id and t.get("project_id") != project_id:
            continue
        if owner and owner not in [o.get("id") for o in (t.get("owners") or [])]:
            continue
        out.append(_task_view(t, pnames))
    out.sort(key=_sorter(sort, _TASK_SORTS))
    data, meta = _paginate(out, page, page_size)
    return {**env, "data": data, **meta, "urgent": compute.urgent_tasks(
        snap["tables"].get("tasks", []))}


@router.get("/tasks/{task_id}")
def api_task_detail(task_id: str):
    snap, env = _require_snapshot()
    if not snap:
        return env
    task_id = _resolve_local(task_id)
    for t in snap["tables"].get("tasks", []):
        if t["id"] == task_id:
            return {**env, "data": _task_view(t, _project_names(snap))}
    raise HTTPException(404, "事项不存在")


# ---------------- 重要节点 ----------------

_MILESTONE_SORTS = {
    "default": lambda x: ((x.get("target") or {}).get("ts") or 0, x.get("id") or ""),
    "target": lambda x: ((x.get("target") or {}).get("ts") or 0, x.get("id") or ""),
    "name": lambda x: x.get("name") or "",
}


@router.get("/milestones")
def api_milestones(sort: str = "default", page: int = 1, page_size: int = 50):
    _page_args(page, page_size)
    _sorter(sort, _MILESTONE_SORTS)
    snap, env = _require_snapshot()
    if not snap:
        return env
    milestones = snap["tables"].get("milestones", [])
    data = compute.future_milestones(milestones)
    data.sort(key=_sorter(sort, _MILESTONE_SORTS))
    page_data, meta = _paginate(data, page, page_size)
    now = compute.datetime_now()
    past = []
    for m in milestones:
        if m.get("_invalid") or not m.get("target"):
            continue
        b = compute.milestone_boundary(m["target"])
        if b is not None and b <= now:
            past.append({"id": m["id"], "name": m["name"], "kind": m.get("kind"),
                         "target": m["target"], "url": m.get("url")})
    past.sort(key=lambda x: ((x["target"] or {}).get("ts") or 0, x["id"]))
    return {**env, "data": page_data, **meta, "history": past}


# ---------------- 公共日程 ----------------

_SCHEDULE_SORTS = {
    "default": lambda x: ((x.get("start") or {}).get("ts") or 0, x.get("id") or ""),
    "start": lambda x: ((x.get("start") or {}).get("ts") or 0, x.get("id") or ""),
    "name": lambda x: x.get("name") or "",
    "kind": lambda x: (x.get("kind") or "", (x.get("start") or {}).get("ts") or 0),
}


def _schedule_view(s):
    return {"id": s["id"], "name": s["name"], "start": s.get("start"),
            "end": s.get("end"), "all_day": s.get("all_day"),
            "kind": s.get("kind"), "groups": s.get("groups") or [],
            "participants": s.get("participants") or [],
            "location": s.get("location"), "project_id": s.get("project_id"),
            "description": s.get("description"), "status": s.get("status"),
            "url": s.get("url"),
            "invalid": bool(s.get("_invalid")), **_sync_of(s)}


@router.get("/schedule")
def api_schedule(days: int = 7, sort: str = "default",
                 page: int = 1, page_size: int = 50):
    _page_args(page, page_size)
    _sorter(sort, _SCHEDULE_SORTS)
    if not (1 <= days <= 366):
        raise HTTPException(422, "days 必须在 1..366")
    snap, env = _require_snapshot()
    if not snap:
        return env
    data = compute.upcoming_schedule(
        snap["tables"].get("schedule", []), days=days)
    data.sort(key=_sorter(sort, _SCHEDULE_SORTS))
    page_data, meta = _paginate(data, page, page_size)
    return {**env, "data": page_data, **meta}


@router.get("/schedule/{schedule_id}")
def api_schedule_detail(schedule_id: str):
    snap, env = _require_snapshot()
    if not snap:
        return env
    schedule_id = _resolve_local(schedule_id)
    for s in snap["tables"].get("schedule", []):
        if s["id"] == schedule_id:
            return {**env, "data": _schedule_view(s)}
    raise HTTPException(404, "日程不存在")


# ---------------- 物资采购（物资明细） ----------------

def _proc_sort_date(x):
    """采购日期倒序（最新在前）；无日期的排最后，不伪装成 1970。"""
    ts = (x.get("purchase_date") or {}).get("ts")
    return (1, 0) if not ts else (0, -ts)


_PROCUREMENT_SORTS = {
    "default": lambda x: (x.get("id") or ""),
    "name": lambda x: (x.get("name") or "", x.get("id") or ""),
    "purchased": lambda x: (0 if x.get("purchased") else 1, x.get("id") or ""),
    "date": lambda x: (_proc_sort_date(x), x.get("id") or ""),
    "amount": lambda x: (-(x.get("total_price") or 0), x.get("id") or ""),
    "code": lambda x: (x.get("code") or "～", x.get("id") or ""),
}


def _procurement_view(p):
    return {"id": p["id"], "name": p["name"],
            "groups": p.get("groups") or [],
            "link": p.get("link"),
            "purpose": p.get("purpose"),
            "attachments": p.get("attachments") or [],
            "invoice_files": p.get("invoice_files") or [],
            "leader_approval": bool(p.get("leader_approval")),
            "purchased": bool(p.get("purchased")),
            "invoice_done": bool(p.get("invoice_done")),
            "season": p.get("season"),
            "category1": p.get("category1"),
            "category2": p.get("category2"),
            "code": p.get("code"),
            "purchase_date": p.get("purchase_date"),
            "unit_price": p.get("unit_price"),
            "quantity": p.get("quantity"),
            "total_price": p.get("total_price"),
            "purchaser": p.get("purchaser") or [],
            "url": p.get("url"),
            "invalid": bool(p.get("_invalid")), **_sync_of(p)}


def _procurement_options(rows):
    """筛选与表单下拉的可选项：常量清单 ∪ 快照里实际出现的值。

    维护者在飞书新增「赛季」「一级/二级分类」选项后门户无需改代码即可选到；
    历史值即使已从飞书选项里删掉，也仍能筛得到。
    """
    def merged(base, key):
        seen = {v for v in (r.get(key) for r in rows) if v}
        return sorted(seen | set(base))
    return {"seasons": merged(normalize.PROC_SEASONS, "season"),
            "category1": merged(normalize.PROC_CATEGORY1, "category1"),
            "category2": merged(normalize.PROC_CATEGORY2, "category2"),
            "groups": list(_GROUPS)}


@router.get("/procurement")
def api_procurement(group: str = None, season: str = None, category1: str = None,
                    category2: str = None, invoice: str = None, q: str = None,
                    sort: str = "default", page: int = 1, page_size: int = 50):
    """物资明细列表：按组别/赛季/一二级分类/开票状态筛选，q 搜名称与编号。

    invoice=done 只看已开票、=undone 只看未开票。响应另带 options（下拉可选项）
    与 stats（当前筛选下的条数与合计金额）。
    """
    _page_args(page, page_size)
    _sorter(sort, _PROCUREMENT_SORTS)
    snap, env = _require_snapshot()
    if not snap:
        return env
    all_rows = snap["tables"].get("procurement", [])
    keyword = (q or "").strip().lower()
    out = []
    for p in all_rows:
        if group and group not in (p.get("groups") or []):
            continue
        if season and p.get("season") != season:
            continue
        if category1 and p.get("category1") != category1:
            continue
        if category2 and p.get("category2") != category2:
            continue
        if invoice == "done" and not p.get("invoice_done"):
            continue
        if invoice == "undone" and p.get("invoice_done"):
            continue
        if keyword:
            hay = " ".join(str(x or "") for x in
                           (p.get("name"), p.get("code"), p.get("purpose"))).lower()
            if keyword not in hay:
                continue
        out.append(_procurement_view(p))
    out.sort(key=_sorter(sort, _PROCUREMENT_SORTS))
    amount = sum((r.get("total_price") or 0) for r in out)
    data, meta = _paginate(out, page, page_size)
    return {**env, "data": data, **meta,
            "options": _procurement_options(all_rows),
            "stats": {"count": len(out), "amount": round(amount, 2)}}


@router.get("/procurement/{record_id}")
def api_procurement_detail(record_id: str):
    snap, env = _require_snapshot()
    if not snap:
        return env
    record_id = _resolve_local(record_id)
    for p in snap["tables"].get("procurement", []):
        if p["id"] == record_id:
            return {**env, "data": _procurement_view(p)}
    raise HTTPException(404, "采购记录不存在（或已从源表移除）")


# ================= 写入接口（组长新建 / 组员白名单修改） =================
# 权威在服务端：组长身份从快照花名册推导（open_id ∈ 某组 leaders），
# 前端按钮只管显隐。全部走同源校验 + 审计日志 + 写后触发手动同步。

from . import writer as portal_writer          # noqa: E402  (靠近使用处)
from .sync import SyncError as _SyncError      # noqa: E402
from ..auth.accounts import audit as _audit    # noqa: E402
import datetime as _dt                         # noqa: E402

_GROUPS = ["机械", "硬件", "电控", "算法", "运营"]
_PRIORITIES = ["P0", "P1", "P2", "P3"]
_EXEC_STATUSES = ["未开始", "进行中", "受阻", "待验收"]
_PROJECT_STATUSES = ["筹备中", "进行中", "已结束", "已归档"]
# 组员（非组长）可改字段白名单：提交验收时间 + 执行状态（仅两态推进）
_MEMBER_TASK_FIELDS = {"submit_time", "status"}
_MEMBER_STATUS_RANGE = {"未开始", "进行中"}

# 本地账号（open_id 形如 local-*，无飞书身份）进不了花名册的「人员」字段，
# 故对维护者/测试账号留一条显式授权旁路：环境变量列出账号 open_id（逗号分隔），
# 命中即按组长处理、管辖全部组别。默认留空 = 不授权，花名册仍是唯一权威。
_LOCAL_LEADER_IDS = {s.strip() for s in
                     (os.environ.get("PORTAL_LOCAL_LEADERS") or "").split(",")
                     if s.strip()}


def leader_ctx(request):
    """(open_id, leader_groups)：组长判定 = open_id 出现在花名册某组 leaders；
    本地账号另经 PORTAL_LOCAL_LEADERS 显式授权（默认关闭）。"""
    rec = getattr(request.state, "session", None) or {}
    open_id = (rec.get("extra") or {}).get("open_id") or ""
    if open_id and open_id in _LOCAL_LEADER_IDS:
        return open_id, list(_GROUPS)
    groups = []
    if open_id:
        try:
            snap = store.current()
            for r in (snap.get("tables", {}).get("roster") or []):
                if any(u.get("id") == open_id for u in r.get("leaders", [])):
                    groups.append(r.get("group"))
        except Exception:
            pass                      # 花名册读取失败按非组长处理（fail-closed）
    return open_id, groups


def _require_leader(request):
    _check_same_origin(request)
    open_id, groups = leader_ctx(request)
    if not groups:
        raise HTTPException(403, "仅组长可执行此操作（以花名册组长列为准）")
    return open_id, groups


# ---- 字段校验（值形状 → 飞书写契约） ----

def _f_text(v, label, required, max_len=500):
    if v in (None, ""):
        if required:
            raise HTTPException(422, f"缺{label}")
        return None
    s = str(v).strip()
    if not s:
        if required:
            raise HTTPException(422, f"缺{label}")
        return None
    if len(s) > max_len:
        raise HTTPException(422, f"{label} 超长（≤{max_len} 字）")
    return s


def _f_enum(v, allowed, label, required=False, default=None):
    if v in (None, ""):
        if required:
            raise HTTPException(422, f"缺{label}")
        return default
    if v not in allowed:
        raise HTTPException(422, f"{label} 非法（允许：{'/'.join(allowed)}）")
    return v


def _f_ms(v, label):
    """ISO 字符串（含 datetime-local 的 YYYY-MM-DDTHH:MM）或秒/毫秒时间戳 → 毫秒。"""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            f = float(v)
            return int(f * 1000) if f < 1e11 else int(f)
        dt = _dt.datetime.fromisoformat(str(v).strip().replace("T", " "))
        return int(dt.timestamp() * 1000)
    except Exception:
        raise HTTPException(422, f"{label} 日期无法解析：{v}")


def _f_users(v, label):
    if v in (None, []):
        return None
    if (not isinstance(v, list)
            or not all(isinstance(x, str) and x for x in v)):
        raise HTTPException(422, f"{label} 需为 open_id 字符串数组")
    return [{"id": x} for x in v]


def _f_groups(v, label, required, single=False):
    if v in (None, [], ""):
        if required:
            raise HTTPException(422, f"缺{label}")
        return None
    lst = [v] if isinstance(v, str) else v
    if not isinstance(lst, list):
        raise HTTPException(422, f"{label} 需为组别数组")
    bad = [g for g in lst if g not in _GROUPS]
    if bad:
        raise HTTPException(422, f"{label} 含未知组别：{bad}")
    if single:
        if len(lst) != 1:
            raise HTTPException(422, f"{label} 只能选一个组")
        return lst[0]
    return lst


def _f_project(v):
    """所属项目：record_id 字符串，必须在当前项目快照中（失效关联不静默写入）。"""
    if v in (None, ""):
        return None
    s = str(v)
    try:
        snap = store.current()
        for p in (snap.get("tables", {}).get("projects") or []):
            if p.get("id") == s:
                return [s]                     # 关联字段写形状 = record_id 数组
    except Exception:
        pass
    raise HTTPException(422, "所属项目不存在（以当前项目列表为准）")


def _task_columns(body, *, editing=False, member=False):
    """语义键 → 工作事项表列名与值；editing=部分更新（无字段的键跳过）。

    状态联动：执行状态与验收结论共同推导终态（normalize）——凡显式写
    执行状态，同时把验收结论复位为"未验收"，否则派生状态可能不变。
    """
    cols = {}
    if "name" in body or not editing:
        v = _f_text(body.get("name"), "事项名称", required=not editing, max_len=120)
        if v is not None:
            cols["事项名称"] = v
    if "description" in body:
        v = _f_text(body.get("description"), "具体事项", False, 2000)
        if v is not None:
            cols["具体事项"] = v
    if "owner_group" in body or not editing:
        v = _f_groups(body.get("owner_group"), "组别", required=not editing)
        if v is not None:
            cols["组别"] = v
    if "priority" in body or not editing:
        v = _f_enum(body.get("priority"), _PRIORITIES, "优先级",
                    required=not editing, default="P2" if editing else None)
        if v is not None:
            cols["优先级"] = v
    for key, col, label in (("owners", "负责人", "负责人"),):
        if key in body:
            v = _f_users(body.get(key), label)
            if v is not None:
                cols[col] = v
    for key, col, label in (("start_time", "计划开始", "计划开始"),
                            ("due_time", "计划截止", "计划截止"),
                            ("submit_time", "提交验收时间", "提交验收时间")):
        if key in body:
            v = _f_ms(body.get(key), label)
            if v is not None:
                cols[col] = v
    if "status" in body or not editing:
        v = _f_enum(body.get("status"), _EXEC_STATUSES, "执行状态",
                    default="未开始")
        cols["执行状态"] = v
        cols["验收结论"] = "未验收"      # 状态复位联动（见 docstring）
    if "project" in body:
        v = _f_project(body.get("project"))
        if v is not None:
            cols["所属项目"] = v
    if not cols:
        raise HTTPException(422, "没有可写入的字段")
    return cols


def _project_columns(body, *, editing=False):
    cols = {}
    if "name" in body or not editing:
        v = _f_text(body.get("name"), "项目名称", required=not editing, max_len=120)
        if v is not None:
            cols["项目名称"] = v
    if "description" in body:
        v = _f_text(body.get("description"), "项目说明", False, 2000)
        if v is not None:
            cols["项目说明"] = v
    if "owner_group" in body or not editing:
        v = _f_groups(body.get("owner_group"), "主责组", required=not editing, single=True)
        if v is not None:
            cols["主责组"] = v
    if "collab_groups" in body:
        v = _f_groups(body.get("collab_groups"), "协作组", False)
        if v is not None:
            cols["协作组"] = v
    if "owners" in body:
        v = _f_users(body.get("owners"), "负责人")
        if v is not None:
            cols["负责人"] = v
    for key, col, label in (("plan_start", "计划开始", "计划开始"),
                            ("plan_end", "计划结束", "计划结束")):
        if key in body:
            v = _f_ms(body.get(key), label)
            if v is not None:
                cols[col] = v
    if "status" in body or not editing:
        cols["项目状态"] = _f_enum(body.get("status"), _PROJECT_STATUSES,
                                   "项目状态", default="筹备中")
    if not cols:
        raise HTTPException(422, "没有可写入的字段")
    return cols


def _resolve_local(record_id):
    """详情页的本地临时 ID：已推到飞书的换成飞书 ID（列表里该记录已换成飞书 ID）。"""
    try:
        return cache_store.resolve_id(record_id)
    except Exception:
        return record_id


def _who(request):
    return (getattr(request.state, "session", {}) or {}).get("user", "?")


def _write_response(request, action, record_id, name, queued=False):
    """写入成功的统一响应。queued=True：已进本地队列，推送线程推完会自己请求同步。"""
    if queued:
        accepted = False
        hint = "已保存，正在推送到飞书（通常几秒内完成）"
    else:
        accepted, _ = sync.scheduler().request_manual(
            "portal-write:" + (getattr(request.state, "session", {}) or {}).get("display", "?"))
        hint = "已写入飞书"
    _audit(action, record=record_id, name=name, by=_who(request),
           sync_triggered=accepted, queued=queued)
    return {"ok": True, "record_id": record_id, "queued": queued,
            "sync_triggered": accepted, "hint": hint}


def _gateway(fn, *args, **kw):
    """经写入网关执行，并把错误转成 HTTP 语义。"""
    try:
        return fn(*args, **kw)
    except _SyncError as e:
        raise _write_error(e)
    except cache_store.RecordUnknown:
        raise HTTPException(409, "该记录不在本地数据中（可能刚在飞书新建、已删除或正在删除），"
                                 "请刷新后重试")


def _write_error(e: _SyncError):
    detail = str(e)
    if e.klass == "config":
        return HTTPException(503, f"飞书写入未配置或权限不足：{detail}")
    if e.klass in ("network", "rate_limited"):
        return HTTPException(502, f"飞书暂时不可达（{e.klass}）：{detail}")
    return HTTPException(502, f"飞书拒绝写入：{detail}")



_SCHED_KINDS = ["例会", "联调", "测试", "训练", "检查", "比赛", "运营活动", "其他"]
_SCHED_STATUSES = ["待进行", "进行中", "已结束", "已取消"]


def _schedule_columns(body, *, editing=False):
    """语义键 → 公共日程表列名与值；结束时间不得早于开始时间。"""
    cols = {}
    if "name" in body or not editing:
        v = _f_text(body.get("name"), "日程名称", required=not editing, max_len=120)
        if v is not None:
            cols["日程名称"] = v
    if "kind" in body or not editing:
        v = _f_enum(body.get("kind"), _SCHED_KINDS, "日程类型", default="其他")
        if v is not None:
            cols["日程类型"] = v
    st = _f_ms(body.get("start_time"), "开始时间") if ("start_time" in body or not editing) else None
    en = _f_ms(body.get("end_time"), "结束时间") if "end_time" in body else None
    if st is not None:
        cols["开始时间"] = st
    if en is not None:
        cols["结束时间"] = en
    if st and en and en < st:
        raise HTTPException(422, "结束时间不能早于开始时间")
    if "groups" in body:
        v = _f_groups(body.get("groups"), "涉及组别", False)
        if v is not None:
            cols["涉及组别"] = v
    if "participants" in body:
        v = _f_users(body.get("participants"), "人员")
        if v is not None:
            cols["人员"] = v
    if "location" in body:
        v = _f_text(body.get("location"), "地点", False, 200)
        if v is not None:
            cols["地点"] = v
    if "status" in body or not editing:
        cols["日程状态"] = _f_enum(body.get("status"), _SCHED_STATUSES,
                                   "日程状态", default="待进行")
    if "description" in body:
        v = _f_text(body.get("description"), "安排详情", False, 2000)
        if v is not None:
            cols["安排详情"] = v
    if not cols:
        raise HTTPException(422, "没有可写入的字段")
    return cols

@router.post("/write/tasks")
def write_task_create(body: dict, request: Request):
    """组长：新建工作事项（写回飞书 + 触发同步）。"""
    _require_leader(request)
    cols = _task_columns(body)
    rid, queued = _gateway(writes.create, "tasks", cols, by=_who(request))
    return _write_response(request, "portal_task_create", rid, cols.get("事项名称"), queued)


@router.post("/write/projects")
def write_project_create(body: dict, request: Request):
    """组长：新建项目（写回飞书 + 触发同步）。"""
    _require_leader(request)
    cols = _project_columns(body)
    rid, queued = _gateway(writes.create, "projects", cols, by=_who(request))
    return _write_response(request, "portal_project_create", rid, cols.get("项目名称"), queued)


@router.patch("/write/tasks/{record_id}")
def write_task_update(record_id: str, body: dict, request: Request):
    """修改事项：组长全字段；组员仅白名单（提交验收时间 + 执行状态两态），且不能改已完成的事项。"""
    _check_same_origin(request)
    open_id, groups = leader_ctx(request)
    if not groups and rec_role(request) != "member":
        raise HTTPException(403, "仅成员可修改事项")
    keys = set(body.keys())
    if not groups:                                   # 组员：白名单
        illegal = keys - _MEMBER_TASK_FIELDS
        if illegal:
            raise HTTPException(403, f"组员仅可修改：{'/'.join(sorted(_MEMBER_TASK_FIELDS))}")
        if "status" in body and body.get("status") not in _MEMBER_STATUS_RANGE:
            raise HTTPException(403, "组员只能把执行状态在 未开始/进行中 之间推进")
        # 已完成即归档：组员改执行状态会把验收结论复位、把事项拉出归档，只有组长能重新打开
        task = _snapshot_record("tasks", record_id)
        if task and task.get("status") == "已完成":
            raise HTTPException(403, "已完成（已归档）的事项只有组长能修改或重新打开")
    else:                                            # 组长：可改集合（不含赛季）
        illegal = keys - {"name", "description", "owner_group", "priority", "owners",
                          "start_time", "due_time", "submit_time",
                          "status", "project"}
        if illegal:
            raise HTTPException(422, f"不可修改的字段：{sorted(illegal)}")
    cols = _task_columns(body, editing=True)
    queued = _gateway(writes.update, "tasks", record_id, cols, by=_who(request))
    return _write_response(request, "portal_task_update", record_id, cols.get("事项名称"),
                           queued)


def _snapshot_record(table, record_id):
    """从当前快照取一条记录（认本地临时 ID）；找不到返回 None。"""
    ids = {record_id, _resolve_local(record_id)}
    try:
        snap = store.current()
        for t in (snap.get("tables", {}).get(table) or []):
            if t.get("id") in ids:
                return t
    except Exception:
        pass
    return None


@router.post("/write/tasks/{record_id}/complete")
def write_task_complete(record_id: str, request: Request):
    """组长：把事项设为已完成（即归档）。

    完成时间由服务端自动记为此刻，写进「验收时间」；状态按验收流写成
    待验收 + 通过（normalize 推导为已完成）。提交验收时间没填过的同填此刻，
    否则成员数据会把它算成「交付时间待补数」；已填过的保留成员原来的提交时间。
    """
    _require_leader(request)
    task = _snapshot_record("tasks", record_id)
    if task is None:
        raise HTTPException(404, "事项不存在（或已从源表移除），请刷新后重试")
    if task.get("status") == "已完成":
        raise HTTPException(409, "该事项已经是已完成状态")
    now_ms = int(time.time() * 1000)
    cols = {"执行状态": "待验收", "验收结论": "通过", "验收时间": now_ms}
    if not task.get("submit_time"):
        cols["提交验收时间"] = now_ms
    queued = _gateway(writes.update, "tasks", record_id, cols, by=_who(request))
    return _write_response(request, "portal_task_complete", record_id, task.get("name"),
                           queued)


def rec_role(request):
    return (getattr(request.state, "session", None) or {}).get("role") or ""


@router.delete("/write/tasks/{record_id}")
def write_task_delete(record_id: str, request: Request):
    """组长：删除事项（写回飞书 + 触发同步；不可恢复）。"""
    _require_leader(request)
    queued = _gateway(writes.delete, "tasks", record_id, by=_who(request))
    return _write_response(request, "portal_task_delete", record_id, "", queued)


@router.delete("/write/projects/{record_id}")
def write_project_delete(record_id: str, request: Request):
    """组长：删除项目；仍有事项关联时拒绝（残留关联不静默，先处理事项）。"""
    _require_leader(request)
    linked = 0
    try:
        snap = store.current()
        for t in (snap.get("tables", {}).get("tasks") or []):
            if t.get("project_id") == record_id and not t.get("_invalid"):
                linked += 1
    except Exception:
        pass                          # 快照读取失败不拦删除（fail-open，飞书侧为准）
    if linked:
        raise HTTPException(
            422, f"该项目仍被 {linked} 条事项关联；请先处理这些事项（改关联或删除）再删除项目")
    queued = _gateway(writes.delete, "projects", record_id, by=_who(request))
    return _write_response(request, "portal_project_delete", record_id, "", queued)


@router.post("/write/schedule")
def write_schedule_create(body: dict, request: Request):
    """组长：新建公共日程（写回飞书 + 触发同步）。"""
    _require_leader(request)
    cols = _schedule_columns(body)
    rid, queued = _gateway(writes.create, "schedule", cols, by=_who(request))
    return _write_response(request, "portal_schedule_create", rid, cols.get("日程名称"), queued)


@router.delete("/write/schedule/{record_id}")
def write_schedule_delete(record_id: str, request: Request):
    """组长：删除公共日程（不可恢复）。"""
    _require_leader(request)
    queued = _gateway(writes.delete, "schedule", record_id, by=_who(request))
    return _write_response(request, "portal_schedule_delete", record_id, "", queued)


# ---- 物资采购写入：全员可建可改（组长批复列除外，仅组长可写） ----

def _f_bool(v, label):
    """复选框：接受布尔或空串（空 = 不勾选）。"""
    if v in (None, ""):
        return False
    if isinstance(v, bool):
        return v
    if v in (0, 1, "0", "1", "true", "false", "是", "否"):
        return v in (1, "1", "true", "是")
    raise HTTPException(422, f"{label} 需为布尔值")


def _f_link(v, label):
    """购物链接：宽松文本，缺协议时补 https://（用户常裸粘域名）。"""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if not s:
        return None
    if len(s) > 2000:
        raise HTTPException(422, f"{label} 超长（≤2000 字）")
    if "://" not in s:
        s = "https://" + s
    return s


# 采购日期按北京时间解释（飞书日期列存的是毫秒时间戳）
_TZ_CN = _dt.timezone(_dt.timedelta(hours=8))


def _f_select(v, label, max_len=40):
    """单选列：空串/None → 清空（飞书写 null 即清空该列）；超长拒绝。"""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if not s:
        return None
    if len(s) > max_len:
        raise HTTPException(422, f"{label} 超长（≤{max_len} 字）")
    return s


def _f_number(v, label, min_value=0.0):
    """数字列：接受数值或数字字符串；空串/None → 清空。"""
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        raise HTTPException(422, f"{label} 需为数字")
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label} 需为数字")
    if n != n or n in (float("inf"), float("-inf")):
        raise HTTPException(422, f"{label} 需为有限数字")
    if n < min_value:
        raise HTTPException(422, f"{label} 不能小于 {min_value:g}")
    if n > 1e12:
        raise HTTPException(422, f"{label} 数值过大")
    return int(n) if n.is_integer() else round(n, 2)


def _f_date_ms(v, label):
    """日期列：接受 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM（按北京时间）→ 毫秒时间戳。

    空串/None → 清空（传 null）。"""
    if v in (None, ""):
        return None
    s = str(v).strip()
    fmt = "%Y-%m-%dT%H:%M" if "T" in s else "%Y-%m-%d"
    try:
        dt = _dt.datetime.strptime(s, fmt).replace(tzinfo=_TZ_CN)
    except ValueError:
        raise HTTPException(422, f"{label} 需为 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM")
    return int(dt.timestamp() * 1000)


def _f_people(v, label):
    """人员列：open_id 字符串或数组 → [{"id": open_id}]；空 → 清空。"""
    if v in (None, ""):
        return None
    ids = [v] if isinstance(v, str) else list(v)
    clean = [str(x).strip() for x in ids if str(x or "").strip()]
    if not clean:
        return None
    if len(clean) > 20:
        raise HTTPException(422, f"{label} 人员过多（≤20）")
    return [{"id": x} for x in clean]


def _procurement_next_code(season, category1):
    """自动采购编号：赛季-分类缩写-四位流水（如 2027-BZJ-0001）。

    流水号取飞书实时数据里同前缀的最大号 +1：读实时而不读快照，是为了不吃 60s
    同步滞后（快照口径下连开两条必撞号）。代价是每次新建整表读一次；并发新建仍有
    极小概率撞号（飞书侧无唯一约束），可人工改号修正。读不到飞书（网络/权限异常）
    时返回 None——宁可不填编号，也不写重复编号。
    """
    if not season or not category1:
        return None
    year = str(season).replace("赛季", "").strip()
    prefix = "%s-%s-" % (year, _PROC_CODE_PREFIX.get(category1, "QT"))
    best = 0
    try:
        cfg = sync._load_sync_cfg()
        tcfg = cfg["tables"].get("procurement") or {}
        if not tcfg.get("table_id"):
            return None
        codes = [normalize._plain((row.get("fields") or {}).get("采购编号"))
                 for row in sync.FeishuReader(cfg).records(tcfg["table_id"])]
        # 本地队列里还没推到飞书的编号（缓存库第 3 步）：不避开就会连开两条撞号
        codes += cache_store.pending_values("procurement", "采购编号")
        for code in codes:
            if isinstance(code, str) and code.startswith(prefix):
                tail = code[len(prefix):]
                if tail.isdigit():
                    best = max(best, int(tail))
    except Exception:
        return None
    return "%s%04d" % (prefix, best + 1)


def _procurement_columns(body, *, editing=False):
    """语义键 → 物资采购表列名与值；editing=部分更新（无字段的键跳过）。

    新增（2026-10-12）：赛季/一二级分类/采购编号（单选或文本）、采购日期（日期）、
    单价/数量（数字）、采购人（人员）、发票已开（复选）。「总价（元）」是飞书
    公式列（＝单价×数量），门户永不写入，只在只读视图里展示。
    采购编号的自动取号见 _procurement_fill_code（新建与编辑都会补）。
    """
    cols = {}
    if "name" in body or not editing:
        v = _f_text(body.get("name"), "物资名称", required=not editing, max_len=120)
        if v is not None:
            cols["采购物资名称"] = v
    if "groups" in body:
        v = _f_groups(body.get("groups"), "申请组别", False)
        if v is not None:
            cols["申请组别"] = v
    if "link" in body:
        v = _f_link(body.get("link"), "购物链接")
        if v is not None:
            cols["购物链接"] = v
    if "purpose" in body:
        v = _f_text(body.get("purpose"), "用途说明", False, 2000)
        if v is not None:
            cols["用途说明"] = v
    for key, col in (("season", "赛季"), ("category1", "一级分类"),
                     ("category2", "二级分类"), ("code", "采购编号")):
        if key in body:
            cols[col] = _f_select(body.get(key), col, 60)
    if "purchase_date" in body:
        cols["采购日期"] = _f_date_ms(body.get("purchase_date"), "采购日期")
    for key, col in (("unit_price", "单价（元）"), ("quantity", "数量")):
        if key in body:
            cols[col] = _f_number(body.get(key), col)
    if "purchaser" in body:
        cols["采购人"] = _f_people(body.get("purchaser"), "采购人")
    for key, col, label in (("leader_approval", "组长批复", "组长批复"),
                            ("purchased", "已采购", "已采购"),
                            ("invoice_done", "发票已开", "发票已开")):
        if key in body:
            cols[col] = _f_bool(body.get(key), label)
    if not cols:
        raise HTTPException(422, "没有可写入的字段")
    return cols


_PROC_CODE_PENDING = "；未填赛季或一级分类，暂不生成采购编号，补上后自动生成"
_PROC_CODE_FAILED = "；读取飞书现有编号失败，采购编号暂未生成，下次修改这条记录时自动补上"


def _procurement_fill_code(cols, current=None):
    """采购编号为空时按 赛季+一级分类 自动取号（就地写进 cols），返回要附在提示后的说明。

    新建传 current=None；编辑传快照里的记录。规则：手填了编号不动；记录已有编号、
    这次也没改编号不动；否则赛季与一级分类（这次改的值优先，没改取记录现值）都有
    才取号——此前的缺陷是只在新建时取号，先建后补分类的记录永远没有编号。
    说明只在新建、或这次改了赛季 / 一级分类时给，免得勾个「已采购」也弹一遍；取到号时说明里带上编号。
    """
    if cols.get("采购编号"):
        return ""
    cur = current or {}
    if cur.get("code") and "采购编号" not in cols:
        return ""
    season = cols["赛季"] if "赛季" in cols else cur.get("season")
    cat1 = cols["一级分类"] if "一级分类" in cols else cur.get("category1")
    tell = current is None or "赛季" in cols or "一级分类" in cols
    if not season or not cat1:
        return _PROC_CODE_PENDING if tell else ""
    auto = _procurement_next_code(season, cat1)
    if not auto:
        return _PROC_CODE_FAILED
    cols["采购编号"] = auto
    return "；采购编号 " + auto


# 一级分类 → 采购编号里的分类缩写（自动取号用；未知分类退化为 QT）
_PROC_CODE_PREFIX = {"电机": "DJ", "标准件": "BZJ", "加工件": "JGJ",
                     "电子元器件": "DZ", "线材接插件": "XC",
                     "工具耗材": "GJ", "其他": "QT"}

# 非组长可写的字段集合：组长批复是唯一受限列。2026-10-12 起金额/发票类字段
# 也全员可写（维护者明确选择）——财务口径由「组长批复」拦一道，不另设金额专属权限。
_MEMBER_PROCUREMENT_FIELDS = {"name", "groups", "link", "purpose", "purchased",
                              "season", "category1", "category2", "code",
                              "purchase_date", "unit_price", "quantity",
                              "purchaser", "invoice_done"}


def _procurement_gate(request, keys):
    """物资采购写前检：member 角色 + 同源；非组长只放行白名单字段
    （即组长批复之外的全部列——该列是唯一的受限字段）。"""
    _check_same_origin(request)
    if rec_role(request) != "member":
        raise HTTPException(403, "仅成员账号可操作物资采购")
    open_id, groups = leader_ctx(request)
    if not groups:
        illegal = keys - _MEMBER_PROCUREMENT_FIELDS
        if illegal:
            raise HTTPException(403, "「组长批复」仅组长可修改")
    return open_id, groups


@router.post("/write/procurement")
def write_procurement_create(body: dict, request: Request):
    """新建采购申请：全员可建（组长批复列仅组长能在新建时勾选）。"""
    _procurement_gate(request, set(body.keys()))
    cols = _procurement_columns(body)
    note = _procurement_fill_code(cols)
    rid, queued = _gateway(writes.create, "procurement", cols, by=_who(request))
    res = _write_response(request, "portal_procurement_create", rid,
                          cols.get("采购物资名称"), queued)
    res["hint"] += note
    res["code"] = cols.get("采购编号")
    return res


@router.patch("/write/procurement/{record_id}")
def write_procurement_update(record_id: str, body: dict, request: Request):
    """修改采购记录：全员可改（除组长批复仅组长，支持勾选与取消）。"""
    _procurement_gate(request, set(body.keys()))
    cols = _procurement_columns(body, editing=True)
    current = _snapshot_record("procurement", record_id)
    note = _procurement_fill_code(cols, current) if current is not None else ""
    queued = _gateway(writes.update, "procurement", record_id, cols, by=_who(request))
    res = _write_response(request, "portal_procurement_update", record_id,
                          cols.get("采购物资名称"), queued)
    res["hint"] += note
    if cols.get("采购编号"):
        res["code"] = cols["采购编号"]
    return res


@router.delete("/write/procurement/{record_id}")
def write_procurement_delete(record_id: str, request: Request):
    """删除采购记录：仅组长（整行删除属破坏性操作，与事项/日程口径一致）。"""
    _require_leader(request)
    queued = _gateway(writes.delete, "procurement", record_id, by=_who(request))
    return _write_response(request, "portal_procurement_delete", record_id, "", queued)


# ---- 附件直传：上传到 Base → 并入既有附件整列写回（图片补充 / 发票文件） ----

# 图片列上限 10MB；发票列放宽到 20MB（飞书 upload_all 接口本身硬上限 20MB）
_PROC_ATTACH_MAX = int(os.environ.get("PROCURE_ATTACH_MAX_MB") or "10") * 1024 * 1024
_PROC_INVOICE_MAX = int(os.environ.get("PROCURE_INVOICE_MAX_MB") or "20") * 1024 * 1024
_PROC_IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
# 附件列白名单：column 查询参数 → (飞书列名, 允许扩展名, 大小上限, 提示语)
_PROC_ATTACH_COLUMNS = {
    "image": ("图片补充", _PROC_IMAGE_TYPES, _PROC_ATTACH_MAX,
              "仅支持图片文件（png/jpg/jpeg/gif/webp/bmp）"),
    "invoice": ("发票文件", _PROC_IMAGE_TYPES + (".pdf",), _PROC_INVOICE_MAX,
                "仅支持发票 PDF 或图片（pdf/png/jpg/jpeg/gif/webp/bmp）"),
}


def _procurement_live_fields(record_id):
    """直读飞书采购记录当前字段（附件并入要用最新 token，不吃快照滞后）；
    记录不存在返回 None，读取失败照常抛 SyncError（不把网络问题说成"记录不存在"）。"""
    rec = portal_writer.FeishuWriter().get("procurement", record_id)
    return (rec.get("fields") or {}) if rec else None


def _procurement_upload_and_attach(data, filename, content_type, record_id, column):
    """阻塞部分（在线程池跑）：上传文件 + 并入既有附件写回记录。

    column 是 _PROC_ATTACH_COLUMNS 的键（image=图片补充 / invoice=发票文件）。
    """
    col_name = _PROC_ATTACH_COLUMNS[column][0]
    # 新建排在本地队列里时拿到的是临时 ID：附件必须挂在飞书记录上，等推送线程推完这条新建
    remote_id = writes.resolve_remote(record_id, timeout=15)
    if remote_id is None:
        raise HTTPException(409, "该记录还在推送到飞书，请稍后在详情页上传附件")
    record_id = remote_id
    w = portal_writer.FeishuWriter()
    token = w.upload_attachment(data, filename, content_type)
    fields = _procurement_live_fields(record_id)
    if fields is None:
        raise SyncError("api", "采购记录不存在（或已从源表移除）")
    cur = [a for a in (fields.get(col_name) or [])
           if isinstance(a, dict) and a.get("file_token")]
    # 重复上传同一文件：飞书会复用 token，去重后放到最后
    cur = [a for a in cur if a["file_token"] != token]
    cols = {col_name: [{"file_token": a["file_token"]} for a in cur] + [{"file_token": token}]}
    w.update("procurement", record_id, cols)
    # 镜像按读形状写回（带文件名）：本地视图规范化附件要用文件名，只有 token 会被当成空
    writes._quietly(cache_store.patch_mirror, "procurement", record_id,
                    {col_name: cur + [{"name": filename, "file_token": token}]})
    writes.changed(False)
    return token


@router.post("/write/procurement/{record_id}/attachments")
async def write_procurement_upload(record_id: str, request: Request,
                                   file: UploadFile = File(...), column: str = "image"):
    """上传附件到采购记录：全员可传（同其余字段不设限）。

    column=image（默认，图片补充，仅图片，≤10MB）/ column=invoice（发票文件，
    PDF 或图片，≤20MB）。先上传文件拿 file_token，再与该记录既有附件合并整列
    写回——避免覆盖掉别人刚传的文件。按扩展名 + content-type 双口径校验。
    """
    import asyncio
    _procurement_gate(request, set())
    col = _PROC_ATTACH_COLUMNS.get(column)
    if col is None:
        await file.close()
        raise HTTPException(422, "附件列只支持 image（图片补充）或 invoice（发票文件）")
    col_name, exts, limit, hint = col
    name = (file.filename or "").strip() or (
        "upload.pdf" if column == "invoice" else "upload.png")
    if not name.lower().endswith(exts):
        await file.close()
        raise HTTPException(422, hint)
    ctype = (file.content_type or "").lower()
    if ctype and not (ctype.startswith("image/") or
                      ctype == "application/octet-stream" or
                      (column == "invoice" and ctype == "application/pdf")):
        await file.close()
        raise HTTPException(422, f"文件类型不符：{ctype}")
    data = await file.read()
    await file.close()
    if not data:
        raise HTTPException(422, "上传内容为空")
    if len(data) > limit:
        raise HTTPException(422, f"{col_name}超过 {limit // 1024 // 1024}MB 上限")
    try:
        token = await asyncio.to_thread(
            _procurement_upload_and_attach, data, name, file.content_type,
            record_id, column)
    except _SyncError as e:
        raise _write_error(e)
    accepted, _ = sync.scheduler().request_manual(
        "portal-write:" + (getattr(request.state, "session", {}) or {}).get("display", "?"))
    _audit("portal_procurement_upload", record=record_id, name=name, column=column,
           by=(getattr(request.state, "session", {}) or {}).get("user", "?"),
           sync_triggered=accepted)
    return {"ok": True, "record_id": record_id, "file_name": name,
            "column": column, "column_name": col_name,
            "file_token": token,
            "sync_triggered": accepted,
            "hint": "已上传并附加到该记录"}


# ================= 考勤打卡（签到/签退；网段门控 + 写回飞书） =================

import ipaddress as _ipaddress                     # noqa: E402

# 允许打卡的客户端网段（逗号分隔 CIDR）；默认仅工作室网段——
# 校园网（10.x）/外网经 DDNS 进来的来源地址不在其中，一律 403。
ATTENDANCE_CIDRS = [c.strip() for c in
                    (os.environ.get("ATTENDANCE_CIDRS") or "192.168.0.0/16").split(",")
                    if c.strip()]
try:
    _ATTEND_NETS = [_ipaddress.ip_network(c) for c in ATTENDANCE_CIDRS]
except ValueError as _e:
    _ATTEND_NETS = [_ipaddress.ip_network("192.168.0.0/16")]

_MIN_SESSION_SEC = int(os.environ.get("ATTENDANCE_MIN_SESSION") or "60")  # 短于该值的签退视为误触
_ANOMALY_SESSION_SEC = 16 * 3600   # 超过 16 小时的会话计入异常


def _attendance_ip_ok(request):
    host = (request.client.host if request.client else "") or ""
    try:
        addr = _ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _ATTEND_NETS)


def _my_member_record(open_id):
    """当前用户在成员数据表中的记录（快照）；无则返回 None。"""
    try:
        snap = store.current()
    except Exception:
        return None
    for m in (snap.get("tables", {}).get("members") or []):
        if ((m.get("member") or {}).get("id") == open_id
                and not m.get("_invalid")):
            return m
    return None


def _feishu_member_fields(record_id):
    """直读飞书成员记录当前字段（跨零点作废前的复核）；失败返回 None。"""
    try:
        rec = portal_writer.FeishuWriter().get("members", record_id)
        return (rec.get("fields") or {}) if rec else None
    except Exception:
        return None


def _attendance_gate(request):
    """通用前检：member 角色 + 网段；返回 open_id。"""
    _check_same_origin(request)
    rec = getattr(request.state, "session", None) or {}
    if rec.get("role") != "member":
        raise HTTPException(403, "考勤打卡仅限成员账号")
    open_id = (rec.get("extra") or {}).get("open_id") or ""
    if not open_id:
        raise HTTPException(400, "会话缺少身份，请重新登录")
    if not _attendance_ip_ok(request):
        raise HTTPException(403, "打卡仅限工作室网段（192.168.x.x）——"
                                "校园网/外网不可打卡")
    return open_id, (rec.get("display") or "")


# 进程内实时打卡态：open_id → 签到时刻，或 None（已签出）。
# 键存在即为权威（写飞书成功即更新）；键不存在（如刚重启）才回退快照判定。
# 为什么需要：写回飞书后快照要等同步（且有全局限频），秒级内重复签到/
# 签退/查状态若读快照会拿到旧数据（曾因此出现重复签到与重复结算）。
_ATTEND_LIVE = {}


def _attendance_cached():
    """考勤写入是否走本地队列（cache 模式且 members 在走队列的表里）。"""
    return writes.queued("members")


def _ledger_state(m, open_id):
    """本地考勤明细给出的状态：(是否签到中, 签到时刻)。

    明细里没有该成员、或快照里有更晚的签到（直写模式或飞书侧产生的）时返回 None，
    交给快照判定。签到时刻与飞书一样按毫秒取整，同一次签到两边相差不超过 1 秒。
    """
    s = cache_store.latest_session(open_id)
    if not s:
        return None
    snap_ci = ((m or {}).get("last_check_in") or {}).get("ts") or 0
    if snap_ci > s["check_in_at"] + 1:
        return None
    on = s["state"] == "open"
    return on, (s["check_in_at"] if on else None)


def _attendance_live(members):
    """考勤页"签到中"的实时态：cache 模式取本地明细，direct 模式取进程内实时态。"""
    if not _attendance_cached():
        return dict(_ATTEND_LIVE)
    out = {}
    try:
        for m in members:
            oid = (m.get("member") or {}).get("id")
            st = _ledger_state(m, oid) if oid else None
            if st is not None:
                out[oid] = st[1]
    except Exception:
        return {}
    return out


def _attendance_state(m, open_id=""):
    if open_id and _attendance_cached():
        st = _ledger_state(m, open_id)
        if st is not None:
            return st[0], st[1], None
    elif open_id and open_id in _ATTEND_LIVE:
        ts = _ATTEND_LIVE[open_id]
        return (ts is not None), (ts if ts else None), None
    ci = (m or {}).get("last_check_in") or {}
    co = (m or {}).get("last_check_out") or {}
    checked_in = bool(ci.get("ts")) and (not co.get("ts") or co["ts"] < ci["ts"])
    return checked_in, ci.get("ts"), co.get("ts")

# ---------- 周结算（2026-10-20 起，口径见 app/portal/attendance.py） ----------

def _members_fields_cfg(cfg=None):
    cfg = cfg or sync._load_sync_cfg()
    return ((cfg["tables"].get("members") or {}).get("fields") or {})


def _week_cols_ready(cfg=None, read_only=False):
    """周结算列是否已在飞书建好并回填到配置（field_id 非空）。

    写入（换周 / 签退）要求门户写的三列都映射；页面展示另外要求本周时长公式列已映射。"""
    fm = _members_fields_cfg(cfg)
    keys = attendance.WEEK_WRITE_KEYS + (("week_hours",) if read_only else ())
    return all(fm.get(k) for k in keys)


def _leave_ready(cfg=None):
    """「请假周」列是否已在飞书建好并回填到配置。"""
    return bool(_members_fields_cfg(cfg).get("leave_week"))


def _duty_ready(cfg=None):
    cfg = cfg or sync._load_sync_cfg()
    return bool((cfg["tables"].get("duty") or {}).get("table_id"))


def weekly_rollover_all(reason="换周"):
    """把「结算周」不是本周的成员行换到本周（写周初时长 / 上周时长 / 结算周）。

    幂等：已换到本周的行跳过，任何时刻重复执行都安全。周结算列未建时什么也不做。
    读取失败或有行写入失败时抛异常（由零点任务 10 分钟后重试），已成功的行不会重复写。"""
    if _attendance_cached():
        return _weekly_rollover_cached(reason)
    cfg = sync._load_sync_cfg()
    tcfg = cfg["tables"].get("members") or {}
    tid = tcfg.get("table_id", "")
    if not tid:
        raise RuntimeError("members 表未映射")
    if not _week_cols_ready(cfg):
        return []
    reader = sync.FeishuReader(cfg)
    rows = sync.FeishuReader.translate_keys(
        reader.records(tid), reader.fields(tid), tcfg.get("fields") or {})
    _, _, this_key, last_key = attendance.week_bounds()
    done, failed, w = [], 0, None
    for row in rows:
        f = row.get("fields") or {}
        if not normalize._users(f.get("member")):
            continue                          # 用户保留的空行
        upd = attendance.rollover_updates(
            normalize._num(f.get("work_hours")), normalize._num(f.get("week_base")),
            normalize._plain(f.get("week_key")), this_key, last_key)
        if upd is None:
            continue
        try:
            if w is None:
                w = portal_writer.FeishuWriter()
            w.update("members", row["record_id"], upd)
        except Exception:
            failed += 1
            continue
        done.append(row["record_id"])
        _audit("attendance_week_rollover", record=row["record_id"], week=this_key,
               last_week=upd[attendance.COL_LAST_WEEK], reason=reason)
    if done:
        try:
            sync.scheduler().request_manual("attendance-week")
        except Exception:
            pass
    if failed:
        raise RuntimeError("换周写入失败 %d 行（已成功 %d 行）" % (failed, len(done)))
    return done


# ---------- 零点孤儿会话自动作废 ----------
# 策略（维护者确认）：每天 00:00（Asia/Shanghai）把"有签到且无更晚签退"
# 的会话作废——清签到、同步说明留痕；工时丢弃（忘记签退
# 付出真实代价，杜绝"故意不签退白拿工时"）。只处理签到时间早于今日零点
# 的会话：当日刚签到的正常会话永不受影响，服务重启补跑也因此安全。
# 服务零点不在线时，启动补跑一次（上次执行日期落在昨天及以前即触发）。

import threading as _threading
from datetime import datetime as _datetime
from zoneinfo import ZoneInfo as _ZoneInfo

_TZ_CN = _ZoneInfo("Asia/Shanghai")
_MIDNIGHT_META = "attendance_midnight_last_run"
_midnight_stop = _threading.Event()


def _today_start_ts():
    now = _datetime.now(_TZ_CN)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def midnight_clear_orphans(reason="零点自动作废"):
    """清掉所有签到时间早于今日零点的孤儿会话；返回清理的 record 列表。

    扫描失败直接抛异常（由调用方决定重试）——绝不能在失败时静默返回空：
    曾导致零点任务把失败的空跑记为"已执行"，孤儿会话漏过整晚。"""
    if _attendance_cached():
        return _midnight_clear_cached(reason)
    import time as _time
    cutoff = _today_start_ts()
    cleared = []
    cfg = sync._load_sync_cfg()
    tid = (cfg["tables"].get("members") or {}).get("table_id", "")
    if not tid:
        raise RuntimeError("members 表未映射")
    rows = sync.FeishuReader(cfg).records(tid)
    w = None
    for row in rows:
        f = row.get("fields") or {}
        ci = f.get("最近签到")
        co = f.get("最近签退")
        if not isinstance(ci, (int, float)):
            continue
        if isinstance(co, (int, float)) and co >= ci:
            continue
        if ci / 1000.0 >= cutoff:            # 今日新签到：不是孤儿
            continue
        try:
            if w is None:
                w = portal_writer.FeishuWriter()
            w.update("members", row["record_id"], {
                "最近签到": None,
                "同步时间": int(_time.time() * 1000),
                "同步说明": "门户考勤：" + reason + "（未签退，工时不计）",
            })
        except Exception:
            continue                          # 单条失败不拖垮整轮
        cleared.append(row["record_id"])
        _audit("attendance_midnight_clear", record=row["record_id"],
               check_in_ms=ci)
        # 进程内实时态同步复位（匹配该记录的成员）
        people = f.get("文本") or []
        if people and people[0].get("id"):
            _ATTEND_LIVE[people[0]["id"]] = None
    if cleared:
        try:
            sync.scheduler().request_manual("attendance-midnight")
        except Exception:
            pass
    return cleared


# 零点任务在 cache 模式下按镜像判断：镜像太旧就不判断，交给 10 分钟后的重试
_MIRROR_MAX_AGE_SEC = 900
_MEMBER_COL = "文本"          # 成员数据表的人员列（飞书列名）


def _members_local_views():
    """cache 模式：成员表每行的本地视图（镜像 ⊕ 未落地操作）；镜像过旧时抛异常等重试。"""
    age = cache_store.mirror_age()
    if age is None or age > _MIRROR_MAX_AGE_SEC:
        raise RuntimeError("飞书镜像超过 %d 秒未刷新，暂不判断（稍后重试）" % _MIRROR_MAX_AGE_SEC)
    out = []
    for rid, _ in cache_store.list_remote("members"):
        try:
            view = cache_store.local_view("members", rid)
        except cache_store.RecordUnknown:
            continue
        if view:
            out.append((rid, view))
    return out


def _weekly_rollover_cached(reason):
    """cache 模式换周：每行入队一条 $rollover 指令，推送时按飞书当前值结算（幂等）。"""
    _, _, this_key, last_key = attendance.week_bounds()
    if not _week_cols_ready():
        return []
    done = []
    for rid, f in _members_local_views():
        if not normalize._users(f.get(_MEMBER_COL)):
            continue                          # 用户保留的空行
        if normalize._plain(f.get(attendance.COL_WEEK_KEY)) == this_key:
            continue
        cache_store.enqueue_update("members", rid,
                                   {"$rollover": {"this": this_key, "last": last_key}},
                                   by="system:" + reason)
        done.append(rid)
        _audit("attendance_week_rollover", record=rid, week=this_key, reason=reason,
               queued=True)
    if done:
        writes.changed(True)
    return done


def _midnight_clear_cached(reason):
    """cache 模式零点作废：按本地视图找孤儿签到，明细作废与"清最近签到"同一事务入队。"""
    import time as _time
    cutoff = _today_start_ts()
    now = _time.time()
    now_ms = int(now * 1000)
    cleared = []
    for rid, f in _members_local_views():
        ci, co = f.get("最近签到"), f.get("最近签退")
        if not isinstance(ci, (int, float)):
            continue
        if isinstance(co, (int, float)) and co >= ci:
            continue
        if ci / 1000.0 >= cutoff:
            continue
        sets = {"最近签到": None, "同步时间": now_ms,
                "同步说明": "门户考勤：" + reason + "（未签退，工时不计）"}
        push = {"table": "members", "record_id": rid, "sets": sets, "by": "system:" + reason}
        people = normalize._users(f.get(_MEMBER_COL))
        oid = people[0]["id"] if people else None
        if not (oid and cache_store.void_session(oid, now, note=reason, push=push)):
            cache_store.enqueue_update("members", rid, sets, by="system:" + reason)
        cleared.append(rid)
        _audit("attendance_midnight_clear", record=rid, check_in_ms=ci, queued=True)
    # 明细里还开着、但镜像里对不上的旧签到（如成员行刚新建还没推上去）：只作废明细
    for sess in cache_store.open_sessions(before=cutoff):
        cache_store.void_session(sess["open_id"], now, note=reason + "（仅本地明细）")
    if cleared:
        writes.changed(True)
    return cleared


_WEEK_META = "attendance_week_last_run"


def _retry_until_done(log, label, fn):
    """失败（网络抖动 / 飞书限流）每 10 分钟重试直至成功；成功才记已执行，绝不把失败的空跑记为完成。"""
    while not _midnight_stop.is_set():
        try:
            fn()
            return
        except Exception as e:
            log.warning("%s失败（10 分钟后重试）: %s", label, str(e)[:120])
            _midnight_stop.wait(600)


def _nightly_jobs(log):
    """每天 00:00：作废孤儿会话；跨入新的一周时再换周（两项各自记账，互不重跑）。"""
    today = _datetime.now(_TZ_CN).strftime("%Y-%m-%d")
    if store.get_meta(_MIDNIGHT_META, "") != today:
        cleared = midnight_clear_orphans()
        store.set_meta(_MIDNIGHT_META, today)
        log.info("考勤零点作废完成：%d 个孤儿会话", len(cleared))
    week_key = attendance.week_bounds()[2]
    if store.get_meta(_WEEK_META, "") != week_key:
        done = weekly_rollover_all()
        store.set_meta(_WEEK_META, week_key)
        log.info("考勤换周完成（%s 起）：%d 行", week_key, len(done))


def _catch_up_jobs(log):
    """启动补跑：本机此前执行过、且最后一次早于今天 / 本周时才补（服务零点不在线的情形）。

    从未执行过的新库（含测试环境）不在启动时访问飞书；首次换周由建列脚本初始化，
    之后由零点任务与签退时的逐行换周兜底。"""
    today = _datetime.now(_TZ_CN).strftime("%Y-%m-%d")
    last = store.get_meta(_MIDNIGHT_META, "")
    if last and last != today:
        cleared = midnight_clear_orphans(reason="补跑零点作废")
        store.set_meta(_MIDNIGHT_META, today)
        if cleared:
            log.info("考勤补跑作废 %d 个孤儿会话", len(cleared))
    week_key = attendance.week_bounds()[2]
    wlast = store.get_meta(_WEEK_META, "")
    if wlast and wlast != week_key:
        done = weekly_rollover_all(reason="补跑换周")
        store.set_meta(_WEEK_META, week_key)
        log.info("考勤补跑换周（%s 起）：%d 行", week_key, len(done))


def _midnight_loop():
    log = __import__("logging").getLogger("app.portal.attendance")
    _retry_until_done(log, "考勤启动补跑", lambda: _catch_up_jobs(log))
    import datetime as _dt
    while not _midnight_stop.is_set():
        now = _datetime.now(_TZ_CN)
        nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + _dt.timedelta(days=1)
        _midnight_stop.wait(max(1.0, nxt.timestamp() - now.timestamp()))
        if _midnight_stop.is_set():
            return
        _retry_until_done(log, "考勤零点任务", lambda: _nightly_jobs(log))


def start_midnight_clearer():
    t = _threading.Thread(target=_midnight_loop, name="attendance-midnight",
                          daemon=True)
    t.start()
    return t



@router.get("/attendance/status")
def attendance_status(request: Request):
    """当前用户打卡状态与累计工时（排行数据随快照走 overview）。"""
    rec = getattr(request.state, "session", None) or {}
    if rec.get("role") != "member":
        return {"checked_in": False, "work_hours": None, "ip_ok": False}
    open_id = (rec.get("extra") or {}).get("open_id") or ""
    m = _my_member_record(open_id) if open_id else None
    checked_in, ci_ts, _ = _attendance_state(m, open_id)
    return {"checked_in": checked_in,
            "since": ci_ts if checked_in else None,
            "work_hours": (m or {}).get("work_hours"),
            "ip_ok": _attendance_ip_ok(request)}


@router.post("/attendance/leave")
def attendance_leave(body: dict, request: Request):
    """组长给成员设 / 取消本周请假（body: {"record_id": 成员数据表记录, "on": true/false}）。

    请假那周没达标、当天没签到都显示「请假」，下周自动恢复（见 attendance.py 口径）。"""
    open_id, _ = _require_leader(request)
    if not _leave_ready():
        raise HTTPException(503, "飞书成员数据表还没建「请假周」列"
                                 "（管理员运行 scripts/feishu_attendance_setup.py --write-config）")
    rid = str(body.get("record_id") or "").strip()
    on = body.get("on")
    if not rid or not isinstance(on, bool):
        raise HTTPException(422, "需要 record_id 与 on（true 设为请假 / false 取消）")
    snap = store.current() or {}
    rid = _resolve_local(rid)
    m = next((x for x in (snap.get("tables", {}).get("members") or []) if x.get("id") == rid), None)
    if m is None or not m.get("member"):
        raise HTTPException(404, "成员数据表里没有这条记录")
    _, _, this_key, last_key = attendance.week_bounds()
    value = attendance.leave_value(m.get("leave_weeks"), on, this_key, last_key)
    queued = _gateway(writes.update, "members", rid, {attendance.COL_LEAVE: value},
                      by=_who(request))
    name = (m.get("member") or {}).get("name") or ""
    _audit("attendance_leave", record=rid, member=name, on=on, week=this_key,
           by=_who(request), queued=queued)
    return {"ok": True, "record_id": rid, "on": on, "week": this_key, "queued": queued,
            "hint": ("已标记%s本周请假" if on else "已取消%s本周请假") % (name and "「%s」" % name)}


@router.post("/attendance/check-in")
def attendance_check_in(request: Request):
    """签到：写最近签到/同步信息；已处于签到状态则拒绝。"""
    import time as _time
    open_id, display = _attendance_gate(request)
    m = _my_member_record(open_id)
    checked_in, _, _ = _attendance_state(m, open_id)
    if checked_in:
        raise HTTPException(409, "当前已处于签到状态，请先签退")
    now_ms = int(_time.time() * 1000)
    cols = {"最近签到": now_ms, "同步时间": now_ms,
            "同步说明": "门户签到"}
    if _attendance_cached():
        return _check_in_cached(request, open_id, m, now_ms, cols)
    try:
        w = portal_writer.FeishuWriter()
        rid = (m or {}).get("id")
        if rid:
            w.update("members", rid, cols)
            writes._quietly(cache_store.patch_mirror, "members", rid, cols)
        else:
            extra = (attendance.new_member_week_cols(attendance.week_bounds(now_ms / 1000)[2])
                     if _week_cols_ready() else {})
            new_cols = {"文本": [{"id": open_id}], **cols, **extra}
            rid = w.create("members", new_cols)
            writes._quietly(cache_store.put_mirror, "members", rid, new_cols)
    except _SyncError as e:
        raise _write_error(e)
    writes.changed(False)
    _ATTEND_LIVE[open_id] = now_ms / 1000.0
    _audit("attendance_check_in", record=rid, by=open_id, ip=request.client.host)
    accepted, _ = sync.scheduler().request_manual("attendance:" + display)
    return {"ok": True, "record_id": rid, "checked_in_at": now_ms / 1000,
            "sync_triggered": accepted}


@router.post("/attendance/check-out")
def attendance_check_out(request: Request):
    """签退：结算本次会话时长累加到有效时长（小时）。"""
    import time as _time
    open_id, display = _attendance_gate(request)
    m = _my_member_record(open_id)
    if _attendance_cached():
        return _check_out_cached(request, open_id, m)
    checked_in, ci_ts, _ = _attendance_state(m, open_id)
    if not m and not checked_in:
        raise HTTPException(409, "尚无签到记录，无法签退")
    if not checked_in:
        raise HTTPException(409, "当前未处于签到状态")
    now = _time.time()
    # 跨零点防线：签到时间早于今日零点 = 零点作废漏网的孤儿会话
    #（守护任务停机/失败期间产生）。按策略作废：清签到、异常 +1、
    # 不计工时——绝不能把跨夜时长当成有效工时结算。
    if ci_ts < _today_start_ts():
        # 复核飞书实时记录：快照/实时态在服务重启或同步滞后时可能给出过期
        # 的"在签"假象（曾把已闭合的会话误判为跨零点孤儿）。只有飞书记录
        # 本身确认"有签到且无更晚签退"才作废——正常会话绝不受影响。
        live_fields = _feishu_member_fields(m["id"])
        if live_fields is not None:
            fci = live_fields.get("最近签到")
            fco = live_fields.get("最近签退")
            if not isinstance(fci, (int, float)) or (
                    isinstance(fco, (int, float)) and fco >= fci):
                _ATTEND_LIVE[open_id] = None   # 假象纠正：实为已闭合
                raise HTTPException(409, "当前未处于签到状态")
        void_cols = {"最近签到": None, "同步时间": int(now * 1000),
                     "同步说明": "门户考勤：跨零点签退按未签退作废（工时不计）"}
        try:
            portal_writer.FeishuWriter().update("members", m["id"], void_cols)
        except _SyncError as e:
            raise _write_error(e)
        writes._quietly(cache_store.patch_mirror, "members", m["id"], void_cols)
        writes.changed(False)
        _ATTEND_LIVE[open_id] = None
        _audit("attendance_cross_midnight_void", record=m["id"], by=open_id)
        raise HTTPException(409, "该次签到已跨零点（当晚未签退），按规则作废不计工时；"
                                "如仍在工作请重新签到")
    session_sec = now - ci_ts
    if session_sec < _MIN_SESSION_SEC:
        raise HTTPException(422, "签到不足 1 分钟，稍后再签退（防误触）")
    session_h = session_sec / 3600.0
    total = round((m.get("work_hours") or 0) + session_h, 2)
    anomalous = session_sec > _ANOMALY_SESSION_SEC
    cols = {"最近签退": int(now * 1000),
            "有效时长（小时）": total,
            "同步时间": int(now * 1000)}
    cols["同步说明"] = ("门户签退 +%.2fh（会话超 16 小时，请核对）" if anomalous
                else "门户签退 +%.2fh") % session_h
    # 换周兜底：零点换周失败或尚未执行时，本人这一行在签退的同一次写入里先换到本周。
    # 起点取本次签退前的累计时长，本次会话（签到在今天零点之后）完整计入本周。
    if _week_cols_ready():
        _, _, this_key, last_key = attendance.week_bounds(now)
        roll = attendance.rollover_updates(m.get("work_hours"), m.get("week_base"),
                                           m.get("week_key"), this_key, last_key)
        if roll:
            cols.update(roll)
    try:
        portal_writer.FeishuWriter().update("members", m["id"], cols)
    except _SyncError as e:
        raise _write_error(e)
    writes._quietly(cache_store.patch_mirror, "members", m["id"], cols)
    writes.changed(False)
    _ATTEND_LIVE[open_id] = None
    _audit("attendance_check_out", record=m["id"], by=open_id,
           hours=round(session_h, 2), total=total, ip=request.client.host)
    accepted, _ = sync.scheduler().request_manual("attendance:" + display)
    return {"ok": True, "session_hours": round(session_h, 2),
            "work_hours": total, "anomalous": anomalous,
            "sync_triggered": accepted}


# ---------- cache 模式的签到 / 签退（缓存库第 3 步） ----------
# 本地考勤明细是权威：签到/签退先落明细，飞书写入在同一事务里入队，由推送线程推送。
# 签退时长按增量推（飞书当前累计值 + 本次时长），换周结算列推送时按飞书当前值计算，
# 飞书上有人手改时长也不会被覆盖。

def _check_in_cached(request, open_id, m, now_ms, cols):
    at = now_ms / 1000.0
    rid = (m or {}).get("id")
    try:
        if rid:
            cache_store.start_session(open_id, rid, at, push={
                "table": "members", "record_id": rid, "sets": cols, "by": open_id})
        else:
            extra = (attendance.new_member_week_cols(attendance.week_bounds(at)[2])
                     if _week_cols_ready() else {})
            rid = cache_store.enqueue_create(
                "members", {_MEMBER_COL: [{"id": open_id}], **cols, **extra}, by=open_id)
            cache_store.start_session(open_id, rid, at)
    except cache_store.AttendanceStateError:
        raise HTTPException(409, "当前已处于签到状态，请先签退")
    except cache_store.RecordUnknown:
        raise HTTPException(409, "成员记录不在本地数据中，请稍后再试")
    writes.changed(True)
    _audit("attendance_check_in", record=rid, by=open_id, ip=request.client.host, queued=True)
    return {"ok": True, "record_id": rid, "checked_in_at": at, "queued": True,
            "sync_triggered": False}


def _check_out_cached(request, open_id, m):
    import time as _time
    checked_in, ci_ts, _ = _attendance_state(m, open_id)
    if not checked_in:
        raise HTTPException(409, "当前未处于签到状态" if (m or cache_store.latest_session(open_id))
                            else "尚无签到记录，无法签退")
    now = _time.time()
    now_ms = int(now * 1000)
    s = cache_store.open_session(open_id)
    from_ledger = s is not None
    if s is None:
        # 签到发生在切换到 cache 模式之前（或飞书侧直接改的）：先把它接管进明细
        if not (m or {}).get("id"):
            raise HTTPException(409, "成员记录不在本地数据中，请稍后再试")
        s = cache_store.start_session(open_id, m["id"], ci_ts)
    rid = s["member_record_id"]
    if ci_ts < _today_start_ts():
        if not from_ledger:
            # 接管来的签到要复核飞书实时记录（与直写口径一致，防快照滞后造成误作废）
            live_fields = _feishu_member_fields(rid)
            if live_fields is not None:
                fci, fco = live_fields.get("最近签到"), live_fields.get("最近签退")
                if not isinstance(fci, (int, float)) or (
                        isinstance(fco, (int, float)) and fco >= fci):
                    cache_store.void_session(open_id, now, note="接管时发现飞书侧已闭合")
                    raise HTTPException(409, "当前未处于签到状态")
        cache_store.void_session(open_id, now, note="跨零点未签退，作废", push={
            "table": "members", "record_id": rid, "by": open_id,
            "sets": {"最近签到": None, "同步时间": now_ms,
                     "同步说明": "门户考勤：跨零点签退按未签退作废（工时不计）"}})
        writes.changed(True)
        _audit("attendance_cross_midnight_void", record=rid, by=open_id, queued=True)
        raise HTTPException(409, "该次签到已跨零点（当晚未签退），按规则作废不计工时；"
                                "如仍在工作请重新签到")
    session_sec = now - ci_ts
    if session_sec < _MIN_SESSION_SEC:
        raise HTTPException(422, "签到不足 1 分钟，稍后再签退（防误触）")
    anomalous = session_sec > _ANOMALY_SESSION_SEC
    session_h = session_sec / 3600.0
    sets = {"最近签退": now_ms, "同步时间": now_ms,
            "同步说明": ("门户签退 +%.2fh（会话超 16 小时，请核对）" if anomalous
                     else "门户签退 +%.2fh") % session_h}
    if _week_cols_ready():
        _, _, this_key, last_key = attendance.week_bounds(now)
        sets["$rollover"] = {"this": this_key, "last": last_key}
    try:
        closed = cache_store.close_session(open_id, now, push={
            "table": "members", "record_id": rid, "sets": sets, "by": open_id,
            "increments": {attendance.COL_WORK: "{hours}"}})
    except cache_store.AttendanceStateError:
        raise HTTPException(409, "当前未处于签到状态")
    except cache_store.RecordUnknown:
        raise HTTPException(409, "成员记录不在本地数据中，请稍后再试")
    writes.changed(True)
    hours = closed["hours"]
    _audit("attendance_check_out", record=rid, by=open_id, hours=round(hours, 2),
           ip=request.client.host, queued=True)
    est = round(((m or {}).get("work_hours") or 0) + hours, 2)
    return {"ok": True, "session_hours": round(hours, 2), "work_hours": est,
            "anomalous": anomalous, "queued": True, "sync_triggered": False}
