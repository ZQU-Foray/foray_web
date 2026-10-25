# -*- coding: utf-8 -*-
"""阶段 8：运维加固用例（健康巡检 / 备份保留策略 / WAL 回收 / 焦点恢复辅助）。

- 健康脚本 `scripts/portal_health.py`：正常 → 0；同步连续失败 → 非零且类别正确；
  schema 版本不匹配 → 4；令牌无效 → 7（退出码即告警契约）。
- 备份保留策略 `portal_backup.py --keep N`：只清理"含 manifest.json 的备份目录"，
  数据本体（config/、firmware/、.env）永不被删。
- WAL 回收 `store.maybe_checkpoint()`：超阈值时 TRUNCATE，未超不动。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                      # noqa: E402
from harness import BASE, api       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def note(key, value):
    print(f"  · {key}: {value}")


def health(*args):
    r = subprocess.run([PY, "scripts/portal_health.py", "--base-url", BASE] + list(args),
                       capture_output=True, text=True, cwd=REPO, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


def t_health_script():
    print("== 8-2 健康巡检脚本（退出码即告警契约） ==")
    harness.start_fake_feishu(tenant_key="tenant-test")
    harness._fake_feishu.bitable = _small_data()      # 不给数据会以 config 失败
    harness.login_member()
    tok = harness._auth["member"]

    # 1) 正常态
    api("/api/portal/sync", body={}, method="POST", auth="member")
    deadline = time.time() + 25
    while time.time() < deadline:
        _, st = api("/api/portal/sync-status", auth="member")
        if st.get("has_snapshot") and not st.get("last_error") and not st.get("syncing"):
            break
        time.sleep(1)
    code, out = health("--token", tok)
    check("同步正常 → 退出码 0（OK）", code == 0, f"rc={code} {out[:90]}")

    # 2) 令牌无效 → 7
    code, out = health("--token", "bogus-token-xyz")
    check("令牌无效 → 退出码 7（鉴权告警）", code == 7, f"rc={code} {out[:90]}")

    # 3) 同步连续失败 → 5（注入 500，失败类别应为 api/network 之类非空）
    state = os.path.join(tempfile.mkdtemp(prefix="health-state-"), "s.json")
    harness._fake_feishu.bitable = {"tblX": {"meta": [("标题", "fld1")], "records": []}}
    # （字段对不上 → config 类失败，且旧快照保留，可用性由 has_snapshot 体现）
    harness._fake_feishu.sync_fail = {"table": "", "mode": "500", "left": 3}
    harness.restart_service(env_extra={"PORTAL_SYNC_INTERVAL": "3600",
                                       "PORTAL_MANUAL_MIN_GAP": "1"})
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    tok = harness._auth["member"]
    api("/api/portal/sync", body={}, method="POST", auth="member")
    time.sleep(8)
    _, st = api("/api/portal/sync-status", auth="member")
    code, out = health("--token", tok, "--fail-streak", "1", "--state-file", state)
    check("同步失败（类别 %s）→ 退出码 5 且连续轮数被记录" % (st.get("last_error") or "?"),
          code == 5 and "连续" in out, f"rc={code} {out[:110]}")
    note("失败注入", f"last_error={st.get('last_error')!r}（假飞书 500）")

    # 4) 恢复后自愈 → 0
    harness._fake_feishu.sync_fail = {"table": "", "mode": None, "left": 0}
    harness._fake_feishu.bitable = _small_data()
    api("/api/portal/sync", body={}, method="POST", auth="member")
    deadline = time.time() + 25
    while time.time() < deadline:
        _, st = api("/api/portal/sync-status", auth="member")
        if not st.get("last_error") and not st.get("syncing"):
            break
        time.sleep(1)
    code, out = health("--token", tok, "--fail-streak", "1", "--state-file", state)
    check("清障后 → 退出码 0（告警自愈，状态文件重置）", code == 0, f"rc={code} {out[:90]}")

    # 5) schema 版本不匹配 → 4
    bad = harness._tmpdir + "/health-bad-schema.db"
    c = sqlite3.connect(bad)
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.executemany("INSERT INTO meta(key,value) VALUES(?,?)",
                  [("schema_version", "99"), ("current_version", "3")])
    c.commit()
    c.close()
    harness.restart_service(env_extra={"PORTAL_DB": bad,
                                       "PORTAL_SYNC_INTERVAL": "3600"})
    harness._auth["emergency"] = harness._login_emergency()
    harness.login_member()
    code, out = health("--token", harness._auth["member"])
    check("快照库版本不匹配 → 退出码 4（指向运维手册）",
          code == 4 and "schema" in out.lower(), f"rc={code} {out[:110]}")

    # 6) 本地写入队列异常 → 8（缓存库第 3 步；同机直读模式）
    sys.path.insert(0, REPO)
    from app.portal import migrations
    qdb = harness._tmpdir + "/health-queue.db"
    c = sqlite3.connect(qdb)
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    migrations.create_v2_objects(c)
    c.executemany("INSERT INTO meta(key,value) VALUES(?,?)",
                  [("schema_version", str(migrations.LATEST)), ("current_version", "3"),
                   ("last_success_at", str(time.time())), ("last_error", "")])
    c.execute("INSERT INTO outbox(table_name, record_id, op, state, client_token, created_at,"
              " updated_at) VALUES('tasks', 'recA', 'update', 'failed', 't1', ?, ?)",
              (time.time(), time.time()))
    c.commit()
    qstate = os.path.join(tempfile.mkdtemp(prefix="health-q-"), "s.json")
    code, out = health("--db", qdb, "--state-file", qstate)
    check("有推送失败的操作 → 退出码 8", code == 8 and "推送失败" in out, f"rc={code} {out[:110]}")
    c.execute("UPDATE outbox SET state='pending', created_at=?", (time.time() - 3600,))
    c.commit()
    code, out = health("--db", qdb, "--state-file", qstate, "--queue-age", "900")
    check("未推送改动积压超过阈值 → 退出码 8", code == 8 and "已等" in out,
          f"rc={code} {out[:110]}")
    c.execute("UPDATE outbox SET state='done'")
    c.commit()
    c.close()
    code, out = health("--db", qdb, "--state-file", qstate)
    check("队列清空后 → 退出码 0", code == 0, f"rc={code} {out[:110]}")


def _small_data():
    """复用验收套件的可同步数据（字段与 harness.FAKE_TABLES_JSON 严格对应）。"""
    from test_acceptance import _small_data as _sd
    return _sd()


def t_backup_keep():
    print("== 8-1 备份保留策略（只删含 manifest 的旧备份） ==")
    root = tempfile.mkdtemp(prefix="keep-drill-")
    data = os.path.join(root, "app")
    os.makedirs(os.path.join(data, "config"))
    os.makedirs(os.path.join(data, "firmware"))
    open(os.path.join(data, "config", "feishu.json"), "w").write("{}")
    open(os.path.join(data, ".env"), "w").write("FEISHU_APP_SECRET=x\n")
    open(os.path.join(data, "firmware", "a.elf"), "wb").write(b"\x7fELF")
    outroot = os.path.join(root, "bks")
    os.makedirs(outroot)
    # 造 3 个旧备份目录（含 manifest）+ 1 个无关目录（不得被删）
    for i in range(3):
        d = os.path.join(outroot, "old-%d" % i)
        os.makedirs(d)
        json.dump({"files": []}, open(os.path.join(d, "manifest.json"), "w"))
        os.utime(d, (time.time() - 100 - i, time.time() - 100 - i))
    unrelated = os.path.join(outroot, "keep-me")
    os.makedirs(unrelated)
    open(os.path.join(unrelated, "note.txt"), "w").write("not a backup\n")

    out = os.path.join(outroot, "new")
    r = subprocess.run([PY, "scripts/portal_backup.py", "--data-dir", data,
                        "--out", out, "--keep", "2"], capture_output=True,
                       text=True, cwd=REPO, timeout=120)
    check("备份 + 保留策略执行成功", r.returncode == 0, (r.stdout + r.stderr)[-120:])
    left = sorted(os.listdir(outroot))
    check("只保留最近 2 份含 manifest 的备份 + 无关目录",
          "new" in left and "keep-me" in left
          and len([d for d in left if d.startswith("old-")]) == 1, str(left))
    check("无关目录未被误删", os.path.isfile(os.path.join(unrelated, "note.txt")))
    check("数据本体未被动过（config/firmware/.env 仍在）",
          os.path.isfile(os.path.join(data, "config", "feishu.json"))
          and os.path.isfile(os.path.join(data, "firmware", "a.elf"))
          and os.path.isfile(os.path.join(data, ".env")))
    check("输出里注明清理数量", "保留策略" in r.stdout, r.stdout[-120:])
    shutil.rmtree(root, ignore_errors=True)


def t_wal_checkpoint():
    print("== 8-3 快照库 WAL 回收 ==")
    root = tempfile.mkdtemp(prefix="wal-drill-")
    db = os.path.join(root, "portal.db")
    sys.path.insert(0, REPO)
    from app import config as cfg
    from app.portal import store
    cfg.PORTAL_DB = __import__("pathlib").Path(db)
    store.close()
    store._conn = None
    ver = store.next_version()
    store.write_candidate(ver, "projects", [{"id": "r%d" % i} for i in range(2000)], [])
    store.publish(ver, {"projects": 2000}, 0.1)
    wal = db + "-wal"
    size_before = os.path.getsize(wal) if os.path.exists(wal) else 0
    # 未到阈值：不动
    ran, before, after = store.maybe_checkpoint(threshold_bytes=10 ** 9)
    check("未到阈值时不 checkpoint（避免无谓阻塞读方）",
          ran is False and before == after, f"{ran} {before}->{after}")
    # 到阈值：TRUNCATE 回收
    ran, before, after = store.maybe_checkpoint(threshold_bytes=1)
    check("超过阈值即执行 TRUNCATE 且 WAL 变小",
          ran is True and after < before, f"{before}->{after}")
    check("回收后数据仍可读（version 未变）",
          store.get_meta("current_version") == str(ver))
    store.close()
    shutil.rmtree(root, ignore_errors=True)
    note("WAL 回收", f"2000 行写入后 WAL {size_before/1e6:.2f}MB → 回收后 {after/1e6:.2f}MB")


def run():
    t_health_script()
    t_backup_keep()
    t_wal_checkpoint()
    print(f"\n运维加固: {len(PASS)} 通过, {len(FAIL)} 失败")
    return len(PASS), len(FAIL)


if __name__ == "__main__":
    def _ensure():
        import urllib.request as _u
        try:
            _u.urlopen(BASE + "/login", timeout=2)
            return True
        except Exception:
            return harness.start_service()
    if not _ensure():
        print("✗ 测试服务未就绪")
        raise SystemExit(1)
    try:
        run()
    finally:
        harness.stop_service()
    raise SystemExit(1 if FAIL else 0)
