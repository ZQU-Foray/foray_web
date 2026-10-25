# -*- coding: utf-8 -*-
"""门户数据恢复（阶段 7）：从 `portal_backup.py` 产物恢复到目标目录。

安全默认：
1. **先逐文件校验 sha256**（manifest 里记录），任何一个不一致即中止；
2. 目标目录非空且未给 `--force` → 中止（避免误覆盖在用数据）；
3. 恢复后打印目录摘要与库的 schema 状态；版本与程序不一致时明确提示
   "先迁移或换匹配版本再启动"，而不是让人直接起服务。

用法：
    python scripts/portal_restore.py --bundle /root/portal-backups/20260910 --target /opt/portal
    python scripts/portal_restore.py --bundle x.tar.gz --target /tmp/drill --force
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack(bundle, workdir):
    """bundle 可以是目录，也可以是 (bundle.tar.gz / 内层目录) 两种形态。"""
    if os.path.isdir(bundle):
        return bundle
    if not os.path.isfile(bundle):
        sys.exit("备份包不存在: %s" % bundle)
    with tarfile.open(bundle, "r:gz") as t:
        t.extractall(workdir)
    inner = [os.path.join(workdir, n) for n in os.listdir(workdir)]
    dirs = [p for p in inner if os.path.isdir(p)]
    for d in dirs:
        if os.path.isfile(os.path.join(d, "manifest.json")):
            return d
    sys.exit("压缩包里找不到 manifest.json")


def main():
    ap = argparse.ArgumentParser(description="门户数据恢复（阶段 7）")
    ap.add_argument("--bundle", required=True, help="备份目录或 bundle.tar.gz")
    ap.add_argument("--target", required=True, help="恢复目标目录（应用数据目录）")
    ap.add_argument("--force", action="store_true", help="允许写入非空目标目录")
    ap.add_argument("--dry-run", action="store_true", help="只校验与打印，不写文件")
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="portal-restore-")
    src = unpack(args.bundle, workdir)
    with open(os.path.join(src, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)

    print("备份包：%s（%s，label=%r）" % (src, man.get("created_at"), man.get("label")))
    bad = []
    for e in man.get("files", []):
        p = os.path.join(src, "files", e["path"])
        if not os.path.isfile(p):
            bad.append((e["path"], "缺失"))
        elif sha256(p) != e["sha256"]:
            bad.append((e["path"], "sha256 不一致"))
    print("校验：%d 个文件，%d 个异常" % (len(man.get("files", [])), len(bad)))
    if bad:
        for p, why in bad[:5]:
            print("  ✗ %s：%s" % (p, why))
        sys.exit("备份包校验失败，拒绝恢复")

    target = os.path.abspath(args.target)
    if os.path.isdir(target) and os.listdir(target) and not args.force:
        sys.exit("目标目录非空（%s）；确认要覆盖请加 --force" % target)

    if args.dry_run:
        print("dry-run：校验通过，未写入；将恢复 %d 个文件 + 快照库"
              % len(man.get("files", [])))
        return 0

    os.makedirs(target, exist_ok=True)
    for e in man.get("files", []):
        dst = os.path.join(target, e["path"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(src, "files", e["path"]), dst)
    db = os.path.join(src, "portal_snapshot.db")
    db_dst = os.path.join(target, "config", "portal_snapshot.db")
    if os.path.isfile(db):
        os.makedirs(os.path.dirname(db_dst), exist_ok=True)
        shutil.copy2(db, db_dst)
        # 旧的 WAL/SHM 与新库不配套，必须清掉
        for suffix in ("-wal", "-shm"):
            stale = db_dst + suffix
            if os.path.exists(stale):
                os.remove(stale)

    info = {"files": len(man.get("files", [])), "db": os.path.isfile(db_dst)}
    if info["db"]:
        c = sqlite3.connect("file:%s?mode=ro" % db_dst, uri=True)
        kv = dict(c.execute("SELECT key, value FROM meta"))
        info["schema_version"] = kv.get("schema_version")
        info["current_version"] = kv.get("current_version")
        info["counts"] = kv.get("counts")
        info["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "outbox" in names:            # 版本 2 起：还没推到飞书的网页改动
            info["outbox_open"] = c.execute(
                "SELECT COUNT(*) FROM outbox WHERE state IN ('pending', 'pushing', 'failed')"
            ).fetchone()[0]
        c.close()
    print("恢复完成 → %s" % target)
    print("  文件 %d 个；快照库 %s" % (info["files"], "已恢复" if info["db"] else "无"))
    if info["db"]:
        print("  schema=%s current_version=%s integrity=%s counts=%s"
              % (info.get("schema_version"), info.get("current_version"),
                 info.get("integrity"), info.get("counts")))
        if info.get("outbox_open") is not None:
            print("  本地缓存：未推到飞书的改动 %d 条（只存在本库里，勿用旧库覆盖）"
                  % info["outbox_open"])
    exp = (man.get("schema") or {}).get("expected")
    if info["db"] and not exp:
        print("  注意：备份时未能确定程序期望版本，请人工核对 schema=%s 后再启动"
              % info.get("schema_version"))
    if info["db"] and exp and str(info.get("schema_version")) != str(exp):
        print("  ✗ 库版本(%s) 与备份时程序期望(%s) 不一致：先迁移或换匹配版本再启动"
              % (info.get("schema_version"), exp))
        return 2
    print("  下一步：把该目录作为应用数据目录启动（config/ 属主需为运行用户），"
          "或按运维手册替换到 /opt/portal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
