"""快照库结构版本与迁移步骤（版本 1 → 2：加本地缓存库的五张表）。

- 新建库：store._db() 直接按最新结构建表（调用 create_v2_objects）；
- 旧库升级：只能由 scripts/portal_migrate.py 执行（先备份、再迁移、再校验），
  服务启动时**不会**自动迁移——版本闸门仍是故障关闭语义，见运维手册 §8.1；
- 每个迁移步骤只做加法（新表/新索引），不改写既有快照数据。
"""
import sqlite3

LATEST = 2

# 版本 2：本地缓存库（网页改动先落本地，再异步推到飞书；飞书改动再合并回来）
V2_STATEMENTS = (
    # 飞书镜像：同步拉到的原始记录（键为飞书列名，值为读形状），作为三方合并的基准
    """CREATE TABLE IF NOT EXISTS remote_records(
        table_name TEXT NOT NULL,
        record_id TEXT NOT NULL,
        fields TEXT NOT NULL,
        modified_at INTEGER,
        modified_by TEXT,
        fetched_at REAL NOT NULL,
        PRIMARY KEY(table_name, record_id))""",
    # 待推送队列：网页改动按入队顺序推到飞书；同一记录的操作严格按序
    """CREATE TABLE IF NOT EXISTS outbox(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name TEXT NOT NULL,
        record_id TEXT NOT NULL,
        op TEXT NOT NULL CHECK(op IN ('create', 'update', 'delete')),
        fields TEXT NOT NULL DEFAULT '{}',
        increments TEXT NOT NULL DEFAULT '{}',
        base TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK(state IN ('pending', 'pushing', 'done', 'failed', 'conflict', 'cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        last_error TEXT,
        client_token TEXT NOT NULL,
        note TEXT,
        created_by TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        finished_at REAL)""",
    "CREATE INDEX IF NOT EXISTS outbox_by_state ON outbox(state, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS outbox_by_record ON outbox(table_name, record_id, state)",
    # 本地临时 ID ↔ 飞书 record_id（推送成功前 remote_id 为空）
    """CREATE TABLE IF NOT EXISTS id_map(
        local_id TEXT PRIMARY KEY,
        table_name TEXT NOT NULL,
        remote_id TEXT UNIQUE,
        created_at REAL NOT NULL,
        mapped_at REAL)""",
    # 冲突记录：一律飞书优先，留痕给组长查看与确认
    """CREATE TABLE IF NOT EXISTS conflicts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        outbox_id INTEGER,
        table_name TEXT NOT NULL,
        record_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        field TEXT,
        base_value TEXT,
        local_value TEXT,
        remote_value TEXT,
        resolution TEXT NOT NULL DEFAULT 'remote_wins',
        detected_at REAL NOT NULL,
        acked_at REAL,
        acked_by TEXT)""",
    "CREATE INDEX IF NOT EXISTS conflicts_open ON conflicts(acked_at, detected_at)",
    # 考勤明细：每次签到/签退一行，推送时按增量加到飞书累计时长
    """CREATE TABLE IF NOT EXISTS attendance_sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        open_id TEXT NOT NULL,
        member_record_id TEXT NOT NULL,
        check_in_at REAL NOT NULL,
        check_out_at REAL,
        hours REAL,
        state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open', 'closed', 'void')),
        note TEXT,
        outbox_id INTEGER,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL)""",
    # 每人同一时刻至多一条未闭合签到
    """CREATE UNIQUE INDEX IF NOT EXISTS attendance_one_open
        ON attendance_sessions(open_id) WHERE state = 'open'""",
)

# 版本 2 新增的表（迁移校验与诊断用）
V2_TABLES = ("remote_records", "outbox", "id_map", "conflicts", "attendance_sessions")


class MigrationError(RuntimeError):
    """迁移被拒绝或失败（版本未知、缺失或执行出错）。"""


def create_v2_objects(conn):
    """建版本 2 的表与索引（全部 IF NOT EXISTS，可重复执行）。"""
    for sql in V2_STATEMENTS:
        conn.execute(sql)


def _v1_to_v2(conn):
    create_v2_objects(conn)


# 起始版本 → 升一级的步骤
STEPS = {1: _v1_to_v2}


def path(found, target=LATEST):
    """从 found 升到 target 要执行的起始版本列表；无路可走时抛 MigrationError。"""
    try:
        cur = int(found)
    except (TypeError, ValueError):
        raise MigrationError(f"库内版本无法识别：{found!r}")
    if cur > target:
        raise MigrationError(f"库内版本 {cur} 比程序版本 {target} 新，不能降级")
    out = []
    while cur < target:
        if cur not in STEPS:
            raise MigrationError(f"没有从版本 {cur} 起的迁移步骤")
        out.append(cur)
        cur += 1
    return out


def read_version(conn):
    """读库内 schema_version；没有 meta 表或没有该键时返回 None。"""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def migrate(conn, target=LATEST):
    """在**一个事务**里把库升到 target，返回执行过的起始版本列表。

    conn 须以 isolation_level=None 打开（由本函数显式 BEGIN IMMEDIATE / COMMIT），
    事务内重读一次版本，防止与其他进程并发迁移。任一步失败整体回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        steps = path(read_version(conn), target)
        for v in steps:
            STEPS[v](conn)
        if steps:
            conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                         (str(target),))
        conn.execute("COMMIT")
        return steps
    except Exception:
        conn.execute("ROLLBACK")
        raise
