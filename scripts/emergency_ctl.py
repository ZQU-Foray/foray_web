#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地应急账号管理工具（门户阶段 3，D1）。

在**服务器本地**创建 / 轮换 / 撤销应急调试账号。散列存储（PBKDF2），
无默认密码；不依赖飞书、互联网或管理快照。

用法（仓库根目录）：
  .venv/bin/python scripts/emergency_ctl.py create <用户名> [--note 备注]   # 创建/重置（轮换密码并废止旧会话）
  .venv/bin/python scripts/emergency_ctl.py disable <用户名>               # 撤销：禁登录 + 废止全部会话
  .venv/bin/python scripts/emergency_ctl.py enable <用户名>                # 重新启用（需再设密码时用 create）
  .venv/bin/python scripts/emergency_ctl.py list                           # 列出账号（不含散列）

密码输入：交互输入两次（不回显）；或环境变量 EMERGENCY_PASSWORD（脚本/
自动化场景）。**不接受命令行参数传密码**。数据文件位置可用
AUTH_EMERGENCY_FILE 环境变量覆盖（默认 config/auth/emergency_users.json）。
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import emergency  # noqa: E402


def read_password():
    pw = os.environ.get("EMERGENCY_PASSWORD")
    if pw:
        return pw
    a = getpass.getpass("新密码: ")
    if len(a) < 8:
        sys.exit("错误: 密码至少 8 个字符（无默认密码策略）")
    b = getpass.getpass("再次输入: ")
    if a != b:
        sys.exit("错误: 两次输入不一致")
    return a


def main():
    ap = argparse.ArgumentParser(description="本地应急账号管理（仅服务器本地使用）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("create", help="创建/重置账号（轮换密码并废止旧会话）")
    p1.add_argument("username")
    p1.add_argument("--note", default="")
    sub.add_parser("disable", help="撤销账号（禁登录 + 废止全部会话）").add_argument("username")
    sub.add_parser("enable", help="重新启用账号").add_argument("username")
    sub.add_parser("list", help="列出账号")
    args = ap.parse_args()

    try:
        if args.cmd == "create":
            pw = read_password()
            r = emergency.create(args.username, pw, args.note)
            print(f"已创建/轮换: {r['username']}"
                  f"{'（旧会话已全部废止）' if r['rotated'] else ''}")
            print(f"数据文件: {os.environ.get('AUTH_EMERGENCY_FILE', 'config/auth/emergency_users.json')}")
        elif args.cmd == "disable":
            print("已撤销:", emergency.disable(args.username)["username"])
        elif args.cmd == "enable":
            print("已启用:", emergency.enable(args.username)["username"])
        elif args.cmd == "list":
            users = emergency.list_users()
            if not users:
                print("(无应急账号)")
            for u in users:
                print(f"{u['username']:<16} "
                      f"{'[已撤销]' if u['disabled'] else '[启用]  '} "
                      f"{u.get('note') or ''}")
    except ValueError as e:
        sys.exit(f"错误: {e}")


if __name__ == "__main__":
    main()
