# -*- coding: utf-8 -*-
"""快照库结构迁移：先备份 → 单事务迁移 → 校验（2026-10-21，版本 1 → 2）。

服务启动时不会自动迁移（版本闸门是故障关闭语义），旧库升级只走本脚本。
部署脚本 deploy.py 会在重启服务前自动调用；手工执行见运维手册 §8.1。

- 迁移只做加法（新表/新索引），既有快照数据不动；
- 可在服务运行时执行：旧进程已通过版本闸门，迁移期间照常读写，
  重启后新程序按新版本打开；迁移等写锁最多 --lock-timeout 秒；
- 备份用 sqlite3 backup API（WAL 下也含最新已提交事务），校验 integrity 后才迁移；
- 版本未知/缺失/比程序新 → 拒绝（退出码 2），库文件零改动。

退出码：0 已迁移或无需迁移；1 执行出错（已回滚）；2 拒绝迁移。

用法：
    python scripts/portal_migrate.py --db /opt/portal/config/portal_snapshot.db \\
        --backup-dir /opt/portal-backups/premigrate-$(date +%Y%m%d-%H%M)
    python scripts/portal_migrate.py --db config/portal_snapshot.db --dry-run
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

# 从脚本所在代码树导入迁移步骤（部署树里同样成立），须在补 sys.path 之后导入
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.portal import migrations


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def has_data(conn):
    """库里是否已有业务数据（无版本号但有数据 → 不能当新库处理）。"""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "meta" in names and conn.execute(
            "SELECT 1 FROM meta WHERE key='current_version'").fetchone():
        return True
    return "snapshot" in names and bool(conn.execute("SELECT 1 FROM snapshot LIMIT 1").fetchone())


def backup(db, out_dir, found):
    """一致性副本 + 完整性校验 + 清单；目标已存在则拒绝覆盖。"""
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "portal_snapshot.v%s.db" % found)
    if os.path.exists(dst):
        raise SystemExit("备份目标已存在，拒绝覆盖: %s" % dst)
    src = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    out = sqlite3.connect(dst)
    try:
        with out:
            src.backup(out)
        ok = out.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        src.close()
        out.close()
    if ok != "ok":
        raise SystemExit("备份副本完整性校验失败（%s），中止迁移" % ok)
    info = {"tool": "scripts/portal_migrate.py", "source": os.path.abspath(db),
            "schema_version": str(found), "file": os.path.basename(dst),
            "sha256": sha256(dst), "size": os.path.getsize(dst),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(os.path.join(out_dir, "premigrate.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    return dst, info


def verify(db, target):
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        ver = migrations.read_version(c)
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        c.close()
    missing = [t for t in migrations.V2_TABLES if t not in names]
    return str(ver) == str(target) and not missing and integrity == "ok", {
        "schema_version": ver, "missing_tables": missing, "integrity": integrity}


def main():
    ap = argparse.ArgumentParser(description="快照库结构迁移（先备份再迁移）")
    ap.add_argument("--db", required=True, help="快照库路径（config/portal_snapshot.db）")
    ap.add_argument("--backup-dir", help="迁移前备份的存放目录（真正迁移时必填）")
    ap.add_argument("--dry-run", action="store_true", help="只报告要做什么，不写任何文件")
    ap.add_argument("--lock-timeout", type=float, default=30.0,
                    help="等待写锁的秒数（服务运行中迁移时用）")
    args = ap.parse_args()
    target = migrations.LATEST

    if not os.path.isfile(args.db):
        print("库不存在：%s\n服务首次启动时会直接按版本 %d 建库，无需迁移" % (args.db, target))
        return 0
    probe = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    try:
        found = migrations.read_version(probe)
        data = has_data(probe)
    finally:
        probe.close()

    if found is None:
        if data:
            print("✗ 库内没有版本号但已有业务数据，拒绝迁移（不猜版本）：%s" % args.db)
            return 2
        print("库内尚无版本号且无业务数据：服务启动时会按版本 %d 初始化，无需迁移" % target)
        return 0
    if str(found) == str(target):
        print("库已是版本 %s，无需迁移：%s" % (found, args.db))
        return 0
    try:
        steps = migrations.path(found, target)
    except migrations.MigrationError as e:
        print("✗ 拒绝迁移：%s（库=%s，路径=%s）" % (e, found, args.db))
        return 2

    plan = " → ".join(str(v) for v in steps + [target])
    print("迁移计划：版本 %s（%s），新增表 %s" % (plan, args.db, ", ".join(migrations.V2_TABLES)))
    if args.dry_run:
        print("（--dry-run：未写任何文件）")
        return 0
    if not args.backup_dir:
        print("✗ 真正迁移须给 --backup-dir（先备份再迁移）")
        return 2

    dst, info = backup(args.db, args.backup_dir, found)
    print("已备份：%s（sha256 %s…，%d 字节）" % (dst, info["sha256"][:12], info["size"]))

    conn = sqlite3.connect(args.db, timeout=args.lock_timeout, isolation_level=None)
    try:
        done = migrations.migrate(conn, target)
    except migrations.MigrationError as e:
        print("✗ 拒绝迁移（事务内复核版本时发现变化）：%s" % e)
        return 2
    except Exception as e:
        print("✗ 迁移失败，已回滚，库保持原样：%s" % e)
        return 1
    finally:
        conn.close()

    ok, st = verify(args.db, target)
    if not ok:
        print("✗ 迁移后校验未通过：%s\n  可用备份恢复：%s" % (st, dst))
        return 1
    print("✓ 迁移完成：执行步骤 %s，库版本 %s，integrity=%s"
          % (done, st["schema_version"], st["integrity"]))
    print("  回退方法：停服务后用备份覆盖库文件（并删掉同名 -wal/-shm），再换回旧程序")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
