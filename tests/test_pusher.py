"""推送线程与写入网关（临时库 + 内存里的假飞书客户端，不访问网络）。

运行：在仓库根目录执行 python -m unittest tests.test_pusher
"""

import unittest

from app import config
from app.portal import cache_store as C
from app.portal import pusher as P
from app.portal import writes as W
from app.portal.sync import SyncError
from tests.test_cache_store import DbCase


class FakeFeishu:
    """内存里的多维表格：按飞书列名存字段；get 时附加公式列，模拟飞书算出来的值。"""

    def __init__(self):
        self.tables = {}
        self.tokens = {}
        self.calls = []
        self.fail = {}                 # 方法名 → 待抛出的异常列表（依次弹出）
        self.lose_create_response = 0  # >0：新建落库后假装响应丢失（抛网络错误）
        self.tick = 1000

    def _fail(self, method):
        q = self.fail.get(method)
        if q:
            raise q.pop(0)

    def _touch(self, table, rid):
        self.tick += 1
        self.tables[table][rid]["mod"] = self.tick

    def create(self, table, fields, client_token=None):
        self.calls.append(("create", table))
        self._fail("create")
        if client_token in self.tokens:
            return self.tokens[client_token]
        rid = "rec%04d" % (sum(len(t) for t in self.tables.values()) + 1)
        self.tables.setdefault(table, {})[rid] = {"fields": dict(fields)}
        self._touch(table, rid)
        self.tokens[client_token] = rid
        if self.lose_create_response:
            self.lose_create_response -= 1
            raise SyncError("network", "timed out")
        return rid

    def get(self, table, rid):
        self.calls.append(("get", table))
        self._fail("get")
        rec = self.tables.get(table, {}).get(rid)
        if rec is None:
            return None
        fields = {k: v for k, v in rec["fields"].items() if v is not None}
        fields["公式"] = "飞书算的"
        return {"record_id": rid, "fields": fields, "last_modified_time": rec["mod"]}

    def update(self, table, rid, fields):
        self.calls.append(("update", table))
        self._fail("update")
        self.tables[table][rid]["fields"].update(fields)
        self._touch(table, rid)

    def delete(self, table, rid):
        self.calls.append(("delete", table))
        self._fail("delete")
        return self.tables.get(table, {}).pop(rid, None) is not None

    def put(self, table, rid, fields):
        """测试用：模拟有人直接在飞书上建/改记录。"""
        self.tables.setdefault(table, {}).setdefault(rid, {"fields": {}})["fields"].update(fields)
        self._touch(table, rid)


class PushCase(DbCase):
    def setUp(self):
        super().setUp()
        self.fs = FakeFeishu()
        self.p = P.Pusher(client_factory=lambda: self.fs)

    def seed(self, table, rid, fields):
        """飞书上有这条记录，且镜像已同步到同样的内容。"""
        self.fs.put(table, rid, fields)
        self.mirror(table, [(rid, dict(fields), self.fs.tables[table][rid]["mod"])])

    def remote(self, table, rid):
        return self.fs.tables[table][rid]["fields"]


class CreateTests(PushCase):
    def test_create_maps_id_and_mirrors_formula(self):
        lid = C.enqueue_create("tasks", {"事项名称": "新事项"})
        self.assertEqual(self.p.drain(), 1)
        rid = C.resolve_id(lid)
        self.assertEqual(self.remote("tasks", rid), {"事项名称": "新事项"})
        self.assertEqual(C.get_remote("tasks", rid)["fields"]["公式"], "飞书算的")
        self.assertEqual(C.backlog()["pending"], 0)

    def test_lost_response_retries_without_duplicate(self):
        self.fs.lose_create_response = 1
        lid = C.enqueue_create("tasks", {"事项名称": "只该有一条"})
        self.p.drain()
        op = C.recent_ops(1)[0]
        self.assertEqual((op["state"], op["attempts"]), ("pending", 1))   # 网络错误 → 退避重试
        C.mark_retry(op["id"], "测试：立即重试", 0)
        self.p.drain()
        self.assertEqual(len(self.fs.tables["tasks"]), 1)                   # 幂等键：没有重复
        self.assertEqual(C.get_op(op["id"])["state"], "done")
        self.assertTrue(C.resolve_id(lid).startswith("rec"))

    def test_edit_after_create_follows_mapping(self):
        lid = C.enqueue_create("tasks", {"事项名称": "甲"})
        self.p.drain()
        C.enqueue_update("tasks", lid, {"执行状态": "进行中"})
        self.p.drain()
        self.assertEqual(self.remote("tasks", C.resolve_id(lid))["执行状态"], "进行中")


class UpdateTests(PushCase):
    def test_push_and_partial_conflict(self):
        self.seed("tasks", "recA", {"执行状态": "未开始", "事项名称": "甲", "优先级": "P2"})
        C.enqueue_update("tasks", "recA", {"执行状态": "进行中", "事项名称": "甲2"})
        self.fs.put("tasks", "recA", {"执行状态": "受阻"})        # 有人在飞书改了同一列
        self.p.drain()
        self.assertEqual(self.remote("tasks", "recA"),
                         {"执行状态": "受阻", "事项名称": "甲2", "优先级": "P2"})
        conflicts = C.list_conflicts()
        self.assertEqual([(c["field"], c["remote_value"]) for c in conflicts], [("执行状态", "受阻")])
        self.assertEqual(C.get_remote("tasks", "recA")["fields"]["执行状态"], "受阻")

    def test_remote_deleted(self):
        self.seed("tasks", "recA", {"执行状态": "未开始"})
        op = C.enqueue_update("tasks", "recA", {"执行状态": "进行中"})
        self.fs.delete("tasks", "recA")
        self.p.drain()
        self.assertEqual(C.get_op(op)["state"], "conflict")
        self.assertIsNone(C.get_remote("tasks", "recA"))
        self.assertEqual(C.list_conflicts()[0]["kind"], "remote_deleted")

    def test_force_cols_and_increment_and_rollover(self):
        self.seed("members", "recM", {"文本": [{"id": "ou_a"}], "有效时长（小时）": "40",
                                      "周初时长": "10", "结算周": "2026-10-12",
                                      "同步说明": "门户签到"})
        C.enqueue_update("members", "recM",
                         {"最近签退": 1, "同步说明": "门户签退 +1.50h",
                          "$rollover": {"this": "2026-10-19", "last": "2026-10-12"}},
                         increments={"有效时长（小时）": 1.5})
        # 推送前：成员同步脚本改了同步说明、有人手改了时长
        self.fs.put("members", "recM", {"同步说明": "成员同步脚本", "有效时长（小时）": "50"})
        self.p.drain()
        r = self.remote("members", "recM")
        # 时长列被人手改成 50、已不等于入队基准 40：不再叠加增量（否则重试会重复结算，
        # 审计 P1-1），改为记一条冲突让人核对；其余列与换周指令照常推送
        self.assertEqual(r["有效时长（小时）"], "50")
        self.assertEqual(C.list_conflicts()[0]["kind"], "increment_base_drift")
        self.assertEqual(r["同步说明"], "门户签退 +1.50h")                  # 日志列直接覆盖
        self.assertEqual((r["周初时长"], r["结算周"], r["上周时长"]), (50.0, "2026-10-19", 40.0))
        self.assertNotIn("$rollover", r)

    def test_rollover_is_idempotent(self):
        self.seed("members", "recM", {"有效时长（小时）": "40", "周初时长": "40",
                                      "结算周": "2026-10-19"})
        op = C.enqueue_update("members", "recM",
                              {"$rollover": {"this": "2026-10-19", "last": "2026-10-12"}})
        n = len(self.fs.calls)
        self.p.drain()
        self.assertEqual(C.get_op(op)["state"], "done")
        self.assertNotIn(("update", "members"), self.fs.calls[n:])          # 已换周：什么也不写

    def test_dependent_create_failed(self):
        self.fs.fail["create"] = [SyncError("network", "timed out"),
                                  SyncError("api", "code=1254045 FieldNameNotFound", code=1254045)]
        lid = C.enqueue_create("tasks", {"不存在的列": 1})
        self.p.drain()                                    # 第一次网络失败 → 退避
        C.enqueue_update("tasks", lid, {"事项名称": "x"})  # 新建已尝试过，另起一条
        C.mark_retry(C.recent_ops(2)[1]["id"], "测试：立即重试", 0)
        self.p.drain()                                    # 新建被拒 → failed；修改随之 failed
        states = [o["state"] for o in reversed(C.recent_ops(2))]
        self.assertEqual(states, ["failed", "failed"])
        self.assertIn("依赖的新建", C.recent_ops(1)[0]["last_error"])
        with self.assertRaises(C.RecordUnknown):          # 再改这条已失败的新建：直接拒绝
            C.enqueue_update("tasks", lid, {"事项名称": "y"})


class DeleteTests(PushCase):
    def test_delete_noop_and_blocked(self):
        self.seed("tasks", "recA", {"事项名称": "甲"})
        self.seed("tasks", "recB", {"事项名称": "乙"})
        self.seed("tasks", "recC", {"事项名称": "丙"})
        a = C.enqueue_delete("tasks", "recA", W.WATCH_COLS["tasks"])
        b = C.enqueue_delete("tasks", "recB", W.WATCH_COLS["tasks"])
        c = C.enqueue_delete("tasks", "recC", W.WATCH_COLS["tasks"])
        self.fs.put("tasks", "recB", {"事项名称": "乙（飞书改）"})
        self.fs.delete("tasks", "recC")
        self.fs.put("tasks", "recA", {"公式": "变了也不影响"})
        self.p.drain()
        self.assertEqual([C.get_op(i)["state"] for i in (a, b, c)], ["done", "conflict", "done"])
        self.assertNotIn("recA", self.fs.tables["tasks"])
        self.assertIn("recB", self.fs.tables["tasks"])
        self.assertEqual(C.list_conflicts()[0]["kind"], "delete_blocked")


class FailureTests(PushCase):
    def test_retry_backoff_and_permanent_failure(self):
        self.seed("tasks", "recA", {"事项名称": "甲"})
        self.seed("tasks", "recB", {"事项名称": "乙"})
        a = C.enqueue_update("tasks", "recA", {"事项名称": "甲2"})
        b = C.enqueue_update("tasks", "recB", {"事项名称": "乙2"})
        self.fs.fail["get"] = [SyncError("rate_limited", "HTTP 429"),
                               SyncError("api", "code=1254045 字段名不存在", code=1254045)]
        self.p.drain()
        oa, ob = C.get_op(a), C.get_op(b)
        self.assertEqual((oa["state"], ob["state"]), ("pending", "failed"))
        self.assertGreater(oa["next_attempt_at"], oa["updated_at"])          # 退避中
        self.assertIn("1254045", ob["last_error"])
        self.assertEqual(P.backoff(1), 2.0)
        self.assertEqual(P.backoff(50), config.PORTAL_PUSH_MAX_BACKOFF_SEC)

    def test_manual_retry_and_discard(self):
        self.seed("tasks", "recA", {"事项名称": "甲"})
        a = C.enqueue_update("tasks", "recA", {"事项名称": "甲2"})
        self.fs.fail["update"] = [SyncError("api", "code=1254045 字段名不存在", code=1254045)]
        self.p.drain()
        self.assertEqual(C.get_op(a)["state"], "failed")
        self.assertFalse(C.discard(a + 99))
        self.assertTrue(C.retry_failed(a))                 # 飞书侧修好后人工重试
        self.assertFalse(C.retry_failed(a))                # 已不是 failed，不重复处理
        self.p.drain()
        self.assertEqual((C.get_op(a)["state"], self.remote("tasks", "recA")["事项名称"]),
                         ("done", "甲2"))
        b = C.enqueue_update("tasks", "recA", {"事项名称": "作废我"})
        self.assertTrue(C.discard(b, "测试作废"))
        self.p.drain()
        self.assertEqual(self.remote("tasks", "recA")["事项名称"], "甲2")

    def test_retryable_feishu_code(self):
        self.seed("tasks", "recA", {"事项名称": "甲"})
        a = C.enqueue_update("tasks", "recA", {"事项名称": "甲2"})
        self.fs.fail["update"] = [SyncError("api", "code=1254291 写冲突", code=1254291)]
        self.p.drain()
        self.assertEqual(C.get_op(a)["state"], "pending")


class GatewayTests(PushCase):
    def setUp(self):
        super().setUp()
        self._mode, self._tables = config.PORTAL_WRITE_MODE, config.PORTAL_CACHE_TABLES
        self._writer = W.portal_writer.FeishuWriter
        W.portal_writer.FeishuWriter = lambda: self.fs

    def tearDown(self):
        config.PORTAL_WRITE_MODE, config.PORTAL_CACHE_TABLES = self._mode, self._tables
        W.portal_writer.FeishuWriter = self._writer
        super().tearDown()

    def test_direct_mode_writes_feishu_and_mirror(self):
        config.PORTAL_WRITE_MODE = "direct"
        rid, queued = W.create("tasks", {"事项名称": "直写"})
        self.assertFalse(queued)
        self.assertEqual(C.get_remote("tasks", rid)["fields"], {"事项名称": "直写"})
        self.assertFalse(W.update("tasks", rid, {"执行状态": "进行中"}))
        self.assertEqual(C.get_remote("tasks", rid)["fields"]["执行状态"], "进行中")
        self.assertFalse(W.delete("tasks", rid))
        self.assertIsNone(C.get_remote("tasks", rid))

    def test_direct_mode_still_queues_behind_open_ops(self):
        config.PORTAL_WRITE_MODE = "direct"
        self.seed("tasks", "recA", {"事项名称": "甲"})
        C.enqueue_update("tasks", "recA", {"事项名称": "排队中"})
        n = len(self.fs.calls)
        self.assertTrue(W.update("tasks", "recA", {"执行状态": "进行中"}))
        self.assertEqual(self.fs.calls[n:], [])                             # 没有插队直写

    def test_cache_mode_per_table(self):
        config.PORTAL_WRITE_MODE = "cache"
        config.PORTAL_CACHE_TABLES = ("schedule",)
        lid, queued = W.create("schedule", {"日程名称": "例会"})
        self.assertTrue(queued and lid.startswith("loc_"))
        rid, queued = W.create("tasks", {"事项名称": "仍直写"})
        self.assertFalse(queued)
        self.assertIsNone(W.resolve_remote(lid, timeout=0))
        self.p.drain()
        self.assertTrue(W.resolve_remote(lid, timeout=0).startswith("rec"))
        with self.assertRaises(C.RecordUnknown):
            W.update("schedule", "recNope", {"日程名称": "x"})


if __name__ == "__main__":
    unittest.main()
