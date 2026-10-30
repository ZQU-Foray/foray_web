"""S5 服务端加固的回归用例（审计 P2-3/5/7/8/9 与 systemd 降权）。

只测「判断逻辑本身」：真实飞书请求、真实 systemd 都不在这里，避免测试依赖环境。
"""
import unittest
from unittest import mock

from fastapi import HTTPException

from app.auth import guard
from app.portal import api, compute, normalize, sync, writer, writes


class _FakeUpload:
    """最小 UploadFile 替身：分块返回，记录是否被关闭。"""

    def __init__(self, chunks, filename="a.png", content_type="image/png"):
        self._chunks = list(chunks)
        self.filename = filename
        self.content_type = content_type
        self.closed = False

    async def read(self, size=-1):
        return self._chunks.pop(0) if self._chunks else b""

    async def close(self):
        self.closed = True


class RecordIdTests(unittest.TestCase):
    """写入前把 record_id 限制成飞书格式，挡住路径拼接与超长 ID。"""

    def test_accepts_flyshu_shape(self):
        for rid in ("recA1b2C3", "rec" + "a" * 40):
            writer.check_record_id(rid)          # 不抛即通过

    def test_rejects_bad_shape(self):
        for rid in ("", None, 123, "12345", "rec", "rec../etc/passwd",
                    "rec a b", "loc_abc", "rec" + "a" * 41):
            with self.assertRaises(sync.SyncError) as cm:
                writer.check_record_id(rid)
            self.assertEqual(cm.exception.klass, "config")

    def test_local_id_not_rejected_by_writes(self):
        """队列里的临时 ID 由 writes 先拦下并给可重试的提示，不是 config 错误。"""
        with self.assertRaises(ValueError) as cm:
            writes._check_remote_id("tasks", "loc_abc")
        self.assertIn("同步队列", str(cm.exception))

    def test_writes_checks_remote_id(self):
        writes._check_remote_id("tasks", "recA1b2C3")     # 通过
        with self.assertRaises(sync.SyncError):
            writes._check_remote_id("tasks", "../etc/passwd")


class UploadLimitTests(unittest.IsolatedAsyncioTestCase):
    """附件按 1MiB 分块读，超限立即放弃且文件一定被关。"""

    async def test_small_file_passes(self):
        f = _FakeUpload([b"x" * 1024, b""])
        data = await api._read_limited(f, 10 * 1024 * 1024, "图片")
        self.assertEqual(len(data), 1024)
        self.assertTrue(f.closed)

    async def test_over_limit_stops_and_closes(self):
        f = _FakeUpload([b"a" * (1024 * 1024)] * 3, filename="big.png")
        with self.assertRaises(HTTPException) as cm:
            await api._read_limited(f, 2 * 1024 * 1024, "图片")
        self.assertEqual(cm.exception.status_code, 422)
        self.assertIn("图片超过", cm.exception.detail)
        self.assertTrue(f.closed)

    async def test_empty_file_rejected(self):
        f = _FakeUpload([b""])
        with self.assertRaises(HTTPException) as cm:
            await api._read_limited(f, 1024, "图片")
        self.assertEqual(cm.exception.status_code, 422)
        self.assertTrue(f.closed)


class LinkStabilityTests(unittest.TestCase):
    """同一多值关联每次同步必须给同一个 project_id，否则每轮都发新版本。"""

    def test_sorted_first_id(self):
        val = [{"record_ids": ["recc", "reca", "recb"], "table_id": "tblX"}]
        first = normalize._link_id(val)[0]
        for order in (["recb", "recc", "reca"], ["reca", "recb", "recc"]):
            again = normalize._link_id(
                [{"record_ids": order, "table_id": "tblX"}])[0]
            self.assertEqual(again, first)
        self.assertEqual(first, "reca")

    def test_multi_value_reports_issue(self):
        rid, issue, ids = normalize._link_id(
            [{"record_ids": ["recb", "reca"], "table_id": "tblX"}])
        self.assertEqual(rid, "reca")
        self.assertEqual(ids, ["reca", "recb"])
        self.assertIn("关联多个值", issue)

    def test_single_value_has_no_issue(self):
        rid, issue, ids = normalize._link_id(
            [{"record_ids": ["reca"], "table_id": "tblX"}])
        self.assertEqual((rid, issue, ids), ("reca", None, ["reca"]))

    def test_project_filter_uses_set(self):
        tasks = [
            {"project_ids": ["recp1", "recp2"], "status": "进行中"},
            {"project_id": "recp1", "status": "进行中"},
            {"project_ids": ["recp2"], "status": "进行中"},
        ]
        self.assertEqual(len(compute.valid_tasks_for_project(tasks, "recp1")), 2)


class _PagedReader(sync.FeishuReader):
    """只替换 _get：按预置分页直接回应，不发任何网络请求。"""

    def __init__(self, pages):
        super().__init__({"app_id": "a", "app_secret": "b", "app_token": "c",
                          "api_base": "http://127.0.0.1", "tables": {}})
        self._pages = list(pages)

    def _get(self, path, params=None):
        return {"code": 0, "data": self._pages.pop(0)}


class TotalToleranceTests(unittest.TestCase):
    """翻页期间别人增删一条不算分页不完整；差两条以上仍然报错。"""

    def test_off_by_one_allowed(self):
        r = _PagedReader([{"items": [{"record_id": "recA"}], "total": 2,
                           "has_more": False}])
        self.assertEqual(len(r.records("tblX")), 1)

    def test_off_by_two_rejected(self):
        r = _PagedReader([{"items": [{"record_id": "recA"}], "total": 3,
                           "has_more": False}])
        with self.assertRaises(sync.SyncError) as cm:
            r.records("tblX")
        self.assertEqual(cm.exception.klass, "parse")

    def test_missing_total_warns_but_passes(self):
        r = _PagedReader([{"items": [{"record_id": "recA"}], "has_more": False}])
        with self.assertLogs("app.portal.sync", level="WARNING") as logs:
            self.assertEqual(len(r.records("tblX")), 1)
        self.assertIn("未返回 total", "\n".join(logs.output))


class RouteDefaultTests(unittest.TestCase):
    """新增路由忘记登记时默认按管理接口处理（默认拒绝）。"""

    def test_unknown_path_is_admin_api(self):
        # /api/* 明确登记为调试台接口；真正"未登记"的是这两类路径
        self.assertEqual(guard.classify("/brand-new-page"), "admin_api")
        self.assertEqual(guard.classify("/report/2026/summary"), "admin_api")

    def test_known_paths_unchanged(self):
        self.assertEqual(guard.classify("/api/portal/overview"), "admin_api")
        self.assertEqual(guard.classify("/api/version"), "debug_api")
        self.assertEqual(guard.classify("/ws/tty"), "ws")

    def test_no_route_falls_through_to_public(self):
        """遍历真实路由表：除白名单外不允许出现 public。"""
        from app import main as app_main
        allowed = {"/", "/debug", "/debug/", "/portal", "/portal/",
                   "/docs", "/redoc", "/openapi.json", "/favicon.ico"}
        seen = 0
        for route in app_main.app.routes:
            path = getattr(route, "path", "")
            if (not path or path in allowed
                    or path.startswith("/docs") or path.startswith("/redoc")
                    or path in guard.PUBLIC_EXACT or path in guard.ONBOARDING_API):
                continue           # 登录/入职这类匿名入口是登记在案的公开面
            kind = guard.classify(path.replace("{", "").replace("}", ""))
            self.assertNotEqual(kind, "public", "%s 落到了 public" % path)
            seen += 1
        self.assertGreater(seen, 10)


class UnitTests(unittest.TestCase):
    """单元文件不允许再出现 User=root。"""

    def test_no_root_units(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "deploy"
        for name in ("portal.service", "portal-redirect.service",
                     "portal-backup.service", "portal-health.service"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("User=root", text, name)
            self.assertIn("User=portal", text, name)

    def test_portal_keeps_single_worker_and_cap(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parent.parent
                / "deploy" / "portal.service").read_text(encoding="utf-8")
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", text)
        exec_line = next(ln for ln in text.splitlines() if ln.startswith("ExecStart="))
        self.assertNotIn("--workers", exec_line)

    def test_deploy_creates_service_account(self):
        """部署脚本必须先把账号建好，否则 User=portal 的单元起不来。"""
        import deploy
        run = mock.Mock()
        deploy.ensure_service_account(run)
        cmds = " ".join(str(c.args[0]) for c in run.must.call_args_list)
        self.assertIn("useradd", cmds)
        self.assertIn("chown -R portal:portal /opt/portal/config", cmds)
        self.assertIn("install -d -o portal -g portal", cmds)


if __name__ == "__main__":
    unittest.main()
