"""同步/读路径性能基准：合成数据三档，量化「每轮全量重建」与「每请求快照反序列化」的成本。

用法（仓库根目录，只依赖标准库与 app 包，不访问飞书、不碰生产库）：

    python scripts/bench_portal_sync.py                 # 默认 600/2000/5000 三档
    python scripts/bench_portal_sync.py --tasks 3000    # 指定单档

输出每个规模一行 JSON：rebuild_ms（一轮同步的本地重建）、table_views_ms（镜像→视图）、
current_first_ms（每版本首次反序列化）、current_cached_ms（缓存命中）、status_ms。
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config                      # noqa: E402
from app.portal import cache_store as C     # noqa: E402
from app.portal import store as S           # noqa: E402
from app.portal import view as V            # noqa: E402

# 只映射项目与事项两张表（与真实配置同一套 field_id → 语义键机制）
CFG = {"tables": {
    "projects": {"table_id": "tblP", "fields": {"name": "fldPn", "owners": "fldPo"}},
    "tasks": {"table_id": "tblT", "fields": {
        "name": "fldTn", "owner_group": "fldTg", "priority": "fldTp",
        "status": "fldTs", "project": "fldTproj"}},
}}
FIELD_IDS = {
    "projects": {"项目名称": "fldPn", "负责人": "fldPo"},
    "tasks": {"事项名称": "fldTn", "组别": "fldTg", "优先级": "fldTp",
              "执行状态": "fldTs", "所属项目": "fldTproj"},
}


def make_items(n_tasks):
    """合成记录：项目 30 条，任务 n 条（按星期轮换优先级/状态，制造一点分支）。"""
    projects = [{"record_id": "recP%03d" % i, "last_modified_time": 1700000000000 + i,
                 "fields": {"项目名称": "项目%d" % i,
                            "负责人": [{"id": "ou_%d" % (i % 40), "name": "成员%d" % (i % 40)}]}}
                for i in range(30)]
    pids = [p["record_id"] for p in projects]
    tasks = [{"record_id": "recT%05d" % i, "last_modified_time": 1700000000000 + i,
              "fields": {"事项名称": "事项%d" % i, "组别": "电控",
                         "优先级": "高" if i % 7 == 0 else "中",
                         "执行状态": "进行中" if i % 3 else "已完成",
                         "所属项目": [{"id": pids[i % len(pids)]}]}}
             for i in range(n_tasks)]
    return {"projects": projects, "tasks": tasks}


def median_ms(fn, n=5):
    xs = []
    for _ in range(n):
        started = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(xs), 2)


def run(n_tasks):
    tmp = tempfile.TemporaryDirectory()
    old_db, old_mode = config.PORTAL_DB, config.PORTAL_WRITE_MODE
    try:
        S.close()
        config.PORTAL_DB = Path(os.path.join(tmp.name, "portal.db"))
        config.PORTAL_WRITE_MODE = "direct"     # 避免基准里触发队列，只测库与重建
        S._db()
        V.save_sync_meta(FIELD_IDS, {})
        items = make_items(n_tasks)
        C.replace_remote_tables(items, time.time())
        V.rebuild(cfg=CFG, sync_round=True, duration=1.0)   # 预热：库里有当前快照
        V.rebuild(cfg=CFG, sync_round=True, duration=1.0)   # 内容不变（哈希门控命中）
        rebuild = median_ms(lambda: V.rebuild(cfg=CFG, sync_round=True, duration=1.0), 3)
        views = median_ms(lambda: C.table_views(sorted(items)), 5)
        S.drop_snapshot_cache()
        first = median_ms(lambda: S.current(), 1)
        cached = median_ms(lambda: S.current(), 20)
        status = median_ms(lambda: S.status(), 20)
        return {
            "tasks": n_tasks,
            "records": sum(len(v) for v in items.values()),
            "rebuild_ms": rebuild,
            "table_views_ms": views,
            "current_first_ms": first,
            "current_cached_ms": cached,
            "status_ms": status,
        }
    finally:
        S.close()
        config.PORTAL_DB, config.PORTAL_WRITE_MODE = old_db, old_mode
        tmp.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=0,
                        help="只跑指定任务规模的单档；省略则跑 600/2000/5000")
    args = parser.parse_args()
    scales = [args.tasks] if args.tasks else [600, 2000, 5000]
    for n in scales:
        print(json.dumps(run(n), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
