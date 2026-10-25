# -*- coding: utf-8 -*-
"""门户数据备份（阶段 7）：把**持久数据**打成可校验、可恢复的包。

保护范围（缺项自动跳过，不报错）：
- `config/`：飞书配置、设备注册表、预设、`auth/`（会话 + 应急账号）、快照库
- `firmware/`：已上传固件
- `.env`：服务器机密（App Secret 等）

要点：
1. **快照库用 sqlite3 backup API 做一致性副本**（WAL 模式下直接拷文件会丢最近
   事务）；副本另存为 `portal_snapshot.db`。
2. `manifest.json` 记录每个文件的 sha256/大小、库的 schema 状态与快照 meta，
   恢复脚本据此逐文件校验。
3. schema 版本与程序不一致时**照样备份**（数据可能仍要救），但标注
   `schema.ok=false` 并以退出码 2 提示。

用法：
    python scripts/portal_backup.py --data-dir /opt/portal --out /root/portal-backups/20260910
    python scripts/portal_backup.py --data-dir . --tar          # 另出 bundle.tar.gz
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import time

KEEP_DIRS = ("config", "firmware")
KEEP_FILES = (".env",)
DB_REL = os.path.join("config", "portal_snapshot.db")
SKIP_SUFFIX = (".log", ".tmp", "-wal", "-shm", "__pycache__")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(data_dir):
    """列出保护范围内的文件（相对路径）。"""
    out = []
    for d in KEEP_DIRS:
        base = os.path.join(data_dir, d)
        for root, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, data_dir).replace("\\", "/")
                if any(s in rel for s in SKIP_SUFFIX):
                    continue
                if rel == DB_REL.replace("\\", "/"):
                    continue            # 快照库走一致性副本，不直接拷
                out.append(rel)
    for f in KEEP_FILES:
        if os.path.isfile(os.path.join(data_dir, f)):
            out.append(f)
    return sorted(out)


def db_state(db_path):
    """只读探测库的 schema 状态与快照 meta（失败不抛）。"""
    st = {"exists": os.path.isfile(db_path), "schema_version": None,
          "current_version": None, "counts": None, "last_success_at": None,
          "last_error": None, "integrity": None, "error": None,
          "outbox_open": None, "conflicts_open": None}
    if not st["exists"]:
        return st
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        kv = dict(c.execute("SELECT key, value FROM meta"))
        st.update(schema_version=kv.get("schema_version"),
                  current_version=kv.get("current_version"),
                  counts=kv.get("counts"), last_success_at=kv.get("last_success_at"),
                  last_error=kv.get("last_error"),
                  integrity=c.execute("PRAGMA integrity_check").fetchone()[0])
        st.update(cache_backlog(c))
        c.close()
    except Exception as e:
        st["error"] = str(e)[:120]
    return st


def cache_backlog(conn):
    """本地缓存库（版本 2 起）里还没推到飞书的改动与未确认冲突；旧库没有这些表返回空。

    这些改动只存在本库里：恢复时库文件必须一起恢复，否则网页改动会丢。
    """
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = {}
    if "outbox" in names:
        out["outbox_open"] = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE state IN ('pending', 'pushing', 'failed')"
        ).fetchone()[0]
    if "conflicts" in names:
        out["conflicts_open"] = conn.execute(
            "SELECT COUNT(*) FROM conflicts WHERE acked_at IS NULL").fetchone()[0]
    return out


def prune(out_root, keep, protect):
    """删除旧备份目录，仅限"含 manifest.json 的目录"，且不删 protect 里的那个。

    返回被删除的目录名列表。config/、firmware/、.env 等数据本体永不在本函数范围内。
    """
    if keep <= 0 or not os.path.isdir(out_root):
        return []
    # 刚做好的那份也参与排序（--keep N = 根目录下总共保留 N 份），但永不删除
    protect_abs = os.path.abspath(protect)
    cands = []
    for name in os.listdir(out_root):
        p = os.path.join(out_root, name)
        if not os.path.isdir(p):
            continue
        if os.path.isfile(os.path.join(p, "manifest.json")):
            cands.append((os.path.getmtime(p), name, os.path.abspath(p) == protect_abs))
    cands.sort(reverse=True)
    removed = []
    for _, name, is_protect in cands[keep:]:
        if is_protect:
            continue                      # 本轮备份永不删除
        shutil.rmtree(os.path.join(out_root, name), ignore_errors=True)
        removed.append(name)
    return removed


def main():
    ap = argparse.ArgumentParser(description="门户数据备份（阶段 7/8）")
    ap.add_argument("--data-dir", default=".", help="应用数据目录（含 config/、firmware/）")
    ap.add_argument("--out", required=True, help="输出目录（不存在则创建）")
    ap.add_argument("--label", default="", help="备注（写入 manifest）")
    ap.add_argument("--tar", action="store_true", help="另出 bundle.tar.gz")
    ap.add_argument("--keep", type=int, default=0,
                    help="备份根目录下保留最近 N 份（0=不清理；只删含 manifest.json 的目录）")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    out = os.path.abspath(args.out)
    if os.path.exists(out) and os.listdir(out):
        sys.exit("输出目录非空，拒绝覆盖: %s" % out)
    os.makedirs(out, exist_ok=True)

    files = collect(data_dir)
    entries = []
    for rel in files:
        src = os.path.join(data_dir, rel)
        dst = os.path.join(out, "files", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        entries.append({"path": rel, "size": os.path.getsize(src),
                        "sha256": sha256(src)})

    db_src = os.path.join(data_dir, DB_REL)
    db_info = db_state(db_src)
    if os.path.isfile(db_src):
        dst = os.path.join(out, "portal_snapshot.db")
        src_conn = sqlite3.connect("file:%s?mode=ro" % db_src, uri=True)
        dst_conn = sqlite3.connect(dst)
        with dst_conn:
            src_conn.backup(dst_conn)          # 一致性副本（WAL 安全）
        src_conn.close()
        dst_conn.close()
        db_info["bundle_sha256"] = sha256(dst)
        db_info["bundle_size"] = os.path.getsize(dst)

    # 程序期望的 schema 版本：优先从脚本所在仓库解析（部署树里也一样成立），
    # 解析不到时明确标注"未知"而不是假装一致
    expected = None
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (repo_root, data_dir):
        if os.path.isdir(os.path.join(candidate, "app", "portal")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            try:
                from app.portal.store import SCHEMA_VERSION
                expected = str(SCHEMA_VERSION)
            except Exception:
                expected = None
            if expected:
                break
    if expected is None:
        schema_ok = None
    else:
        schema_ok = (db_info["schema_version"] is None
                     or str(db_info["schema_version"]) == expected)

    manifest = {
        "tool": "scripts/portal_backup.py",
        "format": 1,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_ts": time.time(),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "data_dir": data_dir,
        "label": args.label,
        "schema": {"expected": expected, "found": db_info["schema_version"],
                   "ok": schema_ok},
        "snapshot": db_info,
        "files": entries,
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    if args.tar:
        bundle = out.rstrip("/") + ".tar.gz"
        with tarfile.open(bundle, "w:gz") as t:
            t.add(out, arcname=os.path.basename(out))
        print("整包: %s (%.1f MB)" % (bundle, os.path.getsize(bundle) / 1e6))

    if args.keep:
        removed = prune(os.path.dirname(out.rstrip("/")) or ".", args.keep, out)
        print("保留策略：根目录保留最近 %d 份，本轮清理 %d 个旧备份%s"
              % (args.keep, len(removed), ("（%s）" % ", ".join(removed[:5])) if removed else ""))

    print("备份完成: %s" % out)
    print("  文件 %d 个（config/firmware/.env），快照库 %s"
          % (len(entries), "已含一致性副本" if db_info["exists"] else "不存在"))
    verdict = "✓" if schema_ok else ("？无法确定程序期望版本" if schema_ok is None
                                     else "✗ 不一致")
    print("  schema: 库内=%s 程序期望=%s %s"
          % (db_info["schema_version"], expected, verdict))
    print("  快照: version=%s counts=%s last_error=%r"
          % (db_info["current_version"], db_info["counts"], db_info["last_error"]))
    if db_info.get("outbox_open") is not None:
        print("  本地缓存：未推到飞书的改动 %s 条，未确认冲突 %s 条"
              % (db_info["outbox_open"], db_info.get("conflicts_open")))
    if schema_ok is False:
        print("  警告：库版本与当前程序不一致，恢复后须先迁移或换匹配版本再启动"
              "（见 docs/portal-operations.md）")
    elif schema_ok is None and db_info["exists"]:
        print("  注意：未能解析程序期望的 schema 版本（未找到 app/portal/）；"
              "请人工核对库内版本后再启动")
    return 2 if schema_ok is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
