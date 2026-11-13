"""工作台：布局规整、按人存储、接口与卡片组装（临时库，不访问飞书）。

运行：在仓库根目录执行 python -m unittest tests.test_workbench
"""

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app import config
from app.portal import api, migrations as M
from app.portal import store as S
from app.portal import workbench as W


class _FakeRequest:
    """最小请求对象：只带 state.session / headers / client（接口用到的字段）。"""

    class _State:
        pass

    def __init__(self, role="member", open_id="ou_a", user="甲", headers=None):
        self.state = _FakeRequest._State()
        self.state.session = {"user": user, "role": role, "extra": {"open_id": open_id}}
        self.headers = headers or {}
        self.client = type("C", (), {"host": "127.0.0.1"})()


class DbCase(unittest.TestCase):
    """每个用例一个全新的临时库；快照用合成数据（与飞书无关）。"""

    TABLES = ("tasks", "projects", "members", "milestones", "schedule", "procurement",
              "roster", "duty")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.PORTAL_DB
        S.close()
        config.PORTAL_DB = Path(self.tmp.name) / "portal.db"
        self._audit_events = []
        self._old_audit = api._audit
        api._audit = lambda event, **fields: self._audit_events.append((event, fields))

    def tearDown(self):
        api._audit = self._old_audit
        S.close()
        config.PORTAL_DB = self._old
        self.tmp.cleanup()

    def publish(self, extra=None):
        """发一版快照：8 张表都写（未给的写空表），保证接口有快照可读。"""
        rows = {t: [] for t in self.TABLES}
        for t, recs in (extra or {}).items():
            rows[t] = recs
        for t, recs in rows.items():
            S.write_candidate(1, t, recs, [])
        S.publish(1, {t: len(r) for t, r in rows.items()}, 0.01)
        return rows


class CatalogTests(unittest.TestCase):
    def test_catalog_ids_unique_and_default_layout_valid(self):
        ids = [c["id"] for c in W.CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 10)
        # 默认布局必须能通过校验，且只包含目录里的卡片
        self.assertEqual(W.normalize_layout(W.default_layout()),
                         [dict(x) for x in W.DEFAULT_LAYOUT])
        for item in W.default_layout():
            self.assertIn(item["card_id"], W.CATALOG_BY_ID)
            self.assertIn(item["width"], W.WIDTHS)

    def test_catalog_view_has_no_internal_fields(self):
        view = W.catalog_view()
        self.assertEqual(len(view), len(W.CATALOG))
        for item in view:
            self.assertEqual(set(item), {"id", "title", "hint", "width"})

    def test_accepts_both_shapes_and_fills_width(self):
        arr = W.normalize_layout([{"card_id": "duty"}])
        self.assertEqual(arr, [{"card_id": "duty", "width": W.CATALOG_BY_ID["duty"]["width"]}])
        obj = W.normalize_layout({"cards": [{"card_id": "my_tasks", "width": "full"}],
                                  "extra": 1})
        self.assertEqual(obj, [{"card_id": "my_tasks", "width": "full"}])

    def test_rejects_bad_layouts(self):
        bad = [
            None,                                    # 没有 cards
            {"cards": "nope"},                       # 不是数组
            [{"card_id": "nope"}],                   # 未知卡片
            [{"card_id": "duty"}, {"card_id": "duty"}],   # 重复
            [{"card_id": "duty", "width": "quarter"}],    # 宽度非法
            [{"card_id": "duty"}] * (W.MAX_CARDS + 1),     # 超量
            ["duty"],                                # 项不是对象
        ]
        for item in bad:
            with self.assertRaises(W.WorkbenchLayoutError):
                W.normalize_layout(item)

    def test_max_cards_boundary_allowed(self):
        cards = [{"card_id": "duty"} for _ in range(W.MAX_CARDS)]
        # 超量拒绝，但同一张卡重复也会拒绝，所以边界用「去重后不超量」的正例
        with self.assertRaises(W.WorkbenchLayoutError):
            W.normalize_layout(cards)
        self.assertEqual(len(W.normalize_layout(W.default_layout())), len(W.DEFAULT_LAYOUT))


class LayoutStoreTests(DbCase):
    def test_save_load_isolated_per_user(self):
        W.save_layout("ou_a", [{"card_id": "duty", "width": "full"}])
        self.assertEqual(W.load_layout("ou_a"), [{"card_id": "duty", "width": "full"}])
        self.assertIsNone(W.load_layout("ou_b"))
        self.assertEqual(W.effective_layout("ou_b"), W.default_layout())
        self.assertGreater(W.layout_updated_at("ou_a"), 0)

    def test_reset_is_idempotent(self):
        W.save_layout("ou_a", [{"card_id": "duty"}])
        self.assertEqual(W.reset_layout("ou_a"), W.default_layout())
        self.assertIsNone(W.load_layout("ou_a"))
        # 再删一次仍成功（幂等），库里不会报错
        self.assertEqual(W.reset_layout("ou_a"), W.default_layout())

    def test_broken_row_falls_back_to_default(self):
        W.save_layout("ou_a", [{"card_id": "duty"}])
        with S._lock, S._db() as c:
            c.execute("UPDATE workbench_layouts SET layout=? WHERE open_id=?",
                      ('{"cards": [{"card_id": "gone"}]}', "ou_a"))
        # 卡片下线后旧布局不合法：当作没保存过，页面回落默认
        self.assertIsNone(W.load_layout("ou_a"))
        self.assertEqual(W.effective_layout("ou_a"), W.default_layout())

    def test_save_requires_open_id(self):
        with self.assertRaises(W.WorkbenchLayoutError):
            W.save_layout("", [{"card_id": "duty"}])


class SchemaTests(DbCase):
    def test_db_has_workbench_table_at_latest(self):
        self.assertEqual(S.get_meta("schema_version"), str(M.LATEST))
        names = {r[0] for r in S._db().execute("SELECT name FROM sqlite_master")}
        self.assertIn("workbench_layouts", names)
        self.assertIn("workbench_layouts", M.required_tables())


class ApiTests(DbCase):
    def test_get_returns_catalog_layout_and_cards(self):
        # 合成记录带齐归一化后的键（urgent_tasks 等计算按归一化口径直取字段）
        self.publish({"tasks": [{"id": "recT1", "name": "事项一", "status": "进行中",
                                 "priority": "P1", "owner_group": "机械",
                                 "owners": [{"id": "ou_a", "name": "甲"}]}]})
        res = api.workbench_page(_FakeRequest())
        self.assertTrue(res["has_snapshot"])
        self.assertEqual([c["card_id"] for c in res["layout"]],
                         [c["card_id"] for c in W.DEFAULT_LAYOUT])
        self.assertFalse(res["saved"])
        self.assertEqual(len(res["cards"]), len(W.DEFAULT_LAYOUT))
        for card in res["cards"]:
            self.assertNotIn("error", card, card)

    def test_get_without_snapshot(self):
        res = api.workbench_page(_FakeRequest())
        self.assertFalse(res["has_snapshot"])
        self.assertEqual(res["cards"], [])
        self.assertEqual(len(res["catalog"]), len(W.CATALOG))

    def test_put_saves_and_marks_saved(self):
        self.publish()
        put = api.workbench_save({"cards": [{"card_id": "duty", "width": "full"}]},
                                 _FakeRequest())
        self.assertTrue(put["ok"])
        self.assertEqual(put["layout"], [{"card_id": "duty", "width": "full"}])
        self.assertTrue(api.workbench_page(_FakeRequest())["saved"])
        # 另一个成员看到的是自己的默认布局
        self.assertFalse(api.workbench_page(_FakeRequest(open_id="ou_b"))["saved"])
        self.assertEqual(self._audit_events[0][0], "workbench_layout_save")

    def test_put_rejects_unknown_card_with_422(self):
        self.publish()
        with self.assertRaises(HTTPException) as ctx:
            api.workbench_save({"cards": [{"card_id": "bogus"}]}, _FakeRequest())
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("未知卡片", ctx.exception.detail)

    def test_put_requires_member_open_id(self):
        self.publish()
        with self.assertRaises(HTTPException) as ctx:
            api.workbench_save({"cards": []}, _FakeRequest(open_id=""))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_put_rejects_cross_site_origin(self):
        self.publish()
        req = _FakeRequest(headers={"origin": "https://evil.example", "host": "portal.test"})
        with self.assertRaises(HTTPException) as ctx:
            api.workbench_save({"cards": []}, req)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_same_origin_is_allowed(self):
        self.publish()
        req = _FakeRequest(headers={"origin": "https://portal.test", "host": "portal.test"})
        self.assertTrue(api.workbench_save({"cards": []}, req)["ok"])

    def test_delete_resets(self):
        self.publish()
        api.workbench_save({"cards": [{"card_id": "duty"}]}, _FakeRequest())
        out = api.workbench_reset(_FakeRequest())
        self.assertTrue(out["ok"])
        self.assertFalse(out["saved"])
        self.assertFalse(api.workbench_page(_FakeRequest())["saved"])
        self.assertEqual(self._audit_events[-1][0], "workbench_layout_reset")


class CardBuildTests(DbCase):
    def test_all_cards_build_on_empty_snapshot(self):
        self.publish()
        snap = S.current()
        cards = W.build_cards(snap, "ou_a", [{"card_id": c["id"], "width": c["width"]}
                                             for c in W.CATALOG])
        self.assertEqual(len(cards), 10)
        for card in cards:
            self.assertNotIn("error", card, card)
            self.assertEqual(card["width"], W.CATALOG_BY_ID[card["id"]]["width"])

    def test_my_tasks_only_mine_and_open(self):
        self.publish({"tasks": [
            {"id": "recT1", "name": "我的未完成", "status": "进行中", "owner_group": "机械",
             "owners": [{"id": "ou_a", "name": "甲"}]},
            {"id": "recT2", "name": "别人的", "status": "进行中", "owner_group": "机械",
             "owners": [{"id": "ou_b", "name": "乙"}]},
            {"id": "recT3", "name": "我的已完成", "status": "已完成", "owner_group": "机械",
             "owners": [{"id": "ou_a", "name": "甲"}]},
        ]})
        card = W.build_cards(S.current(), "ou_a",
                             [{"card_id": "my_tasks", "width": "full"}])[0]
        names = [t["name"] for t in card["data"]["items"]]
        self.assertEqual(names, ["我的未完成"])
        self.assertEqual(card["data"]["total"], 1)

    def test_single_card_error_does_not_break_page(self):
        self.publish()
        snap = S.current()

        def boom(_snap, _open_id):
            raise RuntimeError("故意失败")

        old = W._BUILDERS["duty"]
        W._BUILDERS["duty"] = boom
        try:
            cards = W.build_cards(snap, "ou_a", [{"card_id": "duty", "width": "half"},
                                                 {"card_id": "sync_status", "width": "half"}])
        finally:
            W._BUILDERS["duty"] = old
        self.assertIn("故意失败", cards[0]["error"])
        self.assertNotIn("error", cards[1])

    def test_my_attendance_without_member_note(self):
        self.publish()
        card = W.build_cards(S.current(), "",
                             [{"card_id": "my_attendance", "width": "half"}])[0]
        self.assertNotIn("error", card)
        self.assertIn("花名册身份", card["data"]["note"])

    def test_data_quality_counts(self):
        # 质量报告按表存，条目形状是 (record_id, issue)
        S.write_candidate(1, "tasks", [], [("recX", "未知枚举")])
        S.write_candidate(1, "projects", [], [])
        S.publish(1, {"tasks": 0, "projects": 0}, 0.01)
        card = W.build_cards(S.current(), "ou_a",
                             [{"card_id": "data_quality", "width": "half"}])[0]
        self.assertEqual(card["data"]["total"], 1)
        self.assertEqual(card["data"]["by_table"][0]["table_name"], "tasks")


if __name__ == "__main__":
    unittest.main()
