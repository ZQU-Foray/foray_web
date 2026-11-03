"""Agent 令牌存储（MCP 接入）：签发、校验、撤销与 scope 归一。

令牌是不落明文的独立凭据：文件里只有 sha256 摘要，明文只在创建响应里
出现一次；撤销即时生效（verify 直接返回 None）。
"""
import json
import tempfile
import unittest
from pathlib import Path

from app import config
from app.auth import agents
from app.mcp import scopes


class _AgentCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._saved = (config.AUTH_AGENTS_FILE, config.AUTH_AUDIT_FILE)
        config.AUTH_AGENTS_FILE = base / "agents.json"
        config.AUTH_AUDIT_FILE = base / "audit.log"
        agents._cache["key"], agents._cache["doc"] = None, None

    def tearDown(self):
        config.AUTH_AGENTS_FILE, config.AUTH_AUDIT_FILE = self._saved
        agents._cache["key"], agents._cache["doc"] = None, None
        self._tmp.cleanup()

    def _make(self, name="ci-agent", scopes_=("tasks.read",), open_id="ou_1"):
        return agents.create(name=name, open_id=open_id, scopes=list(scopes_))


class CreateTests(_AgentCase):
    def test_returns_plaintext_once_and_stores_digest_only(self):
        made = self._make()
        token = made["token"]
        self.assertTrue(token.startswith("agent_"))
        self.assertEqual(len(token), len("agent_") + 64)
        raw = config.AUTH_AGENTS_FILE.read_text(encoding="utf-8")
        self.assertNotIn(token, raw)
        self.assertIn(agents.digest(token), raw)
        self.assertNotIn("token_digest", made["agent"])

    def test_scope_normalized_and_sorted(self):
        made = self._make(scopes_=["tasks.read", "meta.read", "tasks.read"])
        self.assertEqual(made["agent"]["scopes"], ["meta.read", "tasks.read"])

    def test_unknown_scope_rejected(self):
        with self.assertRaises(ValueError):
            self._make(scopes_=["tasks.write"])

    def test_empty_inputs_rejected(self):
        for kwargs in ({"name": "", "scopes": ["tasks.read"], "open_id": "ou"},
                       {"name": "x" * 41, "scopes": ["tasks.read"], "open_id": "ou"},
                       {"name": "ok", "scopes": [], "open_id": "ou"},
                       {"name": "ok", "scopes": ["tasks.read"], "open_id": "  "}):
            with self.assertRaises(ValueError):
                agents.create(**kwargs)

    def test_audit_written(self):
        made = self._make()
        text = config.AUTH_AUDIT_FILE.read_text(encoding="utf-8")
        self.assertIn("agent_create", text)
        self.assertIn(made["agent"]["id"], text)


class VerifyTests(_AgentCase):
    def test_verify_roundtrip(self):
        made = self._make(scopes_=["meta.read"])
        rec = agents.verify(made["token"])
        self.assertIsNotNone(rec)
        self.assertEqual(rec["id"], made["agent"]["id"])
        self.assertEqual(rec["scopes"], ["meta.read"])

    def test_verify_rejects_unknown_and_blank(self):
        self.assertIsNone(agents.verify("agent_" + "0" * 64))
        self.assertIsNone(agents.verify(""))
        self.assertIsNone(agents.verify(None))

    def test_touch_updates_last_used(self):
        made = self._make()
        agents.touch(made["agent"]["id"])
        self.assertIsNotNone(agents.get(made["agent"]["id"])["last_used_at"])


class RevokeTests(_AgentCase):
    def test_revoke_kills_token_immediately(self):
        made = self._make()
        self.assertTrue(agents.revoke(made["agent"]["id"], by="tester"))
        self.assertIsNone(agents.verify(made["token"]))
        self.assertIsNotNone(agents.get(made["agent"]["id"])["revoked_at"])

    def test_revoke_is_idempotent_and_404_for_unknown(self):
        made = self._make()
        first = agents.revoke(made["agent"]["id"])
        second = agents.revoke(made["agent"]["id"])
        self.assertEqual(first["revoked_at"], second["revoked_at"])
        self.assertIsNone(agents.revoke("ag_missing"))

    def test_list_hides_digest_and_sorts_newest_first(self):
        a = self._make(name="a")
        b = self._make(name="b")
        rows = agents.list_agents()
        self.assertEqual([r["name"] for r in rows], ["b", "a"])
        self.assertTrue(all("token_digest" not in r for r in rows))
        self.assertEqual(rows[0]["id"], b["agent"]["id"])
        self.assertEqual(rows[1]["id"], a["agent"]["id"])


class ScopeHelperTests(unittest.TestCase):
    def test_describe_covers_all_scopes_once(self):
        rows = scopes.describe()
        self.assertEqual([r["scope"] for r in rows], list(scopes.ALL_SCOPES))
        self.assertEqual(sum(1 for r in rows if r["default"]),
                         len(scopes.DEFAULT_SCOPES))

    def test_normalize_accepts_comma_string(self):
        self.assertEqual(scopes.normalize("meta.read, tasks.read"),
                         ["meta.read", "tasks.read"])

    def test_visible_tools_filters_by_scope(self):
        from app.mcp import tools
        agent = {"scopes": ["tasks.read"]}
        names = scopes.visible_tools(agent, tools.TOOLS)
        self.assertIn("portal_list_tasks", names)
        self.assertNotIn("portal_meta", names)
        self.assertNotIn("portal_sync_status", names)


class StorageTests(_AgentCase):
    def test_corrupt_file_treated_as_empty(self):
        config.AUTH_AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.AUTH_AGENTS_FILE.write_text("{not json", encoding="utf-8")
        agents._cache["key"], agents._cache["doc"] = None, None
        self.assertEqual(agents.list_agents(), [])
        made = self._make()
        self.assertIsNotNone(agents.verify(made["token"]))

    def test_file_is_owner_only(self):
        self._make()
        mode = config.AUTH_AGENTS_FILE.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        doc = json.loads(config.AUTH_AGENTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(doc["version"], 1)


if __name__ == "__main__":
    unittest.main()
