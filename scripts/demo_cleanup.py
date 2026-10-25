"""清理门户演示数据：按 record_id 清单删除示例记录（不动任何既有业务数据）。

用法：
    python3 scripts/demo_cleanup.py            # 只列出将删除的记录
    python3 scripts/demo_cleanup.py --apply    # 真正删除
    python3 scripts/demo_cleanup.py --apply --by-marker   # 不依赖清单，按名称含“示例”兜底查找

清单由造数脚本写出（backups/demo-seed-manifest.json）。默认不删除「项目」表本身——
如需连表一起删，加 --drop-table（表删除不可由 API 恢复，请自行确认）。
"""

import argparse
import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request

API = "https://open.feishu.cn/open-apis"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_token():
    """Base app_token：环境变量 FEISHU_BASE_APP_TOKEN 优先，其次 config/feishu.json；仓库里不写死。"""
    token = os.environ.get("FEISHU_BASE_APP_TOKEN")
    if not token:
        try:
            token = (json.loads((ROOT / "config" / "feishu.json").read_text(encoding="utf-8"))
                     .get("base") or {}).get("app_token")
        except (OSError, ValueError):
            token = None
    if not token:
        raise SystemExit("缺少 Base app_token：请设置 FEISHU_BASE_APP_TOKEN 或准备 config/feishu.json")
    return token


BASE = None  # main() 启动时读取
MANIFEST = ROOT / "backups" / "demo-seed-manifest.json"
MARKER = "示例"
TABLES = ["项目", "工作事项", "公共日程", "重要节点"]


def call(method, path, body=None, token=None, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        url, method=method, headers=headers,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode())
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except Exception:
            return {"code": exc.code, "msg": "HTTP %s" % exc.code}
    except Exception as exc:
        return {"code": -1, "msg": str(exc)}


def items(path, token):
    rows, cursor = [], ""
    while True:
        query = {"page_size": 100}
        if cursor:
            query["page_token"] = cursor
        data = call("GET", path, token=token, params=query)
        if data.get("code") != 0:
            return rows
        block = data.get("data") or {}
        rows.extend(block.get("items", []))
        if not block.get("has_more"):
            return rows
        cursor = block.get("page_token", "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正删除（默认仅列出）")
    parser.add_argument("--by-marker", action="store_true",
                        help="不读清单，按字段文本含“示例”查找")
    parser.add_argument("--drop-table", action="store_true", help="同时删除「项目」表")
    args = parser.parse_args()

    global BASE
    BASE = _base_token()
    token = call("POST", "/auth/v3/tenant_access_token/internal",
                 {"app_id": os.environ["FEISHU_APP_ID"],
                  "app_secret": os.environ["FEISHU_APP_SECRET"]})["tenant_access_token"]
    tables = {t["name"]: t["table_id"] for t in
              items("/bitable/v1/apps/%s/tables" % BASE, token)}
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}

    targets = {}
    for name in TABLES:
        tid = tables.get(name)
        if not tid:
            continue
        ids = list((manifest.get("tables", {}).get(name) or {}).get("record_ids") or [])
        if args.by_marker:
            ids = [r["record_id"] for r in
                   items("/bitable/v1/apps/%s/tables/%s/records" % (BASE, tid), token)
                   if MARKER in json.dumps(r.get("fields") or {}, ensure_ascii=False)]
        if ids:
            targets[name] = (tid, ids)

    for name, (tid, ids) in targets.items():
        print("%s：%d 条" % (name, len(ids)))
        for rid in ids:
            print("   %s" % rid)
            if args.apply:
                resp = call("DELETE", "/bitable/v1/apps/%s/tables/%s/records/%s"
                            % (BASE, tid, rid), token=token)
                if resp.get("code") != 0:
                    print("      删除失败：%s" % str(resp.get("msg"))[:60])

    if args.drop_table and "项目" in tables and args.apply:
        resp = call("DELETE", "/bitable/v1/apps/%s/tables/%s" % (BASE, tables["项目"]),
                    token=token)
        print("删除「项目」表：code=%s" % resp.get("code"))

    if not args.apply:
        print("\n以上为演练，未删除。加 --apply 执行。")


if __name__ == "__main__":
    main()
