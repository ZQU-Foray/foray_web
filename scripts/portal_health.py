# -*- coding: utf-8 -*-
"""门户健康检查（阶段 8）：一次判定 + 明确退出码，供 systemd timer 巡检/告警。

判定项（任一命中即非零退出，便于监控直接接退出码）：
1. 服务不可达（连不上 / 5xx）——退出码 3；
2. 快照库 schema 版本不匹配（数据接口 503 `schema_version_mismatch`）——退出码 4；
3. `last_error` 连续 N 轮非空（默认 3）——退出码 5；
4. 超过 `--stale-sec`（默认取 `PORTAL_SYNC_STALE` 或 600s）未成功同步——退出码 6；
5. HTTP 鉴权失败（401/403，说明令牌过期或身份收紧）——退出码 7；
6. 本地写入队列异常（缓存库，2026-10-21 起）：有推送失败（failed）的操作，
   或最老的未推送操作超过 `--queue-age`（默认 900s）——退出码 8。

两种模式：
- **同机直读（推荐给 systemd timer）**：`--db /opt/portal/config/portal_snapshot.db`
  —— 只读 meta，不需要任何令牌，不受成员会话 12h 过期影响；
- **远程 HTTP**：`--token <成员会话令牌>` / `--token-file`，走 `/api/portal/sync-status`。

用法：
    python scripts/portal_health.py --db /opt/portal/config/portal_snapshot.db
    python scripts/portal_health.py --base-url http://127.0.0.1 --token <令牌>

状态记忆：连续错误轮数写入 `--state-file`（默认 /tmp/portal-health.state），
让"连续 N 轮"跨多次巡检成立（单次进程看不到历史）。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_SCHEMA = 4
EXIT_SYNC_ERROR = 5
EXIT_STALE = 6
EXIT_AUTH = 7
EXIT_QUEUE = 8

MSGS = {
    EXIT_UNREACHABLE: "服务不可达",
    EXIT_SCHEMA: "快照库 schema 版本不匹配（数据接口 503，见运维手册 §8.1）",
    EXIT_SYNC_ERROR: "同步连续失败",
    EXIT_STALE: "超过阈值未成功同步",
    EXIT_AUTH: "健康检查令牌无效（401/403）",
    EXIT_QUEUE: "本地写入队列异常（推送失败或积压，见运维手册 §8.2）",
}


def queue_problems(backlog, max_age):
    """按队列概况判定：{failed, oldest_open_at, ...}；旧库或拿不到概况时不判定。"""
    if not backlog:
        return []
    out = []
    if backlog.get("failed"):
        out.append((EXIT_QUEUE, "推送失败 %s 条（飞书拒绝写入，需人工处理）" % backlog["failed"]))
    oldest = backlog.get("oldest_open_at")
    if oldest and max_age and time.time() - float(oldest) > max_age:
        out.append((EXIT_QUEUE, "最老的未推送改动已等 %.0fs > %ds（推送线程卡住或飞书持续不可写）"
                    % (time.time() - float(oldest), max_age)))
    return out


def db_backlog(conn):
    """直读库里的队列概况；版本 1 的旧库没有队列表，返回 None。"""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "outbox" not in names:
        return None
    failed = conn.execute("SELECT COUNT(*) FROM outbox WHERE state='failed'").fetchone()[0]
    oldest = conn.execute("SELECT MIN(created_at) FROM outbox WHERE state IN "
                          "('pending', 'pushing')").fetchone()[0]
    return {"failed": failed, "oldest_open_at": oldest}


def _ssl_context(cafile="", insecure=False):
    """HTTPS 支持：自签 CA 用 --cafile；仅在明确知道风险时用 --insecure。"""
    import ssl
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return None


def http_status(url, timeout=10, ctx=None):
    """只取状态码（/login 返回 HTML，不能当 JSON 解析——曾因此误报"服务不可达"）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def http_json(url, token="", timeout=10, ctx=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}
    except Exception as e:
        return None, {"error": str(e)[:120]}


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"fail_streak": 0, "last_error": None}


def save_state(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def check_db(db_path, fail_streak, stale_sec, state_file, verbose=False, queue_age=900):
    """同机直读快照库 meta（只读打开，不需要令牌）。返回 (problems, info)。"""
    import sqlite3
    problems, info = [], {}
    if not os.path.isfile(db_path):
        problems.append((EXIT_UNREACHABLE, "快照库不存在：%s" % db_path))
        return problems, info
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        kv = dict(conn.execute("SELECT key, value FROM meta"))
        integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
        backlog = db_backlog(conn)
        conn.close()
    except Exception as e:
        problems.append((EXIT_UNREACHABLE, "快照库不可读：%s" % str(e)[:80]))
        return problems, info
    info = {"schema_version": kv.get("schema_version"),
            "current_version": kv.get("current_version"),
            "last_error": kv.get("last_error") or "",
            "last_success_at": kv.get("last_success_at"),
            "counts": kv.get("counts"), "integrity": integ}
    # 期望版本：优先用本仓库的 app.portal.store，其次只做"存在性"检查
    expected = None
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)
    try:
        from app.portal.store import SCHEMA_VERSION
        expected = str(SCHEMA_VERSION)
    except Exception:
        expected = None
    if expected and str(info["schema_version"]) != expected:
        problems.append((EXIT_SCHEMA, "库内=%s 期望=%s 路径=%s"
                         % (info["schema_version"], expected, db_path)))
    if integ != "ok":
        problems.append((EXIT_UNREACHABLE, "快照库完整性检查=%s" % integ))
    err = info["last_error"]
    state = load_state(state_file)
    if err:
        if state.get("last_error") == err:
            state["fail_streak"] = int(state.get("fail_streak", 0)) + 1
        else:
            state["fail_streak"] = 1
        state["last_error"] = err
        if state["fail_streak"] >= fail_streak:
            problems.append((EXIT_SYNC_ERROR, "类别 %s 连续 %d 轮"
                             % (err, state["fail_streak"])))
    else:
        state = {"fail_streak": 0, "last_error": None}
    save_state(state_file, state)
    last_ok = info["last_success_at"]
    if last_ok:
        age = time.time() - float(last_ok)
        if stale_sec and age > stale_sec:
            problems.append((EXIT_STALE, "距上次成功 %.0fs > %ds" % (age, stale_sec)))
    elif not info["current_version"]:
        problems.append((EXIT_STALE, "尚无任何成功快照（current_version 空）"))
    problems += queue_problems(backlog, queue_age)
    info["queue"] = backlog
    if verbose:
        print("db meta: %s" % info)
    return problems, info


def main():
    ap = argparse.ArgumentParser(description="门户健康检查（阶段 8）")
    ap.add_argument("--db", default="", help="同机直读快照库（推荐；无需令牌）")
    ap.add_argument("--base-url", default="http://127.0.0.1")
    ap.add_argument("--token", default="", help="成员会话令牌（或不给，只做匿名可用性检查）")
    ap.add_argument("--token-file", default="", help="从文件读取令牌（0600）")
    ap.add_argument("--fail-streak", type=int, default=3, help="连续失败阈值")
    ap.add_argument("--stale-sec", type=int, default=0, help="未成功同步阈值（0=用服务端 stale 标记）")
    ap.add_argument("--state-file", default="/tmp/portal-health.state")
    ap.add_argument("--queue-age", type=int, default=900,
                    help="未推送改动的最长等待秒数（超过即告警，0=不检查）")
    ap.add_argument("--cafile", default="", help="自签 CA 路径（HTTPS 巡检用）")
    ap.add_argument("--insecure", action="store_true",
                    help="跳过证书校验（仅排障，勿用于常规巡检）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    ctx = _ssl_context(args.cafile, args.insecure) if args.base_url.startswith("https") else None

    token = args.token
    if not token and args.token_file and os.path.isfile(args.token_file):
        token = open(args.token_file, encoding="utf-8").read().strip()

    base = args.base_url.rstrip("/")
    problems = []

    # 模式选择：显式 --db，或"没给令牌且本机存在默认快照库" → 直读
    db = args.db
    if not db and not token:
        default_db = "/opt/portal/config/portal_snapshot.db"
        if os.path.isfile(default_db):
            db = default_db

    if db:
        problems, info = check_db(db, args.fail_streak,
                                  args.stale_sec or 600,
                                  args.state_file, args.verbose, args.queue_age)
        code = http_status(base + "/login", ctx=ctx)   # 顺带探一次存活（不需要令牌）
        if code is not None and code != 200:
            problems.append((EXIT_UNREACHABLE, "/login HTTP %s" % code))
        if not problems:
            print("OK 门户健康（直读 %s）：同步正常，version=%s counts=%s"
                  % (db, info.get("current_version"), info.get("counts")))
            return EXIT_OK
        problems.sort()
        for code_, why in problems:
            print("FAIL[%d] %s：%s" % (code_, MSGS.get(code_, "异常"), why))
        return problems[0][0]

    code = http_status(base + "/login", ctx=ctx)
    if code != 200:
        problems.append((EXIT_UNREACHABLE, "/login HTTP %s" % code))
    code, body = http_json(base + "/api/portal/sync-status", token, ctx=ctx)
    if code in (401, 403):
        problems.append((EXIT_AUTH, "/api/portal/sync-status HTTP %s" % code))
    elif code is None:
        problems.append((EXIT_UNREACHABLE, "sync-status 不可达：%s" % body.get("error")))
    elif code == 503 and body.get("code") == "schema_version_mismatch":
        problems.append((EXIT_SCHEMA, "found=%s expected=%s path=%s"
                         % (body.get("schema_found"), body.get("schema_expected"),
                            body.get("snapshot_path"))))
    elif code != 200:
        problems.append((EXIT_UNREACHABLE, "sync-status HTTP %s" % code))
    else:
        st = body
        err = st.get("last_error") or ""
        state = load_state(args.state_file)
        if err:
            if state.get("last_error") == err:
                state["fail_streak"] = int(state.get("fail_streak", 0)) + 1
            else:
                state["fail_streak"] = 1
            state["last_error"] = err
            if state["fail_streak"] >= args.fail_streak:
                problems.append((EXIT_SYNC_ERROR,
                                 "类别 %s 连续 %d 轮" % (err, state["fail_streak"])))
        else:
            state = {"fail_streak": 0, "last_error": None}
        save_state(args.state_file, state)

        stale_sec = args.stale_sec
        last_ok = st.get("last_success_at")
        if last_ok:
            age = time.time() - float(last_ok)
            if stale_sec and age > stale_sec:
                problems.append((EXIT_STALE, "距上次成功 %.0fs > %ds"
                                 % (age, stale_sec)))
            elif not stale_sec and st.get("stale"):
                problems.append((EXIT_STALE, "服务端标记 stale（距上次成功 %.0fs）" % age))
        elif not st.get("has_snapshot"):
            problems.append((EXIT_STALE, "尚无任何成功快照（has_snapshot=false）"))
        problems += queue_problems(st.get("cache"), args.queue_age)
        if args.verbose:
            print("sync-status: version=%s counts=%s last_error=%r syncing=%s stale=%s"
                  % (st.get("snapshot_version"), st.get("counts"), err,
                     st.get("syncing"), st.get("stale")))

    if not problems:
        print("OK 门户健康：%s（同步正常）" % base)
        return EXIT_OK
    problems.sort()
    worst = problems[0]
    for code_, why in problems:
        print("FAIL[%d] %s：%s" % (code_, MSGS.get(code_, "异常"), why))
    return worst[0]


if __name__ == "__main__":
    sys.exit(main())
