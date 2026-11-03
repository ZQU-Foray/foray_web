# -*- coding: utf-8 -*-
"""MCP（Model Context Protocol）只读接入：scope 定义与校验。

第一版只开放读：写操作（事项/采购/打卡/烧录/部署）不在此列。
scope 是**按数据域**划分的最小单位，agent 令牌持有哪些 scope 就看得见哪些工具。
"""
SCOPES = {
    "meta.read": "门户元数据（分组、人员、源表链接）",
    "projects.read": "项目与里程碑进度",
    "tasks.read": "事项（任务）列表与详情",
    "schedule.read": "日程",
    "procurement.read": "采购记录",
    "quality.read": "数据质量报告",
    "attendance.read": "本人考勤状态",
    "sync.read": "同步状态与队列积压",
}
ALL_SCOPES = tuple(SCOPES)
# 默认签发集合：除同步队列细节外全给（队列积压属于运维视角，单独授权）
DEFAULT_SCOPES = tuple(s for s in ALL_SCOPES if s != "sync.read")


def normalize(raw):
    """把外部传入的 scope 列表归一：去空白、去重、拒绝未知项。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [s for s in raw.replace(",", " ").split()]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("scopes 必须是字符串数组")
    out = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        if s not in SCOPES:
            raise ValueError("未知 scope：%s" % s)
        if s not in out:
            out.append(s)
    return out


def has(agent: dict, scope: str) -> bool:
    """令牌是否持有该 scope（记录由 agents.verify() 返回）。"""
    return scope in (agent or {}).get("scopes", ())


def visible_tools(agent: dict, tools) -> list:
    """按 scope 过滤工具清单；tools 为 {name: {"scopes": (...), ...}}。"""
    out = []
    for name, meta in tools.items():
        need = meta.get("scopes") or ()
        if any(has(agent, s) for s in need):
            out.append(name)
    return out


def describe() -> list:
    """签发界面用的 scope 清单（顺序稳定，便于前端渲染）。"""
    return [{"scope": s, "description": SCOPES[s], "default": s in DEFAULT_SCOPES}
            for s in ALL_SCOPES]
