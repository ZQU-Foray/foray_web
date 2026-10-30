"""本地缓存库与飞书双向同步的冲突规则（纯函数，不碰数据库与网络）。

规则（2026-10-21 与维护者确认，详见 docs/portal-cache-design.md）：

1. 字段级三方合并：网页改了哪些列就只推哪些列。推送前取飞书当前值 remote，
   与编辑当时的基准 base、本地新值 local 逐列比较：
   - remote 与 local 已一致 → 不用推；
   - remote 仍等于 base（飞书没动过这一列）→ 推 local；
   - 两边都改了且不一致 → 冲突，以飞书为准（不推这一列），记一条冲突给组长看。
2. 飞书侧已删除的记录：本地对它的修改作废，记冲突。
3. 本地删除：推送前飞书侧被关注的列有改动 → 不删（飞书为准），记冲突。
4. 考勤时长按增量记：推送时取飞书当前累计值再加本次增量，永不整数覆盖。
5. 新建记录先用本地临时 ID（loc_ 开头），推到飞书拿到 record_id 后再对应上。
6. 门户自己维护的日志类列（如成员表的同步时间/同步说明，成员同步脚本也会写）
   不做冲突判断，直接覆盖（force）——它们只记"最后一次谁做了什么"。
7. 以 $ 开头的键是推送时才计算的指令（如 $rollover 换周结算），不写进飞书、
   不参与冲突判断，由推送线程按飞书当前值求值。

值比较前先归一（canon）：同一列在读接口与写接口里的形状不同
（人员读回带姓名、关联读回包一层 record_ids、数字读回是字符串等），
归一后只保留"身份"部分再比较。
"""
import json
import math
import re
import uuid
from dataclasses import dataclass, field

LOCAL_PREFIX = "loc_"
DIRECTIVE_PREFIX = "$"

# 纯数字文本（含负号与小数）：数字列读回是字符串，写入是数值，归一后才能相等
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
# 列表元素里出现这些键就不是文本分段
_IDENTITY_KEYS = ("id", "file_token", "record_ids", "link_record_ids")


def new_local_id():
    """本地临时 ID：loc_ + 20 位十六进制（飞书 record_id 以 rec 开头，不会撞）。"""
    return LOCAL_PREFIX + uuid.uuid4().hex[:20]


def is_local_id(record_id):
    return isinstance(record_id, str) and record_id.startswith(LOCAL_PREFIX)


def is_directive(col):
    return isinstance(col, str) and col.startswith(DIRECTIVE_PREFIX)


def split_directives(fields):
    """拆出指令：返回 (普通列, 指令)。"""
    plain, directives = {}, {}
    for k, v in (fields or {}).items():
        (directives if is_directive(k) else plain)[k] = v
    return plain, directives


def _num_key(f):
    # 保留 6 位小数，消除 0.1+0.2 一类浮点噪声
    return ("num", round(float(f), 6))


def _list_key(items):
    """列表 → 排好序的身份元组（顺序不算改动：多选、人员、附件都按集合比较）。"""
    if all(isinstance(x, dict) and "text" in x
           and not any(k in x for k in _IDENTITY_KEYS) for x in items):
        # 文本分段（公式文本、富文本）：拼成纯文本
        return canon("".join(str(x.get("text") or "") for x in items))
    keys = []
    for x in items:
        if isinstance(x, dict):
            rids = x.get("record_ids")
            if rids is None:
                rids = x.get("link_record_ids")
            if rids is not None:
                # 关联读形状 [{record_ids: [...]}] 与写形状 ["rec..."] 归到同一种
                keys += [("str", str(r)) for r in rids]
            elif x.get("id"):
                keys.append(("id", str(x["id"])))
            elif x.get("file_token"):
                keys.append(("file", str(x["file_token"])))
            elif x.get("link"):
                keys.append(("str", str(x["link"]).strip()))
            else:
                keys.append(("obj", json.dumps(x, sort_keys=True, ensure_ascii=False)))
        elif isinstance(x, bool):
            keys.append(("bool", "1" if x else "0"))
        elif isinstance(x, (int, float)):
            keys.append(("num", repr(round(float(x), 6))))
        elif x is not None:
            s = str(x).strip()
            if s:
                keys.append(("str", s))
    return tuple(sorted(keys)) or None


def canon(v):
    """把飞书读/写两种形状的值归一成可直接 == 比较的形式。

    空值（None、空串、空列表、空对象、未勾选的复选框）统一为 None——飞书列表接口
    对空列直接不返回该键，写入时却可能显式写 null/False。

    已知取舍：数字列与文本列无法区分，故形如 "1.0" 与 "1" 的纯数字文本视为相等。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return ("bool", True) if v else None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return ("str", repr(v))
        return _num_key(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if _NUM_RE.match(s):
            return _num_key(s)
        return s
    if isinstance(v, list):
        return _list_key(v) if v else None
    if isinstance(v, dict):
        if not v:
            return None
        if "link" in v:                       # 超链接列 {link, text}
            return canon(v.get("link"))
        return _list_key([v])
    return str(v)


def same(a, b):
    return canon(a) == canon(b)


def to_number(v):
    """飞书数值读回可能是数值、数字字符串或文本分段；取不到按 0。"""
    if isinstance(v, bool) or v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        v = "".join(str(x.get("text") or "") for x in v if isinstance(x, dict))
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if (math.isnan(f) or math.isinf(f)) else f


# 增量列统一保留 4 位小数：与考勤明细（attendance.HOURS_DIGITS）同口径，
# 避免"合并两次增量"与"分两次推送"得到不同结果（审计 P2-1）
INCREMENT_DIGITS = 4


def apply_increment(current, delta, ndigits=INCREMENT_DIGITS):
    """增量列：飞书当前值 + 本次增量（时长类列保留 4 位小数）。"""
    return round(to_number(current) + float(delta), ndigits)


def apply_ops(fields, ops, directive_fn=None):
    """在一份字段上依次叠加尚未落到飞书的操作，得到"本地视图"。

    ops 为 [{"op", "fields", "increments"}]（按入队顺序）；delete 得到 None。
    directive_fn(指令, 当前视图) → 要写的列：给了就按推送时的同一套算法求值指令，
    顺序与推送一致——先写普通列、再求值指令、最后加增量（指令看到的是增量之前的值）。
    用途：给新的编辑取基准，以及生成页面看到的本地视图。
    """
    view = dict(fields or {})
    for op in ops:
        if op.get("op") == "delete":
            return None
        if op.get("op") == "create":
            view = {}
        plain, directives = split_directives(op.get("fields"))
        view.update(plain)
        if directives and directive_fn:
            view.update(directive_fn(directives, view))
        for col, delta in (op.get("increments") or {}).items():
            view[col] = apply_increment(view.get(col), delta)
    return view


@dataclass
class UpdatePlan:
    """一次修改推送前的合并结果。"""
    push: dict = field(default_factory=dict)        # 要写给飞书的列（写形状）
    conflicts: list = field(default_factory=list)   # 冲突明细，见 conflict_item
    unchanged: list = field(default_factory=list)   # 飞书已经是本地值，无需推的列
    remote_missing: bool = False                    # 飞书侧记录已不存在


@dataclass
class DeletePlan:
    """一次删除推送前的判断：delete 推删除 / noop 飞书已删 / blocked 飞书已改不删。"""
    action: str
    conflicts: list = field(default_factory=list)


def conflict_item(kind, field_name=None, base=None, local=None, remote=None):
    """冲突明细：kind 为 field（同列两边都改）/ remote_deleted（飞书已删）/
    delete_blocked（飞书已改，删除未执行）。解决方式统一为飞书优先。"""
    return {"kind": kind, "field": field_name, "base": base,
            "local": local, "remote": remote}


def plan_update(base, sets, remote_fields, increments=None,
                ndigits=INCREMENT_DIGITS, force=()):
    """按规则 1/2/4/6 计算一次修改该推什么（指令键忽略，由调用方另行求值）。

    base：编辑当时这些列的值（列名 → 值，缺列视为空）；sets：本地要写的新值；
    remote_fields：推送前从飞书取到的当前字段，记录已删除时传 None；
    increments：按增量推的数值列（列 → 增量），不参与冲突判断；
    force：直接覆盖、不做冲突判断的列。
    """
    plan = UpdatePlan()
    sets = split_directives(sets)[0]
    if remote_fields is None:
        plan.remote_missing = True
        plan.conflicts.append(conflict_item("remote_deleted", local=dict(sets)))
        return plan
    base = base or {}
    for col, local in sets.items():
        b, r = base.get(col), remote_fields.get(col)
        if same(r, local):
            plan.unchanged.append(col)
        elif col in force or same(r, b):
            plan.push[col] = local
        else:
            plan.conflicts.append(conflict_item("field", col, b, local, r))
    for col, delta in (increments or {}).items():
        r = remote_fields.get(col)
        b = base.get(col) if isinstance(base, dict) else None
        if isinstance(base, dict) and col in base and not same(r, b):
            # 飞书当前值已不等于入队时的基准：上一次推送很可能已经成功（HTTP 超时、
            # 进程在回写前被杀），再叠加一次就是重复结算。这里不再推增量，改记冲突，
            # 由组长核对后手工修正（审计 P1-1：增量推送必须幂等）。
            plan.conflicts.append(conflict_item("increment_base_drift", col, b, delta, r))
            continue
        plan.push[col] = apply_increment(r, delta, ndigits)
    return plan


def plan_delete(base, remote_fields):
    """按规则 3 判断删除：只比较 base 里列出的列（调用方传"门户可写的列"，
    避免公式列随时间自己变化造成误判）。"""
    if remote_fields is None:
        return DeletePlan("noop")
    changed = [conflict_item("delete_blocked", col, b, None, remote_fields.get(col))
               for col, b in (base or {}).items()
               if not same(b, remote_fields.get(col))]
    return DeletePlan("blocked", changed) if changed else DeletePlan("delete")
