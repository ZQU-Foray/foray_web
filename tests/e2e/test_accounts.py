# -*- coding: utf-8 -*-
"""阶段 9：本地账号库（D2）用例——不依赖服务进程，纯模块级 + 隔离目录。

覆盖：开通/用户名唯一与格式/密码策略、本地校验（含停用与错误密码）、
改密即撤销旧会话（revoked_after）、删除不可逆且审计留痕、
原子写与 600 权限、审计文件不含任何密码/散列。
"""
import json
import os
import shutil
import stat
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def run():
    print("== 阶段 9 本地账号库（D2） ==")
    tmp = tempfile.mkdtemp(prefix="acct-")
    from app import config as cfg
    cfg.AUTH_ACCOUNTS_FILE = __import__("pathlib").Path(os.path.join(tmp, "accounts.json"))
    cfg.AUTH_AUDIT_FILE = __import__("pathlib").Path(os.path.join(tmp, "audit.log"))
    from app.auth import accounts as acc

    # 1) 开通
    r = acc.create("ou_alice", "alice", "correct-horse-9", display="Alice", tenant_key="t1")
    check("飞书开通创建账号", r["created"] is True and acc.get("ou_alice")["username"] == "alice")
    check("账号文件权限 600", stat.S_IMODE(os.stat(cfg.AUTH_ACCOUNTS_FILE).st_mode) == 0o600,
          oct(stat.S_IMODE(os.stat(cfg.AUTH_ACCOUNTS_FILE).st_mode)))
    raw = cfg.AUTH_ACCOUNTS_FILE.read_text(encoding="utf-8")
    check("文件不含明文密码", "correct-horse-9" not in raw and "pbkdf2_sha256$" in raw)
    check("未开通账号 needs_onboarding=True", acc.needs_onboarding("ou_bob") is True)
    check("已开通账号 needs_onboarding=False", acc.needs_onboarding("ou_alice") is False)

    # 2) 用户名与密码策略
    for bad, why in (("ab", "太短"), ("a" * 40, "太长"), ("admin", "保留名"),
                     ("有 空格", "非法字符")):
        try:
            acc.create("ou_x", bad, "password123", display="X")
            check(f"拒绝非法用户名（{why}）", False, bad)
        except ValueError:
            check(f"拒绝非法用户名（{why}）", True)
    try:
        acc.create("ou_y", "bobby", "short")
        check("拒绝过短密码", False)
    except ValueError:
        check("拒绝过短密码", True)
    try:
        acc.create("ou_y", "bobby", "bobby")
        check("拒绝密码=用户名", False)
    except ValueError:
        check("拒绝密码=用户名", True)

    # 3) 唯一性与大小写
    acc.create("ou_bob", "bob", "bob-password-1")
    try:
        acc.create("ou_carol", "BOB", "carol-password-1")
        check("用户名唯一（大小写不敏感）", False)
    except ValueError as e:
        check("用户名唯一（大小写不敏感）", "占用" in str(e), str(e))
    check("按用户名查询大小写不敏感",
          acc.by_username("BoB")[0] == "ou_bob")

    # 4) 本地校验
    ok, info = acc.authenticate("bob", "bob-password-1")
    check("正确凭据通过并返回 open_id", ok and info["open_id"] == "ou_bob", str(info)[:60])
    ok, why = acc.authenticate("bob", "wrong-password")
    check("错误密码拒绝（bad_password）", not ok and why == "bad_password", why)
    ok, why = acc.authenticate("nobody", "whatever123")
    check("不存在的用户拒绝（no_such_user）", not ok and why == "no_such_user", why)

    # 5) 停用 / 启用
    acc.disable("ou_bob", by="maintainer")
    ok, why = acc.authenticate("bob", "bob-password-1")
    check("停用后拒绝登录（disabled）", not ok and why == "disabled", why)
    acc.enable("ou_bob")
    check("启用后恢复登录", acc.authenticate("bob", "bob-password-1")[0] is True)

    # 6) 改密撤销旧会话 + 旧密码失效
    before = acc.get("ou_bob").get("revoked_after", 0)
    acc.set_password("ou_bob", "new-password-9", by="feishu")
    after = acc.get("ou_bob").get("revoked_after", 0)
    check("改密写入 revoked_after（撤销旧会话）", after >= before and after > 0,
          f"{before} -> {after}")
    check("旧密码失效、新密码可用",
          acc.authenticate("bob", "bob-password-1")[0] is False
          and acc.authenticate("bob", "new-password-9")[0] is True)

    # 7) 删除不可逆 + 审计留痕
    acc.delete("ou_bob", reason="left_org", detail="复核返回 resigned")
    check("删除后账号消失", acc.get("ou_bob") is None and acc.by_username("bob")[0] is None)
    check("删除后无法登录", acc.authenticate("bob", "new-password-9")[0] is False)
    audit_lines = [json.loads(x) for x in
                   cfg.AUTH_AUDIT_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
    events = [x["event"] for x in audit_lines]
    check("审计含开通/登录成功失败/改密/删除事件",
          {"account_onboard", "login_ok", "login_failed", "password_reset",
           "account_deleted"} <= set(events), str(sorted(set(events))))
    dele = [x for x in audit_lines if x["event"] == "account_deleted"][0]
    check("删除审计保留旧用户名与原因",
          dele.get("username") == "bob" and dele.get("reason") == "left_org", str(dele))
    joined = json.dumps(audit_lines, ensure_ascii=False)
    check("审计不含密码/散列",
          "bob-password-1" not in joined and "new-password-9" not in joined
          and "pbkdf2_sha256$" not in joined)

    # 8) 重新加入需重新开通（用户名可再被占用）
    r2 = acc.create("ou_bob2", "bob", "another-password-1")
    check("删除后用户名释放、可重新开通", r2["created"] is True
          and acc.by_username("bob")[0] == "ou_bob2")

    # 9) 原子写：并发开通不损坏文件
    import threading
    errs = []

    def worker(i):
        try:
            acc.create("ou_c%d" % i, "carol%d" % i, "password-%d-x" % i)
        except Exception as e:               # noqa: BLE001
            errs.append(str(e))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    doc = json.loads(cfg.AUTH_ACCOUNTS_FILE.read_text(encoding="utf-8"))
    check("并发开通 8 个账号后文件仍可解析且齐全",
          not errs and len(doc["accounts"]) >= 9, f"errs={errs[:2]} n={len(doc['accounts'])}")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n账号库: {len(PASS)} 通过, {len(FAIL)} 失败")
    return len(PASS), len(FAIL)


if __name__ == "__main__":
    _, nfail = run()
    raise SystemExit(1 if nfail else 0)
