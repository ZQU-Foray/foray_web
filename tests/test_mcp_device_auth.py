# -*- coding: utf-8 -*-
"""设备码授权（/oauth/device*）：成员用飞书身份自助换发 agent 令牌。

覆盖四层：
- 存储层 app/mcp/device_store.py：设备码/用户码生成、一次性领取、过期、拒绝、限频；
- 端点层 app/mcp/device_auth.py：authorize/token 的错误码、确认页渲染、批准四条闸门；
- 门禁层 app/auth/guard.py：公开路径与批准路径的分类；
- 命令行 scripts/mcp_login.py：轮询、落盘权限、拒绝时的退出码。
"""
import asyncio
import json
import os
import pathlib
import stat
import sys
import time
import tempfile
import unittest
from unittest import mock

from app import config
from app.auth import accounts, agents, guard, sessions
from app.mcp import device_auth, device_store, scopes

_SESSION_COOKIE = config.SESSION_COOKIE
_TENANT = "tk_demo"


class _Client:
    """假客户端地址（限频与审计都按它计数）。"""
    def __init__(self, host="127.0.0.1"):
        self.host = host


class _Url:
    scheme = "http"
    netloc = "portal.test"


class _Req:
    """最小可用 Request：够 device_auth 读 headers/cookies/client/url 与取 body。"""
    def __init__(self, body=None, token=None, ip="127.0.0.1", headers=None):
        self.headers = {"host": "portal.test", "content-type": "application/json"}
        self.headers.update(headers or {})
        self.client = _Client(ip)
        self.url = _Url()
        self.cookies = {_SESSION_COOKIE: token} if token else {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body

    async def form(self):
        return self._body


def _run(coro):
    return asyncio.run(coro)


def _json(resp):
    return json.loads(resp.body.decode("utf-8"))


def _text(resp):
    return resp.body.decode("utf-8")


class _DeviceCase(unittest.TestCase):
    """每个用例一套独立的状态目录：会话/账号/令牌/设备码/审计全部落到临时目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        patcher = mock.patch.multiple(
            config,
            AUTH_SESSIONS_FILE=self.dir / "sessions.json",
            AUTH_ACCOUNTS_FILE=self.dir / "accounts.json",
            AUTH_AGENTS_FILE=self.dir / "agents.json",
            AUTH_AUDIT_FILE=self.dir / "audit.log",
            AUTH_DEVICE_FILE=self.dir / "device_codes.json",
            PORTAL_MAINTAINER_IDS=[],
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        env = {"FEISHU_APP_ID": "cli_demo", "FEISHU_APP_SECRET": "secret",
               "FEISHU_REDIRECT_URL": "http://portal.test/auth/feishu/callback",
               "FEISHU_ALLOWED_TENANT_KEYS": _TENANT}
        env_patcher = mock.patch.dict(os.environ, env)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        device_store.reset_rate()
        self.addCleanup(device_store.reset_rate)
        # 确认页的失败计数是模块级状态，按用例清空，避免互相拖累
        with device_auth._confirm_lock:
            device_auth._confirm_fail.clear()

    def account(self, open_id, onboarded=True):
        """造一个已开通的成员账号（未开通的不建账号行）。"""
        if onboarded:
            accounts.create(open_id=open_id, username=open_id,
                            password="pw-123456", display=open_id)  # noqa: S106

    def session(self, open_id, onboarded=True, tenant=_TENANT):
        return sessions.create(user="feishu:" + open_id, display=open_id,
                               role="member",
                               extra={"open_id": open_id, "onboarded": onboarded,
                                      "tenant_key": tenant})["token"]

    def start(self, client_name="claude-code", ip="127.0.0.1", body=None):
        """申请设备码，返回 (user_code, device_code)。"""
        payload = {"client_name": client_name} if body is None else body
        out = _json(_run(device_auth.device_authorize(_Req(payload, ip=ip))))
        return out["user_code"], out["device_code"]

    def approve(self, user_code, token):
        return _run(device_auth.device_confirm(
            _Req({"user_code": user_code, "action": "approve"}, token=token)))

    def poll(self, device_code):
        return _run(device_auth.device_token(_Req({"device_code": device_code})))


class DeviceStoreTests(_DeviceCase):
    """存储层：生成、一次性、过期、拒绝、限频。"""

    def test_user_code_shape_and_normalize(self):
        code, _ = self.start()
        self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual(device_store.normalize_user_code(code.lower().replace("-", "")),
                         code.replace("-", ""))
        self.assertEqual(device_store.format_user_code(code.replace("-", "")), code)

    def test_unknown_user_code_is_none(self):
        self.assertIsNone(device_store.find_by_user_code("ZZZZ-ZZZZ"))
        self.assertIsNone(device_store.find_by_user_code(""))

    def test_claim_is_one_time(self):
        user_code, device_code = self.start()
        device_store.approve(user_code, open_id="ou_a",
                             agent_id="ag_1", token="agent_secret",  # noqa: S106
                             approved_by="ou_a")
        result, payload = device_store.claim(device_code)
        self.assertEqual(result, "ok")
        self.assertEqual(payload["token"], "agent_secret")
        self.assertEqual(device_store.claim(device_code)[0], "expired")

    def test_expired_device_code(self):
        _, device_code = self.start()
        later = time.time() + config.MCP_DEVICE_TTL_SEC + 5
        self.assertEqual(device_store.claim(device_code, now=later)[0], "expired")

    def test_denied_device_code(self):
        user_code, device_code = self.start()
        self.assertTrue(device_store.deny(user_code, by="ou_a"))
        self.assertEqual(device_store.claim(device_code)[0], "denied")

    def test_pending_respects_interval(self):
        _, device_code = self.start()
        self.assertEqual(device_store.claim(device_code)[0], "pending")
        # 立刻再问一次：还没到 next_poll_at，要求放慢
        self.assertEqual(device_store.claim(device_code)[0], "slow_down")

    def test_store_keeps_only_digests(self):
        user_code, device_code = self.start()
        raw = (self.dir / "device_codes.json").read_text(encoding="utf-8")
        self.assertNotIn(device_code, raw)
        self.assertNotIn(user_code.replace("-", ""), raw)
        self.assertIn("device_digest", raw)

    def test_rate_limit_per_ip(self):
        limit = int(config.MCP_DEVICE_RATE_PER_MIN)
        for _ in range(limit):
            self.assertTrue(device_store.rate_ok("10.0.0.9", now=1000.0))
        self.assertFalse(device_store.rate_ok("10.0.0.9", now=1000.0))
        self.assertTrue(device_store.rate_ok("10.0.0.8", now=1000.0))
        # 窗口过去后恢复
        self.assertTrue(device_store.rate_ok("10.0.0.9", now=1000.0 + 61))


class AuthorizeEndpointTests(_DeviceCase):
    """申请端点：形状、限频、坏请求体。"""

    def test_authorize_returns_rfc8628_fields(self):
        out = _json(_run(device_auth.device_authorize(_Req({"client_name": "cursor"}))))
        for key in ("device_code", "user_code", "verification_uri",
                    "verification_uri_complete", "expires_in", "interval"):
            self.assertIn(key, out)
        self.assertEqual(out["expires_in"], config.MCP_DEVICE_TTL_SEC)
        self.assertEqual(out["interval"], config.MCP_DEVICE_INTERVAL_SEC)
        self.assertEqual(out["verification_uri"], "http://portal.test/oauth/device")
        self.assertIn("user_code=", out["verification_uri_complete"])

    def test_rate_limited_returns_429(self):
        limit = int(config.MCP_DEVICE_RATE_PER_MIN)
        for _ in range(limit):
            _run(device_auth.device_authorize(_Req({}, ip="10.1.1.1")))
        resp = _run(device_auth.device_authorize(_Req({}, ip="10.1.1.1")))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(_json(resp)["error"], "slow_down")

    def test_bad_body_is_tolerated(self):
        req = _Req()
        req.json = mock.AsyncMock(side_effect=ValueError("坏的 JSON"))
        resp = _run(device_auth.device_authorize(req))
        self.assertEqual(resp.status_code, 200)


class TokenEndpointTests(_DeviceCase):
    """轮询端点：四个错误码 + 成功形状。"""

    def test_pending(self):
        _, device_code = self.start()
        resp = self.poll(device_code)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(_json(resp)["error"], "authorization_pending")
        self.assertEqual(_json(resp)["interval"], config.MCP_DEVICE_INTERVAL_SEC)

    def test_slow_down(self):
        _, device_code = self.start()
        self.poll(device_code)
        self.assertEqual(_json(self.poll(device_code))["error"], "slow_down")

    def test_missing_device_code(self):
        resp = _run(device_auth.device_token(_Req({})))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(_json(resp)["error"], "invalid_request")

    def test_expired(self):
        _, device_code = self.start()
        later = time.time() + config.MCP_DEVICE_TTL_SEC + 5
        with mock.patch("time.time", return_value=later):
            self.assertEqual(_json(self.poll(device_code))["error"], "expired_token")

    def test_denied(self):
        user_code, device_code = self.start()
        device_store.deny(user_code, by="ou_a")
        self.assertEqual(_json(self.poll(device_code))["error"], "access_denied")

    def test_success_carries_scope_and_is_single_use(self):
        self.account("ou_a")
        token = self.session("ou_a")
        user_code, device_code = self.start()
        self.assertEqual(self.approve(user_code, token).status_code, 200)
        body = _json(self.poll(device_code))
        self.assertTrue(body["access_token"].startswith("agent_"))
        self.assertEqual(len(body["access_token"]), 70)
        self.assertEqual(body["token_type"], "Bearer")
        granted = body["scope"].split()
        self.assertEqual(sorted(granted), sorted(scopes.DEFAULT_SCOPES))
        self.assertNotIn("sync.read", granted)
        self.assertEqual(body["open_id"], "ou_a")
        self.assertEqual(_json(self.poll(device_code))["error"], "expired_token")


class ConfirmGateTests(_DeviceCase):
    """批准的四条闸门：会话、开通、组织、数量上限。"""

    def test_approve_creates_token(self):
        self.account("ou_a")
        user_code, _ = self.start()
        resp = self.approve(user_code, self.session("ou_a"))
        self.assertEqual(resp.status_code, 200)
        rows = agents.list_agents()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["open_id"], "ou_a")
        self.assertEqual(rows[0]["name"], "claude-code")
        self.assertEqual(rows[0]["created_by"], "feishu_oauth")
        self.assertFalse(rows[0].get("revoked_at"))

    def test_requires_session(self):
        self.account("ou_a")
        user_code, _ = self.start()
        self.assertEqual(self.approve(user_code, None).status_code, 401)
        self.assertEqual(agents.list_agents(), [])

    def test_requires_onboarded(self):
        user_code, _ = self.start()
        resp = self.approve(user_code, self.session("ou_new", onboarded=False))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(agents.list_agents(), [])

    def test_cross_tenant_is_403(self):
        self.account("ou_a")
        user_code, _ = self.start()
        resp = self.approve(user_code, self.session("ou_a", tenant="tk_other"))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(agents.list_agents(), [])

    def test_unknown_user_code_is_400(self):
        self.account("ou_a")
        self.assertEqual(self.approve("ZZZZ-ZZZZ", self.session("ou_a")).status_code, 400)

    def test_max_active_tokens(self):
        self.account("ou_a")
        for i in range(int(config.MCP_DEVICE_MAX_ACTIVE)):
            agents.create(name="old%d" % i, open_id="ou_a", scopes=["tasks.read"])
        user_code, _ = self.start()
        resp = self.approve(user_code, self.session("ou_a"))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(agents.list_agents()), int(config.MCP_DEVICE_MAX_ACTIVE))

    def test_deny_does_not_create_token(self):
        self.account("ou_a")
        user_code, _ = self.start()
        resp = _run(device_auth.device_confirm(
            _Req({"user_code": user_code, "action": "deny"}, token=self.session("ou_a"))))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(agents.list_agents(), [])

    def test_confirm_rate_limit_after_failures(self):
        self.account("ou_a")
        token = self.session("ou_a")
        for _ in range(10):
            self.approve("ZZZZ-ZZZZ", token)
        self.assertEqual(self.approve("ZZZZ-ZZZZ", token).status_code, 429)


class ScopeDerivationTests(_DeviceCase):
    """scope 按角色派生，维护者多拿 sync.read。"""

    def test_member_scopes(self):
        self.assertEqual(sorted(device_auth._scopes_for("ou_a")),
                         sorted(scopes.DEFAULT_SCOPES))

    def test_maintainer_gets_sync(self):
        with mock.patch.object(config, "PORTAL_MAINTAINER_IDS", ["ou_boss"]):
            granted = device_auth._scopes_for("ou_boss")
        self.assertIn("sync.read", granted)
        self.assertEqual(len(granted), len(scopes.DEFAULT_SCOPES) + 1)


class PageTests(_DeviceCase):
    """确认页：未登录引导、未开通拦下、正常页、转义。"""

    def test_page_without_session_offers_feishu_login(self):
        resp = _run(device_auth.device_page(_Req(), user_code=""))
        html = _text(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("用飞书登录", html)
        self.assertIn("/auth/feishu/start?next=", html)

    def test_page_without_onboarding_is_403(self):
        token = self.session("ou_new", onboarded=False)
        resp = _run(device_auth.device_page(_Req(token=token), user_code=""))
        self.assertEqual(resp.status_code, 403)
        self.assertIn("/onboarding", _text(resp))

    def test_page_shows_scopes_and_buttons(self):
        self.account("ou_a")
        token = self.session("ou_a")
        user_code, _ = self.start()
        html = _text(_run(device_auth.device_page(_Req(token=token), user_code=user_code)))
        self.assertIn("claude-code", html)
        self.assertIn("tasks.read", html)
        self.assertNotIn("sync.read", html)
        self.assertIn('value="approve"', html)
        self.assertIn('value="deny"', html)

    def test_page_accepts_compact_user_code(self):
        self.account("ou_a")
        token = self.session("ou_a")
        user_code, _ = self.start()
        compact = user_code.lower().replace("-", "")
        html = _text(_run(device_auth.device_page(_Req(token=token), user_code=compact)))
        self.assertIn("claude-code", html)

    def test_bad_user_code_is_400(self):
        self.account("ou_a")
        token = self.session("ou_a")
        resp = _run(device_auth.device_page(_Req(token=token), user_code="ZZZZ-ZZZZ"))
        self.assertEqual(resp.status_code, 400)

    def test_client_name_is_escaped(self):
        self.account("ou_a")
        token = self.session("ou_a")
        user_code, _ = self.start(client_name="<script>alert(1)</script>")
        html = _text(_run(device_auth.device_page(_Req(token=token), user_code=user_code)))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class GuardTests(unittest.TestCase):
    """门禁分类：CLI 端点公开、批准走管理接口口径。"""

    def test_device_endpoints_are_public(self):
        for path in ("/oauth/device", "/oauth/device/authorize", "/oauth/device/token"):
            self.assertEqual(guard.classify(path), "public", path)

    def test_confirm_is_member_api(self):
        self.assertEqual(guard.classify("/oauth/device/confirm"), "admin_api")

    def test_mcp_and_agent_admin_unchanged(self):
        self.assertEqual(guard.classify("/mcp"), "mcp_api")
        self.assertEqual(guard.classify("/api/portal/agents"), "maintainer_api")


class CliTests(unittest.TestCase):
    """命令行：轮询到批准后落盘 600，拒绝时退出码 3。"""

    def setUp(self):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        # 脚本不在包内，测试里临时加 sys.path
        import mcp_login                                     # noqa: PLC0415
        self.cli = mcp_login
        self._tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self._tmp.name) / "token.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _patch(self, results):
        calls = {"n": 0}

        def fake_post(url, _payload, _timeout=15.0):
            if url.endswith("/authorize"):
                return 200, {"device_code": "dc", "user_code": "ABCD-EFGH",
                             "verification_uri": "http://p/oauth/device",
                             "verification_uri_complete": "http://p/oauth/device?user_code=ABCD-EFGH",
                             "expires_in": 600, "interval": 0.01}
            calls["n"] += 1
            return results[min(calls["n"], len(results)) - 1]

        return mock.patch.object(self.cli, "_post", side_effect=fake_post)

    def test_approved_writes_600_file(self):
        ok = (200, {"access_token": "agent_" + "x" * 64, "token_type": "Bearer",
                    "scope": "tasks.read", "agent_id": "ag_1", "open_id": "ou_a",
                    "client_name": "cursor"})
        with self._patch([ok]):
            with mock.patch.object(self.cli.time, "sleep", lambda _s: None):
                code = self.cli.main(["--base-url", "http://p", "--out", str(self.out)])
        self.assertEqual(code, 0)
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(doc["agent_id"], "ag_1")
        self.assertEqual(doc["mcp_url"], "http://p/mcp")
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)

    def test_denied_exits_3(self):
        with self._patch([(400, {"error": "access_denied"})]):
            with mock.patch.object(self.cli.time, "sleep", lambda _s: None):
                code = self.cli.main(["--base-url", "http://p", "--out", str(self.out)])
        self.assertEqual(code, 3)
        self.assertFalse(self.out.exists())

    def test_no_save_keeps_file_absent(self):
        ok = (200, {"access_token": "agent_" + "x" * 64, "agent_id": "ag_2"})
        with self._patch([ok]):
            with mock.patch.object(self.cli.time, "sleep", lambda _s: None):
                code = self.cli.main(["--base-url", "http://p", "--out", str(self.out),
                                      "--no-save"])
        self.assertEqual(code, 0)
        self.assertFalse(self.out.exists())

    def test_non_http_base_url_is_rejected(self):
        with self.assertRaises(ValueError):
            self.cli._post("file:///etc/passwd", {})


if __name__ == "__main__":
    unittest.main()
