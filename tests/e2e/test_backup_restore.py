# -*- coding: utf-8 -*-
"""阶段 7：备份/恢复隔离演练 + 部署打包保护（纯脚本级，不依赖测试服务）。

覆盖验收矩阵「备份恢复」行与阶段 7 的「凭据轮换不丢业务配置」：

1. 造一个含 config/（含 auth/ 与会话）、firmware/、.env、一致性快照库的
   数据目录 → 备份 → 校验 manifest 逐文件 sha256；
2. 篡改备份包内一个文件 → 恢复必须**中止**（不得静默写入坏数据）；
3. 恢复到空目录 → 逐文件字节一致、库 `PRAGMA integrity_check=ok`、
   schema 与快照 meta 一致（含 WAL 场景：备份用的是 sqlite3 backup API）；
4. 目标目录非空且未 --force → 拒绝恢复；
5. 库 schema 版本与程序期望不一致 → 备份照样成功但标注 ok=false 且退出码 2；
6. **凭据轮换不丢业务配置**：只改 `.env`（App Secret）后重新备份，
   业务文件（config/ 下除 .env 外）哈希必须逐个不变；
7. 部署打包保护：受保护路径（config/、firmware/、backups/、.env）绝不进包，
   且校验函数能抓出违规；真实仓库的包与单元文件通过自检。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from app.portal import migrations  # 程序当前的库版本与本地缓存库表结构

CUR = str(migrations.LATEST)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def _run_cli(args, **kw):
    """跑项目脚本（与套件入口 run() 区分命名，避免遮蔽）。"""
    return subprocess.run([PY] + args, capture_output=True, text=True, cwd=REPO, **kw)


def make_data_dir(root, schema_version=CUR, with_wal=True):
    """造一个贴近生产的数据目录：config/（含 auth/）、firmware/、.env、快照库。"""
    cfg = os.path.join(root, "config")
    os.makedirs(os.path.join(cfg, "auth"), exist_ok=True)
    os.makedirs(os.path.join(root, "firmware"), exist_ok=True)
    with open(os.path.join(cfg, "feishu.json"), "w", encoding="utf-8") as f:
        json.dump({"app_id": "cli_x", "app_token": "bas_x",
                   "app_secret_ref": "env:FEISHU_APP_SECRET",
                   "tables": {"projects": {"table_id": "tbl1"}}}, f)
    with open(os.path.join(cfg, "devices.json"), "w", encoding="utf-8") as f:
        json.dump({"devices": {"legacy-default": {"id": "legacy-default"}}}, f)
    with open(os.path.join(cfg, "presets.json"), "w", encoding="utf-8") as f:
        json.dump({"presets": {"默认": []}}, f)
    with open(os.path.join(cfg, "auth", "sessions.json"), "w", encoding="utf-8") as f:
        json.dump({"sessions": {"deadbeef": {"user": "feishu:x", "role": "member"}}}, f)
    with open(os.path.join(cfg, "auth", "emergency_users.json"), "w", encoding="utf-8") as f:
        json.dump({"users": {"ops": {"salt": "aa", "hash": "bb"}}}, f)
    with open(os.path.join(root, "firmware", "mc02.elf"), "wb") as f:
        f.write(b"\x7fELF" + b"firmware-bytes" * 10)
    with open(os.path.join(root, ".env"), "w", encoding="utf-8") as f:
        f.write("FEISHU_APP_ID=cli_x\nFEISHU_APP_SECRET=secret-v1\n")
    # 快照库：WAL 模式 + 未 checkpoint 的事务，用来验证"直接拷文件会丢数据"
    db = os.path.join(cfg, "portal_snapshot.db")
    c = sqlite3.connect(db)
    if with_wal:
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE snapshot(version INTEGER, table_name TEXT, record_id TEXT,"
              " data TEXT, PRIMARY KEY(version, table_name, record_id))")
    c.execute("CREATE TABLE quality(version INTEGER, table_name TEXT, record_id TEXT,"
              " issue TEXT)")
    c.executemany("INSERT INTO meta(key, value) VALUES(?, ?)",
                  [("schema_version", schema_version), ("current_version", "42"),
                   ("counts", '{"projects": 2, "tasks": 3}'),
                   ("last_success_at", "1789034085.24"), ("last_error", "")])
    c.execute("INSERT INTO snapshot(version, table_name, record_id, data)"
              " VALUES(42, 'projects', 'recA', '{\"id\":\"recA\"}')")
    if schema_version == CUR:
        # 版本 2 起：本地缓存库表 + 一条还没推到飞书的网页改动
        migrations.create_v2_objects(c)
        c.execute("INSERT INTO outbox(table_name, record_id, op, client_token, created_at,"
                  " updated_at) VALUES('tasks', 'recA', 'update', 'tok-1', 1, 1)")
    c.commit()
    c.close()
    return root


def file_hashes(root, skip=()):
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            p = os.path.join(base, f)
            rel = os.path.relpath(p, root).replace("\\", "/")
            if rel in skip or rel.endswith(("-wal", "-shm")):
                continue
            import hashlib
            out[rel] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def run():
    print("== 阶段 7 备份/恢复隔离演练与部署保护 ==")
    root = tempfile.mkdtemp(prefix="portal-drill-")
    data = make_data_dir(os.path.join(root, "app"))
    before = file_hashes(data)

    print("-- 备份 --")
    out = os.path.join(root, "bk1")
    r = _run_cli(["scripts/portal_backup.py", "--data-dir", data, "--out", out,
             "--label", "drill-1"])
    check("备份脚本退出码 0", r.returncode == 0, r.stderr.strip()[:120])
    man = json.load(open(os.path.join(out, "manifest.json"), encoding="utf-8"))
    check("manifest 覆盖 config/firmware/.env（≥7 个文件）",
          len(man["files"]) >= 7, str(len(man["files"])))
    check("manifest 记录一致快照副本（版本 42、counts 齐）",
          os.path.isfile(os.path.join(out, "portal_snapshot.db"))
          and man["snapshot"]["current_version"] == "42"
          and man["snapshot"]["integrity"] == "ok", str(man["snapshot"])[:120])
    check("manifest 标注 schema 一致（expected=%s/ok=true）" % CUR,
          man["schema"]["ok"] is True and str(man["schema"]["expected"]) == CUR,
          str(man["schema"]))
    check("manifest 记录未推到飞书的改动条数（本地缓存库）",
          man["snapshot"].get("outbox_open") == 1, str(man["snapshot"].get("outbox_open")))
    # WAL 安全：备份副本必须含已提交但未 checkpoint 的数据
    cb = sqlite3.connect(os.path.join(out, "portal_snapshot.db"))
    n = cb.execute("SELECT COUNT(*) FROM snapshot WHERE version=42").fetchone()[0]
    cb.close()
    check("一致性副本含最新已提交行（WAL 未被截断）", n == 1, str(n))

    print("-- 篡改必须被发现 --")
    victim = os.path.join(out, "files", "config", "devices.json")
    with open(victim, "w", encoding="utf-8") as f:
        f.write('{"devices": {"tampered": true}}')
    tgt_bad = os.path.join(root, "restore-bad")
    r = _run_cli(["scripts/portal_restore.py", "--bundle", out, "--target", tgt_bad])
    check("篡改备份包 → 恢复中止（非零退出）", r.returncode != 0, str(r.returncode))
    check("中止原因写明 sha256 不一致",
          "sha256" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-120:])
    check("校验失败时未写入目标目录",
          not os.path.exists(tgt_bad) or not os.listdir(tgt_bad),
          str(os.listdir(tgt_bad) if os.path.exists(tgt_bad) else "不存在"))

    print("-- 干净恢复（含 tar.gz 形态） --")
    out2 = os.path.join(root, "bk2")
    r = _run_cli(["scripts/portal_backup.py", "--data-dir", data, "--out", out2, "--tar"])
    check("备份 + 打包退出码 0", r.returncode == 0, r.stderr.strip()[:120])
    bundle = out2 + ".tar.gz"
    check("bundle.tar.gz 已生成", os.path.isfile(bundle))
    tgt = os.path.join(root, "restore-ok")
    r = _run_cli(["scripts/portal_restore.py", "--bundle", bundle, "--target", tgt])
    check("从 tar.gz 恢复到空目录成功", r.returncode == 0, (r.stdout + r.stderr)[-160:])
    after = file_hashes(tgt)
    # 快照库单独比语义：恢复用的是 sqlite3 backup 一致性副本（WAL 下与线上
    # 主文件字节本来就不同），"字节一致"只适用于普通文件
    same = all(after.get(k) == v for k, v in before.items()
               if k != "config/portal_snapshot.db")
    check("恢复后普通文件逐文件字节一致（含 .env 与 auth/）", same,
          str([k for k, v in before.items() if k != "config/portal_snapshot.db"
               and after.get(k) != v])[:120])
    c = sqlite3.connect(os.path.join(tgt, "config", "portal_snapshot.db"))
    integ = c.execute("PRAGMA integrity_check").fetchone()[0]
    kv = dict(c.execute("SELECT key, value FROM meta"))
    rows = c.execute("SELECT COUNT(*) FROM snapshot WHERE version=42").fetchone()[0]
    c.close()
    check("恢复后库完整且快照可用（integrity ok / version=42 / 1 行）",
          integ == "ok" and kv.get("current_version") == "42" and rows == 1,
          f"{integ} {kv.get('current_version')} {rows}")
    check("恢复输出注明 schema 与实际版本", "schema=" in r.stdout, r.stdout[-150:])
    check("恢复输出提示未推到飞书的改动条数", "未推到飞书的改动 1 条" in r.stdout,
          r.stdout[-150:])

    print("-- 非空目标必须拒绝 --")
    r = _run_cli(["scripts/portal_restore.py", "--bundle", out2, "--target", tgt])
    check("非空目标未 --force → 拒绝恢复", r.returncode != 0
          and "非空" in (r.stdout + r.stderr), str(r.returncode))
    r = _run_cli(["scripts/portal_restore.py", "--bundle", out2, "--target", tgt, "--force"])
    check("--force 后允许恢复", r.returncode == 0, (r.stdout + r.stderr)[-120:])

    print("-- 未知 schema 版本：照样备份但标注不一致 --")
    data_bad = make_data_dir(os.path.join(root, "app-bad"), schema_version="99")
    out3 = os.path.join(root, "bk3")
    r = _run_cli(["scripts/portal_backup.py", "--data-dir", data_bad, "--out", out3])
    man3 = json.load(open(os.path.join(out3, "manifest.json"), encoding="utf-8"))
    check("未知版本备份退出码 2（警告而非失败）", r.returncode == 2, str(r.returncode))
    check("manifest 标注 ok=false 且保留库内版本 99",
          man3["schema"]["ok"] is False and str(man3["snapshot"]["schema_version"]) == "99",
          str(man3["schema"]))
    check("警告文案指向迁移/换版本", "迁移" in r.stdout, r.stdout[-160:])
    r = _run_cli(["scripts/portal_restore.py", "--bundle", out3,
             "--target", os.path.join(root, "restore-bad-schema")])
    check("恢复到未知版本库时提示先迁移（退出码 2）",
          r.returncode == 2 and "迁移" in r.stdout, (r.stdout + r.stderr)[-160:])

    print("-- 凭据轮换不丢业务配置 --")
    with open(os.path.join(data, ".env"), "w", encoding="utf-8") as f:
        f.write("FEISHU_APP_ID=cli_x\nFEISHU_APP_SECRET=secret-v2-rotated\n")
    out4 = os.path.join(root, "bk4")
    r = _run_cli(["scripts/portal_backup.py", "--data-dir", data, "--out", out4])
    check("轮换后再次备份成功", r.returncode == 0, r.stderr.strip()[:120])
    biz_before = {k: v for k, v in before.items() if not k.startswith(".env")}
    biz_after = {k: v for k, v in file_hashes(data).items() if not k.startswith(".env")}
    check("轮换 App Secret 后业务文件哈希逐个不变（轮换只动 .env）",
          biz_before == biz_after,
          str([k for k in biz_before if biz_before[k] != biz_after.get(k)])[:120])
    env_b = open(os.path.join(out, "files", ".env"), encoding="utf-8").read()
    env_a = open(os.path.join(out4, "files", ".env"), encoding="utf-8").read()
    check("两次备份的 .env 反映轮换前后差异（备份确实抓到新凭据）",
          "secret-v1" in env_b and "secret-v2-rotated" in env_a)

    print("-- 部署打包保护 --")
    sys.path.insert(0, REPO)
    import deploy as dp
    tmp_repo = os.path.join(root, "repo")
    os.makedirs(os.path.join(tmp_repo, "app"), exist_ok=True)
    os.makedirs(os.path.join(tmp_repo, "config"), exist_ok=True)
    os.makedirs(os.path.join(tmp_repo, "firmware"), exist_ok=True)
    os.makedirs(os.path.join(tmp_repo, "backups"), exist_ok=True)
    os.makedirs(os.path.join(tmp_repo, "tests", "e2e", "browser-libs"), exist_ok=True)
    open(os.path.join(tmp_repo, "app", "main.py"), "w").write("# code\n")
    open(os.path.join(tmp_repo, "config", "feishu.json"), "w").write("{}\n")
    open(os.path.join(tmp_repo, "firmware", "mc02.elf"), "wb").write(b"\x7fELF")
    open(os.path.join(tmp_repo, "backups", "x.log"), "w").write("log\n")
    open(os.path.join(tmp_repo, ".env"), "w").write("FEISHU_APP_SECRET=leak\n")
    open(os.path.join(tmp_repo, "tests", "e2e", "browser-libs", "libx.so"), "wb").write(b"\x00")
    pkg, names = dp.make_tarball(base=tmp_repo,
                                 out=os.path.join(root, "guard.tar.gz"))
    check("打包排除持久数据与机密（config/firmware/backups/.env/browser-libs）",
          dp.assert_no_protected(names) == []
          and "app/main.py" in names,
          str([n for n in names if "config" in n or "firmware" in n or n == ".env"]))
    with tarfile.open(pkg) as t:
        inside = t.getnames()
    check("包内实际不含 config/firmware/backups/.env",
          not any(n.startswith(("config/", "firmware/", "backups/")) or n == ".env"
                  for n in inside), str(inside[:8]))
    check("校验函数能抓出违规条目（防止 guard 失效）",
          dp.assert_no_protected(["config/feishu.json", "app/main.py"])
          == ["config/feishu.json"])
    unit = open(os.path.join(REPO, "deploy", "portal.service"), encoding="utf-8").read()
    # 端口/TLS 由 .env 注入（EnvironmentFile 优先于 Environment=，已实测）：
    # 单元默认 WEB_PORT=80，portal_tls.py 只改 .env 即可切 443 + TLS
    check("门户单元：单 worker + 端口走 ${WEB_PORT}（默认 80）",
          dp.assert_single_worker(unit) is None
          and "--port ${WEB_PORT}" in unit
          and "Environment=WEB_PORT=80" in unit
          and "$TLS_ARGS" in unit)
    check("注入 --workers 会被抓出（校验可证伪）",
          dp.assert_single_worker(
              unit.replace("--host 0.0.0.0", "--workers 4 --host 0.0.0.0")) is not None)
    real_pkg, real_names = dp.make_tarball(out=os.path.join(root, "real.tar.gz"))
    # 体积上限 12MB（2026-10-21 由 2MB 放宽）：自托管中文字体 static/ui/fonts 压缩后约 9MB，
    # 是门户必需的；上限留约 2MB 余量，误把设计预览、浏览器库等打进包仍会被拦下
    check("真实仓库打包零受保护路径，且体积 <12MB",
          dp.assert_no_protected(real_names) == []
          and os.path.getsize(real_pkg) < 12 * 1024 * 1024,
          f"{len(real_names)} 文件 {os.path.getsize(real_pkg)/1e6:.2f} MB")

    shutil.rmtree(root, ignore_errors=True)
    print(f"\n演练: {len(PASS)} 通过, {len(FAIL)} 失败")
    return len(PASS), len(FAIL)


if __name__ == "__main__":
    _, nfail = run()
    raise SystemExit(1 if nfail else 0)
