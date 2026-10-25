"""推送队列与冲突的页面数据（缓存库第 5 步）。

运行：在仓库根目录执行 python -m unittest tests.test_queue_view
"""

import unittest

from app.portal import cache_store as C
from app.portal import queue_view as Q
from tests.test_cache_store import DbCase

SNAP = {"tables": {
    "tasks": [{"id": "recT1", "name": "装配底盘",
               "owners": [{"id": "ou_a", "name": "成员甲"}]}],
    "projects": [{"id": "recP1", "name": "哨兵"}],
}}


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.names = Q.name_index(SNAP)

    def test_name_index(self):
        self.assertEqual(self.names, {"recT1": "装配底盘", "ou_a": "成员甲", "recP1": "哨兵"})

    def test_values(self):
        d = lambda v: Q.display_value(v, self.names)          # noqa: E731
        self.assertEqual(d(None), "（空）")
        self.assertEqual(d(True), "是")
        self.assertEqual(d(12.5), "12.5")
        self.assertEqual(d(3), "3")
        self.assertEqual(d(1761000000000), "2025-10-21 06:40")     # 毫秒时间戳按上海时间
        self.assertEqual(d(["机械", "电控"]), "机械、电控")
        self.assertEqual(d([{"id": "ou_a"}]), "成员甲")              # 只有 open_id 的人员补姓名
        self.assertEqual(d([{"id": "ou_zz9999"}]), "…9999")
        self.assertEqual(d(["recP1"]), "哨兵")                       # 关联列显示记录名
        self.assertEqual(d([{"type": "text", "text": "说明"}]), "说明")
        self.assertTrue(d("长" * 300).endswith("…"))

    def test_user_label(self):
        self.assertEqual(Q.user_label("feishu:ou_a", self.names), "成员甲")
        self.assertEqual(Q.user_label("emergency:运维", self.names), "运维（应急账号）")
        self.assertEqual(Q.user_label(None, self.names), "—")


class PageTests(DbCase):
    def test_page_lists_failed_pending_conflicts(self):
        C.replace_remote_tables({"tasks": [
            {"record_id": "recT1", "fields": {"事项名称": "装配底盘", "执行状态": "进行中"},
             "last_modified_time": 1}]}, 0.0, now=1.0)
        lid = C.enqueue_create("tasks", {"事项名称": "新事项"}, by="feishu:ou_a")
        failed = C.claim_next()
        C.mark_failed(failed["id"], "api: 拒绝")
        upd = C.enqueue_update("tasks", "recT1", {"执行状态": "已完成"}, by="feishu:ou_a")
        C.claim_next()
        C.complete_update(upd, {}, [{"kind": "field", "field": "执行状态", "base": "进行中",
                                     "local": "已完成", "remote": "受阻"}],
                          remote_fields={"事项名称": "装配底盘", "执行状态": "受阻"})
        C.enqueue_update("tasks", "recT1", {"事项名称": "装配底盘二"}, by="emergency:运维")
        data = Q.page(SNAP, {"write_mode": "cache"}, can_manage=True)
        self.assertTrue(data["can_manage"])
        f = data["failed"][0]
        self.assertEqual((f["record_id"], f["name"], f["op_text"], f["created_by"]),
                         (lid, "新事项", "新建", "成员甲"))
        p = data["pending"][0]
        self.assertEqual((p["name"], p["columns"], p["created_by"]),
                         ("装配底盘", ["事项名称"], "运维（应急账号）"))
        c = data["conflicts"][0]
        self.assertEqual((c["name"], c["field"], c["local"], c["remote"]),
                         ("装配底盘", "执行状态", "已完成", "受阻"))


if __name__ == "__main__":
    unittest.main()
