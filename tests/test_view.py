"""本地视图快照（缓存库第 4 步）：镜像 ⊕ 未落地操作 → 规范化 → 发布。

运行：在仓库根目录执行 python -m unittest tests.test_view
"""

import unittest

from app.portal import cache_store as C
from app.portal import pusher as P
from app.portal import store as S
from app.portal import view as V
from tests.test_cache_store import DbCase
from tests.test_pusher import FakeFeishu

# 只映射项目与事项两张表（列名 → field_id → 语义键，与真实配置同一套机制）
CFG = {"tables": {
    "projects": {"table_id": "tblP", "fields": {
        "name": "fldPn", "owner_group": "fldPg", "status": "fldPs", "owners": "fldPo"}},
    "tasks": {"table_id": "tblT", "fields": {
        "name": "fldTn", "owner_group": "fldTg", "priority": "fldTp", "status": "fldTs",
        "project": "fldTproj"}},
}}
FIELD_IDS = {
    "projects": {"项目名称": "fldPn", "主责组": "fldPg", "项目状态": "fldPs", "负责人": "fldPo"},
    "tasks": {"事项名称": "fldTn", "组别": "fldTg", "优先级": "fldTp", "执行状态": "fldTs",
              "所属项目": "fldTproj"},
}
P1 = {"项目名称": "哨兵", "主责组": "电控", "项目状态": "进行中",
      "负责人": [{"id": "ou_a", "name": "成员甲"}]}


class ViewCase(DbCase):
    def setUp(self):
        super().setUp()
        self.mirror("projects", [("recP1", dict(P1), 100)])
        V.save_sync_meta(FIELD_IDS, {"projects": {"recP1": "https://feishu/recP1"}})

    def build(self, **kw):
        return V.rebuild(cfg=CFG, **kw)

    def projects(self):
        return {p["id"]: p for p in S.current()["tables"]["projects"]}


class RebuildTests(ViewCase):
    def test_requires_field_ids(self):
        S.set_meta(V.FIELD_IDS_META, "{}")
        self.assertIsNone(self.build())
        self.assertIsNone(S.current())

    def test_publish_and_digest_gate(self):
        counts = self.build(sync_round=True, duration=1.0)
        self.assertEqual(counts["projects"], 1)
        p = self.projects()["recP1"]
        self.assertEqual((p["name"], p["url"]), ("哨兵", "https://feishu/recP1"))
        ver = S.current()["version"]
        self.assertIsNone(self.build())                     # 内容没变：不发布
        self.assertEqual(S.current()["version"], ver)

    def test_local_publish_keeps_sync_stats(self):
        self.build(sync_round=True, duration=1.0)
        last_ok = S.get_meta("last_success_at")
        C.enqueue_update("projects", "recP1", {"项目名称": "哨兵二代"})
        self.assertIsNotNone(self.build())
        self.assertEqual(self.projects()["recP1"]["name"], "哨兵二代")   # 写完立即可见
        self.assertEqual(S.get_meta("last_success_at"), last_ok)          # 不冒充同步成功

    def test_local_create_fill_names_and_delete(self):
        self.build(sync_round=True)
        lid = C.enqueue_create("projects", {"项目名称": "新项目", "主责组": "机械",
                                            "负责人": [{"id": "ou_a"}]})
        self.build()
        p = self.projects()[lid]
        self.assertEqual(p["owners"], [{"id": "ou_a", "name": "成员甲"}])  # 从镜像补姓名
        C.enqueue_delete("projects", "recP1", ["项目名称"])
        self.build()
        self.assertEqual(set(self.projects()), {lid})

    def test_link_to_local_record(self):
        self.build(sync_round=True)
        lid = C.enqueue_create("projects", {"项目名称": "新项目", "主责组": "机械"})
        tid = C.enqueue_create("tasks", {"事项名称": "挂在新项目下", "组别": ["机械"],
                                         "优先级": "P1", "执行状态": "未开始",
                                         "所属项目": [lid]})
        self.build()
        task = {t["id"]: t for t in S.current()["tables"]["tasks"]}[tid]
        self.assertEqual(task["project_id"], lid)
        self.assertFalse(task.get("_invalid"), task)


class MarkTests(ViewCase):
    """记录上的同步标记：待同步 / 同步失败 / 冲突（缓存库第 5 步）。"""

    def mark(self, rid="recP1"):
        self.build()
        return self.projects()[rid].get("sync")

    def test_no_mark_without_ops(self):
        self.build(sync_round=True)
        self.assertNotIn("sync", self.projects()["recP1"])

    def test_pending_then_retry_error(self):
        self.build(sync_round=True)
        op = C.enqueue_update("projects", "recP1", {"项目名称": "改名"})
        m = self.mark()
        self.assertEqual((m["state"], m["label"]), ("pending", "待同步"))
        self.assertNotIn("上次推送没成功", m["text"])
        C.claim_next()
        C.mark_retry(op, "network: 连接超时", 10)
        self.assertIn("连接超时", self.mark()["text"])

    def test_failed_mark_and_marks_change_version(self):
        self.build(sync_round=True)
        op = C.enqueue_update("projects", "recP1", {"项目名称": "改名"})
        self.build()
        ver = S.current()["version"]
        C.claim_next()
        C.mark_failed(op, "api: code=1254045 字段不存在")
        m = self.mark()
        self.assertEqual(m["state"], "failed")
        self.assertIn("1254045", m["text"])
        self.assertEqual(self.projects()["recP1"]["name"], "哨兵")     # 失败的改动不再叠加
        self.assertNotEqual(S.current()["version"], ver)              # 标记变了 → 新版本
        C.discard(op)
        self.assertIsNone(self.mark())

    def test_conflict_mark_until_ack(self):
        self.build(sync_round=True)
        op = C.enqueue_update("projects", "recP1", {"项目状态": "已完成"})
        C.claim_next()
        remote = dict(P1, **{"项目状态": "暂停"})
        C.complete_update(op, {}, [{"kind": "field", "field": "项目状态", "base": "进行中",
                                    "local": "已完成", "remote": "暂停"}],
                          remote_fields=remote)
        m = self.mark()
        self.assertEqual(m["state"], "conflict")
        self.assertIn("「项目状态」", m["text"])
        cid = C.list_conflicts()[0]["id"]
        C.ack_conflict(cid, by="feishu:ou_a")
        self.assertIsNone(self.mark())

    def test_failed_beats_conflict_beats_pending(self):
        m = {"pending": 1, "retry_error": None, "failed": [],
             "conflicts": [{"id": 1, "kind": "delete_blocked", "field": None}]}
        self.assertEqual(V.sync_mark(m)["state"], "conflict")
        self.assertIn("删除没有执行", V.sync_mark(m)["text"])
        m["failed"].append({"op_id": 3, "op": "delete", "error": "x"})
        self.assertEqual(V.sync_mark(m)["state"], "failed")
        self.assertIn("删除", V.sync_mark(m)["text"])

    def test_local_create_pending_mark_and_failed_after(self):
        self.build(sync_round=True)
        lid = C.enqueue_create("projects", {"项目名称": "新项目"})
        first = C.claim_next()                          # 新建推送中，后续修改另起一条
        C.enqueue_update("projects", lid, {"项目状态": "进行中"})
        self.assertEqual(self.mark(lid)["state"], "pending")
        C.mark_failed(first["id"], "api: 拒绝")
        nxt = C.claim_next()                            # 依赖的新建失败 → 后续也失败
        C.mark_failed(nxt["id"], "依赖的新建没有推送成功")
        self.build()
        self.assertNotIn(lid, self.projects())          # 失败的新建不出现在列表里
        self.assertEqual(C.failed_after(first["id"]), [nxt["id"]])


class PushLinkTests(ViewCase):
    def test_link_waits_for_project_then_pushes_remote_id(self):
        fs = FakeFeishu()
        pusher = P.Pusher(client_factory=lambda: fs)
        lid = C.enqueue_create("projects", {"项目名称": "新项目"})
        C.claim_next()                                          # 新建在推送中（模拟正在进行）
        tid = C.enqueue_create("tasks", {"事项名称": "x", "所属项目": [lid]})
        pusher.drain()                                          # 事项：等项目先推
        op = C.recent_ops(1)[0]
        self.assertEqual((op["state"], op["last_error"]), ("pending", "等待关联的新建记录先推到飞书"))
        C.requeue_stuck(older_than=0)
        C.mark_retry(op["id"], "测试：立即重试", 0)
        pusher.drain()
        rid_p = C.resolve_id(lid)
        rid_t = C.resolve_id(tid)
        self.assertTrue(rid_p.startswith("rec") and rid_t.startswith("rec"))
        self.assertEqual(fs.tables["tasks"][rid_t]["fields"]["所属项目"], [rid_p])

    def test_link_to_failed_create_fails(self):
        from app.portal.sync import SyncError
        fs = FakeFeishu()
        fs.fail["create"] = [SyncError("api", "code=1254045 拒绝", code=1254045)]
        pusher = P.Pusher(client_factory=lambda: fs)
        lid = C.enqueue_create("projects", {"项目名称": "会被拒"})
        C.enqueue_create("tasks", {"事项名称": "x", "所属项目": [lid]})
        pusher.drain()
        states = [o["state"] for o in reversed(C.recent_ops(2))]
        self.assertEqual(states, ["failed", "failed"])
        self.assertIn("关联的新建记录没有推送成功", C.recent_ops(1)[0]["last_error"])


if __name__ == "__main__":
    unittest.main()
