# -*- coding: utf-8 -*-
"""部署脚本：部署**门户**到服务器 /opt/portal，并硬性保护生产数据。

说明：本文件是 2026-10-25 源码抢救时按文档与服务器现场重建的版本
（原 2026-10-21 版本随开发机丢失，部署包本身不含 deploy.py）。重建依据：
`接续开发说明.md` §4、`docs/portal-operations.md` §B/§8、`docs/frontend-vue.md`、
`tests/e2e/test_backup_restore.py` 对本模块的调用，以及服务器上
`/opt/portal-backups/<时间戳>/` 里留下的产物格式（code.tar.gz、premigrate/）。

行为要点：

1. **打包排除持久数据与机密**：`config/`、`firmware/`、`backups/`、`参考/`、
   `tests/e2e/browser-libs/`、`.env`、`*.db`、`*.log` 一律不进包；另外排除
   `node_modules/`、设计预览 `design-v*-preview/` 与工具产物。打包后**硬校验**，
   出现受保护路径即中止。
2. **前端产物校验**：服务器上没有 node，`static/app/` 的三个入口
   （portal/debug/auth）与它们引用的 `assets/` 必须都在，缺一个就中止。
3. **升级前备份**：`/opt/portal/{config,firmware}` 与旧代码（`code.tar.gz`）
   存到 `/opt/portal-backups/<时间戳>/`。
4. **快照库迁移**：重启前执行 `scripts/portal_migrate.py`，迁移前的库备份到
   `/opt/portal-backups/<时间戳>/premigrate/`；迁移失败 → 退回旧代码并中止，不重启。
5. **验证失败自动退回**：探针不过 → 退回旧代码、旧单元与迁移前的快照库并重启。
6. **单 worker 校验**：单元文件 `ExecStart` 出现 `--workers` 即中止。
7. 服务器地址 / SSH 端口 / 用户 / 口令默认从 `config/deploy_server.json` 读
   （`config/` 不进部署包、不入库），命令行参数与环境变量可覆盖。

用法（在项目根目录）：
    python deploy.py --dry-run                     # 只打包与自检，不连服务器
    python deploy.py                               # 部署（凭据读 config/deploy_server.json）
    python deploy.py --list-backups                # 列出服务器上可退回的时间戳
    python deploy.py --restore 20261021-051017     # 退回到某次升级之前
    python deploy.py --host <地址> --ssh-port <端口> --user <用户> --password <口令>

依赖：pip install paramiko（仅连服务器时需要；--dry-run 不需要）
"""
import argparse
import json
import os
import re
import shlex
import sys
import tarfile
import tempfile
import time

# ---------- 常量 ----------

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_CONFIG = os.path.join(HERE, "config", "deploy_server.json")

DEFAULT_USER = "foray"
DEFAULT_SSH_PORT = 22

PORTAL_DIR = "/opt/portal"
PORTAL_UNIT = "portal.service"
PORTAL_DB = PORTAL_DIR + "/config/portal_snapshot.db"
LEGACY_DIR = "/opt/stm32-webserver"
LEGACY_UNIT = "webserver.service"
BACKUP_ROOT = "/opt/portal-backups"
REMOTE_PKG = "/tmp/portal-deploy.tar.gz"
REMOTE_EXTRACT = "/tmp/portal-extract"

# 打包排除目录（相对仓库根的完整路径，或任意层级的目录名）
EXCLUDE_DIRS = {".venv", "venv", ".git", "__pycache__", ".mypy_cache", ".pytest_cache",
                "node_modules", "backups", "参考", "config", "firmware",
                "tests/e2e/browser-libs",
                ".zcode", ".claude", ".idea", ".vscode"}
# 设计预览是独立静态页，不随门户部署（体积大，且不是线上代码）
EXCLUDE_DIR_PATTERNS = (re.compile(r"^design-v\d+-preview$"),)
# 受保护路径前缀：出现在包里就中止（持久数据/机密，绝不随代码覆盖）
PROTECTED_PREFIXES = ("config/", "firmware/", "backups/", "参考/",
                      "tests/e2e/browser-libs/")
# 临时助手脚本可能带口令（.gitignore 同样忽略），一律不进包
SECRET_FILE_PATTERNS = (re.compile(r"^_ssh.*\.py$"), re.compile(r"^_deploy.*\.py$"))

# 前端构建产物（vite build → static/app/）
APP_ENTRIES = ("portal", "debug", "auth")
ASSET_REF = re.compile(r"/app/(assets/[^\"'?#\s>]+)")

# 升级前旧代码备份时不打进 code.tar.gz 的内容（数据另有备份，venv 可重建）
CODE_BACKUP_EXCLUDES = ("./.venv", "./config", "./firmware", "./.env", "*/__pycache__")


def _rel(path, base):
    return os.path.relpath(path, base).replace("\\", "/")


def _skip_dir(name, rel):
    if name in EXCLUDE_DIRS or rel in EXCLUDE_DIRS:
        return True
    return any(p.match(name) for p in EXCLUDE_DIR_PATTERNS)


def _skip_file(name, rel):
    if rel == "deploy.py" or rel.endswith((".tar.gz", ".tgz", ".pyc", ".pyo")):
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if name.endswith((".db", ".db-wal", ".db-shm", ".log")):
        return True
    if ":Zone.Identifier" in name or name in (".DS_Store", "Thumbs.db"):
        return True
    return any(p.match(name) for p in SECRET_FILE_PATTERNS)


def make_tarball(base=None, out=None):
    """打包源码（不含持久数据），返回 (路径, 条目列表)。"""
    base = base or HERE
    tf = out or os.path.join(tempfile.gettempdir(), "portal-deploy.tar.gz")
    names = []
    with tarfile.open(tf, "w:gz") as t:
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs
                             if not _skip_dir(d, _rel(os.path.join(root, d), base)))
            for f in sorted(files):
                rel = _rel(os.path.join(root, f), base)
                if _skip_file(f, rel):
                    continue
                t.add(os.path.join(root, f), arcname=rel)
                names.append(rel)
    return tf, names


def assert_no_protected(names):
    """硬校验：包里不得出现受保护路径。返回违规列表（空 = 通过）。"""
    return [n for n in names
            if n == ".env" or any(n.startswith(p) for p in PROTECTED_PREFIXES)]


def assert_single_worker(unit_text):
    """单 worker 校验：**只看 ExecStart 行**（注释里提到 --workers 不算）。

    返回违规说明或 None。
    """
    starts = [ln.strip() for ln in unit_text.splitlines()
              if ln.strip().startswith("ExecStart=")]
    if not starts:
        return "单元文件缺少 ExecStart"
    if any("uvicorn app.main:app" not in s for s in starts):
        return "ExecStart 未启动 app.main:app"
    if any("--workers" in s for s in starts):
        return "ExecStart 含 --workers（门户必须单 worker）"
    return None


def check_frontend(base=None, names=None):
    """前端产物校验：三个入口都在、入口引用的 assets 都在（且都进了包）。

    返回问题列表（空 = 通过）。服务器上不构建，缺产物就等于线上白屏。
    """
    base = base or HERE
    app_dir = os.path.join(base, "static", "app")
    problems = []
    packed = set(names) if names is not None else None
    for entry in APP_ENTRIES:
        html = os.path.join(app_dir, entry + ".html")
        if not os.path.isfile(html):
            problems.append("缺少构建产物 static/app/%s.html（先在 web/ 下 npm run build）" % entry)
            continue
        refs = ASSET_REF.findall(open(html, encoding="utf-8").read())
        if not refs:
            problems.append("static/app/%s.html 未引用任何 /app/assets/ 资源" % entry)
        for ref in refs:
            rel = "static/app/" + ref
            if not os.path.isfile(os.path.join(base, rel)):
                problems.append("%s.html 引用的 %s 不存在" % (entry, rel))
            elif packed is not None and rel not in packed:
                problems.append("%s 未进部署包" % rel)
    return problems


def frontend_stale_hint(base=None):
    """提示（不中止）：web/ 源码比构建产物新，可能忘了重新构建。"""
    base = base or HERE
    html = os.path.join(base, "static", "app", "portal.html")
    src = os.path.join(base, "web")
    if not (os.path.isfile(html) and os.path.isdir(src)):
        return None
    built = os.path.getmtime(html)
    newer = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        for f in files:
            p = os.path.join(root, f)
            if os.path.getmtime(p) > built + 1:
                newer.append(_rel(p, base))
    if not newer:
        return None
    return "web/ 下有 %d 个文件比 static/app/ 新（如 %s），确认已执行 npm run build" % (
        len(newer), newer[0])


def self_check(base=None):
    """打包 + 全部本地校验。返回 (包路径, 条目列表, 问题列表)。"""
    base = base or HERE
    pkg, names = make_tarball(base=base)
    problems = []
    bad = assert_no_protected(names)
    if bad:
        problems.append("包内出现受保护路径：%s" % bad[:5])
    unit_path = os.path.join(base, "deploy", PORTAL_UNIT)
    if not os.path.isfile(unit_path):
        problems.append("缺少 deploy/%s" % PORTAL_UNIT)
    else:
        problem = assert_single_worker(open(unit_path, encoding="utf-8").read())
        if problem:
            problems.append(problem)
    problems.extend(check_frontend(base, names))
    return pkg, names, problems


# ---------- 服务器配置 ----------

def load_server_config(path=SERVER_CONFIG):
    """读 config/deploy_server.json（不存在返回空字典）。

    格式：{"host": "...", "ssh_port": 22, "user": "...", "password": "..."}，
    可选 "sudo_password"（与登录口令不同时才需要）。
    """
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit("%s 格式不对：应为 JSON 对象" % path)
    return data


def resolve_target(args, cfg):
    """合并命令行 > 环境变量 > config/deploy_server.json > 默认值。"""
    host = args.host or os.environ.get("DEPLOY_HOST") or cfg.get("host")
    port = (args.ssh_port or os.environ.get("DEPLOY_SSH_PORT")
            or cfg.get("ssh_port") or cfg.get("port") or DEFAULT_SSH_PORT)
    user = args.user or os.environ.get("DEPLOY_USER") or cfg.get("user") or DEFAULT_USER
    pwd = args.password or os.environ.get("DEPLOY_PWD") or cfg.get("password")
    sudo_pwd = os.environ.get("DEPLOY_SUDO_PWD") or cfg.get("sudo_password") or pwd
    if not host:
        sys.exit("需要目标服务器：--host <地址>、环境变量 DEPLOY_HOST 或 config/deploy_server.json")
    if not pwd:
        sys.exit("需要口令：--password <口令>、环境变量 DEPLOY_PWD 或 config/deploy_server.json")
    return host, int(port), user, pwd, sudo_pwd


# ---------- 远程执行 ----------

def connect(host, user, pwd, ssh_port=22):
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, port=ssh_port, username=user, password=pwd, timeout=15)
    return cli


class StepError(RuntimeError):
    """关键步骤失败：立即中止（不做"失败了还继续"的部署）。"""


def make_runner(cli, pwd):
    def run(cmd, sudo=False, timeout=900, show=True, feed_password=None):
        """执行远程命令。sudo 密码经 stdin 投递并立刻 EOF（否则 sudo 会挂住等输入）。

        feed_password：命令里已自带 sudo 前缀（如预检）时单独指定是否投递密码。
        """
        # 复合命令必须整体交给 shell：`sudo -S -p '' if ...` 会报语法错误，
        # `sudo a && b` 则只有 a 以 root 执行（静默半权限），两者都曾造成真实事故
        real = ("sudo -S -p '' bash -c " + shlex.quote(cmd)) if sudo else cmd
        feed = sudo if feed_password is None else feed_password
        stdin, stdout, stderr = cli.exec_command(real, timeout=timeout)
        if feed:
            stdin.write(pwd + "\n")
            stdin.flush()
            try:
                stdin.channel.shutdown_write()
            except Exception:
                pass
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        if show:
            # 打印时不回显完整的 bash -c 包装，免得日志又长又难读
            print("$ " + (("sudo " + cmd) if sudo else cmd)[:300])
            if out.strip():
                print(out.rstrip()[:2000])
            if err.strip():
                print("[stderr]", err.rstrip()[:800])
            print("[exit=%d]" % code)
        return code, out, err

    def must(cmd, what, sudo=False, timeout=900):
        """关键步骤：非零即抛 StepError（附提示），避免半成品状态。"""
        code, out, err = run(cmd, sudo=sudo, timeout=timeout)
        if code != 0:
            raise StepError("%s 失败（exit=%d）：%s" % (what, code,
                                                      (err or out).strip()[:200]))
        return out

    run.must = must
    return run


def preflight_sudo(run):
    """预检 sudo：不可用就早停，别等装到一半才发现。"""
    code, out, err = run("sudo -S -p '' true && echo SUDO_OK", show=False,
                         feed_password=True)
    if code != 0 or "SUDO_OK" not in out:
        raise StepError("sudo 不可用（%s）——需要能 sudo 的账号才能安装 systemd 单元"
                        % (err.strip()[:120] or "未知原因"))


def _probe_script(port, asset=None):
    """验证探针：登录页、旧前端资源、匿名 API、调试残留文件、新前端资源。"""
    checks = [("/login", 200), ("/portal/portal.css", 200),
              ("/api/portal/overview", 401), ("/portal/fixtures.js", 404)]
    if asset:
        checks.append(("/app/" + asset, 200))
    return ("%s/.venv/bin/python - <<'EOP'\n"
            "import urllib.request as u, urllib.error as e\n"
            "for p, exp in %r:\n"
            "    try:\n"
            "        c = u.urlopen('http://127.0.0.1:%d' + p, timeout=8).status\n"
            "    except e.HTTPError as x:\n"
            "        c = x.code\n"
            "    except Exception as x:\n"
            "        c = 'ERR ' + type(x).__name__\n"
            "    print(('OK  ' if c == exp else 'BAD ') + p + ' -> ' + str(c)\n"
            "          + ' (期望 ' + str(exp) + ')')\n"
            "EOP") % (PORTAL_DIR, checks, port)


def _first_asset(base=None):
    """取 portal.html 引用的入口脚本，作为线上新前端是否就位的探针。"""
    html = os.path.join(base or HERE, "static", "app", "portal.html")
    try:
        refs = ASSET_REF.findall(open(html, encoding="utf-8").read())
    except OSError:
        return None
    js = [r for r in refs if r.endswith(".js")]
    return (js or refs or [None])[0]


def verify(run, port, asset=None, wait=4):
    time.sleep(wait)
    run("systemctl is-active %s" % PORTAL_UNIT)
    code, out, _ = run(_probe_script(port, asset))
    return code == 0 and "BAD" not in out and "OK" in out


def outbox_open(run):
    """快照库里还没推到飞书的改动条数（无库/无表返回 0，读失败返回 None）。"""
    script = ("%s/.venv/bin/python - <<'EOP'\n"
              "import os, sqlite3\n"
              "db = %r\n"
              "if not os.path.isfile(db):\n"
              "    print('OUTBOX=0'); raise SystemExit\n"
              "c = sqlite3.connect('file:%%s?mode=ro' %% db, uri=True)\n"
              "names = {r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")}\n"
              "n = 0\n"
              "if 'outbox' in names:\n"
              "    n = c.execute(\"SELECT COUNT(*) FROM outbox WHERE state IN "
              "('pending', 'pushing', 'failed')\").fetchone()[0]\n"
              "print('OUTBOX=%%d' %% n)\n"
              "EOP") % (PORTAL_DIR, PORTAL_DB)
    code, out, _ = run(script, sudo=True, show=False)
    m = re.search(r"OUTBOX=(\d+)", out)
    return int(m.group(1)) if (code == 0 and m) else None


# ---------- 升级前备份 / 退回 ----------

def backup_before_upgrade(run, ts):
    """数据（config/firmware）与旧代码（code.tar.gz）备份到 BACKUP_ROOT/<ts>/。

    返回 True 表示有旧代码可退回；首次安装返回 False。
    """
    dst = "%s/%s" % (BACKUP_ROOT, ts)
    run.must("mkdir -p %s %s" % (PORTAL_DIR, dst), "创建备份目录", sudo=True)
    # 升级前数据快照属于安全前置：失败必须中止（历史上曾因 sudo 复合命令语法错
    # 静默跳过备份）
    out = run.must("if [ -d %s/config ]; then cp -a %s/config %s/config && "
                   "(cp -a %s/firmware %s/firmware 2>/dev/null || true); "
                   "echo BACKUP_DONE; else echo NO_DATA; fi"
                   % (PORTAL_DIR, PORTAL_DIR, dst, PORTAL_DIR, dst),
                   "升级前数据备份", sudo=True)
    print("  %s" % ("已备份既有数据 → %s" % dst if "BACKUP_DONE" in out
                    else "首次部署：无既有数据可备份"))
    excludes = " ".join("--exclude=%s" % shlex.quote(e) for e in CODE_BACKUP_EXCLUDES)
    out = run.must("if [ -f %s/app/main.py ]; then tar -czf %s/code.tar.gz %s -C %s . && "
                   "echo CODE_DONE; else echo NO_CODE; fi; ls -la %s"
                   % (PORTAL_DIR, dst, excludes, PORTAL_DIR, dst),
                   "旧代码备份", sudo=True)
    has_code = "CODE_DONE" in out
    print("  %s" % ("旧代码 → %s/code.tar.gz" % dst if has_code else "首次部署：无旧代码"))
    return has_code


def restore_previous(run, ts, port, force=False, restore_db=True):
    """退回到 <ts> 那次升级之前：旧代码、旧单元、迁移前的快照库（若有），然后重启。

    部署验证失败时自动调用；手工退回用 --restore <ts>。
    数据目录 config/ 与 firmware/ 不在这里回滚（只换代码），需要时用
    scripts/portal_restore.py 从同一备份目录恢复。
    """
    src = "%s/%s" % (BACKUP_ROOT, ts)
    code, out, _ = run("test -f %s/code.tar.gz && echo HAVE_CODE; "
                       "test -f %s/premigrate/portal_snapshot.v1.db && echo HAVE_DB; true"
                       % (src, src), sudo=True, show=False)
    if "HAVE_CODE" not in out:
        raise StepError("%s/code.tar.gz 不存在，无法退回（用 --list-backups 查看可用时间戳）" % src)
    have_db = restore_db and "HAVE_DB" in out
    if have_db:
        pending = outbox_open(run)
        if pending is None:
            print("  ⚠ 读不出本地队列状态")
        if (pending is None or pending > 0) and not force:
            raise StepError("本地队列里还有 %s 条改动没推到飞书，退回旧库会丢掉它们；"
                            "先在「数据质量」页或 scripts/portal_queue.py 处理，"
                            "确认可以丢弃再加 --force" % pending)

    print("  停止 %s ..." % PORTAL_UNIT)
    run("systemctl stop %s" % PORTAL_UNIT, sudo=True)
    run.must("tar -xzf %s/code.tar.gz -C %s && find %s -name __pycache__ -type d "
             "-exec rm -rf {} + 2>/dev/null; true" % (src, PORTAL_DIR, PORTAL_DIR),
             "解开旧代码", sudo=True)
    run.must("cp %s/deploy/%s /etc/systemd/system/ && systemctl daemon-reload"
             % (PORTAL_DIR, PORTAL_UNIT), "恢复旧单元", sudo=True)
    if have_db:
        keep = "%s/%s-pre-restore" % (BACKUP_ROOT, time.strftime("%Y%m%d-%H%M%S"))
        run.must("mkdir -p %s && cp -a %s %s/ && "
                 "(cp -a %s-wal %s-shm %s/ 2>/dev/null || true) && "
                 "cp -a %s/premigrate/portal_snapshot.v1.db %s && rm -f %s-wal %s-shm && "
                 "chown --reference=%s/config %s"
                 % (keep, PORTAL_DB, keep, PORTAL_DB, PORTAL_DB, keep,
                    src, PORTAL_DB, PORTAL_DB, PORTAL_DB, PORTAL_DIR, PORTAL_DB),
                 "恢复迁移前的快照库", sudo=True)
        print("  快照库已换回迁移前版本；换下来的库留在 %s" % keep)
    run.must("systemctl start %s" % PORTAL_UNIT, "启动门户", sudo=True)
    ok = verify(run, port, asset=None)
    print("  退回%s：代码与单元来自 %s" % ("完成" if ok else "后验证仍未通过（需人工排查）", src))
    return ok


def list_backups(run):
    run("for d in %s/*/; do d=${d%%/}; "
        "if [ -f $d/code.tar.gz ]; then printf '%%s  code.tar.gz%%s\\n' \"$(basename $d)\" "
        "\"$([ -d $d/premigrate ] && echo ' + premigrate' || true)\"; fi; done" % BACKUP_ROOT,
        sudo=True)
    print("可退回：python deploy.py --restore <时间戳>（退回到该次升级之前的代码）")


# ---------- 部署 ----------

def portal_deploy(args, run, cli):
    ts = time.strftime("%Y%m%d-%H%M%S")
    print("[2/8] 打包并自检 ...")
    pkg, names, problems = self_check()
    if problems:
        for p in problems:
            print("  ✗ " + p)
        return 1
    hint = frontend_stale_hint()
    if hint:
        print("  ⚠ " + hint)
    print("  ✓ %s（%d 个文件，%.1f MB，零受保护路径，前端产物齐全，单 worker）"
          % (pkg, len(names), os.path.getsize(pkg) / 1024 / 1024))

    print("[3/8] 上传 ...")
    sftp = cli.open_sftp()
    sftp.put(pkg, REMOTE_PKG)
    sftp.close()

    print("[4/8] 检查旧服务与端口占用 ...")
    _, out, _ = run("systemctl is-active %s" % LEGACY_UNIT, show=False)
    legacy_active = (out.strip() == "active")
    if legacy_active and not args.cutover:
        print("  ✗ %s 正在占用 %d 端口；确认切换请加 --cutover" % (LEGACY_UNIT, args.port))
        return 1
    print("  旧服务 %s：%s" % (LEGACY_UNIT, "active（将执行切换）" if legacy_active
                               else "未运行"))

    print("[5/8] 升级前备份 → %s/%s ..." % (BACKUP_ROOT, ts))
    has_old = backup_before_upgrade(run, ts)

    print("[6/8] 安装代码到 %s ..." % PORTAL_DIR)
    run.must("rm -rf %s && mkdir -p %s && tar -xzf %s -C %s"
             % (REMOTE_EXTRACT, REMOTE_EXTRACT, REMOTE_PKG, REMOTE_EXTRACT), "解包上传包")
    # 复制用 sudo：目标目录可能由 root 创建（首装），权限不该成为静默失败点
    run.must("cp -a %s/. %s/" % (REMOTE_EXTRACT, PORTAL_DIR), "复制代码", sudo=True)
    run.must("chown -R %s:%s %s && find %s -name __pycache__ -type d "
             "-exec rm -rf {} + 2>/dev/null; true"
             % (args.user, args.user, PORTAL_DIR, PORTAL_DIR), "交接属主", sudo=True)
    run.must("test -f %s/deploy/%s" % (PORTAL_DIR, PORTAL_UNIT),
             "代码树缺少 deploy/%s" % PORTAL_UNIT, sudo=True)

    if args.seed_data:
        print("  迁移既有数据（缺什么补什么，不覆盖已存在项）...")
        run("mkdir -p %s/config" % PORTAL_DIR, sudo=True)
        for name in ("feishu.json", "devices.json", "presets.json",
                     "motor_presets.json"):
            run("[ -f %s/config/%s ] || cp -a %s/config/%s %s/config/ 2>/dev/null; true"
                % (PORTAL_DIR, name, args.seed_data, name, PORTAL_DIR), sudo=True)
        run("[ -d %s/config/auth ] || cp -a %s/config/auth %s/config/ 2>/dev/null; true"
            % (PORTAL_DIR, args.seed_data, PORTAL_DIR), sudo=True)
        run("[ -f %s/.env ] || cp -a %s/.env %s/.env 2>/dev/null; "
            "chmod 600 %s/.env 2>/dev/null; true"
            % (PORTAL_DIR, args.seed_data, PORTAL_DIR, PORTAL_DIR), sudo=True)
        run("mkdir -p %s/firmware && cp -an %s/firmware/. %s/firmware/ 2>/dev/null; true"
            % (PORTAL_DIR, LEGACY_DIR, PORTAL_DIR), sudo=True)

    def undo_code(reason):
        """迁移/安装阶段失败：换回旧代码与旧单元，旧进程不重启、照常运行。"""
        if not has_old:
            print("  首次部署，无旧代码可换回：%s" % reason)
            return
        print("  ↩ %s → 换回旧代码与旧单元（不重启，旧进程照常运行）" % reason)
        run("tar -xzf %s/%s/code.tar.gz -C %s && cp %s/deploy/%s /etc/systemd/system/ "
            "&& systemctl daemon-reload"
            % (BACKUP_ROOT, ts, PORTAL_DIR, PORTAL_DIR, PORTAL_UNIT), sudo=True)

    print("[7/8] 依赖、systemd 单元与快照库迁移 ...")
    try:
        # venv 必须"可用"（解释器在 + 依赖能 import），否则重建：
        # 半成品 venv（上次中断留下）不该让部署带病继续
        code, _, _ = run("%s/.venv/bin/python -c 'import fastapi, uvicorn'"
                         % PORTAL_DIR, show=False)
        if code != 0:
            run.must("rm -rf %s/.venv && python3 -m venv %s/.venv"
                     % (PORTAL_DIR, PORTAL_DIR), "创建 venv", sudo=True, timeout=300)
            run.must("%s/.venv/bin/pip install --quiet --upgrade pip" % PORTAL_DIR,
                     "升级 pip", sudo=True)
            run.must("%s/.venv/bin/pip install --quiet -r %s/requirements.txt"
                     % (PORTAL_DIR, PORTAL_DIR), "安装依赖", sudo=True, timeout=900)
        else:
            print("  venv 可用（运行时不需要联网）")
        run.must("%s/.venv/bin/python -c 'import fastapi, uvicorn'" % PORTAL_DIR,
                 "依赖自检（import fastapi/uvicorn）", sudo=True)
        run.must("cp %s/deploy/%s /etc/systemd/system/" % (PORTAL_DIR, PORTAL_UNIT),
                 "安装 systemd 单元", sudo=True)
        # 备份与健康巡检的单元/定时器（幂等；不存在就跳过不报错）。
        # portal.service.d/ 附加配置（写入模式开关）由运维在服务器上维护，这里不覆盖
        run.must("chmod +x %s/scripts/portal_backup_daily.sh 2>/dev/null; "
                 "for u in portal-backup.service portal-backup.timer "
                 "portal-health.service portal-health.timer; do "
                 "[ -f %s/deploy/$u ] && cp %s/deploy/$u /etc/systemd/system/; done; true"
                 % (PORTAL_DIR, PORTAL_DIR, PORTAL_DIR), "安装备份/巡检单元", sudo=True)
        run.must("if [ -f %s/deploy/journald.conf.d/portal.conf ]; then "
                 "mkdir -p /etc/systemd/journald.conf.d && "
                 "if ! cmp -s %s/deploy/journald.conf.d/portal.conf "
                 "/etc/systemd/journald.conf.d/portal.conf; then "
                 "cp %s/deploy/journald.conf.d/portal.conf /etc/systemd/journald.conf.d/ && "
                 "systemctl restart systemd-journald; fi; fi; true"
                 % (PORTAL_DIR, PORTAL_DIR, PORTAL_DIR), "安装 journald 上限", sudo=True)
        run.must("systemctl daemon-reload", "daemon-reload", sudo=True)
        # 迁移只做加法，可在旧进程运行时执行；迁移前的库备份到本次备份目录
        code, out, err = run("cd %s && .venv/bin/python scripts/portal_migrate.py --db %s "
                             "--backup-dir %s/%s/premigrate"
                             % (PORTAL_DIR, PORTAL_DB, BACKUP_ROOT, ts), sudo=True)
        if code != 0:
            raise StepError("快照库迁移未通过（exit=%d）：%s"
                            % (code, (out or err).strip()[-200:]))
    except StepError as e:
        undo_code(str(e))
        raise

    print("[8/8] 重启与验证 ...")
    if legacy_active:
        run.must("systemctl disable --now %s" % LEGACY_UNIT, "停用旧服务", sudo=True)
    run.must("systemctl enable %s" % PORTAL_UNIT, "启用门户", sudo=True)
    run.must("systemctl restart %s" % PORTAL_UNIT, "重启门户", sudo=True)
    # 定时器：备份（每日）与巡检（每 5 分钟）——只在单元存在时启用
    run("if [ -f /etc/systemd/system/portal-backup.timer ]; then "
        "systemctl enable --now portal-backup.timer portal-health.timer; fi; true",
        sudo=True)
    if not verify(run, args.port, _first_asset()):
        if legacy_active:
            print("\n✗ 门户验证未通过，自动回滚到旧调试台（不把 80 端口留在坏状态）")
            run("systemctl disable --now %s" % PORTAL_UNIT, sudo=True)
            run("systemctl enable --now %s" % LEGACY_UNIT, sudo=True)
        elif has_old:
            print("\n✗ 门户验证未通过，自动退回旧代码、旧单元与迁移前的快照库")
            restore_previous(run, ts, args.port, force=True)
        else:
            print("\n✗ 门户验证未通过（首次部署，无旧版本可退回），请看 journalctl -u %s"
                  % PORTAL_UNIT)
        return 1
    # 巡检只做信息展示：同步刚重启可能还没跑完第一轮
    run("%s/.venv/bin/python %s/scripts/portal_health.py --db %s; true"
        % (PORTAL_DIR, PORTAL_DIR, PORTAL_DB), sudo=True)
    print("\n完成。本次备份：%s/%s" % (BACKUP_ROOT, ts))
    print("退回：python deploy.py --restore %s" % ts)
    return 0


def portal_rollback(args, run):
    """旧路径回滚：停门户、起旧调试台。旧目录不存在时拒绝，改用 --restore。"""
    code, _, _ = run("test -d %s && test -f /etc/systemd/system/%s"
                     % (LEGACY_DIR, LEGACY_UNIT), show=False)
    if code != 0:
        print("✗ 服务器上没有旧调试台（%s / %s），--rollback 无旧服务可起。"
              % (LEGACY_DIR, LEGACY_UNIT))
        print("  改用：python deploy.py --list-backups，然后 python deploy.py --restore <时间戳>")
        return 2
    print("[1/3] 停用门户 ...")
    run("systemctl disable --now %s" % PORTAL_UNIT, sudo=True)
    print("[2/3] 恢复旧调试台 ...")
    run("systemctl enable --now %s" % LEGACY_UNIT, sudo=True)
    print("[3/3] 验证 80 端口 ...")
    time.sleep(3)
    run("systemctl is-active %s" % LEGACY_UNIT)
    run("python3 -c \"import urllib.request as u; "
        "print('旧调试台 / ->', u.urlopen('http://127.0.0.1/', timeout=8).status)\"")
    print("门户目录与数据仍保留在 %s（未删除），备份在 %s" % (PORTAL_DIR, BACKUP_ROOT))
    return 0


def legacy_deploy(args, run, cli):
    """旧路径：打包内容覆盖 /opt/stm32-webserver 并重启 webserver（仅历史兼容，不要再用）。"""
    code, _, _ = run("test -d %s" % LEGACY_DIR, show=False)
    if code != 0:
        print("✗ %s 不存在，--target legacy 已无目标可部署" % LEGACY_DIR)
        return 2
    print("警告：--target legacy 会覆盖 %s 的代码并重启 80 端口旧服务。" % LEGACY_DIR)
    pkg, names = make_tarball()
    bad = assert_no_protected(names)
    if bad:
        print("✗ 包内出现受保护路径，中止：", bad[:5])
        return 1
    sftp = cli.open_sftp()
    sftp.put(pkg, REMOTE_PKG)
    sftp.close()
    ts = time.strftime("%Y%m%d-%H%M%S")
    run.must("mkdir -p %s/%s && cp -a %s/config %s/%s/config"
             % (BACKUP_ROOT, ts, LEGACY_DIR, BACKUP_ROOT, ts), "旧目录数据备份", sudo=True)
    run.must("rm -rf %s && mkdir -p %s && tar -xzf %s -C %s"
             % (REMOTE_EXTRACT, REMOTE_EXTRACT, REMOTE_PKG, REMOTE_EXTRACT), "解包上传包")
    run.must("cp -a %s/. %s/" % (REMOTE_EXTRACT, LEGACY_DIR), "复制代码", sudo=True)
    run.must("cp %s/deploy/webserver.service /etc/systemd/system/ && "
             "systemctl daemon-reload && systemctl restart webserver" % LEGACY_DIR,
             "重启旧服务", sudo=True)
    time.sleep(4)
    run("systemctl is-active webserver")
    return 0


def dry_run():
    pkg, names, problems = self_check()
    print("dry-run：包 %s（%d 文件，%.1f MB）"
          % (pkg, len(names), os.path.getsize(pkg) / 1024 / 1024))
    print("受保护路径 / 单 worker / 前端产物：%s"
          % ("全部通过" if not problems else "；".join(problems)))
    hint = frontend_stale_hint()
    if hint:
        print("提示：" + hint)
    print("顶层条目：", sorted({n.split("/")[0] for n in names}))
    cfg = load_server_config()
    print("服务器配置：%s" % ("config/deploy_server.json → %s:%s（用户 %s）"
                             % (cfg.get("host"), cfg.get("ssh_port") or cfg.get("port"),
                                cfg.get("user"))
                             if cfg else "未找到 config/deploy_server.json（需用参数或环境变量）"))
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(description="部署门户（数据保护 + 迁移 + 失败自动退回）")
    ap.add_argument("--host", help="服务器地址（默认读 config/deploy_server.json 或 DEPLOY_HOST）")
    ap.add_argument("--ssh-port", type=int, help="SSH 端口（端口转发场景，如 2222）")
    ap.add_argument("--user", help="SSH 用户（默认读配置，否则 %s）" % DEFAULT_USER)
    ap.add_argument("--password", help="SSH/sudo 口令（或环境变量 DEPLOY_PWD）")
    ap.add_argument("--port", type=int, default=80, help="门户监听端口（验证探针用，默认 80）")
    ap.add_argument("--target", choices=("portal", "legacy"), default="portal")
    ap.add_argument("--seed-data",
                    help="首装时的既有数据来源目录（含 config/ 与 .env），如 ~/portal-test")
    ap.add_argument("--cutover", action="store_true",
                    help="允许停用旧调试台以让门户占用 80 端口")
    ap.add_argument("--restore", metavar="时间戳",
                    help="退回到某次升级之前（/opt/portal-backups/ 下的目录名）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --restore：本地队列有未推送改动时仍换回旧库")
    ap.add_argument("--list-backups", action="store_true", help="列出可退回的备份")
    ap.add_argument("--rollback", action="store_true",
                    help="停门户、恢复旧调试台（旧目录已不存在时会拒绝，改用 --restore）")
    ap.add_argument("--dry-run", action="store_true", help="只打包与自检，不连服务器")
    args = ap.parse_args()

    if args.dry_run:
        return dry_run()

    host, ssh_port, user, pwd, sudo_pwd = resolve_target(args, load_server_config())
    args.user = user

    print("[1/8] 连接 %s@%s:%d ..." % (user, host, ssh_port))
    cli = connect(host, user, pwd, ssh_port)
    run = make_runner(cli, sudo_pwd)
    try:
        preflight_sudo(run)
        if args.list_backups:
            list_backups(run)
            return 0
        if args.restore:
            return 0 if restore_previous(run, args.restore, args.port, force=args.force) else 1
        if args.rollback:
            return portal_rollback(args, run)
        if args.target == "legacy":
            return legacy_deploy(args, run, cli)
        return portal_deploy(args, run, cli)
    except StepError as e:
        print("\n✗ 部署中止：%s" % e)
        print("  查看可退回的备份：python deploy.py --list-backups；"
              "退回：python deploy.py --restore <时间戳>")
        return 1
    finally:
        try:
            run("rm -f %s; rm -rf %s" % (REMOTE_PKG, REMOTE_EXTRACT), show=False)
        except Exception:
            pass
        cli.close()


if __name__ == "__main__":
    raise SystemExit(main())
