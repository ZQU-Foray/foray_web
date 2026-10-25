"""成员统计口径的边界验证；不向真实飞书写入测试记录。"""

import unittest
import datetime as dt
from scripts.feishu_member_sync import task_metrics, attendance_metrics, merged_seconds, sync, COUNTS, MEMBERS, TASKS, ApiError


class MemberStatisticsTests(unittest.TestCase):
    def test_task_deadlines_and_shared_owner(self):
        def task(**fields):
            return {"fields": {"负责人": [{"id": "a"}], **fields}}
        rows = [task(负责人=[{"id": "a"}, {"id": "b"}], 计划截止=100, 提交验收时间=100, 验收结论="通过"),
                task(计划截止=100, 提交验收时间=101, 验收结论="通过"),
                task(计划截止=100), task(计划截止=300), task(验收结论="取消"), task()]
        got = task_metrics(rows, "a", 200)
        self.assertEqual(list(got.values()), [5, 2, 3, 1, 1, 0, 1])
        self.assertEqual(task_metrics(rows, "b", 200)["准时完成数"], 1)
        self.assertEqual(task_metrics(rows, "a", 200, start=101)["到期事项数"], 0)
        self.assertEqual(task_metrics(rows, "a", 200, end=99)["到期事项数"], 0)

    def test_missing_delivery_is_not_on_time(self):
        result = task_metrics([{"fields": {"负责人": [{"id": "a"}], "计划截止": 100, "验收结论": "通过"}}], "a", 200)
        self.assertEqual(result["准时完成数"], 0)
        self.assertEqual(result["交付时间待补数"], 1)

    def test_overlapping_intervals(self):
        self.assertEqual(merged_seconds([(10, 20), (10, 20), (15, 25), (30, 40)]), 25)
        self.assertEqual(merged_seconds([]), 0)

    def test_attendance_incomplete_duplicate_and_cross_midnight(self):
        pair = {"check_in_record": {"check_time": "82800"}, "check_out_record": {"check_time": "90000"}, "check_in_result": "Normal", "check_out_result": "Normal"}
        result = {"day": 20260901, "result_id": "one", "records": [pair, {"check_in_record": {"check_time": "100000"}, "check_out_result": "Lack"}]}
        got = attendance_metrics([result, result], 110000)
        self.assertEqual(got["有效时长（小时）"], 2)
        self.assertEqual(got["异常考勤数"], 1)
        self.assertEqual(set(got), {"最近签到", "有效时长（小时）", "异常考勤数"})

    def test_pending_and_synthetic_punches_excluded(self):
        pair = {"check_in_record": {"check_time": "100", "check_result": "PendingApproval"}, "check_out_record": {"check_time": "200", "type": 4}, "check_in_result": "Normal", "check_out_result": "Normal"}
        got = attendance_metrics([{"day": 20260901, "records": [pair]}], 300)
        self.assertEqual(got["有效时长（小时）"], 0)
        self.assertIsNone(got["最近签到"])

    def test_attendance_failure_preserves_previous_snapshot(self):
        """考勤失败时只更新同步元数据，不覆盖上次成功的考勤快照；且不再依赖按日明细表。"""
        class FakeClient:
            def __init__(self):
                self.updates = []
            def items(self, path):
                if path.endswith("/fields"):
                    if MEMBERS in path:
                        return ([{"field_name": "成员", "type": 11, "is_primary": True}]
                                + [{"field_name": n, "type": 2} for n in COUNTS]
                                + [{"field_name": "同步时间", "type": 5},
                                   {"field_name": "同步说明", "type": 1}])
                    return [{"field_name": n, "type": t} for n, t in
                            [("负责人", 11), ("验收结论", 3), ("计划截止", 5), ("提交验收时间", 5)]]
                if MEMBERS in path:
                    return [{"record_id": "member", "fields": {"成员": [{"id": "open"}], "有效时长（小时）": 12}}]
                return []
            def call(self, method, path, body=None):
                if "/contact/" in path:
                    return {"data": {"user": {"user_id": "user"}}}
                if "/attendance/" in path:
                    raise ApiError("无权限")
                self.updates.append(body["fields"])
                return {}
        client = FakeClient()
        report = sync(client, dt.date(2026, 9, 1), dt.date(2026, 9, 2))
        self.assertEqual(report["failures"], 1)
        fields = client.updates[-1]
        self.assertEqual(set(fields), set(COUNTS) | {"同步时间", "同步说明"})
        self.assertIn("考勤：同步失败", fields["同步说明"])
        self.assertNotIn("有效时长（小时）", fields)


if __name__ == "__main__":
    unittest.main()
