"""考勤页口径的边界验证（纯逻辑，不访问飞书）：周区间、换周结算、值日判定、页面数据。

运行：在仓库根目录执行 python -m unittest tests.test_attendance
"""

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from app.portal import attendance as A
from app.portal.normalize import Normalizer

TZ = ZoneInfo("Asia/Shanghai")


def ts(y, mo, d, h=0, mi=0, s=0):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp()


# 2026-10-19 是周一
MON = ts(2026, 10, 19)


class WeekBoundsTests(unittest.TestCase):
    def test_monday_midnight_starts_new_week(self):
        start, end, key, last = A.week_bounds(MON)
        self.assertEqual((start, end, key, last), (MON, ts(2026, 10, 26), "2026-10-19", "2026-10-12"))

    def test_sunday_last_second_is_previous_week(self):
        self.assertEqual(A.week_bounds(MON - 1)[2], "2026-10-12")
        self.assertEqual(A.week_bounds(ts(2026, 10, 25, 23, 59, 59))[2], "2026-10-19")

    def test_today_info_is_beijing(self):
        # 北京时间周三 00:30（UTC 仍是周二）
        self.assertEqual(A.today_info(ts(2026, 10, 21, 0, 30))[:2], ("2026-10-21", 3))
        self.assertEqual(A.today_info(ts(2026, 10, 25, 12))[1], 7)


class RolloverTests(unittest.TestCase):
    def test_same_week_skipped(self):
        self.assertIsNone(A.rollover_updates(40, 10, "2026-10-19", "2026-10-19", "2026-10-12"))

    def test_from_last_week(self):
        got = A.rollover_updates("52.5", 20, "2026-10-12", "2026-10-19", "2026-10-12")
        self.assertEqual(got, {"周初时长": 52.5, "结算周": "2026-10-19", "上周时长": 32.5})

    def test_never_settled_or_gap_week_has_unknown_last_week(self):
        for key, base in ((None, None), ("2026-09-28", 3)):
            got = A.rollover_updates(12, base, key, "2026-10-19", "2026-10-12")
            self.assertEqual(got, {"周初时长": 12, "结算周": "2026-10-19", "上周时长": None})

    def test_negative_clamped_and_blank_work(self):
        self.assertEqual(A.rollover_updates(5, 9, "2026-10-12", "2026-10-19", "2026-10-12")["上周时长"], 0)
        self.assertEqual(A.rollover_updates(None, None, None, "2026-10-19", "2026-10-12")["周初时长"], 0)


class DutyTests(unittest.TestCase):
    def test_parse_weekday(self):
        for raw, want in (("周一", 1), ("星期二", 2), ("礼拜三", 3), ("四", 4), ("5", 5), (6, 6),
                          ("Sunday", 7), ("mon", 1), ("周日", 7), ("星期天", 7), ("周天", 7),
                          (["周六"], 6), ([{"text": "周五"}], 5)):
            self.assertEqual(A.parse_weekday(raw), want, raw)
        for raw in ("", None, "八", "周八", 0, 8, "x", True):
            self.assertIsNone(A.parse_weekday(raw), raw)

    def test_split_names(self):
        self.assertEqual(A.split_names("张三、李四, 王五 /赵六"), ["张三", "李四", "王五", "赵六"])
        self.assertEqual(A.split_names([{"text": "甲、乙"}]), ["甲", "乙"])
        self.assertEqual(A.split_names(None), [])

    def rows(self):
        return [
            {"id": "w3", "date": None, "weekday": 3, "people": [{"id": "a", "name": "甲"}], "names": []},
            {"id": "w3b", "date": None, "weekday": 3, "people": [{"id": "a", "name": "甲"}],
             "names": ["丁"]},
            {"id": "w4", "date": None, "weekday": 4, "people": [{"id": "b", "name": "乙"}], "names": []},
            {"id": "d1", "date": "2026-10-22", "weekday": None, "people": [{"id": "c", "name": "丙"}],
             "names": []},
            {"id": "bad", "date": None, "weekday": 3, "people": [{"id": "x", "name": "坏"}],
             "names": [], "_invalid": True},
        ]

    def test_weekday_rotation_dedup_and_invalid_skipped(self):
        people, src = A.duty_today(self.rows(), "2026-10-21", 3)
        self.assertEqual(src, "weekday")
        self.assertEqual(people, [{"id": "a", "name": "甲"}, {"id": None, "name": "丁"}])

    def test_date_row_overrides_weekday(self):
        people, src = A.duty_today(self.rows(), "2026-10-22", 4)
        self.assertEqual((src, people), ("date", [{"id": "c", "name": "丙"}]))

    def test_no_duty(self):
        self.assertEqual(A.duty_today(self.rows(), "2026-10-24", 6), ([], None))


def member(oid, name, **kw):
    return {"id": "r" + oid, "member": {"id": oid, "name": name}, **kw}


class PageDataTests(unittest.TestCase):
    NOW = ts(2026, 10, 21, 15)        # 周三

    def test_threshold_strictly_greater_and_formula_priority(self):
        ms = [member("a", "甲", week_key="2026-10-19", week_hours=30),
              member("b", "乙", week_key="2026-10-19", week_hours=30.01),
              member("c", "丙", week_key="2026-10-19", week_hours=12, week_ok="达标")]
        got = {r["member"]["id"]: r for r in A.page_data(ms, [], self.NOW, week_ready=True)["members"]}
        self.assertIs(got["a"]["week_ok"], False)        # 恰好 30 不算达标
        self.assertIs(got["b"]["week_ok"], True)
        self.assertIs(got["c"]["week_ok"], True)         # 以飞书公式结果为准

    def test_unsettled_rows_are_pending_not_cumulative(self):
        ms = [member("a", "甲", work_hours=90, week_key="2026-10-12", week_hours=88, last_week_hours=31),
              member("b", "乙", work_hours=120, week_key=None, week_hours=120)]
        d = A.page_data(ms, [], self.NOW, week_ready=True)
        for r in d["members"]:
            self.assertTrue(r["pending"])
            self.assertIsNone(r["week_hours"])
            self.assertIsNone(r["last_week_hours"])
        self.assertEqual(d["summary"]["pending"], 2)

    def test_never_checked_out_counts_as_zero(self):
        r = A.page_data([member("c", "丙")], [], self.NOW, week_ready=True)["members"][0]
        self.assertEqual((r["week_hours"], r["week_ok"], r["pending"], r["last_week_hours"]),
                         (0, False, False, None))

    def test_not_configured(self):
        d = A.page_data([member("a", "甲", week_key="2026-10-19", week_hours=40)], [], self.NOW)
        self.assertFalse(d["configured"]["week"])
        self.assertIsNone(d["members"][0]["week_hours"])
        self.assertFalse(d["members"][0]["pending"])

    def test_checked_in_snapshot_and_live(self):
        today9 = ts(2026, 10, 21, 9)
        ms = [member("a", "甲", last_check_in={"ts": today9}),
              member("b", "乙", last_check_in={"ts": today9}, last_check_out={"ts": today9 + 3600}),
              member("c", "丙", last_check_in={"ts": ts(2026, 10, 20, 9)}),   # 昨天未签退：已作废
              member("d", "丁", last_check_in={"ts": today9})]
        got = {r["member"]["id"]: r["checked_in"]
               for r in A.page_data(ms, [], self.NOW, live={"d": None})["members"]}
        self.assertEqual(got, {"a": True, "b": False, "c": False, "d": False})

    def test_on_duty_by_id_and_name_and_sorting(self):
        ms = [member("a", "甲", week_key="2026-10-19", week_hours=5),
              member("b", "乙", week_key="2026-10-19", week_hours=35),
              member("c", "丙")]
        duty = [{"id": "w", "date": None, "weekday": 3, "people": [{"id": "a", "name": "甲"}],
                 "names": ["丙", "外援"]}]
        d = A.page_data(ms, duty, self.NOW, week_ready=True, duty_ready=True)
        self.assertEqual([r["member"]["id"] for r in d["members"]], ["b", "a", "c"])
        self.assertEqual({r["member"]["id"]: r["on_duty"] for r in d["members"]},
                         {"a": True, "b": False, "c": True})
        self.assertEqual([(p["name"], p["in_members"]) for p in d["duty"]["people"]],
                         [("甲", True), ("丙", True), ("外援", False)])
        self.assertEqual(d["duty"]["source"], "weekday")
        self.assertEqual(d["today"]["label"], "周三")


class NormalizeTests(unittest.TestCase):
    def test_duty_rows(self):
        n = Normalizer({})
        day = int(ts(2026, 10, 22) * 1000)     # 飞书日期列：当地零点毫秒数
        out, issues = n.duty([
            {"record_id": "1", "fields": {"date": day, "people": [{"id": "a", "name": "甲"}]}},
            {"record_id": "2", "fields": {"weekday": "星期三", "people": "乙、丙"}},
            {"record_id": "3", "fields": {}},                                    # 空白行跳过
            {"record_id": "4", "fields": {"people": [{"id": "a", "name": "甲"}]}},
            {"record_id": "5", "fields": {"weekday": "周八", "people": "丁"}},
            {"record_id": "6", "fields": {"weekday": "周一"}},
        ])
        by = {r["id"]: r for r in out}
        self.assertEqual(by["1"]["date"], "2026-10-22")
        self.assertEqual((by["2"]["weekday"], by["2"]["names"]), (3, ["乙", "丙"]))
        self.assertNotIn("3", by)
        self.assertEqual(dict(issues), {"4": "缺日期或星期", "5": "星期无法识别", "6": "缺值日人员"})

    def test_member_week_fields(self):
        out, _ = Normalizer({}).members([{"record_id": "m", "fields": {
            "member": [{"id": "a", "name": "甲"}], "work_hours": "40.5", "week_base": "10",
            "week_key": [{"type": "text", "text": "2026-10-19"}],
            "week_hours": [{"type": "text", "text": "30.5"}], "week_ok": [{"type": "text", "text": "达标"}],
            "last_week_hours": "28"}}])
        m = out[0]
        self.assertEqual((m["week_key"], m["week_hours"], m["week_ok"], m["last_week_hours"], m["week_base"]),
                         ("2026-10-19", 30.5, "达标", 28, 10))


class LeaveTests(unittest.TestCase):
    """请假：本周请假没达标 / 没签到显示请假，达标照常；上周请假只影响「上周」一栏；下周自动恢复。"""
    NOW = ts(2026, 10, 21, 15)        # 周三，本周键 2026-10-19，上周键 2026-10-12

    def test_leave_weeks_parse(self):
        self.assertEqual(A.leave_weeks("2026-10-12, 2026-10-19、坏值"), {"2026-10-12", "2026-10-19"})
        self.assertEqual(A.leave_weeks(None), set())

    def test_leave_value_add_remove_prune(self):
        self.assertEqual(A.leave_value("", True, "2026-10-19", "2026-10-12"), "2026-10-19")
        self.assertEqual(A.leave_value("2026-10-05,2026-10-12", True, "2026-10-19", "2026-10-12"),
                         "2026-10-12,2026-10-19")              # 更早的清掉
        self.assertEqual(A.leave_value("2026-10-12,2026-10-19", False, "2026-10-19", "2026-10-12"),
                         "2026-10-12")                         # 只取消本周
        self.assertEqual(A.leave_value("2026-10-19", False, "2026-10-19", "2026-10-12"), "")

    def test_page_statuses(self):
        ms = [member("a", "甲", week_key="2026-10-19", week_hours=12, leave_weeks="2026-10-19"),
              member("b", "乙", week_key="2026-10-19", week_hours=35, leave_weeks="2026-10-19"),
              member("c", "丙", week_key="2026-10-19", week_hours=10, last_week_hours=20,
                     leave_weeks="2026-10-12"),
              member("d", "丁", week_key="2026-10-19", week_hours=40)]
        d = A.page_data(ms, [], self.NOW, week_ready=True, leave_ready=True)
        by = {r["member"]["id"]: r for r in d["members"]}
        self.assertEqual((by["a"]["leave"], by["a"]["on_leave"], by["a"]["week_ok"]), (True, True, False))
        self.assertEqual((by["b"]["leave"], by["b"]["on_leave"]), (False, True))   # 达标照常显示达标
        self.assertEqual((by["c"]["leave"], by["c"]["last_week_leave"]), (False, True))
        self.assertFalse(by["d"]["leave"] or by["d"]["last_week_leave"])
        self.assertEqual((d["summary"]["met"], d["summary"]["leave"], d["summary"]["required"]),
                         (2, 1, 3))
        self.assertTrue(d["configured"]["leave"])

    def test_next_week_recovers(self):
        ms = [member("a", "甲", week_key="2026-10-26", week_hours=3, leave_weeks="2026-10-19")]
        r = A.page_data(ms, [], ts(2026, 10, 27, 9), week_ready=True)["members"][0]
        self.assertEqual((r["on_leave"], r["leave"]), (False, False))

    def test_pending_row_on_leave_shows_leave(self):
        ms = [member("a", "甲", work_hours=5, week_key="2026-10-12", leave_weeks="2026-10-19")]
        r = A.page_data(ms, [], self.NOW, week_ready=True)["members"][0]
        self.assertEqual((r["pending"], r["leave"]), (True, True))

    def test_normalize_leave_column(self):
        out, _ = Normalizer({}).members([{"record_id": "m", "fields": {
            "member": [{"id": "a", "name": "甲"}],
            "leave_week": [{"type": "text", "text": "2026-10-19"}]}}])
        self.assertEqual(out[0]["leave_weeks"], "2026-10-19")


class RolloverAllTests(unittest.TestCase):
    """api.weekly_rollover_all：只写结算周不是本周的行；空行跳过；部分失败抛异常以便重试。"""

    CFG = {"tables": {"members": {"table_id": "tblM", "fields": {
        "member": "f1", "work_hours": "f2", "week_base": "f3", "week_key": "f4",
        "last_week_hours": "f5", "week_hours": "f6"}}}}
    NAME2ID = {"文本": "f1", "有效时长（小时）": "f2", "周初时长": "f3", "结算周": "f4",
               "上周时长": "f5", "本周时长": "f6"}
    ITEMS = [
        {"record_id": "done", "fields": {"文本": [{"id": "a"}], "有效时长（小时）": "50",
                                         "周初时长": "40", "结算周": "2026-10-19"}},
        {"record_id": "last", "fields": {"文本": [{"id": "b"}], "有效时长（小时）": "70.25",
                                         "周初时长": "35", "结算周": [{"text": "2026-10-12"}]}},
        {"record_id": "new", "fields": {"文本": [{"id": "c"}], "有效时长（小时）": "8"}},
        {"record_id": "blank", "fields": {"有效时长（小时）": "3"}},
    ]

    def test_only_unsettled_rows_written(self):
        from unittest import mock
        from app.portal import api
        writer = mock.Mock()
        FR = api.sync.FeishuReader
        with mock.patch.object(api.sync, "_load_sync_cfg", return_value=self.CFG), \
                mock.patch.object(FR, "__init__", lambda s, c: None), \
                mock.patch.object(FR, "fields", lambda s, t: self.NAME2ID), \
                mock.patch.object(FR, "records", lambda s, t: self.ITEMS), \
                mock.patch.object(api.portal_writer, "FeishuWriter", return_value=writer), \
                mock.patch.object(api, "_audit"), mock.patch.object(api.sync, "scheduler"), \
                mock.patch.object(api.attendance, "week_bounds",
                                  return_value=(MON, MON + 7 * 86400, "2026-10-19", "2026-10-12")):
            done = api.weekly_rollover_all()
        self.assertEqual(done, ["last", "new"])
        self.assertEqual(writer.update.call_args_list, [
            mock.call("members", "last", {"周初时长": 70.25, "结算周": "2026-10-19", "上周时长": 35.25}),
            mock.call("members", "new", {"周初时长": 8, "结算周": "2026-10-19", "上周时长": None}),
        ])

    def test_partial_failure_raises_and_unmapped_columns_noop(self):
        from unittest import mock
        from app.portal import api
        writer = mock.Mock()
        writer.update.side_effect = [RuntimeError("飞书 500"), None]
        FR = api.sync.FeishuReader
        common = [mock.patch.object(FR, "__init__", lambda s, c: None),
                  mock.patch.object(FR, "fields", lambda s, t: self.NAME2ID),
                  mock.patch.object(FR, "records", lambda s, t: self.ITEMS),
                  mock.patch.object(api.portal_writer, "FeishuWriter", return_value=writer),
                  mock.patch.object(api, "_audit"), mock.patch.object(api.sync, "scheduler"),
                  # 本周必须固定：不 patch 时断言会随"今天是第几周"漂移——
                  # ITEMS 里 done 行的结算周写死 2026-10-19，到下一周它就变成待结算行，
                  # 应写行数从 2 变 3，用例假失败（原本只在 2026-10-19 那周通过）
                  mock.patch.object(api.attendance, "week_bounds",
                                    return_value=(MON, MON + 7 * 86400,
                                                  "2026-10-19", "2026-10-12"))]
        for p in common:
            p.start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(api.sync, "_load_sync_cfg", return_value=self.CFG):
            with self.assertRaises(RuntimeError):
                api.weekly_rollover_all()
        self.assertEqual(writer.update.call_count, 2)          # 失败一行后仍继续写其余行
        # 周结算列没回填到配置：什么也不写
        cfg = {"tables": {"members": {"table_id": "tblM", "fields": {"member": "f1", "work_hours": "f2"}}}}
        writer.reset_mock()
        with mock.patch.object(api.sync, "_load_sync_cfg", return_value=cfg):
            self.assertEqual(api.weekly_rollover_all(), [])
        writer.update.assert_not_called()


class BoolJudgeTests(unittest.TestCase):
    """达标判定：公式列优先；缺公式列时按 30 小时阈值，且先按对外精度量化。"""

    def test_formula_column_wins(self):
        from app.portal import attendance
        self.assertTrue(attendance._bool_judge("达标", 0))
        self.assertFalse(attendance._bool_judge("未达标", 99))

    def test_none_hours_returns_none(self):
        from app.portal import attendance
        self.assertIsNone(attendance._bool_judge("", None))

    def test_float_dust_does_not_flip_verdict(self):
        # 浮点相减会得到 30.000000000000004 / 29.999999999999996：
        # 量化到对外精度后都等于 30，一律判未达标（>，不含等于）
        from app.portal import attendance
        self.assertFalse(attendance._bool_judge("", 30.000000000000004))
        self.assertFalse(attendance._bool_judge("", 29.999999999999996))

    def test_above_threshold_passes(self):
        from app.portal import attendance
        self.assertTrue(attendance._bool_judge("", 30.01))
        self.assertFalse(attendance._bool_judge("", 29.99))


if __name__ == "__main__":
    unittest.main()
