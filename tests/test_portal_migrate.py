"""迁移脚本 scripts/portal_migrate.py 的端到端行为（子进程执行，临时库文件）。

运行：在仓库根目录执行 python -m unittest tests.test_portal_migrate
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tests.test_cache_store import file_hash, make_v1_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "portal_migrate.py")


def run(*args):
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                       timeout=60)
    return r.returncode, r.stdout + r.stderr


def version(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    finally:
        c.close()


class MigrateScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "portal_snapshot.db")
        self.bk = os.path.join(self.tmp.name, "premigrate")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_migration_with_backup(self):
        make_v1_db(self.db)
        code, out = run("--db", self.db, "--backup-dir", self.bk)
        self.assertEqual(code, 0, out)
        self.assertEqual(version(self.db), "2")
        c = sqlite3.connect(self.db)
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
        rows = c.execute("SELECT record_id FROM snapshot").fetchall()
        c.close()
        self.assertTrue({"outbox", "remote_records", "id_map", "conflicts",
                         "attendance_sessions"} <= names)
        self.assertEqual(rows, [("recT1",)])
        with open(os.path.join(self.bk, "premigrate.json"), encoding="utf-8") as f:
            man = json.load(f)
        bfile = os.path.join(self.bk, man["file"])
        self.assertEqual((man["schema_version"], file_hash(bfile)), ("1", man["sha256"]))
        self.assertEqual(version(bfile), "1")          # 备份是迁移前的样子
        code, out = run("--db", self.db, "--backup-dir", self.bk)
        self.assertEqual(code, 0, out)
        self.assertIn("无需迁移", out)

    def test_dry_run_and_missing_backup_dir_write_nothing(self):
        make_v1_db(self.db)
        before = file_hash(self.db)
        code, out = run("--db", self.db, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("1 → 2", out)
        code, out = run("--db", self.db)
        self.assertEqual(code, 2, out)
        self.assertEqual(file_hash(self.db), before)
        self.assertFalse(os.path.exists(self.bk))

    def test_refuses_unknown_or_missing_version(self):
        make_v1_db(self.db, version="99")
        before = file_hash(self.db)
        code, out = run("--db", self.db, "--backup-dir", self.bk)
        self.assertEqual(code, 2, out)
        self.assertEqual(file_hash(self.db), before)
        os.remove(self.db)
        make_v1_db(self.db)
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM meta WHERE key='schema_version'")
        c.commit()
        c.close()
        code, out = run("--db", self.db, "--backup-dir", self.bk)
        self.assertEqual(code, 2, out)
        self.assertIn("没有版本号但已有业务数据", out)

    def test_no_db_is_noop(self):
        code, out = run("--db", self.db, "--backup-dir", self.bk)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.db))

    def test_migrates_while_another_connection_is_open(self):
        """模拟旧服务进程仍开着库（已过版本闸门）时执行迁移。"""
        make_v1_db(self.db)
        old = sqlite3.connect(self.db)
        old.execute("SELECT COUNT(*) FROM snapshot").fetchone()
        code, out = run("--db", self.db, "--backup-dir", self.bk)
        self.assertEqual(code, 0, out)
        old.execute("INSERT INTO snapshot VALUES(4, 'tasks', 'recT2', '{}')")   # 旧进程照常写
        old.commit()
        old.close()
        self.assertEqual(version(self.db), "2")

    def test_refuses_to_overwrite_backup(self):
        make_v1_db(self.db)
        os.makedirs(self.bk)
        open(os.path.join(self.bk, "portal_snapshot.v1.db"), "w").close()
        before = file_hash(self.db)
        code, out = run("--db", self.db, "--backup-dir", self.bk)
        self.assertNotEqual(code, 0, out)
        self.assertIn("拒绝覆盖", out)
        self.assertEqual(file_hash(self.db), before)


if __name__ == "__main__":
    unittest.main()
