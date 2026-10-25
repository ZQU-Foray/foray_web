# -*- coding: utf-8 -*-
"""本站账号维护（D2，阶段 9）：服务器本地执行，用于断网/无法飞书验证时的兜底。

用法（在 /opt/portal 下，用项目 venv）：
    .venv/bin/python scripts/account_ctl.py list
    .venv/bin/python scripts/account_ctl.py disable <用户名>     # 停用（并撤销其会话）
    .venv/bin/python scripts/account_ctl.py enable <用户名>
    .venv/bin/python scripts/account_ctl.py reset-password <用户名>  # 交互输入新密码
    .venv/bin/python scripts/account_ctl.py delete <用户名>      # 删除（不可逆，留审计）

注意：这里的操作直接作用于 `config/auth/accounts.json`；每次操作都写审计
（`config/auth/audit.log`，by=maintainer）。密码不接受命令行参数，也不回显。
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import accounts          # noqa: E402
from app.auth import sessions          # noqa: E402


def _find(username: str):
    open_id, acc = accounts.by_username(username)
    if not open_id:
        sys.exit(f"账号不存在: {username}")
    return open_id, acc


def cmd_list(_args):
    rows = accounts.list_accounts()
    if not rows:
        print("(无本站账号)")
        return 0
    print("%-16s %-10s %-9s %-20s %s" % ("用户名", "状态", "有密码", "最近登录", "open_id"))
    for a in rows:
        import time
        last = a.get("last_login_at")
        print("%-16s %-10s %-9s %-20s %s" % (
            a["username"] or "-", a["status"] or "-",
            "是" if a["has_password"] else "否",
            time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "-",
            (a["open_id"][:12] + "…")))
    return 0


def cmd_disable(args):
    open_id, _ = _find(args.username)
    accounts.disable(open_id, by="maintainer")
    print(f"已停用 {args.username}（旧会话已撤销）")
    return 0


def cmd_enable(args):
    open_id, _ = _find(args.username)
    accounts.enable(open_id)
    print(f"已启用 {args.username}")
    return 0


def cmd_reset(args):
    open_id, _ = _find(args.username)
    pw = getpass.getpass("新密码（至少 8 位，不回显）: ")
    pw2 = getpass.getpass("再输一次: ")
    if pw != pw2:
        sys.exit("两次输入不一致")
    try:
        accounts.set_password(open_id, pw, by="maintainer")
    except ValueError as e:
        sys.exit(str(e))
    n = sessions.revoke_by_user("feishu:" + open_id)
    print(f"已重置 {args.username} 的密码；撤销旧会话 {n} 个")
    return 0


def cmd_delete(args):
    open_id, acc = _find(args.username)
    if not args.yes:
        sys.exit("删除不可逆：确认请加 --yes（审计会记录操作）")
    r = accounts.delete(open_id, reason="maintainer_delete",
                        detail=f"by maintainer: {args.username}")
    n = sessions.revoke_by_user("feishu:" + open_id)
    print(f"已删除 {r.get('username') or args.username}；撤销会话 {n} 个")
    return 0


def main():
    ap = argparse.ArgumentParser(description="本站账号维护（D2）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出账号").set_defaults(func=cmd_list)
    for name, fn, help_ in (("disable", cmd_disable, "停用账号并撤销会话"),
                            ("enable", cmd_enable, "重新启用账号"),
                            ("reset-password", cmd_reset, "交互设置新密码"),
                            ("delete", cmd_delete, "删除账号（不可逆，留审计）")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("username")
        if name == "delete":
            p.add_argument("--yes", action="store_true", help="确认删除")
        p.set_defaults(func=fn)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
