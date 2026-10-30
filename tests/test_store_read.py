"""读侧快照缓存与 status 合并（I4）：同版本只反序列化一次，发布即失效。

运行：在仓库根目录执行 python -m unittest tests.test_store_read
"""

import time
import unittest

from app import config
from app.portal import store as S
from tests.test_cache_store import DbCase


class ReadCacheTests(DbCase):
    def test_empty_db_returns_none(self):
        self.assertIsNone(S.current())

    def test_same_version_returns_cached_object(self):
        ver = S.next_version()
        S.write_candidate(ver, "tasks", [{"id": "recT1", "name": "甲"}], [])
        S.publish(ver, {"tasks": 1}, 0.1)
        first = S.current()
        second = S.current()
        # 同一版本命中同一对象：整库反序列化只发生一次
        self.assertIs(first, second)
        self.assertEqual(first["tables"]["tasks"][0]["name"], "甲")

    def test_new_version_invalidates_cache(self):
        ver = S.next_version()
        S.write_candidate(ver, "tasks", [{"id": "recT1", "name": "甲"}], [])
        S.publish(ver, {"tasks": 1}, 0.1)
        old = S.current()
        nxt = S.next_version()
        S.write_candidate(nxt, "tasks", [{"id": "recT1", "name": "乙"}], [])
        S.publish(nxt, {"tasks": 1}, 0.1)
        new = S.current()
        self.assertIsNot(old, new)
        self.assertEqual(new["tables"]["tasks"][0]["name"], "乙")

    def test_local_publish_also_invalidates(self):
        ver = S.next_version()
        S.write_candidate(ver, "tasks", [{"id": "recT1", "name": "甲"}], [])
        S.publish(ver, {"tasks": 1}, 0.1)
        old = S.current()
        nxt = S.next_version()
        S.write_candidate(nxt, "tasks", [{"id": "recT1", "name": "本地改"}], [])
        S.publish_local(nxt, {"tasks": 1})
        new = S.current()
        self.assertIsNot(old, new)
        self.assertEqual(new["tables"]["tasks"][0]["name"], "本地改")

    def test_quality_rows_are_included(self):
        ver = S.next_version()
        S.write_candidate(ver, "tasks", [{"id": "recT1", "name": "甲"}],
                          [("recT1", "缺少负责人")])
        S.publish(ver, {"tasks": 1}, 0.1)
        self.assertEqual(S.current()["quality"],
                         [{"table": "tasks", "record_id": "recT1",
                           "issue": "缺少负责人"}])


class StatusTests(DbCase):
    def test_status_shape_on_empty_db(self):
        st = S.status(now=10)
        self.assertEqual(set(st), {
            "snapshot_version", "has_snapshot", "last_attempt_at",
            "last_success_at", "last_duration_sec", "counts", "last_error", "stale"})
        self.assertFalse(st["has_snapshot"])
        self.assertIsNone(st["last_success_at"])

    def test_status_reads_published_values(self):
        ver = S.next_version()
        S.write_candidate(ver, "tasks", [{"id": "recT1", "name": "甲"}], [])
        S.publish(ver, {"tasks": 1}, 0.25)
        st = S.status(now=time.time())
        self.assertEqual(st["snapshot_version"], ver)
        self.assertEqual(st["counts"], {"tasks": 1})
        self.assertEqual(st["last_duration_sec"], 0.25)
        self.assertFalse(st["stale"])            # 刚发布不算过期

    def test_status_marks_stale_after_threshold(self):
        ver = S.next_version()
        S.write_candidate(ver, "tasks", [], [])
        S.publish(ver, {}, 0.1)
        far = time.time() + config.PORTAL_SYNC_STALE_SEC + 1
        self.assertTrue(S.status(now=far)["stale"])


if __name__ == "__main__":
    unittest.main()


class ConcurrencyTests(DbCase):
    def test_concurrent_read_and_publish_do_not_raise(self):
        """读缓存与发布并发：只读连接 + 读锁，不应出现 sqlite 错误或半截快照。"""
        import threading
        ver = S.next_version()
        S.write_candidate(ver, "tasks", [{"id": "recT1", "name": "甲"}], [])
        S.publish(ver, {"tasks": 1}, 0.1)
        errors = []

        def reader():
            for _ in range(60):
                try:
                    snap = S.current()
                    if snap is None or len(snap["tables"]["tasks"]) != 1:
                        errors.append("snapshot shape")
                except Exception as e:          # 线程里抛出的异常记下来断言
                    errors.append(repr(e))

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for i in range(3):
            v = S.next_version()
            S.write_candidate(v, "tasks", [{"id": "recT1", "name": "改%d" % i}], [])
            S.publish(v, {"tasks": 1}, 0.1)
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
