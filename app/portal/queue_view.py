"""同步队列与冲突的页面数据（缓存库第 5 步，「数据质量」页的推送与冲突模块用）。

把队列与冲突表里的原始值（飞书列名、读写形状、毫秒时间戳、open_id）整理成
页面能直接显示的文字；只读，不改队列状态。处理动作（重试 / 放弃 / 确认冲突）在 api.py。
"""
import json
from datetime import datetime

from . import cache_rules as rules
from . import cache_store
from .compute import TZ

# 各表的名称列：记录不在快照里时（例如新建推送失败）从操作字段里取名称
NAME_COLS = {"tasks": "事项名称", "projects": "项目名称", "schedule": "日程名称",
             "procurement": "采购物资名称"}
OP_TEXT = {"create": "新建", "update": "修改", "delete": "删除"}
CONFLICT_TEXT = {"field": "两边都改了同一列", "remote_deleted": "飞书上已删除",
                 "delete_blocked": "飞书上已修改，删除未执行"}
DIRECTIVE_TEXT = {"$rollover": "换周结算"}
LIMIT = 100


def name_index(snap):
    """快照里出现过的 id → 名称：记录 id → 记录名，人员 open_id → 姓名。"""
    names = {}
    for rows in ((snap or {}).get("tables") or {}).values():
        for r in rows:
            if r.get("id") and r.get("name"):
                names.setdefault(r["id"], r["name"])
            for v in r.values():
                if isinstance(v, list):
                    for u in v:
                        if isinstance(u, dict) and u.get("id") and u.get("name"):
                            names.setdefault(u["id"], u["name"])
    return names


def display_value(v, names=None, limit=120):
    """飞书字段值 → 一行文字（冲突对比用）。"""
    names = names or {}

    def one(x):
        if x is None or x == "" or x == []:
            return ""
        if isinstance(x, bool):
            return "是" if x else "否"
        if isinstance(x, (int, float)):
            # 飞书日期列是毫秒时间戳（2001–2286 年）；其余数值原样
            if 1e12 <= x < 1e13:
                return datetime.fromtimestamp(x / 1000, TZ).strftime("%Y-%m-%d %H:%M")
            return ("%.2f" % x).rstrip("0").rstrip(".") if isinstance(x, float) else str(x)
        if isinstance(x, str):
            return names.get(x, x) if (x.startswith("rec") or rules.is_local_id(x)) else x
        if isinstance(x, list):
            return "、".join(s for s in (one(y) for y in x) if s)
        if isinstance(x, dict):
            if isinstance(x.get("record_ids"), list):
                return one(x["record_ids"])
            if x.get("name"):
                return str(x["name"])
            if x.get("text"):
                return str(x["text"])
            if x.get("id"):
                return names.get(x["id"], "…" + str(x["id"])[-4:])
            return json.dumps(x, ensure_ascii=False)
        return str(x)

    text = one(v) or "（空）"
    return text if len(text) <= limit else text[:limit] + "…"


def user_label(user, names):
    """操作人（会话里的 feishu:<open_id> / emergency:<名字>）→ 显示名。"""
    user = str(user or "")
    if user.startswith("feishu:"):
        oid = user[len("feishu:"):]
        return names.get(oid, "…" + oid[-4:])
    if user.startswith("emergency:"):
        return user[len("emergency:"):] + "（应急账号）"
    return user or "—"


def _record_name(table, rid, fields, names):
    if names.get(rid):
        return names[rid]
    col = NAME_COLS.get(table)
    if col and (fields or {}).get(col):
        return display_value(fields[col], names, limit=60)
    return ""


def _columns(op):
    cols = []
    for k in list(op.get("fields") or {}) + list(op.get("increments") or {}):
        k = DIRECTIVE_TEXT.get(k, k)
        if k not in cols:
            cols.append(k)
    return cols


def op_item(op, names):
    rid = cache_store.resolve_id(op["record_id"])
    return {"id": op["id"], "table": op["table_name"], "record_id": rid,
            "op": op["op"], "op_text": OP_TEXT.get(op["op"], op["op"]),
            "state": op["state"],
            "name": _record_name(op["table_name"], rid, op.get("fields"), names),
            "columns": _columns(op), "error": op.get("last_error") or "",
            "attempts": op.get("attempts") or 0,
            "created_by": user_label(op.get("created_by"), names),
            "created_at": op.get("created_at")}


def conflict_entry(c, names):
    kind = c.get("kind")
    if kind == "remote_deleted":
        # 整条修改作废：local 是本地要写的各列
        local = "；".join("%s：%s" % (k, display_value(v, names, 40))
                         for k, v in (c.get("local_value") or {}).items()) or "（空）"
        remote = "（飞书上已删除）"
    else:
        local = display_value(c.get("local_value"), names)
        remote = display_value(c.get("remote_value"), names)
    return {"id": c["id"], "table": c["table_name"], "record_id": c["record_id"],
            "name": names.get(c["record_id"], ""),
            "kind": kind, "kind_text": CONFLICT_TEXT.get(kind, kind),
            "field": c.get("field") or "", "local": local, "remote": remote,
            "detected_at": c.get("detected_at")}


def page(snap, cache_status, can_manage):
    """「数据质量」页推送与冲突模块的数据。"""
    names = name_index(snap)
    failed = cache_store.recent_ops(LIMIT, states=("failed",))
    pending = cache_store.recent_ops(LIMIT, states=("pending", "pushing"))
    conflicts = cache_store.list_conflicts(open_only=True, limit=LIMIT)
    return {"cache": cache_status, "can_manage": bool(can_manage),
            "failed": [op_item(o, names) for o in failed],
            "pending": [op_item(o, names) for o in reversed(pending)],
            "conflicts": [conflict_entry(c, names) for c in conflicts]}
