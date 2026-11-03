# -*- coding: utf-8 -*-
"""MCP 只读工具层（2026-11）。

每个工具返回**纯 JSON 可序列化**的 dict，统一带 `snapshot_version` / `stale`，
让 agent 知道"读到的是哪一轮同步"。数据来源只有快照库与既有视图组装函数，
不新增任何数据通路——门户页面看到的与 agent 读到的是同一份。
"""
import time

from ..portal import attendance, cache_store, compute, store

MAX_PAGE = 200


class ToolError(Exception):
    """工具层可预期的失败（参数不合法 / 快照未就绪），由 MCP 端点转成错误响应。"""

    def __init__(self, message, code=-32602):
        super().__init__(message)
        self.code = code


def _snapshot():
    snap = store.current()
    if not snap:
        raise ToolError("快照尚未生成（首次同步未完成），请稍后再试", -32010)
    return snap


def _envelope(snap):
    st = store.status()
    return {"snapshot_version": snap.get("version"),
            "last_success_at": st.get("last_success_at"),
            "stale": st.get("stale"),
            "server_time": time.time()}


def _page(args):
    try:
        limit = int(args.get("limit") or 50)
        offset = int(args.get("offset") or 0)
    except (TypeError, ValueError):
        raise ToolError("limit/offset 必须是整数") from None
    if limit < 1:
        limit = 1
    if limit > MAX_PAGE:
        limit = MAX_PAGE
    if offset < 0:
        offset = 0
    return limit, offset


def _slice(rows, args):
    limit, offset = _page(args)
    return rows[offset:offset + limit], {"returned": len(rows[offset:offset + limit]),
                                         "total": len(rows), "limit": limit,
                                         "offset": offset}


def _match(value, want):
    return not want or value == want


# ---------------- 工具实现 ----------------

def meta(agent, args):
    """门户元数据：分组、人员、常量选项。"""
    from ..portal import api as portal_api
    snap = _snapshot()
    members = []
    for m in snap["tables"].get("members", []):
        if m.get("_invalid") or not m.get("member"):
            continue
        members.append({"open_id": (m.get("member") or {}).get("id"),
                        "name": (m.get("member") or {}).get("name")})
    return {**_envelope(snap),
            "groups": list(portal_api._GROUPS),
            "members": members,
            "procurement_options": portal_api._procurement_options(
                snap["tables"].get("procurement", []))}


def overview(agent, args):
    """总览：倒计时、五组紧急事项、近期日程、打卡工时排行。"""
    from ..portal import api as portal_api
    snap = _snapshot()
    data = compute.overview(snap)
    members = snap["tables"].get("members", [])
    live = portal_api._attendance_live(members)
    _, _, today_start = attendance.today_info()
    data["kpi"]["checked_in"] = sum(
        1 for m in members
        if not m.get("_invalid") and m.get("member")
        and attendance.is_checked_in(m, today_start, live)[0])
    return {**_envelope(snap), "data": data}


def list_projects(agent, args):
    """项目列表：含进度、主责组、计划起止。"""
    from ..portal import api as portal_api
    snap = _snapshot()
    tasks = snap["tables"].get("tasks", [])
    want_group = (args.get("group") or "").strip()
    include_archived = bool(args.get("include_archived"))
    rows = []
    for p in snap["tables"].get("projects", []):
        if not include_archived and p.get("status") == "已归档":
            continue
        if want_group and p.get("owner_group") != want_group:
            continue
        rows.append(portal_api._project_view(p, tasks))
    rows.sort(key=lambda x: (0 if x.get("status") != "已归档" else 1,
                             x.get("name") or ""))
    data, page = _slice(rows, args)
    return {**_envelope(snap), "data": data, "page": page}


def get_project(agent, args):
    """项目详情：本条项目 + 其下事项 + 里程碑。"""
    from ..portal import api as portal_api
    snap = _snapshot()
    pid = (args.get("project_id") or "").strip()
    if not pid:
        raise ToolError("缺少 project_id")
    project = next((p for p in snap["tables"].get("projects", [])
                    if p.get("id") == pid), None)
    if not project:
        raise ToolError("项目不存在：%s" % pid, -32004)
    tasks = [t for t in snap["tables"].get("tasks", [])
             if t.get("project_id") == pid or pid in (t.get("project_ids") or [])]
    milestones = [m for m in snap["tables"].get("milestones", [])
                  if m.get("project_id") == pid]
    return {**_envelope(snap),
            "data": {"project": portal_api._project_view(project, tasks),
                     "tasks": [portal_api._task_view(t) for t in tasks],
                     "milestones": milestones}}


def list_tasks(agent, args):
    """事项列表：可按组/状态/优先级/项目/关键字过滤。"""
    from ..portal import api as portal_api
    snap = _snapshot()
    names = {p["id"]: p["name"] for p in snap["tables"].get("projects", [])}
    q = (args.get("q") or "").strip().lower()
    project_id = (args.get("project_id") or "").strip()
    rows = []
    for t in snap["tables"].get("tasks", []):
        if not _match(t.get("owner_group"), (args.get("group") or "").strip()):
            continue
        if not _match(t.get("status"), (args.get("status") or "").strip()):
            continue
        if not _match(t.get("priority"), (args.get("priority") or "").strip()):
            continue
        if project_id and project_id not in [t.get("project_id")] + (t.get("project_ids") or []):
            continue
        if q and q not in (t.get("name") or "").lower():
            continue
        rows.append(portal_api._task_view(t, names))
    data, page = _slice(rows, args)
    return {**_envelope(snap), "data": data, "page": page}


def get_task(agent, args):
    """事项详情。"""
    from ..portal import api as portal_api
    snap = _snapshot()
    tid = (args.get("task_id") or "").strip()
    if not tid:
        raise ToolError("缺少 task_id")
    task = next((t for t in snap["tables"].get("tasks", [])
                 if t.get("id") == tid), None)
    if not task:
        raise ToolError("事项不存在：%s" % tid, -32004)
    names = {p["id"]: p["name"] for p in snap["tables"].get("projects", [])}
    return {**_envelope(snap), "data": portal_api._task_view(task, names)}


def list_schedule(agent, args):
    """日程列表：默认未来 14 天，可按天数或组别收窄。"""
    from ..portal import api as portal_api
    snap = _snapshot()
    try:
        days = int(args.get("days") or 14)
    except (TypeError, ValueError):
        raise ToolError("days 必须是整数") from None
    want_group = (args.get("group") or "").strip()
    horizon = time.time() + days * 86400
    rows = []
    for s in snap["tables"].get("schedule", []):
        start = (s.get("start") or {}).get("ts")
        end = (s.get("end") or {}).get("ts")
        if start is None:
            continue
        if end is not None:
            if end < time.time():
                continue
        elif start > horizon:
            continue
        if start > horizon:
            continue
        if want_group and want_group not in (s.get("groups") or []):
            continue
        rows.append(portal_api._schedule_view(s))
    rows.sort(key=lambda x: (x.get("start") or {}).get("ts") or 0)
    data, page = _slice(rows, args)
    return {**_envelope(snap), "data": data, "page": page}


def list_procurement(agent, args):
    """采购记录列表。"""
    from ..portal import api as portal_api
    snap = _snapshot()
    rows = []
    for p in snap["tables"].get("procurement", []):
        if not _match(p.get("season"), (args.get("season") or "").strip()):
            continue
        if not _match(p.get("category1"), (args.get("category1") or "").strip()):
            continue
        if args.get("unpurchased") and p.get("purchased"):
            continue
        rows.append(portal_api._procurement_view(p))
    rows.sort(key=lambda x: (x.get("purchase_date") or {}, x.get("id") or ""))
    data, page = _slice(rows, args)
    return {**_envelope(snap), "data": data, "page": page}


def get_procurement(agent, args):
    """采购记录详情。"""
    from ..portal import api as portal_api
    snap = _snapshot()
    rid = (args.get("record_id") or args.get("id") or "").strip()
    if not rid:
        raise ToolError("缺少 record_id")
    rec = next((p for p in snap["tables"].get("procurement", [])
                if p.get("id") == rid), None)
    if not rec:
        raise ToolError("采购记录不存在：%s" % rid, -32004)
    return {**_envelope(snap), "data": portal_api._procurement_view(rec)}


def quality(agent, args):
    """数据质量：被排除的记录与原因（不含成员姓名）。"""
    snap = _snapshot()
    rows = sorted(snap.get("quality", []),
                  key=lambda x: (x.get("table") or "", x.get("record_id") or ""))
    by_table = {}
    for q in rows:
        by_table[q.get("table")] = by_table.get(q.get("table"), 0) + 1
    data, page = _slice(rows, args)
    return {**_envelope(snap), "data": data, "by_table": by_table, "page": page}


def my_attendance(agent, args):
    """本人考勤：本周时长与达标、最近签到/签退、是否签到中。"""
    snap = _snapshot()
    open_id = (agent or {}).get("open_id") or ""
    if not open_id:
        raise ToolError("令牌未绑定成员身份", -32003)
    mine = next((m for m in snap["tables"].get("members", [])
                 if (m.get("member") or {}).get("id") == open_id), None)
    if not mine:
        raise ToolError("花名册里找不到该成员", -32004)
    session = cache_store.latest_session(open_id)
    _, _, today_start = attendance.today_info()
    live = {open_id: (session["check_in_at"] if session and session["state"] == "open"
                      else None)} if session else None
    on, since = attendance.is_checked_in(mine, today_start, live)
    return {**_envelope(snap),
            "data": {"open_id": open_id,
                     "name": (mine.get("member") or {}).get("name"),
                     "work_hours": mine.get("work_hours"),
                     "week_base": mine.get("week_base"),
                     "week_key": mine.get("week_key"),
                     "last_check_in": mine.get("last_check_in"),
                     "last_check_out": mine.get("last_check_out"),
                     "checked_in": bool(on),
                     "checked_in_since": since,
                     "session": session}}


def sync_status(agent, args):
    """同步状态：最近一轮、积压队列、冲突与失败数。"""
    from ..portal import sync
    st = store.status()
    return {"server_time": time.time(),
            "snapshot_version": st.get("snapshot_version"),
            "last_success_at": st.get("last_success_at"),
            "last_attempt_at": st.get("last_attempt_at"),
            "last_duration_sec": st.get("last_duration_sec"),
            "stale": st.get("stale"),
            "counts": st.get("counts"),
            "last_error": st.get("last_error"),
            "cache": cache_store.backlog(),
            "next_run_in_sec": sync.scheduler().next_run_in(),
            "fail_streak": sync.scheduler().fail_streak()}


# 工具注册表：name → {handler, scopes, description, inputSchema}
TOOLS = {
    "portal_meta": {
        "handler": meta, "scopes": ("meta.read",),
        "description": "门户元数据：五组顺序、成员 open_id→姓名、采购选项。先调它拿到成员与分组再查别的。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    "portal_overview": {
        "handler": overview, "scopes": ("meta.read",),
        "description": "总览：倒计时、各组紧急事项、未来 7 天日程、打卡工时排行。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    "portal_list_projects": {
        "handler": list_projects, "scopes": ("projects.read",),
        "description": "项目列表（默认不含已归档），含进度百分比与主责组。",
        "inputSchema": {"type": "object", "properties": {
            "group": {"type": "string", "description": "按主责组过滤"},
            "include_archived": {"type": "boolean", "description": "是否包含已归档项目"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
            "offset": {"type": "integer", "minimum": 0}}}},
    "portal_get_project": {
        "handler": get_project, "scopes": ("projects.read",),
        "description": "单个项目详情：项目本身 + 其下事项 + 里程碑。",
        "inputSchema": {"type": "object", "properties": {
            "project_id": {"type": "string"}}, "required": ["project_id"]}},
    "portal_list_tasks": {
        "handler": list_tasks, "scopes": ("tasks.read",),
        "description": "事项列表，可按组、状态、优先级、项目、名称关键字过滤。",
        "inputSchema": {"type": "object", "properties": {
            "group": {"type": "string"}, "status": {"type": "string"},
            "priority": {"type": "string"}, "project_id": {"type": "string"},
            "q": {"type": "string", "description": "名称包含的关键字"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
            "offset": {"type": "integer", "minimum": 0}}}},
    "portal_get_task": {
        "handler": get_task, "scopes": ("tasks.read",),
        "description": "单个事项详情（含逾期/临期判定与飞书记录链接）。",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]}},
    "portal_list_schedule": {
        "handler": list_schedule, "scopes": ("schedule.read",),
        "description": "日程列表：默认未来 14 天（含正在进行的）。",
        "inputSchema": {"type": "object", "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "group": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
            "offset": {"type": "integer", "minimum": 0}}}},
    "portal_list_procurement": {
        "handler": list_procurement, "scopes": ("procurement.read",),
        "description": "采购记录列表，可按赛季/一级分类过滤或只看未采购项。",
        "inputSchema": {"type": "object", "properties": {
            "season": {"type": "string"}, "category1": {"type": "string"},
            "unpurchased": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
            "offset": {"type": "integer", "minimum": 0}}}},
    "portal_get_procurement": {
        "handler": get_procurement, "scopes": ("procurement.read",),
        "description": "单条采购记录详情（含金额、审批、发票与附件链接）。",
        "inputSchema": {"type": "object", "properties": {
            "record_id": {"type": "string"}}, "required": ["record_id"]}},
    "portal_quality": {
        "handler": quality, "scopes": ("quality.read",),
        "description": "数据质量报告：被排除的记录与原因。",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
            "offset": {"type": "integer", "minimum": 0}}}},
    "portal_my_attendance": {
        "handler": my_attendance, "scopes": ("attendance.read",),
        "description": "令牌绑定成员的考勤：本周时长、是否签到中、最近签到签退。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    "portal_sync_status": {
        "handler": sync_status, "scopes": ("sync.read",),
        "description": "同步状态：最近一轮耗时、积压队列、失败与冲突数。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
}


def call(name: str, agent: dict, args: dict):
    """按名调用工具；调用前已由端点完成 scope 校验。"""
    meta_info = TOOLS.get(name)
    if not meta_info:
        raise ToolError("未知工具：%s" % name, -32601)
    if not isinstance(args, dict):
        raise ToolError("arguments 必须是对象")
    return meta_info["handler"](agent, args)
