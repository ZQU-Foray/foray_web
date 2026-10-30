# -*- coding: utf-8 -*-
"""飞书登录回调地址推导（多地址访问）与失败提示的单元测试。"""
import os
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException

from app import config
import app.main as main


class _Req:
    """最小请求替身：只需要 headers 与 url.scheme。"""

    def __init__(self, host, scheme="http", xfp=None, cookies=None):
        self.headers = {"host": host}
        if xfp is not None:
            self.headers["x-forwarded-proto"] = xfp
        self.url = types.SimpleNamespace(scheme=scheme)
        self.cookies = dict(cookies or {})


class RedirectUrlTests(unittest.TestCase):
    """_oauth_redirect_url：只对白名单主机按请求 Host 推导，其余回退配置值。"""

    FALLBACK = "http://127.0.0.1:8080/auth/feishu/callback"

    def _gen(self, host, hosts, **kw):
        with mock.patch.object(config, "FEISHU_REDIRECT_HOSTS", hosts):
            return main._oauth_redirect_url(_Req(host, **kw), self.FALLBACK)

    def test_empty_allowlist_keeps_config(self):
        self.assertEqual(self._gen("192.168.1.9:8080", []), self.FALLBACK)

    def test_allowed_host_derives_callback(self):
        got = self._gen("localhost:8080", ["localhost"])
        self.assertEqual(got, "http://localhost:8080/auth/feishu/callback")

    def test_bare_host_matches_allowlist_with_port(self):
        got = self._gen("192.168.1.9:8080", ["192.168.1.9:8080"])
        self.assertEqual(got, "http://192.168.1.9:8080/auth/feishu/callback")

    def test_unknown_host_falls_back(self):
        got = self._gen("evil.example.com", ["127.0.0.1", "localhost"])
        self.assertEqual(got, self.FALLBACK)

    def test_host_smuggling_attempt_falls_back(self):
        # 带路径或井号的主机名不做任何拼接，直接回退，避免把授权码送去别处
        for host in ("evil.com/127.0.0.1", "127.0.0.1#evil.com", "evil.com\\@127.0.0.1"):
            self.assertEqual(self._gen(host, ["127.0.0.1"]), self.FALLBACK)

    def test_forwarded_proto_https(self):
        got = self._gen("portal.example.com", ["portal.example.com"], xfp="https")
        self.assertEqual(got, "https://portal.example.com/auth/feishu/callback")

    def test_empty_host_falls_back(self):
        self.assertEqual(self._gen("", ["127.0.0.1"]), self.FALLBACK)


class StartRouteTests(unittest.TestCase):
    """授权页 URL 里的 redirect_uri 与当前访问地址一致，state cookie 才能带回来。"""

    ENV = {
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "secret",
        "FEISHU_REDIRECT_URL": "http://127.0.0.1:8080/auth/feishu/callback",
    }

    def _redirect_uri(self, host, hosts):
        with mock.patch.dict(os.environ, self.ENV, clear=False), \
                mock.patch.object(config, "FEISHU_REDIRECT_HOSTS", hosts):
            resp = main.auth_feishu_start(_Req(host), next="/", purpose="login")
        location = resp.headers["location"]
        return parse_qs(urlsplit(location).query)["redirect_uri"][0]

    def test_allowed_host_redirect_uri_matches_request(self):
        self.assertEqual(self._redirect_uri("127.0.0.1:8080", ["127.0.0.1"]),
                         "http://127.0.0.1:8080/auth/feishu/callback")
        self.assertEqual(self._redirect_uri("localhost:8080", ["localhost"]),
                         "http://localhost:8080/auth/feishu/callback")

    def test_disallowed_host_keeps_configured_url(self):
        self.assertEqual(self._redirect_uri("10.0.0.9:8080", ["127.0.0.1"]),
                         "http://127.0.0.1:8080/auth/feishu/callback")

    def test_state_cookie_is_set(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False), \
                mock.patch.object(config, "FEISHU_REDIRECT_HOSTS", []):
            resp = main.auth_feishu_start(_Req("127.0.0.1:8080"), next="/", purpose="login")
        cookies = resp.headers.getlist("set-cookie")
        self.assertTrue(any(c.startswith("oauth_state=") for c in cookies))


class CallbackMessageTests(unittest.TestCase):
    """失败提示要能指向具体原因：缺 cookie（换地址）与 cookie 不匹配（重复点击）。"""

    def test_missing_cookie_hints_same_address(self):
        with self.assertRaises(HTTPException) as ctx:
            main.auth_feishu_callback(_Req("127.0.0.1:8080", cookies={}),
                                      code="c", state="s")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("同一个地址", ctx.exception.detail)

    def test_mismatched_cookie_hints_duplicate_login(self):
        req = _Req("127.0.0.1:8080", cookies={"oauth_state": "other"})
        with self.assertRaises(HTTPException) as ctx:
            main.auth_feishu_callback(req, code="c", state="s")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("重复发起", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
