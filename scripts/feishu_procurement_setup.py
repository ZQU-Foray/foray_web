"""物资采购表结构初始化：幂等补建「物资明细」所需字段（只建结构，不碰业务数据）。

凭据只从环境变量读（FEISHU_APP_ID / FEISHU_APP_SECRET），密钥不进仓库：
  .venv/bin/python scripts/feishu_procurement_setup.py --dry-run   # 只看计划
  .venv/bin/python scripts/feishu_procurement_setup.py             # 实际补建
  .venv/bin/python scripts/feishu_procurement_setup.py --verify    # 回读现状

字段名与门户 config/feishu.json 的语义键一一对应；建完后用 --verify 打印的
JSON 片段把新 field_id 回填到 config/feishu.json（门户按 field_id 认列，
飞书里改列名不会影响门户）。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "feishu.json"
API_BASE = "https://open.feishu.cn/open-apis"

# 赛季：门户顶部按此切换；飞书里增删选项后门户会自动跟随实际数据
SEASONS = ["2026赛季", "2027赛季", "2028赛季"]

# 一级分类
CATEGORY1 = ["电机", "标准件", "加工件", "电子元器件", "线材接插件", "工具耗材", "其他"]

# 二级分类：飞书单选是平铺的，故选项名全局唯一，门户按一级分类联动过滤
CATEGORY2 = [
    "直流电机", "无刷电机", "舵机", "电机驱动", "编码器", "减速箱",
    "螺丝", "螺母", "垫片垫圈", "轴承", "销与键", "弹簧", "卡簧", "铆钉",
    "板材", "型材", "管材", "轴类", "法兰", "支架", "3D打印件", "定制机加",
    "主控板", "电源模块", "传感器", "电阻电容", "连接器", "芯片", "显示屏",
    "电源线", "信号线", "端子", "插头插座", "线束",
    "螺丝刀", "扳手", "钻头", "胶粘剂", "扎带", "砂纸", "焊锡",
    "其他",
]

# 语义键 → (飞书列名, 字段类型, 字段属性)；顺序即建列顺序
FIELDS = [
    ("season", "赛季", 3, {"options": [{"name": x} for x in SEASONS]}),
    ("category1", "一级分类", 3, {"options": [{"name": x} for x in CATEGORY1]}),
    ("category2", "二级分类", 3, {"options": [{"name": x} for x in CATEGORY2]}),
    ("code", "采购编号", 1, None),
    ("purchase_date", "采购日期", 5, {"date_formatter": "yyyy-MM-dd", "auto_fill": False}),
    ("unit_price", "单价（元）", 2, {"formatter": "0.00"}),
    ("quantity", "数量", 2, {"formatter": "0"}),
    # 公式表达式在 ensure_formula() 里按真实 field_id 生成（飞书要求显式引用）
    ("total_price", "总价（元）", 20, {"formatter": "0.00"}),
    ("purchaser", "采购人", 11, None),
    ("invoice_done", "发票已开", 7, None),
    ("invoice_files", "发票文件", 17, None),
]

# 公式列 → 被引用的列名；飞书不接受按列名书写的引用（实测写了也不参与计算），
# 必须用 bitable::$table[...].$field[...] 显式引用，故在 ensure_formula() 里生成。
FORMULA_SOURCES = {"总价（元）": ("单价（元）", "数量")}


class ApiError(Exception):
    pass


class Client:
    """飞书应用身份客户端（与 scripts/feishu_member_sync.py 同一套最小实现）。"""

    def __init__(self, app_id, secret):
        self.app_id, self.secret = app_id, secret
        self.token, self.expires = "", 0

    def call(self, method, path, body=None, auth=True):
        if auth and time.time() >= self.expires:
            token = self.call("POST", "/auth/v3/tenant_access_token/internal",
                              {"app_id": self.app_id, "app_secret": self.secret}, False)
            self.token = token["tenant_access_token"]
            self.expires = time.time() + token["expire"] - 120
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(
            API_BASE + path, method=method, headers=headers,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode())
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                data = json.load(exc)
            except (ValueError, OSError):
                raise ApiError("接口 HTTP %s" % exc.code) from None
        if data.get("code") != 0:
            raise ApiError("接口错误 %s：%s" % (data.get("code"), str(data.get("msg"))[:180]))
        return data

    def items(self, path):
        rows, cursor = [], ""
        while True:
            query = urllib.parse.urlencode(
                {"page_size": 100, **({"page_token": cursor} if cursor else {})})
            data = self.call("GET", path + ("&" if "?" in path else "?") + query)["data"]
            rows.extend(data.get("items", []))
            if not data.get("has_more"):
                return rows
            cursor = data["page_token"]


def load_target():
    """从 config/feishu.json 读 app_token 与物资采购表 ID（env 可覆盖）。"""
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    base = os.environ.get("FEISHU_BASE_APP_TOKEN") or (raw.get("base") or {}).get("app_token", "")
    tcfg = (raw.get("tables") or {}).get("procurement") or {}
    table = os.environ.get("FEISHU_PROCUREMENT_TABLE") or tcfg.get("table_id", "")
    if not base or not table:
        raise SystemExit("config/feishu.json 缺 base.app_token 或 tables.procurement.table_id")
    return base, table, tcfg


def field_path(base, table):
    return "/bitable/v1/apps/%s/tables/%s/fields" % (base, table)


def current_fields(client, base, table):
    rows = client.items(field_path(base, table))
    return {f["field_name"]: f for f in rows}


def plan(client, base, table, sync_options):
    """比对现状与目标；返回待建/待改清单（不写任何东西）。"""
    have = current_fields(client, base, table)
    create, update, skip = [], [], []
    for key, name, kind, prop in FIELDS:
        cur = have.get(name)
        if cur is None:
            create.append((key, name, kind, prop))
            continue
        if cur.get("type") != kind:
            skip.append("字段『%s』类型不符（现有 %s，期望 %s）——请人工处理"
                        % (name, cur.get("type"), kind))
            continue
        changes = {}
        cur_prop = cur.get("property") or {}
        if prop:
            for pk, pv in prop.items():
                if pk == "options":
                    old = [o.get("name") for o in (cur_prop.get("options") or [])]
                    add = [o for o in pv if o.get("name") not in old]
                    if add and sync_options:
                        changes["options"] = (cur_prop.get("options") or []) + add
                elif cur_prop.get(pk) != pv:
                    changes[pk] = pv
        if changes:
            update.append((key, name, kind, {**cur_prop, **changes}, changes))
        else:
            skip.append("字段『%s』已就绪" % name)
    return have, create, update, skip


def apply(client, base, table, create, update, dry_run):
    path = field_path(base, table)
    for key, name, kind, prop in create:
        body = {"field_name": name, "type": kind}
        if prop:
            body["property"] = prop
        if dry_run:
            print("  [计划] 新建 %-12s 类型=%-3s" % (name, kind))
            continue
        fid = client.call("POST", path, body)["data"]["field"]["field_id"]
        print("  [完成] 新建 %-12s 类型=%-3s field_id=%s" % (name, kind, fid))
    for key, name, kind, prop, changes in update:
        if dry_run:
            print("  [计划] 更新 %-12s 变更=%s" % (name, list(changes)))
            continue
        body = {"field_name": name, "type": kind, "property": prop}
        client.call("PUT", path + "/" + name_to_id(client, base, table, name), body)
        print("  [完成] 更新 %-12s 变更=%s" % (name, list(changes)))


def name_to_id(client, base, table, name):
    return current_fields(client, base, table)[name]["field_id"]


def ensure_formula(client, base, table, dry_run):
    """把公式列重写成显式字段引用（见 FORMULA_SOURCES 说明）。"""
    for target, (left, right) in FORMULA_SOURCES.items():
        have = current_fields(client, base, table)
        if not all(k in have for k in (target, left, right)):
            print("  [跳过] 公式列『%s』缺字段（需先有 %s / %s）" % (target, left, right))
            continue
        expr = ("bitable::$table[%s].$field[%s]*bitable::$table[%s].$field[%s]"
                % (table, have[left]["field_id"], table, have[right]["field_id"]))
        prop = dict(have[target].get("property") or {})
        if prop.get("formula_expression") == expr:
            print("  [跳过] 公式列『%s』已是目标表达式" % target)
            continue
        prop["formula_expression"] = expr
        if dry_run:
            print("  [计划] 公式列『%s』→ %s" % (target, expr))
            continue
        client.call("PUT", field_path(base, table) + "/" + have[target]["field_id"],
                    {"field_name": target, "type": 20, "property": prop})
        print("  [完成] 公式列『%s』→ %s" % (target, expr))


def verify(client, base, table):
    have = current_fields(client, base, table)
    print("物资采购表现有字段 %d 个：" % len(have))
    mapping = {}
    known = {name: key for key, name, _, _ in FIELDS}
    for name, f in have.items():
        prop = f.get("property") or {}
        extra = ""
        if f.get("type") == 3:
            extra = " 选项=%s" % "、".join(o.get("name", "") for o in prop.get("options") or [])
        if f.get("type") == 20:
            extra = " 公式=%s 格式=%s" % (prop.get("formula_expression"),
                                          prop.get("formatter"))
        print("  - %-14s type=%-4s id=%s%s" % (name, f.get("type"), f["field_id"], extra))
        if name in known:
            mapping[known[name]] = f["field_id"]
    print("\nconfig/feishu.json → tables.procurement.fields 回填片段：")
    print(json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True))
    return mapping


def main():
    ap = argparse.ArgumentParser(description="物资采购表结构初始化（幂等）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写飞书")
    ap.add_argument("--verify", action="store_true", help="只回读现状并打印映射")
    ap.add_argument("--no-sync-options", action="store_true",
                    help="不自动补齐单选字段缺失的选项")
    args = ap.parse_args()

    app_id = os.environ.get("FEISHU_APP_ID", "")
    secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not secret:
        raise SystemExit("缺 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量")

    base, table, _ = load_target()
    client = Client(app_id, secret)
    print("Base=%s 表=%s" % (base, table))

    if args.verify:
        verify(client, base, table)
        return 0

    have, create, update, skip = plan(client, base, table, not args.no_sync_options)
    print("\n现状：已有 %d 个字段" % len(have))
    for line in skip:
        print("  [跳过] " + line)
    if create or update:
        print("\n%s：" % ("执行计划" if args.dry_run else "开始执行"))
        apply(client, base, table, create, update, args.dry_run)
    else:
        print("\n结构字段无变化，仅校验公式列。")
    print("\n公式列：")
    ensure_formula(client, base, table, args.dry_run)
    print("\n完成。")
    verify(client, base, table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
