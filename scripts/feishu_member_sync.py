"""同步成员事项指标和飞书原生考勤；密钥仅从环境变量或隐藏输入读取。

成员数据表只保留程序写入的统计快照（七项事项计数）与合并后的同步元数据
（同步时间、同步说明）。赛季与按日考勤明细表已移除：成员归属只按
「负责人」匹配，统计范围由命令行参数或默认本月至今决定。

考勤字段（最近签到/最近签退/有效时长/异常考勤数）由门户打卡接口专有管理：
本程序不再读取飞书原生考勤、也不再覆盖或清空这些字段——此前"接口无记录时
清空"的逻辑会把门户打卡数据每轮抹掉（线上事故根因）。
"""

import argparse
import datetime as dt
import fcntl
import getpass
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


def _feishu_ids():
    """读取应用 ID、Base 与表 ID：环境变量优先，其次 config/feishu.json（与门户主程序同一口径）。

    仓库公开，这些标识不再写死在代码里；缺配置时返回尖括号占位，由 main() 拒绝运行。
    """
    try:
        cfg = json.loads((ROOT / "config/feishu.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    tables = cfg.get("tables") or {}
    return (os.environ.get("FEISHU_APP_ID") or cfg.get("app_id") or "",
            os.environ.get("FEISHU_BASE_APP_TOKEN") or (cfg.get("base") or {}).get("app_token") or "<Base app_token>",
            os.environ.get("FEISHU_MEMBERS_TABLE_ID") or (tables.get("members") or {}).get("table_id") or "<成员数据表 table_id>",
            os.environ.get("FEISHU_TASKS_TABLE_ID") or (tables.get("tasks") or {}).get("table_id") or "<事项表 table_id>")


APP_ID, BASE, MEMBERS, TASKS = _feishu_ids()
COUNTS = ["有效事项数", "已完成事项数", "到期事项数", "准时完成数", "逾期未完成数", "交付时间待补数", "未设截止事项数"]


class ApiError(Exception):
    pass


class Client:
    def __init__(self, app_id, secret):
        self.app_id, self.secret = app_id, secret
        self.token, self.expires = "", 0

    def call(self, method, path, body=None, auth=True):
        if auth and time.time() >= self.expires:
            token = self.call("POST", "/auth/v3/tenant_access_token/internal", {"app_id": self.app_id, "app_secret": self.secret}, False)
            self.token = token["tenant_access_token"]
            self.expires = time.time() + token["expire"] - 120
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request("https://open.feishu.cn/open-apis" + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body, ensure_ascii=False).encode())
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                data = json.load(exc)
            except (ValueError, OSError):
                raise ApiError(f"接口 HTTP {exc.code}") from None
        if data.get("code") != 0:
            raise ApiError(f"接口错误 {data.get('code')}：{data.get('msg', '')[:180]}")
        return data

    def items(self, path):
        rows, cursor = [], ""
        while True:
            query = urllib.parse.urlencode({"page_size": 100, **({"page_token": cursor} if cursor else {})})
            data = self.call("GET", path + ("&" if "?" in path else "?") + query)["data"]
            rows.extend(data.get("items", []))
            if not data.get("has_more"):
                return rows
            cursor = data["page_token"]


def table_path(table):
    return f"/bitable/v1/apps/{BASE}/tables/{table}"


def scalar(value):
    if isinstance(value, dict):
        return scalar(value.get("value"))
    if isinstance(value, list):
        return "".join(str(x.get("text", "")) for x in value if isinstance(x, dict))
    return value


def task_metrics(records, member, now_ms, start=None, end=None):
    result = dict.fromkeys(COUNTS, 0)
    cutoff = min(now_ms, end) if end is not None else now_ms
    for record in records:
        f = record["fields"]
        if member not in [p["id"] for p in f.get("负责人", [])] or scalar(f.get("验收结论")) == "取消":
            continue
        result["有效事项数"] += 1
        completed = scalar(f.get("验收结论")) == "通过"
        result["已完成事项数"] += int(completed)
        due, submitted = f.get("计划截止"), f.get("提交验收时间")
        if due is None:
            result["未设截止事项数"] += 1
            continue
        if due > cutoff or (start is not None and due < start):
            continue
        result["到期事项数"] += 1
        result["逾期未完成数"] += int(not completed)
        result["交付时间待补数"] += int(completed and submitted is None)
        result["准时完成数"] += int(completed and submitted is not None and submitted <= due)
    return result


def merged_seconds(intervals):
    total, left, right = 0, None, None
    for begin, end in sorted(set(intervals)):
        if left is None:
            left, right = begin, end
        elif begin <= right:
            right = max(right, end)
        else:
            total += right - left
            left, right = begin, end
    return total + (right - left if left is not None else 0)


def attendance_metrics(results, now):
    """有效时长按当日有效签到/签退成对区间的并集计算；异常班段单独计数。"""
    intervals, ins, anomalies = [], [], 0
    seen = set()
    for task in results:
        day = task.get("day")
        for index, rec in enumerate(task.get("records", [])):
            key = (task.get("result_id"), day, index, rec.get("check_in_record_id"), rec.get("check_out_record_id"))
            if key in seen:
                continue
            seen.add(key)
            def stamp(side):
                try:
                    flow = rec.get(side + "_record") or {}
                    if flow.get("type") in (3, 4) or flow.get("check_result") in ("PendingApproval", "Invalid") or rec.get(side + "_result") in ("Lack", "Invalid", "NoNeedCheck", "SystemCheck", "PendingApproval", "Todo", "None"):
                        return None
                    value = int(flow.get("check_time", 0))
                    return value if 0 < value <= now else None
                except (TypeError, ValueError):
                    return None
            begin, end = stamp("check_in"), stamp("check_out")
            if begin:
                ins.append(begin)
            invalid = {"Lack", "Invalid", "NoNeedCheck", "SystemCheck", "PendingApproval"}
            valid = begin and end and end > begin and not any(rec.get(k) in invalid for k in ("check_in_result", "check_out_result"))
            if valid:
                intervals.append((begin, end))
            exempt = not begin and not end and all(rec.get(k) == "NoNeedCheck" for k in ("check_in_result", "check_out_result"))
            if not exempt and (not valid or any(rec.get(k) not in (None, "Normal", "NoNeedCheck") for k in ("check_in_result", "check_out_result"))):
                anomalies += 1
    return {"最近签到": max(ins) * 1000 if ins else None,
            "有效时长（小时）": round(merged_seconds(intervals) / 3600, 2),
            "异常考勤数": anomalies}


def fields(client, table):
    return {f["field_name"]: f for f in client.items(table_path(table) + "/fields")}


def ensure_field(client, table, name, kind, prop=None, convert=False):
    existing = fields(client, table).get(name)
    body = {"field_name": name, "type": kind}
    if prop is not None:
        body["property"] = prop
    if existing:
        if existing["type"] != kind:
            if not convert:
                raise ApiError(f"字段类型不兼容：{name}")
            client.call("PUT", table_path(table) + "/fields/" + existing["field_id"], body)
        return existing["field_id"]
    return client.call("POST", table_path(table) + "/fields", body)["data"]["field"]["field_id"]


def setup(client):
    """初始化本程序管理的字段；只补建，不改动业务字段与用户数据。"""
    for name in COUNTS:
        ensure_field(client, MEMBERS, name, 2, {"formatter": "0"}, convert=True)
    ensure_field(client, MEMBERS, "同步时间", 5, {"date_formatter": "yyyy-MM-dd HH:mm"})
    ensure_field(client, MEMBERS, "同步说明", 1)


def sync(client, start, end, window=(None, None)):
    """window 为事项统计区间（毫秒，含边界），默认不设界 = 截至本次同步。"""
    now = time.time()
    window_start, window_end = window
    members = client.items(table_path(MEMBERS) + "/records?user_id_type=open_id")
    member_fields = fields(client, MEMBERS)
    person_field = next(f["field_name"] for f in member_fields.values() if f.get("is_primary") and f["type"] == 11)
    required = {"负责人": 11, "验收结论": 3, "计划截止": 5, "提交验收时间": 5}
    task_fields = fields(client, TASKS)
    if any(task_fields.get(name, {}).get("type") != kind for name, kind in required.items()):
        raise ApiError("工作事项字段已改变，请核对字段名称和类型；本轮不覆盖统计")
    if any(member_fields.get(name, {}).get("type") != 2 for name in COUNTS):
        raise ApiError("成员统计字段未正确初始化，请核对后运行 --setup")
    tasks = client.items(table_path(TASKS) + "/records?user_id_type=open_id")
    report = {"time": dt.datetime.now(TZ).isoformat(), "from": str(start), "to": str(end), "members": [], "failures": 0}
    for row in members:
        f = row["fields"]
        people = f.get(person_field, [])
        if not people:
            continue
        uid = people[0]["id"]
        updates = dict(task_metrics(tasks, uid, int(now * 1000), window_start, window_end))
        notes = ["事项：已同步", "考勤：由门户打卡管理（本程序不覆盖）"]
        status = {"record_id": row["record_id"], "tasks": "success"}
        updates.update({"同步时间": int(now * 1000), "同步说明": "；".join(notes)})
        client.call("PUT", table_path(MEMBERS) + "/records/" + row["record_id"], {"fields": updates})
        report["members"].append(status)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="初始化本程序管理的统计与同步字段")
    parser.add_argument("--watch", action="store_true", help="持续同步，默认每五分钟")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--from-date", type=dt.date.fromisoformat, help="统计起日，默认本月 1 日")
    parser.add_argument("--to-date", type=dt.date.fromisoformat, help="统计止日，默认今天")
    parser.add_argument("--prompt-secret", action="store_true")
    args = parser.parse_args()
    lock = open(ROOT / "config/.feishu-member-sync.lock", "a", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("已有成员同步程序运行，请先停止原进程")
    secret = os.environ.get("FEISHU_APP_SECRET") or (getpass.getpass("应用密钥（隐藏输入）：") if args.prompt_secret else "")
    if not secret:
        parser.error("请设置 FEISHU_APP_SECRET，或使用 --prompt-secret")
    if args.interval < 60:
        parser.error("同步间隔不能小于 60 秒")
    if not APP_ID or any(v.startswith("<") for v in (BASE, MEMBERS, TASKS)):
        parser.error("缺少飞书应用 ID、Base 或表 ID：请准备 config/feishu.json，或设置 FEISHU_APP_ID / FEISHU_BASE_APP_TOKEN 等环境变量")
    client = Client(APP_ID, secret)
    if args.setup:
        setup(client)
    while True:
        today = dt.datetime.now(TZ).date()
        start, end = args.from_date or today.replace(day=1), args.to_date or today
        if start > end or end > today:
            parser.error("统计起日应不晚于止日，止日不能晚于今天")
        try:
            # 显式给了命令行区间才限制事项统计；未给则截至本次同步，与历史默认一致
            window = tuple(
                int(dt.datetime.combine(day, dt.time(23, 59, 59) if last else dt.time(), TZ).timestamp() * 1000)
                if day else None
                for day, last in ((args.from_date, False), (args.to_date, True)))
            report = sync(client, start, end, window)
            output = ROOT / "docs/feishu-member-sync-result.json"
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            output.chmod(0o600)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if not args.watch:
                return int(bool(report["failures"]))
        except (ApiError, urllib.error.URLError, TimeoutError) as exc:
            print("本轮同步失败，未完成的数据保留上次快照：" + str(exc), flush=True)
            if not args.watch:
                return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
