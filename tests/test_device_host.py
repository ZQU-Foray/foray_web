"""设备 host 白名单策略测试（审计 P1-5）。

覆盖：合法 IP 放行、越界拒绝、域名/带端口/空值拒绝、显式白名单生效、
注册与更新入口在强制模式下的拦截，以及 DEVICE_HOST_ENFORCE=0 的告警放行。
"""
import os
import time
import tempfile
import unittest
from pathlib import Path

from app import config, net_policy
from app import device_registry


class _PolicyCase(unittest.TestCase):
    """把设备库重定向到临时文件，避免污染 config/devices.json。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = config.DEVICES_FILE
        config.DEVICES_FILE = Path(self._tmp.name) / "devices.json"
        self._old_enforce = config.DEVICE_HOST_ENFORCE
        self._old_allow = config.DEVICE_HOST_ALLOWLIST
        self._old_subnet = os.environ.pop("SCAN_SUBNET", None)

    def tearDown(self):
        config.DEVICES_FILE = self._old_path
        config.DEVICE_HOST_ENFORCE = self._old_enforce
        config.DEVICE_HOST_ALLOWLIST = self._old_allow
        if self._old_subnet is not None:
            os.environ["SCAN_SUBNET"] = self._old_subnet
        self._tmp.cleanup()


class CheckHostTests(_PolicyCase):
    def test_loopback_allowed(self):
        self.assertEqual(net_policy.check_host("127.0.0.1"), (True, ""))

    def test_out_of_range_rejected(self):
        ok, why = net_policy.check_host("203.0.113.9")
        self.assertFalse(ok)
        self.assertIn("不在允许网段", why)

    def test_non_ip_rejected(self):
        for bad in ("example.com", "", "192.168.1.5:4441", "  "):
            ok, why = net_policy.check_host(bad)
            self.assertFalse(ok, bad)
            self.assertTrue(why)

    def test_explicit_allowlist(self):
        config.DEVICE_HOST_ALLOWLIST = "10.1.2.0/24"
        self.assertTrue(net_policy.check_host("10.1.2.7")[0])

    def test_scan_subnet_env(self):
        os.environ["SCAN_SUBNET"] = "10.9.8"
        self.assertTrue(net_policy.check_host("10.9.8.20")[0])

    def test_enforce_flag_follows_config(self):
        config.DEVICE_HOST_ENFORCE = True
        self.assertTrue(net_policy.enforcing())
        config.DEVICE_HOST_ENFORCE = False
        self.assertFalse(net_policy.enforcing())


class RegistryEnforcementTests(_PolicyCase):
    def _dev(self, host):
        return {"id": "tcp-1", "transport": "vllink_tcp", "name": "测试设备",
                "probe_kind": "vllink", "host": host}

    def test_register_rejects_out_of_range(self):
        config.DEVICE_HOST_ENFORCE = True
        with self.assertRaises(ValueError):
            device_registry.register(self._dev("203.0.113.9"))

    def test_register_warns_when_disabled(self):
        config.DEVICE_HOST_ENFORCE = False
        doc = device_registry.register(self._dev("203.0.113.9"))
        self.assertIn("tcp-1", doc["devices"])

    def test_update_rejects_out_of_range_host(self):
        config.DEVICE_HOST_ENFORCE = True
        device_registry.register(self._dev("127.0.0.1"))
        with self.assertRaises(ValueError):
            device_registry.update("tcp-1", {"host": "203.0.113.9"})

    def test_update_allows_in_range_host(self):
        config.DEVICE_HOST_ENFORCE = True
        device_registry.register(self._dev("127.0.0.1"))
        doc = device_registry.update("tcp-1", {"host": "127.0.0.2"})
        self.assertEqual(doc["devices"]["tcp-1"]["host"], "127.0.0.2")


class ScanGateTests(_PolicyCase):
    """扫描接口的门禁：非组长 403、冷却期内 429（成功路径由 e2e 覆盖）。"""

    def _req(self, user="member-1"):
        req = type("Req", (), {})()
        req.state = type("St", (), {})()
        req.state.session = {"user": user, "created": 0, "extra": {}}
        return req

    def test_member_without_leader_forbidden(self):
        from app import main as app_main
        old = app_main.portal_api.leader_ctx
        app_main.portal_api.leader_ctx = lambda request: (None, [])
        try:
            with self.assertRaises(app_main.HTTPException) as ctx:
                app_main.v2_scan_sta(self._req())
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            app_main.portal_api.leader_ctx = old

    def test_cooldown_blocks_rapid_scan(self):
        from app import main as app_main
        old = app_main.portal_api.leader_ctx
        old_at = dict(app_main._SCAN_LAST)
        app_main.portal_api.leader_ctx = lambda request: ("ou_x", ["A"])
        app_main._SCAN_LAST["at"] = time.time()
        try:
            with self.assertRaises(app_main.HTTPException) as ctx:
                app_main.v2_scan_sta(self._req("member-1"))
            self.assertEqual(ctx.exception.status_code, 429)
        finally:
            app_main.portal_api.leader_ctx = old
            app_main._SCAN_LAST.clear()
            app_main._SCAN_LAST.update(old_at)


if __name__ == "__main__":
    unittest.main()
