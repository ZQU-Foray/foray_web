"""MCP 端点（/mcp）：鉴权、协议方法、scope 过滤与错误码。

端点不依赖浏览器会话：只认 `Authorization: Bearer <agent 令牌>`。
这里直接调用端点的协程函数（仓库没有 httpx，TestClient 用不了）。
"""
import json
import tempfile
import unittest
from pathlib import Path

from app import config
from app.auth import agents
from app.mcp import server
from app.portal import store as S
from tests.test_cache_store import DbCase


class _Req:
    """最小的 Request 替身：headers + 异步 json()。"""

    def __init__(self, body, token=None, origin=None):
        self.headers = {}
        if token:
            self.headers["authorization"] = "Bearer " + token
        if origin:
            self.headers["origin"] = origin
        self._body = body

    async def json(self):
        return self._body


class _McpCase(DbCase):
    def setUp(self):
        super().setUp()
        self._tmp2 = tempfile.TemporaryDirectory()
        base = Path(self._tmp2.name)
        self._saved = (config.AUTH_AGENTS_FILE, config.AUTH_AUDIT_FILE)
        config.AUTH_AGENTS_FILE = base / "agents.json"
        config.AUTH_AUDIT_FILE = base / "audit.log"
        agents._cache["key"], agents._cache["doc"] = None, None
        self.token = agents.create(name="t", open_id="ou_1",
                                   scopes=["meta.read", "tasks.read"])["token"]

    def tearDown(self):
        config.AUTH_AGENTS_FILE, config.AUTH_AUDIT_FILE = self._saved
        agents._cache["key"], agents._cache["doc"] = None, None
        self._tmp2.cleanup()
        super().tearDown()

    def call(self, body, token=None, origin=None):
        import asyncio
        return asyncio.run(server.mcp_endpoint(
            _Req(body, token=self.token if token is None else token, origin=origin)))

    def body(self, resp):
        return json.loads(resp.body)

    def seed_snapshot(self):
        ver = S.next_version()
        S.write_candidate(ver, "tasks", [{"id": "recT1", "name": "事项一"}], [])
        S.publish(ver, {"tasks": 1}, 0.1)


class AuthTests(_McpCase):
    def test_missing_token_is_401_with_challenge(self):
        resp = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, token="")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Bearer", resp.headers.get("www-authenticate", ""))

    def test_bad_token_is_401(self):
        resp = self.call({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         token="agent_" + "f" * 64)
        self.assertEqual(resp.status_code, 401)

    def test_revoked_token_is_401(self):
        made = agents.create(name="x", open_id="ou_2", scopes=["meta.read"])
        agents.revoke(made["agent"]["id"])
        resp = self.call({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         token=made["token"])
        self.assertEqual(resp.status_code, 401)

    def test_browser_origin_is_403(self):
        resp = self.call({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         origin="http://127.0.0.1:8080")
        self.assertEqual(resp.status_code, 403
                         )
        self.assertEqual(self.body(resp)["error"], "forbidden")


class ProtocolTests(_McpCase):
    def test_initialize(self):
        resp = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        result = self.body(resp)["result"]
        self.assertEqual(result["protocolVersion"], server.PROTOCOL_VERSION)
        self.assertEqual(result["serverInfo"]["name"], "foray-portal-mcp")
        self.assertIn("tools", result["capabilities"])

    def test_initialized_notification_is_202(self):
        resp = self.call({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(resp.status_code, 202)

    def test_unknown_method(self):
        resp = self.call({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
        self.assertEqual(self.body(resp)["error"]["code"], -32601)

    def test_batch_rejected(self):
        resp = self.call([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.body(resp)["error"]["code"], -32600)

    def test_non_object_rejected(self):
        resp = self.call("hello")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.body(resp)["error"]["code"], -32600)


class ToolTests(_McpCase):
    def test_list_filters_by_scope_and_sorted(self):
        resp = self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in self.body(resp)["result"]["tools"]]
        self.assertEqual(names, sorted(names))
        self.assertIn("portal_meta", names)
        self.assertIn("portal_list_tasks", names)
        self.assertNotIn("portal_sync_status", names)   # 未授予 sync.read

    def test_call_without_scope_is_32001_and_audited(self):
        resp = self.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "portal_sync_status", "arguments": {}}})
        self.assertEqual(self.body(resp)["error"]["code"], -32001)
        text = config.AUTH_AUDIT_FILE.read_text(encoding="utf-8")
        self.assertIn("mcp_tool_call", text)
        self.assertIn("denied", text)

    def test_call_without_snapshot_is_32010(self):
        resp = self.call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "portal_meta", "arguments": {}}})
        self.assertEqual(self.body(resp)["error"]["code"], -32010)

    def test_successful_call_returns_json_text(self):
        self.seed_snapshot()
        resp = self.call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "portal_meta", "arguments": {}}})
        result = self.body(resp)["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertIn("snapshot_version", payload)
        self.assertIsNotNone(agents.get(agents.list_agents()[0]["id"])["last_used_at"])

    def test_rate_limit(self):
        old = server._rate
        server._rate = {}
        try:
            for _ in range(server.RATE_PER_MIN + 1):
                server._rate_ok("ag_x")
            self.assertFalse(server._rate_ok("ag_x"))
            # 换一个令牌不受影响（按令牌独立计数）
            self.assertTrue(server._rate_ok("ag_y"))
        finally:
            server._rate = old


if __name__ == "__main__":
    unittest.main()
