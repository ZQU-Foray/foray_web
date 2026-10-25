"""本地缓存库数据层与库结构迁移（临时库文件，不访问飞书）。

运行：在仓库根目录执行 python -m unittest tests.test_cache_store
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import config
from app.portal import cache_rules as R
from app.portal import cache_store as C
from app.portal import migrations as M
from app.portal import store as S


def make_v1_db(path, version="1"):
    """按版本 1 的结构造一个有快照数据的库（迁移前的生产库形状）。"""
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE snapshot(version INTEGER, table_name TEXT, record_id TEXT, "
              "data TEXT, PRIMARY KEY(version, table_name, record_id))")
    c.execute("CREATE TABLE quality(version INTEGER, table_name TEXT, record_id TEXT, "
              "issue TEXT)")
    c.executemany("INSERT INTO meta(key, value) VALUES(?, ?)",
                  [("schema_version", version), ("current_version", "3"),
                   ("counts", '{"tasks": 1}')])
    c.execute("INSERT INTO snapshot VALUES(3, 'tasks', 'recT1', ?)",
              (json.dumps({"id": "recT1", "name": "事项一"}, ensure_ascii=False),))
    c.commit()
    c.close()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DbCase(unittest.TestCase):
    """每个用例一个全新的临时库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "portal.db")
        self._old = config.PORTAL_DB
        S.close()
        config.PORTAL_DB = Path(self.db)

    def tearDown(self):
        S.close()
        config.PORTAL_DB = self._old
        self.tmp.cleanup()

    def mirror(self, table, rows, started=0.0, now=1.0):
        items = [{"record_id": rid, "fields": f, "last_modified_time": mod}
                 for rid, f, mod in rows]
        return C.replace_remote_tables({table: items}, started, now=now)


class SchemaTests(DbCase):
    def test_fresh_db_is_v2_with_cache_tables(self):
        self.assertEqual(S.get_meta("schema_version"), str(M.LATEST))
        names = {r[0] for r in S._db().execute("SELECT name FROM sqlite_master")}
        for t in M.V2_TABLES:
            self.assertIn(t, names)

    def test_v1_db_rejected_without_writes(self):
        make_v1_db(self.db)
        before = file_hash(self.db)
        with self.assertRaises(S.SchemaVersionError) as cm:
            S.get_meta("schema_version")
        self.assertEqual((cm.exception.found, cm.exception.expected), ("1", "2"))
        self.assertIn("portal_migrate.py", str(cm.exception))
        self.assertEqual(file_hash(self.db), before)

    def test_migrate_v1_keeps_snapshot(self):
        make_v1_db(self.db)
        conn = sqlite3.connect(self.db, isolation_level=None)
        self.assertEqual(M.migrate(conn), [1])
        self.assertEqual(M.migrate(conn), [])             # 再跑一次是空操作
        conn.close()
        snap = S.current()
        self.assertEqual(snap["version"], 3)
        self.assertEqual(snap["tables"]["tasks"][0]["name"], "事项一")
        self.assertEqual(C.backlog()["pending"], 0)

    def test_migrate_refuses_unknown_and_newer(self):
        for ver in ("99", "abc"):
            make_v1_db(self.db, version=ver)
            before = file_hash(self.db)
            conn = sqlite3.connect(self.db, isolation_level=None)
            with self.assertRaises(M.MigrationError):
                M.migrate(conn)
            conn.close()
            self.assertEqual(file_hash(self.db), before)
            os.remove(self.db)
        with self.assertRaises(M.MigrationError):
            M.path(0)


class MirrorTests(DbCase):
    def test_replace_and_digest_gate(self):
        self.assertTrue(self.mirror("tasks", [("recA", {"名称": "甲"}, 100)]))
        self.assertFalse(self.mirror("tasks", [("recA", {"名称": "甲"}, 100)]))
        self.assertEqual(C.get_remote("tasks", "recA")["fields"], {"名称": "甲"})
        self.assertTrue(self.mirror("tasks", [("recB", {"名称": "乙"}, 100)], started=5, now=6))
        self.assertIsNone(C.get_remote("tasks", "recA"))      # 飞书侧已删
        self.assertEqual(C.mirror_count("tasks"), 1)

    def test_keeps_rows_written_back_after_fetch_started(self):
        self.mirror("tasks", [("recA", {"名称": "甲"}, 100)], started=0, now=1)
        # 第 10 秒推送成功写回 recA，并新建 recNew
        S._db().execute("UPDATE remote_records SET fields=?, fetched_at=10 WHERE record_id='recA'",
                        (json.dumps({"名称": "甲（已推）"}, ensure_ascii=False),))
        S._db().execute("INSERT INTO remote_records VALUES('tasks','recNew','{}',NULL,NULL,10)")
        S._db().commit()
        # 第 8 秒开始的一轮拉取拿到的是旧数据：不应冲掉写回
        self.mirror("tasks", [("recA", {"名称": "甲"}, 100)], started=8, now=12)
        self.assertEqual(C.get_remote("tasks", "recA")["fields"], {"名称": "甲（已推）"})
        self.assertIsNotNone(C.get_remote("tasks", "recNew"))

    def test_does_not_regress_modified_time(self):
        self.mirror("tasks", [("recA", {"名称": "新"}, 200)], started=0, now=1)
        self.mirror("tasks", [("recA", {"名称": "旧"}, 150)], started=2, now=3)
        self.assertEqual(C.get_remote("tasks", "recA")["fields"], {"名称": "新"})


class OutboxTests(DbCase):
    def test_create_then_edit_then_delete_locally(self):
        lid = C.enqueue_create("tasks", {"名称": "新事项"}, by="ou_a", now=1)
        self.assertTrue(R.is_local_id(lid))
        C.enqueue_update("tasks", lid, {"状态": "进行中"}, now=2)
        ops = C.open_ops()
        self.assertEqual(len(ops), 1)                         # 并进了新建
        self.assertEqual(ops[0]["fields"], {"名称": "新事项", "状态": "进行中"})
        self.assertIsNone(C.enqueue_delete("tasks", lid, ["名称"], now=3))
        self.assertEqual(C.open_ops(), [])
        self.assertEqual(C.get_op(ops[0]["id"])["state"], "cancelled")

    def test_update_base_from_mirror_and_coalesce(self):
        self.mirror("tasks", [("recA", {"状态": "未开始", "名称": "甲"}, 100)])
        i1 = C.enqueue_update("tasks", "recA", {"状态": "进行中"}, now=2)
        i2 = C.enqueue_update("tasks", "recA", {"状态": "受阻", "名称": "甲2"}, now=3)
        self.assertEqual(i1, i2)
        op = C.get_op(i1)
        self.assertEqual(op["fields"], {"状态": "受阻", "名称": "甲2"})
        self.assertEqual(op["base"], {"状态": "未开始", "名称": "甲"})   # 保留最早基准

    def test_unknown_record(self):
        with self.assertRaises(C.RecordUnknown):
            C.enqueue_update("tasks", "recNope", {"状态": "进行中"})
        with self.assertRaises(C.RecordUnknown):
            C.enqueue_delete("tasks", "recNope", ["状态"])
        with self.assertRaises(ValueError):
            C.enqueue_update("tasks", "recNope", {})

    def test_base_includes_in_flight_push(self):
        """自己正在推的改动不能被当成飞书侧改动（否则下一次编辑必报假冲突）。"""
        self.mirror("tasks", [("recA", {"状态": "未开始"}, 100)])
        C.enqueue_update("tasks", "recA", {"状态": "进行中"}, now=2)
        first = C.claim_next(now=3)
        self.assertEqual(first["state"], "pushing")
        second = C.enqueue_update("tasks", "recA", {"状态": "已完成"}, now=4)
        self.assertNotEqual(first["id"], second)
        self.assertEqual(C.get_op(second)["base"], {"状态": "进行中"})
        # 第一条推完后飞书是"进行中"：第二条按规则直接推，无冲突
        C.complete_update(first["id"], {"状态": "进行中"}, now=5)
        nxt = C.claim_next(now=6)
        plan = R.plan_update(nxt["base"], nxt["fields"], C.get_remote("tasks", "recA")["fields"])
        self.assertEqual((plan.push, plan.conflicts), ({"状态": "已完成"}, []))

    def test_claim_order_per_record(self):
        self.mirror("tasks", [("recA", {"x": 1}, 1), ("recB", {"x": 1}, 1)])
        a1 = C.enqueue_update("tasks", "recA", {"x": 2}, now=1)
        b1 = C.enqueue_update("tasks", "recB", {"x": 2}, now=2)
        self.assertEqual(C.claim_next(now=3)["id"], a1)
        a2 = C.enqueue_update("tasks", "recA", {"x": 3}, now=4)   # a1 推送中，不合并
        self.assertEqual(C.claim_next(now=5)["id"], b1)
        self.assertIsNone(C.claim_next(now=6))                   # a2 被 a1 挡住
        C.mark_retry(a1, "network", delay=30, now=7)
        self.assertIsNone(C.claim_next(now=8))                   # a1 退避中，a2 仍须等
        self.assertEqual(C.claim_next(now=40)["id"], a1)
        C.complete_update(a1, {"x": 2}, now=41)
        self.assertEqual(C.claim_next(now=42)["id"], a2)

    def test_complete_create_maps_ids(self):
        lid = C.enqueue_create("tasks", {"名称": "新"}, now=1)
        op = C.claim_next(now=2)
        self.assertIsNone(op["remote_id"])
        later = C.enqueue_update("tasks", lid, {"状态": "进行中"}, now=3)   # 新建已在推，另起一条
        self.assertNotEqual(later, op["id"])
        C.complete_create(op["id"], "recNEW", now=4)
        self.assertEqual(C.resolve_id(lid), "recNEW")
        self.assertEqual(C.local_alias("recNEW"), lid)
        self.assertEqual(C.get_op(later)["record_id"], "recNEW")
        self.assertEqual(C.get_remote("tasks", "recNEW")["fields"], {"名称": "新"})
        nxt = C.claim_next(now=5)
        self.assertEqual((nxt["id"], nxt["remote_id"]), (later, "recNEW"))

    def test_conflicts_recorded_and_acked(self):
        self.mirror("tasks", [("recA", {"状态": "未开始", "名称": "甲"}, 1)])
        C.enqueue_update("tasks", "recA", {"状态": "进行中", "名称": "甲2"}, now=1)
        op = C.claim_next(now=2)
        remote = {"状态": "受阻", "名称": "甲"}
        plan = R.plan_update(op["base"], op["fields"], remote)
        after = dict(remote, **plan.push)                     # 推送后飞书上的样子
        C.complete_update(op["id"], plan.push, plan.conflicts, remote_fields=after, now=3)
        self.assertEqual(C.get_op(op["id"])["state"], "done")
        self.assertEqual(C.get_remote("tasks", "recA")["fields"], {"状态": "受阻", "名称": "甲2"})
        open_ = C.list_conflicts()
        self.assertEqual([(c["field"], c["local_value"], c["remote_value"]) for c in open_],
                         [("状态", "进行中", "受阻")])
        self.assertEqual(C.backlog()["open_conflicts"], 1)
        self.assertTrue(C.ack_conflict(open_[0]["id"], "ou_leader"))
        self.assertFalse(C.ack_conflict(open_[0]["id"], "ou_leader"))
        self.assertEqual(C.list_conflicts(), [])

    def test_all_fields_conflict_and_remote_deleted(self):
        self.mirror("tasks", [("recA", {"状态": "未开始"}, 1), ("recB", {"状态": "未开始"}, 1)])
        C.enqueue_update("tasks", "recA", {"状态": "进行中"}, now=1)
        C.enqueue_update("tasks", "recB", {"状态": "进行中"}, now=1)
        a, b = C.claim_next(now=2), C.claim_next(now=2)
        plan = R.plan_update(a["base"], a["fields"], {"状态": "受阻"})
        C.complete_update(a["id"], plan.push, plan.conflicts, now=3)
        self.assertEqual(C.get_op(a["id"])["state"], "conflict")
        gone = R.plan_update(b["base"], b["fields"], None)
        C.reject(b["id"], gone.conflicts, note="飞书已删除", remote_gone=True, now=3)
        self.assertEqual(C.get_op(b["id"])["state"], "conflict")
        self.assertIsNone(C.get_remote("tasks", "recB"))

    def test_delete_supersedes_pending_updates(self):
        self.mirror("tasks", [("recA", {"名称": "甲", "公式": 1}, 1)])
        up = C.enqueue_update("tasks", "recA", {"名称": "甲2"}, now=1)
        d = C.enqueue_delete("tasks", "recA", ["名称"], now=2)
        self.assertEqual(C.get_op(up)["state"], "cancelled")
        self.assertEqual(C.get_op(d)["base"], {"名称": "甲"})     # 作废的修改不进基准
        self.assertEqual(C.enqueue_delete("tasks", "recA", ["名称"], now=3), d)
        with self.assertRaises(C.RecordUnknown):
            C.enqueue_update("tasks", "recA", {"名称": "再改"}, now=4)
        op = C.claim_next(now=5)
        C.complete_delete(op["id"], now=6)
        self.assertIsNone(C.get_remote("tasks", "recA"))

    def test_increment_coalescing(self):
        self.mirror("members", [("recM", {"有效时长（小时）": "10"}, 1)])
        i = C.enqueue_update("members", "recM", {"最近签退": 1}, increments={"有效时长（小时）": 1.5})
        C.enqueue_update("members", "recM", {}, increments={"有效时长（小时）": 0.25})
        self.assertEqual(C.get_op(i)["increments"], {"有效时长（小时）": 1.75})
        C.enqueue_update("members", "recM", {"有效时长（小时）": 99})   # 改为直接写
        op = C.get_op(i)
        self.assertEqual(op["increments"], {})
        self.assertEqual(op["fields"]["有效时长（小时）"], 99)
        self.assertEqual(op["base"]["有效时长（小时）"], "10")
        with self.assertRaises(ValueError):
            C.enqueue_update("members", "recM", {"a": 1}, increments={"a": 1})

    def test_failure_paths_and_backlog(self):
        self.mirror("tasks", [("recA", {"x": 1}, 1), ("recB", {"x": 1}, 1)])
        a = C.enqueue_update("tasks", "recA", {"x": 2}, now=100)
        b = C.enqueue_update("tasks", "recB", {"x": 2}, now=101)
        C.claim_next(now=102)
        C.mark_failed(a, "code=1254045 字段不存在", now=103)
        C.claim_next(now=104)
        self.assertEqual(C.requeue_stuck(older_than=300, now=200), 0)
        self.assertEqual(C.requeue_stuck(older_than=60, now=200), 1)
        bl = C.backlog()
        self.assertEqual((bl["pending"], bl["pushing"], bl["failed"]), (1, 0, 1))
        self.assertEqual(bl["oldest_open_at"], 101)
        self.assertEqual(C.get_op(b)["attempts"], 1)
        self.assertIn("字段不存在", C.get_op(a)["last_error"])


class AttendanceTests(DbCase):
    def test_session_lifecycle_with_atomic_push(self):
        self.mirror("members", [("recM", {"有效时长（小时）": "10"}, 1)])
        s = C.start_session("ou_a", "recM", at=1000)
        self.assertEqual(s["state"], "open")
        with self.assertRaises(C.AttendanceStateError):
            C.start_session("ou_a", "recM", at=1001)
        done = C.close_session("ou_a", at=1000 + 5400, push={
            "table": "members", "record_id": "recM", "sets": {"最近签退": 6400000},
            "increments": {"有效时长（小时）": "{hours}"}, "by": "ou_a"})
        self.assertEqual((done["state"], done["hours"]), ("closed", 1.5))
        op = C.get_op(done["outbox_id"])
        self.assertEqual(op["increments"], {"有效时长（小时）": 1.5})
        self.assertIsNone(C.open_session("ou_a"))
        with self.assertRaises(C.AttendanceStateError):
            C.close_session("ou_a", at=9999)

    def test_close_rolls_back_when_push_fails(self):
        C.start_session("ou_b", "recUnknown", at=1000)
        with self.assertRaises(C.RecordUnknown):
            C.close_session("ou_b", at=2000, push={
                "table": "members", "record_id": "recUnknown", "sets": {"最近签退": 1}})
        self.assertEqual(C.open_session("ou_b")["state"], "open")   # 明细未被闭合
        self.assertEqual(C.open_ops(), [])

    def test_void_and_list(self):
        C.start_session("ou_c", "recM", at=1000)
        self.assertEqual(C.void_session("ou_c", at=2000, note="跨零点未签退")["state"], "void")
        self.assertIsNone(C.void_session("ou_c", at=2001))
        C.start_session("ou_c", "recM", at=3000)
        self.assertEqual([s["state"] for s in C.list_sessions("ou_c")], ["open", "void"])
        self.assertEqual(len(C.list_sessions(since=2500)), 1)


if __name__ == "__main__":
    unittest.main()
