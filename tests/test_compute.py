"""compute 口径单测：完成率舍入、多项目归属、近期日程的取消过滤与结束边界。

审计 2026-10-30 指出 compute.py 在单测层没有任何覆盖（只有 e2e 一处间接断言），
这里补上最小但能捕捉回归的用例。
"""
import unittest
from datetime import datetime

from app.portal import compute


def task(status="进行中", project_id="p", name="t"):
    """最小事项记录：字段与 normalize.tasks 产物对齐（只保留计算用到的键）。"""
    return {"id": name, "name": name, "status": status,
            "project_id": project_id, "_invalid": False}


class ProjectProgressTests(unittest.TestCase):
    """完成率：half-up 四舍五入 + 已取消/异常记录不进分母。"""

    def test_half_up_not_bankers_rounding(self):
        # 1/8 = 12.5：银行家舍入给 12，四舍五入给 13（审计 P3）
        tasks = [task("已完成")] + [task() for _ in range(7)]
        self.assertEqual(compute.project_progress(tasks, "p")["percent"], 13)

    def test_half_up_rounds_37_5_up(self):
        tasks = [task("已完成") for _ in range(3)] + [task() for _ in range(5)]
        self.assertEqual(compute.project_progress(tasks, "p")["percent"], 38)

    def test_half_up_rounds_2_5_up(self):
        tasks = [task("已完成")] + [task() for _ in range(39)]
        self.assertEqual(compute.project_progress(tasks, "p")["percent"], 3)

    def test_no_valid_tasks_returns_none(self):
        self.assertEqual(compute.project_progress([], "p"),
                         {"done": 0, "total": 0, "percent": None})

    def test_cancelled_excluded_from_denominator(self):
        tasks = [task("已完成"), task("已取消")]
        self.assertEqual(compute.project_progress(tasks, "p"),
                         {"done": 1, "total": 1, "percent": 100})

    def test_other_project_not_counted(self):
        tasks = [task("已完成", project_id="p1")]
        self.assertEqual(compute.project_progress(tasks, "p1")["total"], 1)
        self.assertEqual(compute.project_progress(tasks, "p2")["total"], 0)

    def test_invalid_record_excluded(self):
        bad = task("已完成")
        bad["_invalid"] = True
        self.assertEqual(compute.project_progress([bad], "p")["total"], 0)


class UpcomingScheduleTests(unittest.TestCase):
    """近期日程：已取消不出现；结束那一刻不算进行中。"""

    NOW = datetime(2026, 10, 30, 12, 0, tzinfo=compute.TZ)

    def at(self, hour, day=30):
        return int(datetime(2026, 10, day, hour, 0, tzinfo=compute.TZ).timestamp())

    def sched(self, rid, start, end=None, status="未开始", all_day=False):
        return {"id": rid, "name": rid, "status": status, "all_day": all_day,
                "start": {"ts": start, "precision": "dt"},
                "end": {"ts": end} if end else None,
                "_invalid": False}

    def ids(self, rows):
        return [r["id"] for r in compute.upcoming_schedule(rows, days=7, now=self.NOW)]

    def test_cancelled_excluded(self):
        rows = [self.sched("a", self.at(14), status="已取消"),
                self.sched("b", self.at(15))]
        self.assertEqual(self.ids(rows), ["b"])

    def test_ended_exactly_now_is_not_ongoing(self):
        # 10:00–12:00，正好 12:00 结束：不再算进行中，也不在未来窗口内
        rows = [self.sched("a", self.at(10), end=self.at(12))]
        self.assertEqual(self.ids(rows), [])

    def test_ongoing_before_end(self):
        rows = [self.sched("a", self.at(10), end=self.at(13))]
        out = compute.upcoming_schedule(rows, days=7, now=self.NOW)
        self.assertEqual([r["id"] for r in out], ["a"])
        self.assertTrue(out[0]["ongoing"])

    def test_past_week_excluded_future_included(self):
        rows = [self.sched("past", self.at(9, day=29)),
                self.sched("soon", self.at(14, day=31))]
        self.assertEqual(self.ids(rows), ["soon"])


if __name__ == "__main__":
    unittest.main()
