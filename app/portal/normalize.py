"""飞书七表规范化：field_id 映射 → 语义模型 + 数据质量报告。

值形状契约 = 阶段 1 实证（docs/feishu-setup.md §2.2）：
文本=字符串、单选=选项名、多选=数组、日期=毫秒时间戳、人员=[{id,name,...}]、
关联=[{table_id, record_ids, ...}]。

补充实证（2026-09-12 实读）：公式文本字段返回 [{text,type}] 而非字符串；
数字字段返回字符串（"0"）——分别由 _plain() 与 _num() 归一。

质量规则（§5.2）：未知枚举/失效关联/结束早于开始/有截止无精度 →
计入 quality 并从统计排除（不静默当正常数据）。
"""
import time

from . import attendance

GROUPS = ["机械", "硬件", "电控", "算法", "运营"]
PRIORITIES = ["P0", "P1", "P2", "P3"]
TASK_STATUSES = ["未开始", "进行中", "受阻", "已完成", "已取消"]
PROJECT_STATUSES = ["筹备中", "进行中", "已结束", "已归档"]
OPEN_STATUSES = ("未开始", "进行中", "受阻")

# 物资采购明细字段（2026-10-12 新增）：下面的分类/赛季清单只作门户下拉的兜底提示，
# 飞书里增删选项后门户会跟随快照里实际出现的值（api.py 的 options 字段做并集），
# 故这里不做白名单校验，避免把维护者新加的选项误报成数据质量异常。
PROC_SEASONS = ["2026赛季", "2027赛季", "2028赛季"]
PROC_CATEGORY1 = ["电机", "标准件", "加工件", "电子元器件", "线材接插件", "工具耗材", "其他"]
PROC_CATEGORY2 = [
    "直流电机", "无刷电机", "舵机", "电机驱动", "编码器", "减速箱",
    "螺丝", "螺母", "垫片垫圈", "轴承", "销与键", "弹簧", "卡簧", "铆钉",
    "板材", "型材", "管材", "轴类", "法兰", "支架", "3D打印件", "定制机加",
    "主控板", "电源模块", "传感器", "电阻电容", "连接器", "芯片", "显示屏",
    "电源线", "信号线", "端子", "插头插座", "线束",
    "螺丝刀", "扳手", "钻头", "胶粘剂", "扎带", "砂纸", "焊锡",
    "其他",
]


def _text(v):
    return v if isinstance(v, str) and v.strip() else None


def _plain(v):
    """普通文本或公式文本：公式实测返回 [{text,type}]，统一取纯文本。"""
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list):
        joined = "".join(x.get("text", "") for x in v if isinstance(x, dict))
        return joined.strip() or None
    return None


def _num(v):
    """数字字段：飞书实测返回字符串（"0"），统一强转为数值；非法值 → None。"""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _formula_num(v):
    """公式数值列：飞书可能返回数值、数字字符串或 [{text,type}]，统一转为数值。"""
    if isinstance(v, list):
        v = _plain(v)
    return _num(v)


def _req_enum(v, allowed, label):
    """必填单选：返回 (值, issue 或 None)。缺值/未知值都记为质量异常。"""
    if v is None or v == "":
        return None, f"缺{label}"
    if isinstance(v, str) and v in allowed:
        return v, None
    return v, f"{label}未知"


def _opt_enum(v, allowed, label):
    """可空单选：空值合法；非空但未知 → issue。"""
    if v is None or v == "":
        return None, None
    if isinstance(v, str) and v in allowed:
        return v, None
    return v, f"{label}未知"


def _enum_list(v, allowed):
    if not isinstance(v, list):
        return [], True
    return v, all(x in allowed for x in v)


def _users(v):
    """人员 → [{id, name}]（姓名仅展示；同步日志不输出）。"""
    if not isinstance(v, list):
        return []
    out = []
    for u in v:
        if isinstance(u, dict) and u.get("id"):
            out.append({"id": u["id"], "name": u.get("name") or ""})
    return out


def _attachments(v):
    """附件列 → [{name, url, token}]（门户只读展示，上传见 api.py）。

    值形状（2026-09-29 实读）：[{name, url?, file_token?, ...}]；url 是飞书临时
    链接，过期与否不影响快照（展示端兜底为文件名）。
    """
    if not isinstance(v, list):
        return []
    out = []
    for a in v:
        if isinstance(a, dict) and a.get("name"):
            out.append({"name": a["name"],
                        "url": a.get("url") or None,
                        "token": a.get("file_token") or None})
    return out


def _link_id(v, expect_table=None):
    """关联 → (record_id|None, 多值列表)；值异常记录 issue。

    飞书返回的 record_ids 顺序不保证稳定，直接取 [0] 会让同一个多项目事项
    每次同步换一个 project_id，进而让内容摘要变化、每轮都发新版本（审计 P2-7）。
    这里按 ID 排序取首个，并让调用方拿到完整列表用于归属统计。
    """
    if not isinstance(v, list) or not v:
        return None, None, []
    first = v[0]
    if isinstance(first, str) and first:
        # 写形状（record_id 字符串数组）：本地视图里排队中的关联就是这个形状
        return first, None, [first]
    if not isinstance(first, dict):
        return None, "关联值形状异常", []
    rids = sorted({r for r in (first.get("record_ids") or []) if isinstance(r, str) and r})
    tid = first.get("table_id")
    issue = None
    if expect_table and tid and tid != expect_table:
        issue = f"关联指向其他表({tid[-6:]})"
    elif len(rids) > 1:
        issue = "关联多个值，取 id 最小者"
    return (rids[0] if rids else None), issue, rids


def _date(v):
    """毫秒时间戳 → {"ts": 秒, "raw_ms": v}；非法 → (None, issue)。"""
    if v is None or v == "":
        return None, None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return {"ts": v / 1000.0, "raw_ms": v}, None
    return None, f"日期值非法({type(v).__name__})"


class Normalizer:
    """把（键已翻译为语义键的）records.items[].fields 转成语义记录。

    键翻译（字段名→field_id→语义键）由 sync.FeishuReader.translate_keys
    完成；本类只按语义键读取（§4：不把中文列名当契约）。
    """

    def __init__(self, tables_cfg):
        self.cfg = tables_cfg

    # ---- 各表 ----

    def projects(self, items):
        out, issues = [], []
        for it in items or []:
            f = it.get("fields") or {}
            rid = it.get("record_id")
            name = _text(f.get("name"))
            group, e_g = _req_enum(f.get("owner_group"), GROUPS, "主责组")
            status, e_s = _opt_enum(f.get("status"), PROJECT_STATUSES, "项目状态")
            collab, ok_c = _enum_list(f.get("collab_groups"), GROUPS)
            ps, e1 = _date(f.get("plan_start"))
            pe, e2 = _date(f.get("plan_end"))
            bad = [m for m in (e_g, e_s) if m]
            if not name:
                bad.append("缺项目名称")
            bad += [m for m in (e1, e2) if m]
            if ok_c is False:
                bad.append("协作组含未知值")
            if ps and pe and pe["ts"] < ps["ts"]:
                bad.append("计划结束早于开始")
            rec = {
                "id": rid, "name": name or "(未命名)",
                "description": _text(f.get("description")),
                "owner_group": group, "collab_groups": collab if ok_c else [],
                "owners": _users(f.get("owners")),
                "plan_start": ps, "plan_end": pe, "status": status,
            }
            if bad:
                issues.append((rid, "; ".join(bad)))
                rec["_invalid"] = True
            out.append(rec)
        return out, issues

    def tasks(self, items):
        out, issues = [], []
        for it in items or []:
            f = it.get("fields") or {}
            rid = it.get("record_id")
            name = _text(f.get("name"))
            # 组别兼容两种形状：契约单选（字符串）；新数据源多选（数组）——
            # 首个为主责组，其余并入协作组
            og = f.get("owner_group")
            if isinstance(og, list):
                collab_from_group = [x for x in og[1:] if x in GROUPS]
                og = og[0] if og else None
            else:
                collab_from_group = []
            group, e_g = _req_enum(og, GROUPS, "主责组")
            collab, ok_c = _enum_list(f.get("collab_groups"), GROUPS)
            collab = list(dict.fromkeys((collab if ok_c else []) + collab_from_group))
            pri, e_p = _req_enum(f.get("priority"), PRIORITIES, "P等级")
            # 状态适配（新数据源验收流）：待验收 + 验收结论（accept）推导终态——
            # 通过→已完成、取消→已取消、未验收/退回修改→进行中；其余按契约枚举
            raw_status = f.get("status")
            accept = f.get("accept")
            if raw_status == "待验收":
                raw_status = ("已完成" if accept == "通过"
                              else "已取消" if accept == "取消" else "进行中")
            status, e_s = _req_enum(raw_status, TASK_STATUSES, "状态")
            proj, e_l, proj_ids = _link_id(f.get("project"))
            st, e1 = _date(f.get("start_time"))
            due, e2 = _date(f.get("due_time"))
            done, e3 = _date(f.get("done_time"))
            submit, e4 = _date(f.get("submit_time"))
            # 截止精度：契约数据源显式标注；新数据源无该字段，其日期列均带
            # 时刻（yyyy-MM-dd HH:mm），缺省按"精确时刻"处理，不再判质量异常
            prec, e_prec = _opt_enum(f.get("due_precision") or "精确时刻",
                                     ["仅日期", "精确时刻"], "截止时间精度")
            bad = [m for m in (e_g, e_p, e_s, e_prec) if m]
            if not name:
                bad.append("缺事项名称")
            if ok_c is False:
                bad.append("协作组含未知值")
            for m in (e1, e2, e3, e4, e_l):
                if m:
                    bad.append(m)
            # 精度已缺省补"精确时刻"，此处 prec 为空只可能是显式给了未知值
            if due and not prec and e_prec:
                bad.append(e_prec)
            rec = {
                "id": rid, "name": name or "(未命名)",
                "project_id": proj, "project_ids": proj_ids, "owner_group": group,
                "collab_groups": collab if ok_c else [],
                "owners": _users(f.get("owners")),
                "priority": pri, "status": status,
                "start": st, "due": due,
                "due_precision": ("date" if prec == "仅日期"
                                  else "datetime" if prec == "精确时刻" else None),
                "done_time": done, "submit_time": submit,
                "description": _text(f.get("description")),
                "blocker_note": _text(f.get("blocker_note")),
            }
            if bad:
                issues.append((rid, "; ".join(bad)))
                rec["_invalid"] = True
            out.append(rec)
        return out, issues

    def milestones(self, items):
        out, issues = [], []
        for it in items or []:
            f = it.get("fields") or {}
            rid = it.get("record_id")
            name = _text(f.get("name"))
            kind, e_k = _opt_enum(f.get("kind"), ["比赛", "关键检查"], "节点类型")
            # 时间精度：重要节点表无该字段，缺省按"精确时刻"处理（与 tasks()
            # 的截止精度同口径），避免为每行多填一个枚举；显式给未知值才记异常
            prec, e_p = _opt_enum(f.get("time_precision") or "精确时刻",
                                  ["仅日期", "精确时刻"], "时间精度")
            vis, e_v = _opt_enum(f.get("visibility"),
                                 ["展示", "隐藏", "已取消"], "展示状态")
            tgt, e1 = _date(f.get("target_time"))
            proj, e_l, proj_ids = _link_id(f.get("project"))
            bad = [m for m in (e_k, e_p, e_v, e1, e_l) if m]
            if not name:
                bad.append("缺节点名称")
            if not tgt:
                bad.append("缺目标时间")
            rec = {
                "id": rid, "name": name or "(未命名)",
                "kind": kind,
                "target": (dict(tgt, precision="date" if prec == "仅日期"
                                else "datetime") if tgt else None),
                "project_id": proj, "project_ids": proj_ids, "visibility": vis,
                "description": _text(f.get("description")),
            }
            if bad:
                issues.append((rid, "; ".join(bad)))
                rec["_invalid"] = True
            out.append(rec)
        return out, issues

    def schedule(self, items):
        out, issues = [], []
        for it in items or []:
            f = it.get("fields") or {}
            rid = it.get("record_id")
            name = _text(f.get("name"))
            kind, e_k = _opt_enum(f.get("kind"),
                                  # 新数据源类型超集（训练/检查/比赛/运营活动）
                                  ["例会", "联调", "测试", "训练", "检查",
                                   "比赛", "运营活动", "其他"], "活动类型")
            groups, ok_g = _enum_list(f.get("groups"), GROUPS)
            # 状态适配（新数据源）：待进行/进行中/已结束 都是"正常"日程，
            # 契约只区分 正常/已取消
            raw_status = f.get("status")
            if raw_status in ("待进行", "进行中", "已结束"):
                raw_status = "正常"
            status, e_s = _opt_enum(raw_status, ["正常", "已取消"], "状态")
            st, e1 = _date(f.get("start_time"))
            en, e2 = _date(f.get("end_time"))
            proj, e_l, _proj_ids = _link_id(f.get("project"))
            all_day = bool(f.get("all_day"))
            bad = [m for m in (e_k, e_s, e1, e2, e_l) if m]
            if not name:
                bad.append("缺日程名称")
            if ok_g is False:
                bad.append("相关组含未知值")
            if not st:
                bad.append("缺开始时间")
            if st and en and en["ts"] < st["ts"]:
                bad.append("结束早于开始")
            rec = {
                "id": rid, "name": name or "(未命名)",
                "start": st, "end": en, "all_day": all_day, "kind": kind,
                "groups": groups if ok_g else [],
                "participants": _users(f.get("participants")),
                "location": _text(f.get("location")),
                "project_id": proj,
                "description": _text(f.get("description")),
                "status": status,
            }
            if bad:
                issues.append((rid, "; ".join(bad)))
                rec["_invalid"] = True
            out.append(rec)
        return out, issues

    def members(self, items):
        """成员数据表：由 scripts/feishu_member_sync.py 写入，门户只读。

        保留用户空行不算异常（无人员引用直接跳过）；人员字段取首位成员。
        计划/截止类字段属另一条流水线（考勤与事项统计），此处只做展示口径。
        """
        out, issues = [], []
        for it in items or []:
            f = it.get("fields") or {}
            rid = it.get("record_id")
            people = _users(f.get("member"))
            if not people:
                continue
            ci, e1 = _date(f.get("last_check_in"))
            co, e1b = _date(f.get("last_check_out"))
            sy, e2 = _date(f.get("synced_at"))
            bad = [m for m in (e1, e1b, e2) if m]
            rec = {
                "id": rid,
                "member": people[0],
                "valid_tasks": _num(f.get("valid_tasks")),
                "done_tasks": _num(f.get("done_tasks")),
                "due_tasks": _num(f.get("due_tasks")),
                "on_time_tasks": _num(f.get("on_time_tasks")),
                "overdue_tasks": _num(f.get("overdue_tasks")),
                "missing_delivery": _num(f.get("missing_delivery")),
                "tasks_without_due": _num(f.get("tasks_without_due")),
                "completion_rate": _num(f.get("completion_rate")),
                "on_time_rate": _num(f.get("on_time_rate")),
                "summary_note": _plain(f.get("summary_note")),
                "last_check_in": ci,
                "last_check_out": co,
                "work_hours": _num(f.get("work_hours")),
                "attendance_anomalies": _num(f.get("attendance_anomalies")),
                "synced_at": sy,
                "sync_note": _plain(f.get("sync_note")),
                # 周结算（2026-10-20 起）：周初时长 / 结算周 / 上周时长由门户换周时写入，
                # 本周时长与两个达标列是飞书公式（见 app/portal/attendance.py）
                "week_base": _num(f.get("week_base")),
                "week_key": _plain(f.get("week_key")),
                "last_week_hours": _num(f.get("last_week_hours")),
                "week_hours": _formula_num(f.get("week_hours")),
                "week_ok": _plain(f.get("week_ok")),
                "last_week_ok": _plain(f.get("last_week_ok")),
                # 请假周（2026-10-21 起）：组长在考勤页设置，见 attendance.leave_weeks
                "leave_weeks": _plain(f.get("leave_week")),
            }
            if bad:
                issues.append((rid, "; ".join(bad)))
                rec["_invalid"] = True
            out.append(rec)
        return out, issues

    def roster(self, items):
        """花名册（组长/组员身份判定源）：轻量直通，不进质量统计。

        组长判定 = 会话 open_id 出现在某组记录的 leaders 里；
        组员集合预留给后续按组过滤视图。
        """
        out = []
        for it in items or []:
            f = it.get("fields") or {}
            group = _text(f.get("group"))
            if not group:
                continue
            out.append({
                "id": it.get("record_id"),
                "duty": _text(f.get("duty")) or group,
                "group": group,
                "leaders": _users(f.get("leaders")),
                "members": _users(f.get("members")),
            })
        return out, []

    def duty(self, items):
        """值日表（维护者导入，门户只读）：每行「日期」或「星期」之一 + 值日人员。

        值日人员优先按飞书人员列（open_id）匹配成员；从表格导入成文本时按姓名匹配。
        完全空白的行跳过；有内容但缺日期和星期、星期写法认不出、或没填人员的行计入质量异常。"""
        out, issues = [], []
        for it in items or []:
            f = it.get("fields") or {}
            rid = it.get("record_id")
            people = _users(f.get("people"))
            names = [] if people else attendance.split_names(f.get("people"))
            d, e = _date(f.get("date"))
            wd_raw = f.get("weekday")
            has_wd = wd_raw not in (None, "", [])
            wd = attendance.parse_weekday(wd_raw) if has_wd else None
            if not people and not names and d is None and not e and not has_wd:
                continue
            bad = [e] if e else []
            if has_wd and wd is None:
                bad.append("星期无法识别")
            if d is None and not has_wd and not e:
                bad.append("缺日期或星期")
            if not people and not names:
                bad.append("缺值日人员")
            rec = {"id": rid,
                   "date": attendance.date_key(d["ts"]) if d else None,
                   "weekday": wd, "people": people, "names": names}
            if bad:
                issues.append((rid, "; ".join(bad)))
                rec["_invalid"] = True
            out.append(rec)
        return out, issues

    def procurement(self, items):
        """物资采购（物资明细）表：全员可写的轻量表（组长批复列除外，写权限在 api.py）。

        值形状（2026-09-29 / 2026-10-12 两次实读）：复选框=布尔（含 false，缺省即未勾选）、
        单选=选项名字符串、多选=字符串数组、附件=[{name,url?,file_token?}]、
        数字列（含公式列「总价（元）」）=字符串或数值（_num 统一强转）、
        日期=毫秒时间戳、人员=[{id,name}]。

        分类/赛季用 _text 直通而不做白名单校验：维护者在飞书里新增选项时门户
        必须照常显示，不能记成「未知值」质量异常（常量清单只用于前端下拉提示）。
        """
        out, issues = [], []
        for it in items or []:
            f = it.get("fields") or {}
            rid = it.get("record_id")
            name = _text(f.get("name"))
            groups, ok_g = _enum_list(f.get("groups"), GROUPS)
            bad = []
            if not name:
                bad.append("缺物资名称")
            if ok_g is False:
                bad.append("申请组别含未知值")
            purchase_date, date_issue = _date(f.get("purchase_date"))
            if date_issue:
                bad.append("采购日期" + date_issue)
            rec = {
                "id": rid, "name": name or "(未命名)",
                "groups": groups if ok_g else [],
                "link": _plain(f.get("link")),
                "purpose": _text(f.get("purpose")),
                "attachments": _attachments(f.get("attachments")),
                "invoice_files": _attachments(f.get("invoice_files")),
                "leader_approval": bool(f.get("leader_approval")),
                "purchased": bool(f.get("purchased")),
                "invoice_done": bool(f.get("invoice_done")),
                "season": _text(f.get("season")),
                "category1": _text(f.get("category1")),
                "category2": _text(f.get("category2")),
                "code": _text(f.get("code")),
                "purchase_date": purchase_date,
                "unit_price": _num(f.get("unit_price")),
                "quantity": _num(f.get("quantity")),
                "total_price": _num(f.get("total_price")),
                "purchaser": _users(f.get("purchaser")),
            }
            if bad:
                issues.append((rid, "; ".join(bad)))
                rec["_invalid"] = True
            out.append(rec)
        return out, issues

    def normalize_all(self, raw):
        """raw = {table: items[]} → (records_by_table, quality_by_table)。

        失效关联（project_id 不在项目表）在跨表校验阶段补记。
        """
        fn = {"projects": self.projects, "tasks": self.tasks,
              "milestones": self.milestones, "schedule": self.schedule,
              "members": self.members, "roster": self.roster,
              "procurement": self.procurement, "duty": self.duty}
        out, q = {}, {}
        for t, items in raw.items():
            recs, issues = fn[t](items)
            out[t], q[t] = recs, issues
        # 跨表：失效项目关联
        pids = {r["id"] for r in out.get("projects", []) if not r.get("_invalid")}
        for t in ("tasks", "milestones", "schedule"):
            for r in out.get(t, []):
                pid = r.get("project_id")
                if pid and pid not in pids:
                    q[t].append((r["id"], "关联项目已移除"))
                    r["_invalid"] = True
        return out, q
