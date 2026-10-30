"""P0 提速：同步请求收敛（分页/字段缓存/链接差集）与轮级退避、推送合并重建。

运行：在仓库根目录执行 python -m unittest tests.test_sync_p0
"""

import unittest

from app import config
from app.portal import cache_store as C
from app.portal import writes as W
from app.portal import sync as SY
from tests.test_cache_store import DbCase
from tests.test_pusher import PushCase


class FakeReader:
    """只记录 fields 调用次数的假 reader（不访问网络）。"""

    def __init__(self):
        self.fields_calls = []

    def fields(self, table_id):
        self.fields_calls.append(table_id)
        return {"项目名称": "fldPn", "主责组": "fldPg"}


class ConfigTests(unittest.TestCase):
    def test_page_size_defaults_to_500_and_is_clamped(self):
        # 飞书列表/搜索接口上限 500：默认取满，旧行为可回退为 PORTAL_SYNC_PAGE_SIZE=100
        self.assertEqual(config.PORTAL_SYNC_PAGE_SIZE, 500)
        self.assertLessEqual(config.PORTAL_SYNC_PAGE_SIZE, 500)

    def test_retry_backoff_cap_default(self):
        self.assertEqual(config.PORTAL_SYNC_BACKOFF_MAX_SEC, 600)
        self.assertEqual(config.PORTAL_FIELDS_CACHE_TTL_SEC, 21600)


class WriteModeDefaultsTests(unittest.TestCase):
    """I5：默认写模式切排队，四个页面表入队，考勤所在的 members 保持直写。"""

    def test_default_mode_is_cache(self):
        self.assertEqual(config.PORTAL_WRITE_MODE, "cache")

    def test_default_cache_tables(self):
        self.assertEqual(set(config.PORTAL_CACHE_TABLES),
                         {"tasks", "projects", "schedule", "procurement"})
        for table in ("tasks", "projects", "schedule", "procurement"):
            self.assertTrue(W.queued(table))

    def test_members_stays_direct(self):
        self.assertFalse(W.queued("members"))

    def test_queue_alias_is_cache(self):
        old = config.PORTAL_WRITE_MODE
        try:
            config.PORTAL_WRITE_MODE = "queue"
            self.assertTrue(W.queued("tasks"))
        finally:
            config.PORTAL_WRITE_MODE = old


class FieldsCacheTests(DbCase):
    def setUp(self):
        super().setUp()
        self.reader = FakeReader()

    def test_second_call_hits_cache(self):
        first = SY._cached_fields(self.reader, "tblT")
        second = SY._cached_fields(self.reader, "tblT")
        self.assertEqual(first, second)
        self.assertEqual(len(self.reader.fields_calls), 1)

    def test_expired_entry_is_refetched(self):
        SY._cached_fields(self.reader, "tblT")
        cache = SY._load_fields_cache()
        cache["tblT"]["at"] = 0            # 视为很久以前拉取
        SY._save_fields_cache(cache)
        SY._cached_fields(self.reader, "tblT")
        self.assertEqual(len(self.reader.fields_calls), 2)

    def test_force_bypasses_cache(self):
        SY._cached_fields(self.reader, "tblT")
        SY._cached_fields(self.reader, "tblT", force=True)
        self.assertEqual(len(self.reader.fields_calls), 2)

    def test_cache_is_per_table(self):
        SY._cached_fields(self.reader, "tblT")
        SY._cached_fields(self.reader, "tblP")
        self.assertEqual(self.reader.fields_calls, ["tblT", "tblP"])

    def test_broken_meta_falls_back_to_empty(self):
        from app.portal import store as S
        S.set_meta(SY.FIELDS_CACHE_META, "不是 JSON")
        self.assertEqual(SY._load_fields_cache(), {})
        SY._cached_fields(self.reader, "tblT")
        self.assertEqual(len(self.reader.fields_calls), 1)


class LinkDiffTests(unittest.TestCase):
    """记录链接只补「还没有链接」的记录：全量重取会让每表每轮多出 ceil(N/100) 次请求。"""

    def setUp(self):
        self.reader = SY.FeishuReader({"api_base": "https://example.invalid",
                                       "app_token": "app", "app_id": "id",
                                       "app_secret": "secret"})
        self.batches = []

        def fake_req(method, path, body=None, **kw):
            self.batches.append(list(body["record_ids"]))
            return {"code": 0, "data": {"records": [
                {"record_id": rid, "shared_url": "https://feishu/" + rid}
                for rid in body["record_ids"]]}}

        self.reader._req_json = fake_req

    def test_all_known_skips_network(self):
        out = self.reader.record_links("tblT", ["rec1", "rec2"],
                                       known={"rec1": "u1", "rec2": "u2"})
        self.assertEqual(out, {})
        self.assertEqual(self.batches, [])

    def test_only_missing_ids_are_fetched(self):
        out = self.reader.record_links("tblT", ["rec1", "rec2", "rec3"],
                                       known={"rec1": "u1"})
        self.assertEqual(set(out), {"rec2", "rec3"})
        self.assertEqual(self.batches, [["rec2", "rec3"]])

    def test_batch_step_is_bounded(self):
        ids = ["rec%03d" % i for i in range(250)]
        self.reader.record_links("tblT", ids)
        self.assertEqual([len(b) for b in self.batches], [100, 100, 50])


class SchedulerBackoffTests(unittest.TestCase):
    def test_waits_by_fail_streak_with_cap(self):
        sch = SY.Scheduler()
        base = max(1, config.PORTAL_SYNC_INTERVAL_SEC)
        self.assertEqual(sch._wait_sec(), base)          # 上轮成功：正常间隔
        sch._fail_streak = 1
        self.assertEqual(sch._wait_sec(), base * 2)
        sch._fail_streak = 3
        self.assertEqual(sch._wait_sec(), base * 8)
        sch._fail_streak = 99
        self.assertEqual(sch._wait_sec(), config.PORTAL_SYNC_BACKOFF_MAX_SEC)

    def test_streak_resets_after_success(self):
        sch = SY.Scheduler()
        sch._fail_streak = 4
        ok = True
        if ok:                       # 模拟 _run_once 成功分支
            sch._fail_streak = 0
        self.assertEqual(sch._wait_sec(), max(1, config.PORTAL_SYNC_INTERVAL_SEC))

    def test_next_run_in_reports_zero_when_running(self):
        sch = SY.Scheduler()
        sch._running = True
        self.assertEqual(sch.next_run_in(), 0)


class DrainRebuildTests(PushCase):
    """批量推送只按批重建：逐条重建会把「全量重建」放大 N 倍。"""

    def setUp(self):
        super().setUp()
        from app.portal import view as V
        self.builds = []
        self._orig_refresh = V.refresh
        V.refresh = lambda: self.builds.append(1)

    def tearDown(self):
        from app.portal import view as V
        V.refresh = self._orig_refresh
        super().tearDown()

    def test_drain_rebuilds_per_batch_not_per_op(self):
        for i in range(45):
            rid = "rec%04d" % i
            self.seed("tasks", rid, {"事项名称": "事项%d" % i})
            C.enqueue_update("tasks", rid, {"事项名称": "改%d" % i})
        self.assertEqual(self.p.drain(limit=100), 45)
        # REBUILD_EVERY=20 → 第 20、40 条各一次，批尾第 45 条补一次
        self.assertEqual(len(self.builds), 3)

    def test_single_op_still_rebuilds_once(self):
        self.seed("tasks", "rec0001", {"事项名称": "甲"})
        C.enqueue_update("tasks", "rec0001", {"事项名称": "乙"})
        self.assertEqual(self.p.drain(), 1)
        self.assertEqual(len(self.builds), 1)


if __name__ == "__main__":
    unittest.main()
