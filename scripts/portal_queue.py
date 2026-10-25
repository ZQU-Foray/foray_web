# -*- coding: utf-8 -*-
"""本地写入队列运维工具（缓存库第 3 步）：查看队列与冲突、重试或作废失败的操作、确认冲突。

可在服务运行时使用（与服务共用同一个库文件，SQLite 写锁互斥）；重试的操作
由服务里的推送线程在几秒内取走。

用法（在部署目录执行）：
    python scripts/portal_queue.py --db /opt/portal/config/portal_snapshot.db status
    python scripts/portal_queue.py --db ... list --state failed
    python scripts/portal_queue.py --db ... retry 12 13
    python scripts/portal_queue.py --db ... discard 14 --note "飞书侧已手工改好"
    python scripts/portal_queue.py --db ... conflicts
    python scripts/portal_queue.py --db ... ack 5 --by 维护者

退出码：0 成功；1 有操作没能处理（编号不存在或状态不允许）；2 参数或库版本问题。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# 从脚本所在代码树导入（部署树里同样成立），须在补 sys.path 之后导入
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app import config
from app.portal import cache_store, store


def _ts(v):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(v)) if v else "—"


def _brief(d, n=60):
    s = json.dumps(d, ensure_ascii=False)
    return s if len(s) <= n else s[:n - 1] + "…"


def cmd_status(args):
    st = cache_store.backlog()
    print("待推 %(pending)s  推送中 %(pushing)s  失败 %(failed)s  未确认冲突 %(open_conflicts)s" % st)
    print("最老的未推送改动：%s" % _ts(st["oldest_open_at"]))
    return 0


def cmd_list(args):
    ops = cache_store.recent_ops(args.limit, states=args.state or None)
    if not ops:
        print("（没有符合条件的操作）")
    for op in ops:
        print("#%-5s %-9s %-11s %-6s %-22s 尝试%-2s 建于 %s 由 %s"
              % (op["id"], op["state"], op["table_name"], op["op"], op["record_id"],
                 op["attempts"], _ts(op["created_at"]), op["created_by"] or "—"))
        print("        列 %s%s" % (_brief(op["fields"]),
                                   ("  增量 " + _brief(op["increments"])) if op["increments"] else ""))
        if op["last_error"] or op["note"]:
            print("        %s%s" % (("错误：" + op["last_error"]) if op["last_error"] else "",
                                    ("  备注：" + op["note"]) if op["note"] else ""))
    return 0


def _each(ids, fn, ok_msg, bad_msg):
    bad = 0
    for i in ids:
        if fn(i):
            print("#%s %s" % (i, ok_msg))
        else:
            print("#%s %s" % (i, bad_msg))
            bad += 1
    return 1 if bad else 0


def cmd_retry(args):
    return _each(args.ids, cache_store.retry_failed, "已放回待推，推送线程几秒内会取走",
                 "不是 failed 状态（或不存在），未改动")


def cmd_discard(args):
    return _each(args.ids, lambda i: cache_store.discard(i, args.note),
                 "已作废（不会再推到飞书）", "不是 failed/pending 状态（或不存在），未改动")


def cmd_conflicts(args):
    rows = cache_store.list_conflicts(open_only=not args.all, limit=args.limit)
    if not rows:
        print("（没有%s冲突）" % ("" if args.all else "未确认的"))
    for c in rows:
        print("冲突 %-4s %s %s/%s 列「%s」 %s%s"
              % (c["id"], _ts(c["detected_at"]), c["table_name"], c["record_id"],
                 c["field"] or "—", c["kind"],
                 ("  已确认（%s）" % c["acked_by"]) if c["acked_at"] else ""))
        print("        基准 %s → 网页 %s；飞书 %s（以飞书为准）"
              % (_brief(c["base_value"], 40), _brief(c["local_value"], 40),
                 _brief(c["remote_value"], 40)))
    return 0


def cmd_ack(args):
    return _each(args.ids, lambda i: cache_store.ack_conflict(i, args.by),
                 "已确认", "不存在或已确认过")


def main():
    ap = argparse.ArgumentParser(description="本地写入队列运维工具")
    ap.add_argument("--db", required=True, help="快照库路径（config/portal_snapshot.db）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="队列与冲突概况")
    p = sub.add_parser("list", help="列出最近的操作")
    p.add_argument("--state", action="append",
                   choices=["pending", "pushing", "done", "failed", "conflict", "cancelled"])
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("retry", help="重试 failed 的操作")
    p.add_argument("ids", type=int, nargs="+")
    p = sub.add_parser("discard", help="作废 failed/pending 的操作")
    p.add_argument("ids", type=int, nargs="+")
    p.add_argument("--note", default="人工作废")
    p = sub.add_parser("conflicts", help="列出冲突")
    p.add_argument("--all", action="store_true", help="含已确认的")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("ack", help="确认冲突")
    p.add_argument("ids", type=int, nargs="+")
    p.add_argument("--by", required=True, help="确认人")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print("库不存在：%s" % args.db)
        return 2
    config.PORTAL_DB = Path(args.db)
    try:
        return {"status": cmd_status, "list": cmd_list, "retry": cmd_retry,
                "discard": cmd_discard, "conflicts": cmd_conflicts,
                "ack": cmd_ack}[args.cmd](args)
    except store.SchemaVersionError as e:
        print("✗ %s" % e)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
