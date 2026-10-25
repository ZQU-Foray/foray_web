"""本地缓存库数据层：飞书镜像、待推送队列、临时 ID 对照、冲突记录、考勤明细。

- 与快照共用一个库文件、一个连接和一把锁（store._lock / store._db()），
  版本闸门同样生效：库版本不对时这里的函数一律抛 SchemaVersionError；
- 冲突判断本身在 cache_rules（纯函数），这里只负责存取与状态流转；
- 队列状态：pending 待推 → pushing 推送中 → done 完成 / failed 永久失败 /
  conflict 冲突放弃（飞书优先）/ cancelled 被后续操作取代。
  pending 与 pushing 统称"未落地"，会影响本地视图；
- 第 2 步只提供这些函数并让同步写镜像；网页写入改走队列、后台推送线程、
  合并式同步分别在第 3、4 步接入（见 docs/portal-cache-design.md）。
"""
import hashlib
import json
import time
import uuid

from . import cache_rules as rules
from . import directives
from . import store

OPEN_STATES = ("pending", "pushing")
FINAL_STATES = ("done", "failed", "conflict", "cancelled")


class RecordUnknown(LookupError):
    """本地既没有镜像也没有未落地操作的记录：无法给编辑取基准。"""


class AttendanceStateError(ValueError):
    """考勤明细状态不允许该操作（重复签到、未签到就签退等）。"""


def _dumps(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _loads(s, default):
    return json.loads(s) if s else default


def _now(now):
    return time.time() if now is None else float(now)


def _set_meta(c, key, value):
    # 不复用 store.set_meta：它自带事务，嵌套会把外层事务提前提交
    c.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _op(row):
    if row is None:
        return None
    d = dict(row)
    d["fields"] = _loads(d.get("fields"), {})
    d["increments"] = _loads(d.get("increments"), {})
    d["base"] = _loads(d.get("base"), {})
    return d


# ---------------------------------------------------------------- 飞书镜像

def mirror_digest(items_by_table):
    """一轮原始记录的内容指纹（记录 ID + 字段 + 飞书修改时间）。"""
    h = hashlib.sha256()
    for t in sorted(items_by_table):
        for it in sorted(items_by_table[t] or [], key=lambda x: str(x.get("record_id"))):
            h.update(_dumps([t, it.get("record_id"), it.get("fields") or {},
                             it.get("last_modified_time")]).encode("utf-8"))
    return h.hexdigest()


def _modified_by(it):
    who = it.get("last_modified_by")
    if isinstance(who, list):
        who = who[0] if who else None
    return (who or {}).get("id") if isinstance(who, dict) else None


def _modified_at(it):
    v = it.get("last_modified_time")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def replace_remote_tables(items_by_table, fetch_started_at, now=None):
    """用一轮完整拉取刷新镜像（单事务）。内容与上次相同则不写库，返回 False。

    items_by_table：{语义表名: 飞书原始 items}，items 的 fields 以飞书列名为键。
    两条保护，防止拉取途中推送成功写回的新数据被旧数据冲掉：
    - 镜像行的 fetched_at 晚于本轮拉取开始时间（推送后写回的）→ 不覆盖、不删除；
    - 镜像里飞书修改时间比本轮拉到的更新 → 不回退。
    """
    now = _now(now)
    digest = mirror_digest(items_by_table)
    with store._lock, store._db() as c:
        _set_meta(c, "remote_mirror_checked_at", now)
        row = c.execute("SELECT value FROM meta WHERE key='remote_mirror_hash'").fetchone()
        if row and row["value"] == digest:
            return False
        for t, items in items_by_table.items():
            existing = {r["record_id"]: (r["modified_at"], r["fetched_at"]) for r in c.execute(
                "SELECT record_id, modified_at, fetched_at FROM remote_records "
                "WHERE table_name=?", (t,))}
            seen, rows = set(), []
            for it in items or []:
                rid = it.get("record_id")
                if not rid:
                    continue
                seen.add(rid)
                mod = _modified_at(it)
                old = existing.get(rid)
                if old and old[1] > fetch_started_at:
                    continue
                if old and old[0] and mod and old[0] > mod:
                    continue
                rows.append((t, rid, _dumps(it.get("fields") or {}), mod,
                             _modified_by(it), now))
            c.executemany(
                "INSERT INTO remote_records(table_name, record_id, fields, modified_at, "
                "modified_by, fetched_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(table_name, record_id) DO UPDATE SET fields=excluded.fields, "
                "modified_at=excluded.modified_at, modified_by=excluded.modified_by, "
                "fetched_at=excluded.fetched_at", rows)
            gone = [(t, rid) for rid, (_, fetched) in existing.items()
                    if rid not in seen and fetched <= fetch_started_at]
            c.executemany("DELETE FROM remote_records WHERE table_name=? AND record_id=?", gone)
        _set_meta(c, "remote_mirror_hash", digest)
        _set_meta(c, "remote_mirror_at", now)
    return True


def get_remote(table, record_id):
    """镜像里的一条记录：{fields, modified_at, modified_by, fetched_at}；没有返回 None。"""
    with store._lock:
        r = store._db().execute(
            "SELECT fields, modified_at, modified_by, fetched_at FROM remote_records "
            "WHERE table_name=? AND record_id=?", (table, record_id)).fetchone()
    if r is None:
        return None
    return {"fields": _loads(r["fields"], {}), "modified_at": r["modified_at"],
            "modified_by": r["modified_by"], "fetched_at": r["fetched_at"]}


def mirror_count(table=None):
    with store._lock:
        if table:
            return store._db().execute(
                "SELECT COUNT(*) FROM remote_records WHERE table_name=?", (table,)).fetchone()[0]
        return store._db().execute("SELECT COUNT(*) FROM remote_records").fetchone()[0]


def list_remote(table):
    """镜像里某张表的全部记录：[(record_id, fields)]。"""
    with store._lock:
        rows = store._db().execute(
            "SELECT record_id, fields FROM remote_records WHERE table_name=? ORDER BY record_id",
            (table,)).fetchall()
    return [(r["record_id"], _loads(r["fields"], {})) for r in rows]


def mirror_age(now=None):
    """距上次镜像刷新（内容有变才算）或核对过的秒数；从未刷新返回 None。"""
    v = store.get_meta("remote_mirror_checked_at") or store.get_meta("remote_mirror_at")
    return (_now(now) - float(v)) if v else None


def put_mirror(table, record_id, fields, modified_at=None, now=None):
    """直写飞书新建成功后写入镜像（整行）。"""
    now = _now(now)
    with store._lock, store._db() as c:
        _upsert_mirror(c, table, record_id, dict(fields or {}), modified_at, now)


def patch_mirror(table, record_id, fields, now=None):
    """直写飞书修改成功后把写过的列并进镜像；镜像里没有该行时不补建（返回 False），
    免得造出只有几列的残缺行、把后续编辑的基准带偏。"""
    now = _now(now)
    with store._lock, store._db() as c:
        cur = _mirror_fields(c, table, record_id)
        if cur is None:
            return False
        cur.update(fields or {})
        _upsert_mirror(c, table, record_id, cur, None, now)
        return True


def drop_mirror(table, record_id):
    """直写飞书删除成功后移除镜像行。"""
    with store._lock, store._db() as c:
        c.execute("DELETE FROM remote_records WHERE table_name=? AND record_id=?",
                  (table, record_id))


def _upsert_mirror(c, table, record_id, fields, modified_at, now):
    c.execute(
        "INSERT INTO remote_records(table_name, record_id, fields, modified_at, modified_by, "
        "fetched_at) VALUES(?,?,?,?,NULL,?) ON CONFLICT(table_name, record_id) DO UPDATE SET "
        "fields=excluded.fields, modified_at=COALESCE(excluded.modified_at, modified_at), "
        "fetched_at=excluded.fetched_at", (table, record_id, _dumps(fields), modified_at, now))


def _mirror_fields(c, table, record_id):
    r = c.execute("SELECT fields FROM remote_records WHERE table_name=? AND record_id=?",
                  (table, record_id)).fetchone()
    return _loads(r["fields"], {}) if r else None


# ---------------------------------------------------------------- 临时 ID

def _resolve(c, record_id):
    if not rules.is_local_id(record_id):
        return record_id
    r = c.execute("SELECT remote_id FROM id_map WHERE local_id=?", (record_id,)).fetchone()
    return (r["remote_id"] if r and r["remote_id"] else record_id)


def local_state(local_id):
    """本地临时 ID 的推送状态：mapped 已对应上 / pending 还在队列里 / failed 新建没推成 /
    unknown 没有这个 ID。推送线程据此决定"等一等"还是"放弃"。"""
    with store._lock:
        c = store._db()
        r = c.execute("SELECT remote_id FROM id_map WHERE local_id=?", (local_id,)).fetchone()
        if r is None:
            return "unknown"
        if r["remote_id"]:
            return "mapped"
        st = c.execute("SELECT state FROM outbox WHERE record_id=? AND op='create' "
                       "ORDER BY id DESC LIMIT 1", (local_id,)).fetchone()
    return "pending" if st and st["state"] in OPEN_STATES else "failed"


def resolve_id(record_id):
    """本地临时 ID 已对应上飞书记录时返回飞书 record_id，否则原样返回。"""
    with store._lock:
        return _resolve(store._db(), record_id)


def local_alias(remote_id):
    """飞书 record_id 对应的本地临时 ID（旧链接跳转用）；没有返回 None。"""
    with store._lock:
        r = store._db().execute("SELECT local_id FROM id_map WHERE remote_id=?",
                                (remote_id,)).fetchone()
    return r["local_id"] if r else None


# ---------------------------------------------------------------- 待推送队列

def _open_ops(c, table, record_id):
    return [_op(r) for r in c.execute(
        "SELECT * FROM outbox WHERE table_name=? AND record_id=? AND state IN (?, ?) "
        "ORDER BY id", (table, record_id) + OPEN_STATES)]


def _view(c, table, record_id, ops):
    """本地视图 = 镜像 ⊕ 未落地操作；镜像与操作都没有时抛 RecordUnknown。"""
    fields = _mirror_fields(c, table, record_id)
    if fields is None and not ops:
        raise RecordUnknown(f"{table}/{record_id} 不在本地镜像中")
    return rules.apply_ops(fields or {}, ops, directives.evaluate)


def table_views(tables):
    """多张表的本地视图（同一时刻的一致读取）：{表: [(record_id, 字段)]}。

    镜像每行叠加它的未落地操作；本地新建还没推出去的记录以临时 ID 出现；
    排在删除队列里的记录不出现。页面读到的快照就由它规范化而来（缓存库第 4 步）。
    """
    out = {}
    with store._lock:
        c = store._db()
        for t in tables:
            views = {r["record_id"]: _loads(r["fields"], {}) for r in c.execute(
                "SELECT record_id, fields FROM remote_records WHERE table_name=?", (t,))}
            grouped = {}
            for r in c.execute("SELECT * FROM outbox WHERE table_name=? AND state IN (?, ?) "
                               "ORDER BY id", (t,) + OPEN_STATES):
                op = _op(r)
                grouped.setdefault(op["record_id"], []).append(op)
            for rid, ops in grouped.items():
                if rid not in views and ops[0]["op"] != "create":
                    continue                  # 镜像里没有、又不是本地新建：无从叠加
                view = rules.apply_ops(views.get(rid) or {}, ops, directives.evaluate)
                if view is None:
                    views.pop(rid, None)
                else:
                    views[rid] = view
            out[t] = sorted(views.items())
    return out


def local_view(table, record_id):
    """本地视图：镜像叠加未落地操作后的字段（飞书列名为键）；已在删除队列返回 None。
    记录不在本地时抛 RecordUnknown。"""
    with store._lock:
        c = store._db()
        rid = _resolve(c, record_id)
        return _view(c, table, rid, _open_ops(c, table, rid))


def _insert_op(c, table, record_id, op, fields, increments, base, by, now):
    cur = c.execute(
        "INSERT INTO outbox(table_name, record_id, op, fields, increments, base, state, "
        "client_token, created_by, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,'pending',?,?,?,?)",
        (table, record_id, op, _dumps(fields or {}), _dumps(increments or {}),
         _dumps(base or {}), str(uuid.uuid4()), by, now, now))
    return cur.lastrowid


def _save_op(c, op, now):
    c.execute("UPDATE outbox SET fields=?, increments=?, base=?, updated_at=? WHERE id=?",
              (_dumps(op["fields"]), _dumps(op["increments"]), _dumps(op["base"]),
               now, op["id"]))


def _mergeable(op):
    # 只并进还没开始推的操作：已经尝试过的新建可能已在飞书落库（幂等键会让飞书
    # 返回旧内容），这时再改它的字段会丢改动
    return op["state"] == "pending" and op["attempts"] == 0 and op["op"] in ("create", "update")


def enqueue_create(table, fields, by=None, now=None):
    """新建：分配本地临时 ID 并入队，返回临时 ID。"""
    now = _now(now)
    lid = rules.new_local_id()
    with store._lock, store._db() as c:
        c.execute("INSERT INTO id_map(local_id, table_name, created_at) VALUES(?,?,?)",
                  (lid, table, now))
        _insert_op(c, table, lid, "create", dict(fields or {}), {}, {}, by, now)
    return lid


def _enqueue_update(c, table, record_id, sets, increments, by, now):
    rid = _resolve(c, record_id)
    sets = dict(sets or {})
    inc = {k: float(v) for k, v in (increments or {}).items()}
    if not sets and not inc:
        raise ValueError("没有要写的列")
    if set(sets) & set(inc):
        raise ValueError("同一列不能既直接写又按增量写")
    ops = _open_ops(c, table, rid)
    if ops and ops[-1]["op"] == "delete":
        raise RecordUnknown(f"{table}/{rid} 已在删除队列中")
    view = _view(c, table, rid, ops)
    last = ops[-1] if ops else None
    if last and _mergeable(last):
        f, b, i = last["fields"], last["base"], last["increments"]
        if last["op"] == "create":
            f.update(sets)
            for col, d in inc.items():
                f[col] = rules.apply_increment(f.get(col), d)
        else:
            before = rules.apply_ops(_mirror_fields(c, table, rid) or {}, ops[:-1],
                                     directives.evaluate) or {}
            for col, v in sets.items():
                i.pop(col, None)                     # 原先按增量写的列改为直接写
                if col not in b:                     # 已在队列里的列保留最早的基准
                    b[col] = before.get(col)
                f[col] = v
            for col, d in inc.items():
                if col in f:                         # 原先直接写，在其上累加
                    f[col] = rules.apply_increment(f[col], d)
                else:
                    i[col] = round(i.get(col, 0.0) + d, 6)
        _save_op(c, last, now)
        return last["id"]
    base = {col: view.get(col) for col in sets}
    return _insert_op(c, table, rid, "update", sets, inc, base, by, now)


def enqueue_update(table, record_id, sets, by=None, increments=None, now=None):
    """修改：基准取本地视图（镜像 ⊕ 未落地操作），返回队列 ID。

    最后一条未落地操作还没开始推时直接合并进去（同列取最新值、基准保留最早的），
    同一记录连续改几次只推一次。记录不在本地时抛 RecordUnknown。
    """
    now = _now(now)
    with store._lock, store._db() as c:
        return _enqueue_update(c, table, record_id, sets, increments, by, now)


def enqueue_delete(table, record_id, watch_cols, by=None, now=None):
    """删除：返回队列 ID；新建还没推出去就被删时两者一起作废，返回 None。

    watch_cols：推送前要核对的列（传门户可写的列，避免公式列自己变化造成误判）。
    还没开始推的修改会被作废（cancelled），删除的基准取剩余未落地操作之后的视图。
    """
    now = _now(now)
    with store._lock, store._db() as c:
        rid = _resolve(c, record_id)
        ops = _open_ops(c, table, rid)
        if ops and ops[-1]["op"] == "delete":
            return ops[-1]["id"]
        if (rules.is_local_id(rid) and ops and ops[0]["op"] == "create"
                and _mergeable(ops[0])):
            c.execute("UPDATE outbox SET state='cancelled', note=?, finished_at=?, "
                      "updated_at=? WHERE table_name=? AND record_id=? AND state IN (?, ?)",
                      ("新建尚未推送即被删除", now, now, table, rid) + OPEN_STATES)
            return None
        drop = [o["id"] for o in ops if o["op"] == "update" and _mergeable(o)]
        c.executemany("UPDATE outbox SET state='cancelled', note=?, finished_at=?, "
                      "updated_at=? WHERE id=?",
                      [("被后续删除取代", now, now, i) for i in drop])
        keep = [o for o in ops if o["id"] not in drop]
        view = _view(c, table, rid, keep) or {}
        base = {col: view.get(col) for col in watch_cols}
        return _insert_op(c, table, rid, "delete", {}, {}, base, by, now)


def claim_next(now=None):
    """取下一条可推的操作并置为 pushing；没有返回 None。

    同一记录严格按入队顺序：前面还有未落地的操作就不取后面的。返回值多带一个
    remote_id：已对应上的飞书 record_id；新建之外的操作取不到时为 None
    （说明它依赖的新建失败了，推送方应把它置为 failed）。
    """
    now = _now(now)
    with store._lock, store._db() as c:
        row = c.execute(
            "SELECT o.* FROM outbox o WHERE o.state='pending' AND o.next_attempt_at<=? "
            "AND NOT EXISTS(SELECT 1 FROM outbox p WHERE p.table_name=o.table_name "
            "AND p.record_id=o.record_id AND p.id<o.id AND p.state IN (?, ?)) "
            "ORDER BY o.id LIMIT 1", (now,) + OPEN_STATES).fetchone()
        if row is None:
            return None
        c.execute("UPDATE outbox SET state='pushing', attempts=attempts+1, updated_at=? "
                  "WHERE id=?", (now, row["id"]))
        op = _op(row)
        op["state"], op["attempts"] = "pushing", op["attempts"] + 1
        rid = _resolve(c, op["record_id"])
        op["remote_id"] = None if rules.is_local_id(rid) else rid
        return op


def get_op(op_id):
    with store._lock:
        return _op(store._db().execute("SELECT * FROM outbox WHERE id=?", (op_id,)).fetchone())


def _finish(c, op_id, state, now, note=None, error=None):
    c.execute("UPDATE outbox SET state=?, note=COALESCE(?, note), last_error=COALESCE(?, "
              "last_error), finished_at=?, updated_at=? WHERE id=?",
              (state, note, error, now, now, op_id))


def _record_conflicts(c, op, record_id, items, now):
    c.executemany(
        "INSERT INTO conflicts(outbox_id, table_name, record_id, kind, field, base_value, "
        "local_value, remote_value, detected_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [(op["id"], op["table_name"], record_id, it["kind"], it.get("field"),
          _dumps(it.get("base")), _dumps(it.get("local")), _dumps(it.get("remote")), now)
         for it in items])


def mark_retry(op_id, error, delay, now=None):
    """临时失败（网络/限流）：放回 pending，delay 秒后再试。"""
    now = _now(now)
    with store._lock, store._db() as c:
        c.execute("UPDATE outbox SET state='pending', next_attempt_at=?, last_error=?, "
                  "updated_at=? WHERE id=?", (now + delay, str(error)[:200], now, op_id))


def mark_failed(op_id, error, now=None):
    """永久失败（飞书拒绝、权限不足等）：不再重试，等人处理。"""
    now = _now(now)
    with store._lock, store._db() as c:
        _finish(c, op_id, "failed", now, error=str(error)[:200])


def complete_create(op_id, remote_id, pushed_fields=None, modified_at=None, now=None):
    """新建推送成功：对应上临时 ID，后续操作改挂飞书 ID，写回镜像。"""
    now = _now(now)
    with store._lock, store._db() as c:
        op = _op(c.execute("SELECT * FROM outbox WHERE id=?", (op_id,)).fetchone())
        lid = op["record_id"]
        c.execute("UPDATE id_map SET remote_id=?, mapped_at=? WHERE local_id=?",
                  (remote_id, now, lid))
        c.execute("UPDATE outbox SET record_id=?, updated_at=? WHERE table_name=? "
                  "AND record_id=? AND id<>? AND state IN (?, ?)",
                  (remote_id, now, op["table_name"], lid, op_id) + OPEN_STATES)
        _upsert_mirror(c, op["table_name"], remote_id,
                       pushed_fields if pushed_fields is not None else op["fields"],
                       modified_at, now)
        _finish(c, op_id, "done", now)


def complete_update(op_id, pushed, conflicts=(), remote_fields=None, modified_at=None,
                    now=None):
    """修改推送结束（可能部分列冲突）：写回镜像、记冲突。

    remote_fields：推送**之后**飞书上该记录的完整字段（已含本次推的列，读形状），
    给了就整行替换镜像；没给则在现有镜像上叠加已推的列。
    全部列冲突且一列没推 → 状态 conflict；否则 done。
    """
    now = _now(now)
    with store._lock, store._db() as c:
        op = _op(c.execute("SELECT * FROM outbox WHERE id=?", (op_id,)).fetchone())
        table, rid = op["table_name"], _resolve(c, op["record_id"])
        if remote_fields is not None:
            _upsert_mirror(c, table, rid, dict(remote_fields), modified_at, now)
        elif pushed:
            fields = _mirror_fields(c, table, rid) or {}
            fields.update(pushed)
            _upsert_mirror(c, table, rid, fields, modified_at, now)
        if conflicts:
            _record_conflicts(c, op, rid, conflicts, now)
        state = "conflict" if (conflicts and not pushed) else "done"
        note = ("部分列冲突，已按飞书为准" if conflicts and pushed
                else "全部列冲突，已按飞书为准" if conflicts else None)
        _finish(c, op_id, state, now, note=note)


def complete_delete(op_id, now=None):
    """删除推送成功（或飞书侧早已删除）：移除镜像行。"""
    now = _now(now)
    with store._lock, store._db() as c:
        op = _op(c.execute("SELECT * FROM outbox WHERE id=?", (op_id,)).fetchone())
        c.execute("DELETE FROM remote_records WHERE table_name=? AND record_id=?",
                  (op["table_name"], _resolve(c, op["record_id"])))
        _finish(c, op_id, "done", now)


def reject(op_id, conflicts, note=None, remote_gone=False, now=None):
    """整条操作因冲突放弃（飞书已删 / 删除被拦）：记冲突，状态 conflict。"""
    now = _now(now)
    with store._lock, store._db() as c:
        op = _op(c.execute("SELECT * FROM outbox WHERE id=?", (op_id,)).fetchone())
        rid = _resolve(c, op["record_id"])
        if remote_gone:
            c.execute("DELETE FROM remote_records WHERE table_name=? AND record_id=?",
                      (op["table_name"], rid))
        _record_conflicts(c, op, rid, conflicts, now)
        _finish(c, op_id, "conflict", now, note=note)


def retry_failed(op_id, now=None):
    """人工重试一条 failed 操作（改完飞书侧问题后用）：放回 pending、立即可推。"""
    now = _now(now)
    with store._lock, store._db() as c:
        return c.execute(
            "UPDATE outbox SET state='pending', next_attempt_at=?, finished_at=NULL, "
            "note=?, updated_at=? WHERE id=? AND state='failed'",
            (now, "人工重试", now, op_id)).rowcount == 1


def discard(op_id, note="人工作废", now=None):
    """人工作废一条还没落到飞书的操作（failed 或 pending）；推送中的不能作废。"""
    now = _now(now)
    with store._lock, store._db() as c:
        return c.execute(
            "UPDATE outbox SET state='cancelled', note=?, finished_at=?, updated_at=? "
            "WHERE id=? AND state IN ('failed', 'pending')",
            (note, now, now, op_id)).rowcount == 1


def failed_after(op_id):
    """同一记录上排在 op_id 之后、也已失败的操作 id（新建失败会连带后续操作失败，
    组长重试或放弃新建时一并处理）。"""
    with store._lock:
        c = store._db()
        op = c.execute("SELECT table_name, record_id FROM outbox WHERE id=?",
                       (op_id,)).fetchone()
        if op is None:
            return []
        return [r["id"] for r in c.execute(
            "SELECT id FROM outbox WHERE table_name=? AND record_id=? AND id>? "
            "AND state='failed' ORDER BY id", (op["table_name"], op["record_id"], op_id))]


def requeue_stuck(older_than=300, now=None):
    """服务异常退出时停在 pushing 的操作放回 pending（启动时调用），返回条数。

    新建靠 client_token 幂等，重推不会在飞书产生重复记录。
    """
    now = _now(now)
    with store._lock, store._db() as c:
        return c.execute(
            "UPDATE outbox SET state='pending', next_attempt_at=?, last_error=?, updated_at=? "
            "WHERE state='pushing' AND updated_at<?",
            (now, "推送中断，已重新排队", now, now - older_than)).rowcount


def open_ops(table=None):
    """全部未落地操作（按入队顺序），页面叠加本地改动与显示"待同步"用。"""
    sql = "SELECT * FROM outbox WHERE state IN (?, ?)"
    args = OPEN_STATES
    if table:
        sql += " AND table_name=?"
        args += (table,)
    with store._lock:
        return [_op(r) for r in store._db().execute(sql + " ORDER BY id", args)]


def has_open_ops(table, record_id):
    """该记录是否还有未落地的操作（有就必须排队，不能绕过队列直写）。"""
    with store._lock:
        c = store._db()
        rid = _resolve(c, record_id)
        return bool(c.execute(
            "SELECT 1 FROM outbox WHERE table_name=? AND record_id=? AND state IN (?, ?) LIMIT 1",
            (table, rid) + OPEN_STATES).fetchone()) or rules.is_local_id(rid)


def pending_values(table, col):
    """未落地操作里某列的值（采购自动编号要避开排队中的号）。"""
    return [op["fields"][col] for op in open_ops(table)
            if op["op"] in ("create", "update") and col in op["fields"]]


def next_due(now=None):
    """最近一条待推操作的可推时刻（推送线程算等待时长用）；没有待推的返回 None。"""
    with store._lock:
        v = store._db().execute(
            "SELECT MIN(next_attempt_at) FROM outbox WHERE state='pending'").fetchone()[0]
    return v


def recent_ops(limit=50, states=None):
    """最近的队列操作（排障与状态页用），新的在前。"""
    sql, args = "SELECT * FROM outbox", []
    if states:
        sql += " WHERE state IN (%s)" % ",".join("?" * len(states))
        args += list(states)
    with store._lock:
        rows = store._db().execute(sql + " ORDER BY id DESC LIMIT ?",
                                   args + [int(limit)]).fetchall()
    return [_op(r) for r in rows]


def record_marks(tables):
    """每条记录的同步标记原料（页面显示「待同步 / 同步失败 / 冲突」用，缓存库第 5 步）。

    返回 {表: {record_id: {"pending": 未落地条数, "retry_error": 最近一次临时失败原因,
    "failed": [{"op_id", "op", "error"}], "conflicts": [{"id", "kind", "field"}]}}}；
    没有任何标记的记录不出现。record_id 已换成飞书 ID（临时 ID 已对应上时）。
    """
    tables = tuple(tables)
    if not tables:
        return {}
    marks = {}

    def slot(c, table, record_id):
        rid = _resolve(c, record_id)
        return marks.setdefault(table, {}).setdefault(
            rid, {"pending": 0, "retry_error": None, "failed": [], "conflicts": []})

    ph = ",".join("?" * len(tables))
    with store._lock:
        c = store._db()
        for r in c.execute(
                "SELECT id, table_name, record_id, op, state, last_error FROM outbox "
                "WHERE table_name IN (%s) AND state IN ('pending', 'pushing', 'failed') "
                "ORDER BY id" % ph, tables):
            m = slot(c, r["table_name"], r["record_id"])
            if r["state"] == "failed":
                m["failed"].append({"op_id": r["id"], "op": r["op"],
                                    "error": r["last_error"] or ""})
            else:
                m["pending"] += 1
                if r["last_error"]:
                    m["retry_error"] = r["last_error"]
        for r in c.execute(
                "SELECT id, table_name, record_id, kind, field FROM conflicts "
                "WHERE acked_at IS NULL AND table_name IN (%s) ORDER BY id" % ph, tables):
            slot(c, r["table_name"], r["record_id"])["conflicts"].append(
                {"id": r["id"], "kind": r["kind"], "field": r["field"]})
    return marks


def backlog():
    """队列与冲突概况（同步状态页、备份清单、巡检用）。"""
    with store._lock:
        db = store._db()
        counts = {r["state"]: r["n"] for r in db.execute(
            "SELECT state, COUNT(*) AS n FROM outbox WHERE state IN "
            "('pending', 'pushing', 'failed') GROUP BY state")}
        oldest = db.execute("SELECT MIN(created_at) FROM outbox WHERE state IN (?, ?)",
                            OPEN_STATES).fetchone()[0]
        open_conflicts = db.execute(
            "SELECT COUNT(*) FROM conflicts WHERE acked_at IS NULL").fetchone()[0]
    return {"pending": counts.get("pending", 0), "pushing": counts.get("pushing", 0),
            "failed": counts.get("failed", 0), "open_conflicts": open_conflicts,
            "oldest_open_at": oldest}


# ---------------------------------------------------------------- 冲突记录

def _conflict(row):
    if row is None:
        return None
    d = dict(row)
    for k in ("base_value", "local_value", "remote_value"):
        d[k] = _loads(d[k], None)
    return d


def list_conflicts(open_only=True, limit=100):
    sql = "SELECT * FROM conflicts"
    if open_only:
        sql += " WHERE acked_at IS NULL"
    with store._lock:
        rows = store._db().execute(sql + " ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [_conflict(r) for r in rows]


def get_conflict(conflict_id):
    with store._lock:
        return _conflict(store._db().execute("SELECT * FROM conflicts WHERE id=?",
                                             (conflict_id,)).fetchone())


def ack_conflict(conflict_id, by, now=None):
    """组长确认已知悉一条冲突；返回是否确有未确认的这一条。"""
    now = _now(now)
    with store._lock, store._db() as c:
        return c.execute("UPDATE conflicts SET acked_at=?, acked_by=? WHERE id=? "
                         "AND acked_at IS NULL", (now, by, conflict_id)).rowcount == 1


# ---------------------------------------------------------------- 考勤明细

def _session(row):
    return dict(row) if row else None


def open_session(open_id):
    with store._lock:
        return _session(store._db().execute(
            "SELECT * FROM attendance_sessions WHERE open_id=? AND state='open'",
            (open_id,)).fetchone())


def latest_session(open_id):
    """该成员最近一条明细（任何状态）；没有返回 None。"""
    with store._lock:
        return _session(store._db().execute(
            "SELECT * FROM attendance_sessions WHERE open_id=? ORDER BY check_in_at DESC, id DESC "
            "LIMIT 1", (open_id,)).fetchone())


def _push_in_txn(c, push, at):
    """在当前事务里把一次飞书写入入队（考勤明细与队列要么都成、要么都不成）。"""
    if not push:
        return None
    return _enqueue_update(c, push["table"], push["record_id"], push.get("sets") or {},
                           push.get("increments") or {}, push.get("by"), at)


def start_session(open_id, member_record_id, at=None, push=None):
    """签到：新开一条明细；已有未闭合的签到时抛 AttendanceStateError。

    push 同 close_session：给了就在同一事务里把"写最近签到"入队。
    """
    at = _now(at)
    with store._lock, store._db() as c:
        if c.execute("SELECT 1 FROM attendance_sessions WHERE open_id=? AND state='open'",
                     (open_id,)).fetchone():
            raise AttendanceStateError("已在签到状态")
        op_id = _push_in_txn(c, push, at)
        cur = c.execute(
            "INSERT INTO attendance_sessions(open_id, member_record_id, check_in_at, state, "
            "outbox_id, created_at, updated_at) VALUES(?,?,?,'open',?,?,?)",
            (open_id, member_record_id, at, op_id, at, at))
        return _session(c.execute("SELECT * FROM attendance_sessions WHERE id=?",
                                  (cur.lastrowid,)).fetchone())


def close_session(open_id, at=None, push=None, note=None):
    """签退：闭合明细并算出本次时长（小时，4 位小数）。

    push 给了就在**同一事务**里把飞书写入入队（明细与队列要么都成、要么都不成），
    形如 {"table", "record_id", "sets", "increments", "by"}；increments 里的时长列
    可用 "{hours}" 占位，入队时替换成本次时长。
    """
    at = _now(at)
    with store._lock, store._db() as c:
        s = c.execute("SELECT * FROM attendance_sessions WHERE open_id=? AND state='open'",
                      (open_id,)).fetchone()
        if s is None:
            raise AttendanceStateError("当前未处于签到状态")
        if at < s["check_in_at"]:
            raise AttendanceStateError("签退时间早于签到时间")
        hours = round((at - s["check_in_at"]) / 3600.0, 4)
        op_id = None
        if push:
            inc = {k: (hours if v == "{hours}" else v)
                   for k, v in (push.get("increments") or {}).items()}
            op_id = _push_in_txn(c, dict(push, increments=inc), at)
        c.execute("UPDATE attendance_sessions SET check_out_at=?, hours=?, state='closed', "
                  "note=COALESCE(?, note), outbox_id=?, updated_at=? WHERE id=?",
                  (at, hours, note, op_id, at, s["id"]))
        return _session(c.execute("SELECT * FROM attendance_sessions WHERE id=?",
                                  (s["id"],)).fetchone())


def void_session(open_id, at=None, note=None, push=None):
    """作废未闭合的签到（跨零点未签退等），不计时长；没有未闭合签到时返回 None。

    push 给了就在同一事务里把"清最近签到"入队。
    """
    at = _now(at)
    with store._lock, store._db() as c:
        s = c.execute("SELECT id FROM attendance_sessions WHERE open_id=? AND state='open'",
                      (open_id,)).fetchone()
        if s is None:
            return None
        op_id = _push_in_txn(c, push, at)
        c.execute("UPDATE attendance_sessions SET state='void', note=?, outbox_id=?, "
                  "updated_at=? WHERE id=?", (note, op_id, at, s["id"]))
        return _session(c.execute("SELECT * FROM attendance_sessions WHERE id=?",
                                  (s["id"],)).fetchone())


def open_sessions(before=None):
    """全部未闭合的签到（before 给了只取签到早于该时刻的），零点作废用。"""
    sql, args = "SELECT * FROM attendance_sessions WHERE state='open'", []
    if before is not None:
        sql += " AND check_in_at<?"
        args.append(float(before))
    with store._lock:
        return [_session(r) for r in store._db().execute(sql + " ORDER BY id", args)]


def list_sessions(open_id=None, since=None, limit=200):
    sql, args = "SELECT * FROM attendance_sessions WHERE 1=1", []
    if open_id:
        sql += " AND open_id=?"
        args.append(open_id)
    if since is not None:
        sql += " AND check_in_at>=?"
        args.append(float(since))
    with store._lock:
        rows = store._db().execute(sql + " ORDER BY check_in_at DESC LIMIT ?",
                                   args + [int(limit)]).fetchall()
    return [_session(r) for r in rows]
