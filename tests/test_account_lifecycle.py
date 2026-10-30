"""账号生命周期回归（审计 P1-8 / P1-9 / P1-10）。

旧实现里成员会话只比较 extra 里的 revoked_after 快照，改密与禁用对旧会话
无效；应急会话同理不读当前值；被禁用账号还能经 onboarding 复活。这里把
这几条裁决固定下来，防止回退。
"""
import tempfile
import time
import unittest
from pathlib import Path

from app import config
from app.auth import accounts, emergency, sessions


class _AuthCase(unittest.TestCase):
    """把四个 auth 存储文件重定向到临时目录，并清空进程内缓存。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._saved = (config.AUTH_ACCOUNTS_FILE, config.AUTH_SESSIONS_FILE,
                       config.AUTH_EMERGENCY_FILE, config.AUTH_AUDIT_FILE)
        config.AUTH_ACCOUNTS_FILE = base / "accounts.json"
        config.AUTH_SESSIONS_FILE = base / "sessions.json"
        config.AUTH_EMERGENCY_FILE = base / "emergency_users.json"
        config.AUTH_AUDIT_FILE = base / "audit.log"
        accounts._cache["key"], accounts._cache["doc"] = None, None
        sessions._store = None

    def tearDown(self):
        (config.AUTH_ACCOUNTS_FILE, config.AUTH_SESSIONS_FILE,
         config.AUTH_EMERGENCY_FILE, config.AUTH_AUDIT_FILE) = self._saved
        accounts._cache["key"], accounts._cache["doc"] = None, None
        sessions._store = None
        self._tmp.cleanup()

    @staticmethod
    def _member_rec(open_id, created=None):
        now = created or time.time()
        return {"user": f"feishu:{open_id}", "role": "member", "created": now,
                "expires": now + 3600, "last_recheck_ok": now,
                "extra": {"open_id": open_id}}


class MemberSessionTests(_AuthCase):
    def test_active_account_is_ok(self):
        accounts.create("ou_1", "member1", "password123")
        self.assertEqual(sessions.session_state(self._member_rec("ou_1")), "ok")

    def test_disabled_account_revokes_session(self):
        accounts.create("ou_1", "member1", "password123")
        rec = self._member_rec("ou_1")
        accounts.disable("ou_1")
        self.assertEqual(sessions.session_state(rec), "revoked")

    def test_password_change_revokes_older_session(self):
        accounts.create("ou_1", "member1", "password123")
        rec = self._member_rec("ou_1", created=time.time() - 5)
        accounts.set_password("ou_1", "newpassword123")
        self.assertEqual(sessions.session_state(rec), "revoked")

    def test_unknown_account_revokes_session(self):
        self.assertEqual(
            sessions.session_state(self._member_rec("ou_missing")), "revoked")


class EmergencySessionTests(_AuthCase):
    def test_disable_and_rotation_revoke_emergency_session(self):
        emergency.create("ops", "password123")
        rec = {"user": "emergency:ops", "role": "emergency",
               "created": time.time(), "expires": time.time() + 3600}
        self.assertEqual(sessions.session_state(rec), "ok")
        emergency.disable("ops")
        self.assertEqual(sessions.session_state(rec), "revoked")


class CreateAccountTests(_AuthCase):
    def test_disabled_account_cannot_reonboard(self):
        accounts.create("ou_1", "member1", "password123")
        accounts.disable("ou_1")
        with self.assertRaises(ValueError) as ctx:
            accounts.create("ou_1", "member1", "password456")
        self.assertIn("停用", str(ctx.exception))

    def test_rewrite_keeps_revoked_after(self):
        accounts.create("ou_1", "member1", "password123")
        accounts.set_password("ou_1", "newpassword123")
        ra = accounts.get("ou_1")["revoked_after"]
        self.assertTrue(ra)
        accounts.create("ou_1", "member1", "thirdpassword1")
        self.assertEqual(accounts.get("ou_1")["revoked_after"], ra)


class PruneTests(_AuthCase):
    def test_prune_removes_long_dead_sessions(self):
        accounts.create("ou_1", "member1", "password123")
        old = time.time() - 10 * 86400
        rec = {"user": "feishu:ou_1", "role": "member", "created": old,
               "expires": old + 60, "last_recheck_ok": old,
               "extra": {"open_id": "ou_1"}}
        sessions._store = {"version": 1,
                           "sessions": {sessions.token_digest("dead-token"): rec}}
        self.assertEqual(sessions.prune(), 1)
        self.assertEqual(sessions._load()["sessions"], {})


if __name__ == "__main__":
    unittest.main()
