"""SQLite 快照存储：候选版本写入 → 原子发布 → 旧版本清理。

- schema_version 随库创建写入 meta；读写前必须比对（`SCHEMA_VERSION`）：库内
  版本缺失或不等 → 抛 `SchemaVersionError` **拒绝任何读写**，绝不对未知版本
  直接降级读取（阶段 7 条款）；升级前须先备份再迁移（见运维手册）。
- 发布语义：候选版本（next_version）四表 + 质量报告全部写入后，
  单事务把 meta.current_version 指向新版本并删除所有更旧版本行——
  读方任意时刻看到的都是一份完整版本（失败保留旧版）。
- 全部方法为同步 SQLite 调用，由同步线程（受控 worker）执行；
  API 查询走同一线程池（快照只读查询轻量，不阻塞调试事件循环）。
"""
import json
import sqlite3
import threading
import time

from .. import config
from . import migrations

# 版本 2（2026-10-21）：加本地缓存库五张表，旧库须先跑 scripts/portal_migrate.py
SCHEMA_VERSION = migrations.LATEST
# 快照表为通用布局（table_name 为文本列），新语义表入快照无需迁移升版
TABLE_ORDER = ("projects", "tasks", "milestones", "schedule", "members", "roster",
               "procurement", "duty")

_lock = threading.RLock()     # 可重入：status() 持锁调用 get_meta()
_conn = None


class SchemaVersionError(RuntimeError):
    """快照库 schema 版本不可识别（缺失或与本程序不一致）：拒绝读写。

    这是**故障关闭**语义：宁可让门户显示"存储版本不匹配"，也不能把未知
    版本的行按当前 schema 解释（可能静默读错或写坏）。处置见运维手册
    「快照库版本不匹配」：备份 → 迁移/恢复 → 再启动。
    """

    code = "schema_version_mismatch"

    def __init__(self, found):
        self.found = "缺失" if found is None else str(found)
        self.expected = str(SCHEMA_VERSION)
        self.path = str(config.PORTAL_DB)
        super().__init__(
            f"快照库 schema 版本不匹配（库内={self.found}，本程序={self.expected}，"
            f"路径={self.path}）：已停止读写以防误读误写；请先备份该文件并按"
            "运维手册迁移（scripts/portal_migrate.py）或恢复后再启动")


def _db():
    """打开（并按需初始化）快照库。**版本闸门在任何写入之前**：

    闸门只做 SELECT；版本缺失或不等 → 关闭连接并抛 `SchemaVersionError`，
    连 `PRAGMA journal_mode=WAL` 与建表都不执行，确保未知版本库零写入
    （阶段 7 条款：不对未知版本直接降级读取）。
    """
    global _conn
    if _conn is None:
        config.PORTAL_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.PORTAL_DB), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            found, has_data = None, False
            if "meta" in names:
                kv = {r["key"]: r["value"] for r in
                      conn.execute("SELECT key, value FROM meta")}
                found = kv.get("schema_version")
                has_data = ("current_version" in kv
                            or bool("snapshot" in names and conn.execute(
                                "SELECT 1 FROM snapshot LIMIT 1").fetchone()))
        except Exception:
            conn.close()
            raise
        if (found is None and has_data) or (
                found is not None and str(found) != str(SCHEMA_VERSION)):
            conn.close()
            raise SchemaVersionError(found)
        # 通过闸门后才允许写入：WAL + 建表 + 全新库补写版本键
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY, value TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS snapshot(
                version INTEGER, table_name TEXT, record_id TEXT, data TEXT,
                PRIMARY KEY(version, table_name, record_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS quality(
                version INTEGER, table_name TEXT, record_id TEXT, issue TEXT)""")
            # 本地缓存库的表（版本 2）：新库直接建，旧库由迁移脚本补建
            migrations.create_v2_objects(conn)
            if found is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
        _conn = conn
    return _conn


def schema_state():
    """诊断用：不抛异常的版本状态（脚本/排障用）。

    返回 {path, exists, expected, found, ok}；ok=True 表示可读写。
    """
    import os
    st = {"path": str(config.PORTAL_DB), "exists": config.PORTAL_DB.exists(),
          "expected": str(SCHEMA_VERSION), "found": None, "ok": False}
    if not st["exists"]:
        st["ok"] = True                      # 尚未建库：启动时会按当前版本创建
        return st
    try:
        with _lock:
            st["found"] = get_meta("schema_version")
    except SchemaVersionError as e:
        st["found"] = e.found
        return st
    except Exception as e:                    # 损坏/无权限等
        st["found"] = f"读取失败: {str(e)[:60]}"
        return st
    st["ok"] = st["found"] == st["expected"]
    return st


def maybe_checkpoint(threshold_bytes=4 * 1024 * 1024):
    """发布后回收 WAL（阶段 8）：WAL 超阈值就 TRUNCATE，避免长期只涨不落。

    返回 (是否执行, WAL 大小, 执行后 WAL 大小)。读方都是短查询，TRUNCATE
    失败（有长读事务占用）时静默跳过——下次发布再试。
    """
    import os
    wal = str(config.PORTAL_DB) + "-wal"
    try:
        size = os.path.getsize(wal) if os.path.exists(wal) else 0
    except Exception:
        size = 0
    if size < threshold_bytes:
        return False, size, size
    try:
        with _lock:
            _db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except (SchemaVersionError, sqlite3.Error):
        return False, size, size
    try:
        after = os.path.getsize(wal) if os.path.exists(wal) else 0
    except Exception:
        after = size
    return True, size, after


def close():
    global _conn
    with _lock:
        if _conn:
            _conn.close()
            _conn = None


def get_meta(key, default=None):
    with _lock:
        row = _db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key, value):
    with _lock, _db() as c:
        c.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


def next_version():
    """候选版本号 = 当前 +1（无当前则 1）。"""
    return int(get_meta("current_version", "0") or 0) + 1


def write_candidate(version, table_name, records, issues):
    """写候选版本的表数据与质量报告（未发布对读方不可见）。"""
    rows = [(version, table_name, r["id"], json.dumps(r, ensure_ascii=False))
            for r in records]
    qrows = [(version, table_name, rid, issue) for rid, issue in issues]
    with _lock, _db() as c:
        c.execute("DELETE FROM snapshot WHERE version=? AND table_name=?",
                  (version, table_name))
        c.execute("DELETE FROM quality WHERE version=? AND table_name=?",
                  (version, table_name))
        c.executemany("INSERT INTO snapshot(version, table_name, record_id, data) "
                      "VALUES(?,?,?,?)", rows)
        c.executemany("INSERT INTO quality(version, table_name, record_id, issue) "
                      "VALUES(?,?,?,?)", qrows)


def discard_candidate(version):
    """同步失败：丢弃候选（当前版本不受影响）。"""
    with _lock, _db() as c:
        c.execute("DELETE FROM snapshot WHERE version=?", (version,))
        c.execute("DELETE FROM quality WHERE version=?", (version,))


def publish(version, counts, duration, error=None):
    """原子发布：current_version 切换 + 旧版本清理 + 状态记录（单事务）。"""
    now = time.time()
    with _lock, _db() as c:
        c.execute("DELETE FROM snapshot WHERE version < ?", (version,))
        c.execute("DELETE FROM quality WHERE version < ?", (version,))
        c.execute("INSERT INTO meta(key, value) VALUES('current_version', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (str(version),))
        for k, v in (("last_success_at", now), ("published_at", now),
                     ("last_attempt_at", now),
                     ("counts", json.dumps(counts, ensure_ascii=False)),
                     ("last_duration_sec", round(duration, 2)),
                     ("last_error", "")):
            c.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, str(v)))
    # 每轮成功后按阈值回收 WAL（阶段 8：实测曾涨到 4MB 不落）
    ran, before, after = maybe_checkpoint()
    if ran:
        import logging
        logging.getLogger("app.portal.store").info(
            "WAL 已回收: %.1fMB -> %.1fMB", before / 1e6, after / 1e6)


def publish_local(version, counts):
    """本地重建后的原子发布（缓存库第 4 步）：只切版本、清旧版本、更新计数。

    与 publish 的区别：不动 last_success_at / last_attempt_at 等同步统计——本地重建
    不访问飞书，不能让它掩盖同步故障（stale 判定与巡检都依赖这些时间）。
    """
    with _lock, _db() as c:
        c.execute("DELETE FROM snapshot WHERE version < ?", (version,))
        c.execute("DELETE FROM quality WHERE version < ?", (version,))
        for k, v in (("current_version", version), ("published_at", time.time()),
                     ("counts", json.dumps(counts, ensure_ascii=False))):
            c.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    maybe_checkpoint()


def mark_success(duration):
    """数据零变化、跳过发布新版本时使用：只刷新成功时间/耗时并清错误，
    不递增 current_version——否则门户轮询会把每轮同步当作"有更新"整页重渲染。"""
    now = time.time()
    with _lock, _db() as c:
        for k, v in (("last_success_at", now), ("last_attempt_at", now),
                     ("last_duration_sec", round(duration, 2)), ("last_error", "")):
            c.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, str(v)))


def mark_attempt(error_class):
    # 版本不匹配时 mark_attempt 也不得写入（故障关闭）；调度线程需要继续活着，
    # 故在此吞掉异常并留日志，状态改由 API 的 503 与运维手册呈现
    try:
        set_meta("last_attempt_at", time.time())
        set_meta("last_error", error_class or "")
    except SchemaVersionError as e:
        import logging
        logging.getLogger("app.portal.store").warning("快照库版本不匹配，跳过状态写入: %s", e)


def current():
    """读当前快照：{version, tables: {name: [record...]}, quality: [...]}或 None。"""
    with _lock:
        ver = int(get_meta("current_version", "0") or 0)
        if not ver:
            return None
        out = {"version": ver, "tables": {}, "quality": []}
        for t in TABLE_ORDER:
            rows = _db().execute(
                "SELECT data FROM snapshot WHERE version=? AND table_name=? "
                "ORDER BY record_id", (ver, t)).fetchall()
            out["tables"][t] = [json.loads(r["data"]) for r in rows]
        for r in _db().execute(
                "SELECT table_name, record_id, issue FROM quality WHERE version=?",
                (ver,)):
            out["quality"].append({"table": r["table_name"],
                                   "record_id": r["record_id"],
                                   "issue": r["issue"]})
        return out


def status(now=None):
    """同步状态（§6.3 envelope 用；脱敏：只有错误类别，无内部 URL/令牌）。"""
    now = now or time.time()
    ver = int(get_meta("current_version", "0") or 0)
    last_ok = float(get_meta("last_success_at", "0") or 0)
    stale = bool(last_ok) and (now - last_ok) > config.PORTAL_SYNC_STALE_SEC
    return {
        "snapshot_version": ver or None,
        "has_snapshot": bool(ver),
        "last_attempt_at": float(get_meta("last_attempt_at", "0") or 0) or None,
        "last_success_at": last_ok or None,
        "last_duration_sec": float(get_meta("last_duration_sec", "0") or 0) or None,
        "counts": json.loads(get_meta("counts") or "{}"),
        "last_error": get_meta("last_error") or None,
        "stale": stale,
    }
