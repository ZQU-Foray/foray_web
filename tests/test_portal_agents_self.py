# -*- coding: utf-8 -*-
"""门户成员自助签发 MCP 令牌（/api/portal/agents/self）。

端点函数直接调用（仓库没有 httpx），只造一个带 state.session 的 Request 替身。

运行：在仓库根目录执行 python -m unittest tests.test_portal_agents_self
"""

import types
import unittest
from pathlib import Path
import tempfile

from fastapi import HTTPException

from app import config
from app.auth import agents
from app.mcp import scopes
from app.portal import api


class _Req:
    """最小的 Request 替身：headers + state.session + 异步 json()。"""

    def __init__(self, body=None, open_id="ou_self", user="demo", role="member"):
        self.headers = {}
        self.state = types.SimpleNamespace(
            session={"user": user, "role": role, "extra": {"open_id": open_id} if open_id else {}})
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._saved = (config.AUTH_AGENTS_FILE, config.AUTH_AUDIT_FILE,
                       config.PORTAL_MAINTAINER_IDS)
        config.AUTH_AGENTS_FILE = base / "agents.json"
        config.AUTH_AUDIT_FILE = base / "audit.log"
        config.PORTAL_MAINTAINER_IDS = []
        agents._cache["key"], agents._cache["doc"] = None, None

    def tearDown(self):
        (config.AUTH_AGENTS_FILE, config.AUTH_AUDIT_FILE,
         config.PORTAL_MAINTAINER_IDS) = self._saved
        agents._cache["key"], agents._cache["doc"] = None, None
        self._tmp.cleanup()

    def _create(self, body=None, open_id="ou_self"):
        import asyncio
        return asyncio.run(api.api_self_agents_create(_Req(body, open_id=open_id)))


class CreateTests(_Case):
    def test_returns_plaintext_once_and_binds_session_identity(self):
        out = self._create({"name": "我的客户端"})
        self.assertTrue(out["token"].startswith("agent_"))
        self.assertEqual(out["agent"]["open_id"], "ou_self")
        self.assertEqual(out["agent"]["created_by"], "self:demo")
        self.assertIn("只显示这一次", out["notice"])
        # 明文不进存储：列表里既没有 token 也没有摘要
        listed = api.api_self_agents(_Req())
        row = listed["data"][0]
        self.assertEqual(row["id"], out["agent"]["id"])
        self.assertNotIn("token", row)
        self.assertNotIn("token_digest", row)

    def test_name_defaults_when_empty(self):
        self.assertEqual(self._create({})["agent"]["name"], "门户网页生成")

    def test_scopes_are_derived_not_chosen(self):
        out = self._create({"scopes": ["sync.read", "tasks.read"]})
        self.assertEqual(out["agent"]["scopes"], sorted(scopes.DEFAULT_SCOPES))
        self.assertNotIn("sync.read", out["agent"]["scopes"])

    def test_maintainer_gets_sync_scope(self):
        config.PORTAL_MAINTAINER_IDS = ["ou_boss"]
        out = self._create(open_id="ou_boss")
        self.assertIn("sync.read", out["agent"]["scopes"])

    def test_body_may_be_empty(self):
        out = self._create(None)
        self.assertTrue(out["token"].startswith("agent_"))

    def test_without_feishu_identity_is_403(self):
        with self.assertRaises(HTTPException) as ctx:
            self._create(open_id="")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("飞书身份", ctx.exception.detail)

    def test_active_limit(self):
        for i in range(api._SELF_MAX_ACTIVE):
            self._create({"name": "第 %d 个" % (i + 1)})
        with self.assertRaises(HTTPException) as ctx:
            self._create({"name": "超了"})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("撤销", ctx.exception.detail)


class ListTests(_Case):
    def test_only_own_tokens(self):
        self._create({"name": "我的"})
        agents.create(name="别人的", open_id="ou_other", scopes=["tasks.read"])
        listed = api.api_self_agents(_Req())
        self.assertEqual([r["name"] for r in listed["data"]], ["我的"])
        self.assertEqual([s["scope"] for s in listed["scopes"]],
                         list(scopes.DEFAULT_SCOPES))
        self.assertEqual(listed["max_active"], api._SELF_MAX_ACTIVE)


class RevokeTests(_Case):
    def test_revoke_kills_token_immediately(self):
        made = self._create()
        self.assertIsNotNone(agents.verify(made["token"]))
        out = api.api_self_agents_revoke(made["agent"]["id"], _Req())
        self.assertTrue(out["ok"])
        self.assertIsNone(agents.verify(made["token"]))

    def test_cannot_revoke_others(self):
        other = agents.create(name="别人的", open_id="ou_other", scopes=["tasks.read"])
        with self.assertRaises(HTTPException) as ctx:
            api.api_self_agents_revoke(other["agent"]["id"], _Req())
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIsNotNone(agents.verify(other["token"]))

    def test_revoke_twice_is_409(self):
        made = self._create()
        api.api_self_agents_revoke(made["agent"]["id"], _Req())
        with self.assertRaises(HTTPException) as ctx:
            api.api_self_agents_revoke(made["agent"]["id"], _Req())
        self.assertEqual(ctx.exception.status_code, 409)

    def test_unknown_id_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            api.api_self_agents_revoke("ag_nope", _Req())
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
