"""Agent 令牌管理接口（/api/portal/agents）：维护者门禁 + 签发/撤销。

维护者 = 应急会话，或 open_id 在 PORTAL_MAINTAINER_IDS 白名单里；
普通成员即使登录成功也只能拿到 403（令牌能读全部只读数据，不能随便发）。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app import config
from app.auth import agents
from app.portal import api


class _State:
    def __init__(self, session):
        self.session = session


class _Req:
    def __init__(self, session=None, body=None):
        self.headers = {"host": "portal.local"}
        self.state = _State(session or {})
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


class MaintainerGateTests(unittest.TestCase):
    def test_member_is_403(self):
        req = _Req({"user": "feishu:ou_1", "role": "member",
                    "extra": {"open_id": "ou_1"}})
        with self.assertRaises(HTTPException) as ctx:
            api._require_maintainer(req)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_emergency_is_allowed(self):
        req = _Req({"user": "emergency:ops", "role": "emergency", "extra": {}})
        self.assertEqual(api._require_maintainer(req), "emergency:ops")

    def test_whitelisted_open_id_is_allowed(self):
        old = api._MAINTAINER_IDS
        api._MAINTAINER_IDS = frozenset({"ou_boss"})
        try:
            req = _Req({"user": "feishu:ou_boss", "role": "member",
                        "extra": {"open_id": "ou_boss"}})
            self.assertEqual(api._require_maintainer(req), "ou_boss")
        finally:
            api._MAINTAINER_IDS = old

    def test_cross_site_origin_is_403(self):
        req = _Req({"user": "emergency:ops", "role": "emergency"})
        req.headers["origin"] = "http://evil.example"
        with self.assertRaises(HTTPException) as ctx:
            api._require_maintainer(req)
        self.assertEqual(ctx.exception.status_code, 403)


class AgentEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._saved = (config.AUTH_AGENTS_FILE, config.AUTH_AUDIT_FILE)
        config.AUTH_AGENTS_FILE = base / "agents.json"
        config.AUTH_AUDIT_FILE = base / "audit.log"
        agents._cache["key"], agents._cache["doc"] = None, None
        self._saved_ids = api._MAINTAINER_IDS
        api._MAINTAINER_IDS = frozenset({"ou_boss"})
        self.req = _Req({"user": "feishu:ou_boss", "role": "member",
                         "extra": {"open_id": "ou_boss"}})

    def tearDown(self):
        config.AUTH_AGENTS_FILE, config.AUTH_AUDIT_FILE = self._saved
        agents._cache["key"], agents._cache["doc"] = None, None
        api._MAINTAINER_IDS = self._saved_ids
        self._tmp.cleanup()

    def test_list_and_scope_catalog(self):
        out = api.api_agents(self.req)
        self.assertEqual(out["data"], [])
        self.assertIn("meta.read", [s["scope"] for s in out["scopes"]])
        self.assertTrue(out["default_scopes"])

    def test_create_returns_token_once_then_list_hides_it(self):
        req = _Req(self.req.state.session,
                   {"name": "claude", "open_id": "ou_boss",
                    "scopes": ["tasks.read", "meta.read"]})
        made = asyncio.run(api.api_agents_create(req))
        self.assertTrue(made["token"].startswith("agent_"))
        self.assertNotIn("token_digest", made["agent"])
        rows = api.api_agents(self.req)["data"]
        self.assertEqual([r["name"] for r in rows], ["claude"])
        self.assertNotIn(made["token"], str(rows))

    def test_create_validates_input(self):
        req = _Req(self.req.state.session, {"name": "", "open_id": "ou_boss",
                                            "scopes": ["tasks.read"]})
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(api.api_agents_create(req))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_revoke(self):
        made = asyncio.run(api.api_agents_create(_Req(
            self.req.state.session,
            {"name": "x", "open_id": "ou_boss", "scopes": ["tasks.read"]})))
        agent_id = made["agent"]["id"]
        self.assertEqual(api.api_agents_revoke(agent_id, self.req), {"ok": True})
        self.assertIsNone(agents.verify(made["token"]))
        with self.assertRaises(HTTPException) as ctx:
            api.api_agents_revoke(agent_id, self.req)
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(HTTPException) as ctx2:
            api.api_agents_revoke("ag_missing", self.req)
        self.assertEqual(ctx2.exception.status_code, 404)

    def test_audit_records_create_and_revoke(self):
        made = asyncio.run(api.api_agents_create(_Req(
            self.req.state.session,
            {"name": "y", "open_id": "ou_boss", "scopes": ["meta.read"]})))
        api.api_agents_revoke(made["agent"]["id"], self.req)
        text = config.AUTH_AUDIT_FILE.read_text(encoding="utf-8")
        self.assertIn("portal_agent_create", text)
        self.assertIn("portal_agent_revoke", text)


if __name__ == "__main__":
    unittest.main()
