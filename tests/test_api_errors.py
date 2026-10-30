"""采购附件上传的错误分支回归（审计 P1-1）。

`app/portal/api.py` 只导入了 `SyncError as _SyncError`，路由里曾写裸名
`SyncError`：飞书侧记录被删时抛 NameError → 500 且没有 detail。这里锁住
两条路径：记录不存在走 502 中文提示；未预期异常走兜底 502 并留审计线索。
"""
import asyncio
import unittest

from fastapi import HTTPException

from app.portal import api


class _FakeUpload:
    filename = "shot.png"
    content_type = "image/png"

    async def read(self):
        return b"fake-image-bytes"

    async def close(self):
        return None


class _FakeWriter:
    """只实现被调用到的方法，记录写入的列便于断言。"""

    def __init__(self):
        self.updates = []

    def upload_attachment(self, data, filename, content_type):
        return "tok_fake"

    def update(self, table, record_id, cols):
        self.updates.append((table, record_id, cols))


class _FakeRequest:
    class _State:
        pass

    def __init__(self):
        self.state = _FakeRequest._State()
        self.state.session = {"user": "tester", "display": "tester"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.headers = {}


class ProcurementUploadErrorTests(unittest.TestCase):
    def setUp(self):
        self._audit_events = []
        self._saved = {}
        for name in ("_procurement_gate", "_procurement_live_fields",
                     "_procurement_upload_and_attach", "_audit"):
            self._saved[name] = getattr(api, name)
        self._saved_writer = api.portal_writer.FeishuWriter
        api._procurement_gate = lambda request, cols: None
        api._audit = lambda event, **fields: self._audit_events.append((event, fields))
        api.portal_writer.FeishuWriter = _FakeWriter

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(api, name, value)
        api.portal_writer.FeishuWriter = self._saved_writer

    def _call(self):
        return asyncio.run(api.write_procurement_upload(
            record_id="rec_fake", request=_FakeRequest(),
            file=_FakeUpload(), column="image"))

    def test_missing_record_returns_502_with_reason(self):
        """飞书侧记录已删除：返回 502 与可读原因，而不是未捕获的 NameError。"""
        api._procurement_live_fields = lambda rid: None
        with self.assertRaises(HTTPException) as ctx:
            self._call()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("采购记录不存在", str(ctx.exception.detail))

    def test_unexpected_error_is_caught_and_audited(self):
        """未预期异常走兜底分支：502 + 审计事件，前端不再只看到裸 500。"""
        def boom(*args, **kwargs):
            raise RuntimeError("模拟线程池异常")

        api._procurement_upload_and_attach = boom
        with self.assertRaises(HTTPException) as ctx:
            self._call()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("服务端异常", str(ctx.exception.detail))
        events = [e for e, _ in self._audit_events]
        self.assertIn("portal_procurement_upload_failed", events)


if __name__ == "__main__":
    unittest.main()
