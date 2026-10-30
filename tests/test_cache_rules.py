"""本地缓存库冲突规则（纯逻辑，不访问数据库与飞书）。

运行：在仓库根目录执行 python -m unittest tests.test_cache_rules
"""

import unittest

from app.portal import cache_rules as R


class CanonTests(unittest.TestCase):
    """读形状与写形状归一后应相等；真正不同的值不能被归一成相等。"""

    def test_people_read_vs_write(self):
        read = [{"id": "ou_b", "name": "乙", "email": "b@x"}, {"id": "ou_a", "name": "甲"}]
        self.assertTrue(R.same(read, [{"id": "ou_a"}, {"id": "ou_b"}]))
        self.assertFalse(R.same(read, [{"id": "ou_a"}]))

    def test_link_read_vs_write(self):
        read = [{"record_ids": ["recP1"], "table_id": "tblX", "text": "项目甲", "type": "text"}]
        self.assertTrue(R.same(read, ["recP1"]))
        self.assertTrue(R.same([{"link_record_ids": ["recP1"]}], ["recP1"]))
        self.assertFalse(R.same(read, ["recP2"]))

    def test_attachments_compare_by_token(self):
        read = [{"name": "a.pdf", "file_token": "t1", "url": "https://tmp"}]
        self.assertTrue(R.same(read, [{"file_token": "t1"}]))
        self.assertFalse(R.same(read, [{"file_token": "t1"}, {"file_token": "t2"}]))

    def test_numbers_string_vs_number(self):
        self.assertTrue(R.same("12", 12))
        self.assertTrue(R.same("12.50", 12.5))
        self.assertTrue(R.same(0.1 + 0.2, 0.3))
        self.assertFalse(R.same("12", 13))
        self.assertFalse(R.same("0", None))           # 0 是有值，不等于空

    def test_text_segments_vs_plain(self):
        self.assertTrue(R.same([{"text": "你好", "type": "text"}, {"text": "世界", "type": "text"}],
                               "你好世界"))
        self.assertTrue(R.same("  前后空白 ", "前后空白"))
        self.assertFalse(R.same("甲", "乙"))

    def test_empty_values_are_equal(self):
        for v in (None, "", "   ", [], {}, False):
            self.assertIsNone(R.canon(v), v)
        self.assertFalse(R.same(True, None))

    def test_multi_select_order_does_not_matter(self):
        self.assertTrue(R.same(["机械", "电控"], ["电控", "机械"]))
        self.assertFalse(R.same(["机械"], ["机械", "电控"]))

    def test_url_field(self):
        self.assertTrue(R.same({"link": "https://a.cn", "text": "链接"}, {"link": "https://a.cn"}))

    def test_bool_not_equal_to_number_one(self):
        self.assertFalse(R.same(True, 1))


class PlanUpdateTests(unittest.TestCase):
    def test_push_when_remote_untouched(self):
        p = R.plan_update({"状态": "未开始"}, {"状态": "进行中"}, {"状态": "未开始"})
        self.assertEqual((p.push, p.conflicts, p.unchanged), ({"状态": "进行中"}, [], []))

    def test_skip_when_remote_already_equal(self):
        p = R.plan_update({"状态": "未开始"}, {"状态": "进行中"}, {"状态": "进行中"})
        self.assertEqual((p.push, p.unchanged), ({}, ["状态"]))

    def test_conflict_when_both_changed(self):
        p = R.plan_update({"状态": "未开始"}, {"状态": "进行中"}, {"状态": "受阻"})
        self.assertEqual(p.push, {})
        self.assertEqual(p.conflicts, [{"kind": "field", "field": "状态", "base": "未开始",
                                        "local": "进行中", "remote": "受阻"}])

    def test_field_level_merge(self):
        # 飞书改了 A，网页改了 B：B 照推，A 不受影响
        p = R.plan_update({"B": "旧"}, {"B": "新"}, {"A": "飞书改过", "B": "旧"})
        self.assertEqual(p.push, {"B": "新"})

    def test_missing_base_and_remote_means_empty(self):
        p = R.plan_update({}, {"备注": "补充"}, {})
        self.assertEqual(p.push, {"备注": "补充"})

    def test_shapes_do_not_create_false_conflicts(self):
        base = {"负责人": [{"id": "ou_a", "name": "甲"}]}
        remote = {"负责人": [{"id": "ou_a", "name": "甲", "email": "a@x"}]}
        p = R.plan_update(base, {"负责人": [{"id": "ou_b"}]}, remote)
        self.assertEqual(p.push, {"负责人": [{"id": "ou_b"}]})

    def test_remote_deleted(self):
        p = R.plan_update({"状态": "未开始"}, {"状态": "进行中"}, None)
        self.assertTrue(p.remote_missing)
        self.assertEqual(p.conflicts[0]["kind"], "remote_deleted")
        self.assertEqual(p.push, {})

    def test_increment_applies_on_current_remote(self):
        # 飞书上有人把时长手改成 50：本次 +1.5 在 50 上加，不覆盖
        p = R.plan_update({}, {"最近签退": 1}, {"有效时长（小时）": "50"},
                          increments={"有效时长（小时）": 1.5})
        self.assertEqual(p.push, {"最近签退": 1, "有效时长（小时）": 51.5})
        self.assertEqual(p.conflicts, [])

    def test_increment_on_empty_remote(self):
        # 没有入队基准（升级前的老数据）时仍按旧口径累加；
        # 精度改为与明细一致的 4 位小数（审计 P2-1）
        p = R.plan_update({}, {}, {}, increments={"有效时长（小时）": 0.256})
        self.assertEqual(p.push, {"有效时长（小时）": 0.256})


class PlanDeleteTests(unittest.TestCase):
    def test_delete_when_untouched(self):
        self.assertEqual(R.plan_delete({"名称": "甲"}, {"名称": "甲", "公式": "变了"}).action,
                         "delete")

    def test_blocked_when_watched_col_changed(self):
        p = R.plan_delete({"名称": "甲"}, {"名称": "甲（飞书改）"})
        self.assertEqual(p.action, "blocked")
        self.assertEqual(p.conflicts[0]["kind"], "delete_blocked")

    def test_noop_when_already_deleted(self):
        self.assertEqual(R.plan_delete({"名称": "甲"}, None).action, "noop")


class ApplyOpsTests(unittest.TestCase):
    def test_overlay_order(self):
        ops = [{"op": "update", "fields": {"A": 1}},
               {"op": "update", "fields": {"A": 2, "B": "x"}, "increments": {"H": 1.25}}]
        self.assertEqual(R.apply_ops({"A": 0, "H": "3"}, ops), {"A": 2, "B": "x", "H": 4.25})

    def test_create_and_delete(self):
        self.assertEqual(R.apply_ops({"旧": 1}, [{"op": "create", "fields": {"新": 2}}]), {"新": 2})
        self.assertIsNone(R.apply_ops({"A": 1}, [{"op": "delete"}]))


class LocalIdTests(unittest.TestCase):
    def test_local_ids(self):
        a, b = R.new_local_id(), R.new_local_id()
        self.assertNotEqual(a, b)
        self.assertTrue(R.is_local_id(a))
        self.assertFalse(R.is_local_id("recAbc"))
        self.assertFalse(R.is_local_id(None))


if __name__ == "__main__":
    unittest.main()
