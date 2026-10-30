"""考勤累计值口径测试（审计 P1-1 / P1-2 / P1-3 / P2-1 / P2-3）。

三块内容：
1. 增量推送的幂等护栏（CAS）：飞书当前值偏离入队基准时不再叠加增量；
2. 精度统一 4 位：合并两次增量与分两次推送结果一致；
3. 直写签退的基数取飞书实时值，读不到就 503 而不是拿旧快照覆盖工时。
"""
import time
import unittest

from app.portal import api, attendance, cache_rules


class IncrementCasTests(unittest.TestCase):
    """plan_update 对增量列的 CAS 与精度。"""

    def test_remote_matches_base_pushes_increment(self):
        plan = cache_rules.plan_update({attendance.COL_WORK: 22.0}, {},
                                       {attendance.COL_WORK: 22.0},
                                       {attendance.COL_WORK: 8.0})
        self.assertEqual(plan.push[attendance.COL_WORK], 30.0)
        self.assertEqual(plan.conflicts, [])

    def test_remote_drifted_records_conflict_without_push(self):
        # 上一次推送其实成功了（飞书已 30.0），重试读到 30.0 不等于基准 22.0
        plan = cache_rules.plan_update({attendance.COL_WORK: 22.0}, {},
                                       {attendance.COL_WORK: 30.0},
                                       {attendance.COL_WORK: 8.0})
        self.assertNotIn(attendance.COL_WORK, plan.push)
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.conflicts[0]["kind"], "increment_base_drift")

    def test_missing_base_keeps_legacy_behaviour(self):
        # 升级前入队的老数据没有增量基准，仍按旧口径累加，不能把队列卡死
        plan = cache_rules.plan_update({}, {}, {attendance.COL_WORK: 10.0},
                                       {attendance.COL_WORK: 2.5})
        self.assertEqual(plan.push[attendance.COL_WORK], 12.5)

    def test_precision_is_four_digits(self):
        self.assertEqual(cache_rules.apply_increment(0, 3.3333), 3.3333)
        first = cache_rules.apply_increment(0, 3.3333)
        second = cache_rules.apply_increment(first, 3.3333)
        self.assertEqual(second, 6.6666)

    def test_rollover_uses_four_digits(self):
        roll = attendance.rollover_updates(12.34568, 10.1234, "2026-10-19",
                                           "2026-10-26", "2026-10-19")
        self.assertEqual(roll[attendance.COL_WEEK_BASE], 12.3457)
        self.assertEqual(roll[attendance.COL_LAST_WEEK], 2.2223)


class _FakeClient:
    host = "192.168.1.9"


class _FakeRequest:
    def __init__(self):
        self.client = _FakeClient()
        self.state = type("St", (), {})()


class _FakeWriter:
    updates = []

    def update(self, table, record_id, cols):
        self.updates.append((table, record_id, dict(cols)))


class _FakeScheduler:
    def request_manual(self, who):
        return False, "未触发"


class _FakeSync:
    def scheduler(self):
        return _FakeScheduler()


class DirectCheckOutTests(unittest.TestCase):
    """直写签退：基数、实时候选与 503 早退。"""

    def setUp(self):
        self._saved = {}
        self._session_sec = 1800.0
        self._live = {attendance.COL_WORK: 10.52}
        self._snapshot_hours = 10.0

        def swap(name, value):
            self._saved[name] = getattr(api, name)
            setattr(api, name, value)

        swap("_attendance_gate", lambda request: ("ou_test", "测试"))
        swap("_my_member_record",
             lambda open_id: {"id": "rec_m1", "work_hours": self._snapshot_hours})
        swap("_attendance_cached", lambda: False)
        swap("_attendance_state",
             lambda m, open_id: (True, time.time() - self._session_sec, None))
        swap("_today_start_ts", lambda: 0.0)
        swap("_feishu_member_fields", lambda rid: self._live)
        swap("_week_cols_ready", lambda: False)
        swap("_audit", lambda *a, **k: None)
        self._saved_writer = api.portal_writer.FeishuWriter
        api.portal_writer.FeishuWriter = _FakeWriter
        _FakeWriter.updates = []
        self._saved_sync = api.sync
        api.sync = _FakeSync()
        self._saved_patch = api.cache_store.patch_mirror
        api.cache_store.patch_mirror = lambda *a, **k: None
        self._saved_changed = api.writes.changed
        api.writes.changed = lambda flag=True: None

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(api, name, value)
        api.portal_writer.FeishuWriter = self._saved_writer
        api.sync = self._saved_sync
        api.cache_store.patch_mirror = self._saved_patch
        api.writes.changed = self._saved_changed

    def test_base_comes_from_live_not_snapshot(self):
        out = api.attendance_check_out(_FakeRequest())
        # 10.52（飞书实时）+ 0.5h = 11.02；用旧快照 10.0 会算成 10.5 并覆盖前一次的 0.52h
        self.assertAlmostEqual(out["work_hours"], 11.02, places=4)
        sent = _FakeWriter.updates[-1][2]
        self.assertAlmostEqual(sent[attendance.COL_WORK], 11.02, places=4)

    def test_live_unavailable_returns_503(self):
        self._live = None
        with self.assertRaises(api.HTTPException) as ctx:
            api.attendance_check_out(_FakeRequest())
        self.assertEqual(ctx.exception.status_code, 503)

    def test_live_not_a_number_returns_503(self):
        self._live = {attendance.COL_WORK: "不是数字"}
        with self.assertRaises(api.HTTPException) as ctx:
            api.attendance_check_out(_FakeRequest())
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
