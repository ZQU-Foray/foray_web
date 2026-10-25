#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书门户只读配置校验工具（阶段 1 交付）。

用途：
  - 未配置时：明确诊断缺什么（不猜测、不伪造成功）；
  - 已配置时：只读验证 凭据 -> Base 授权 -> 表/字段映射 -> 分页读取 -> 成员复核权限；
  - --mock 时：用仓库内匿名 fixture 演练同一套检查逻辑（无网络、无凭据）；
  - --scan TABLE_ID 时：对指定数据表打印字段清单（field_id/type/ui_type/选项），
    供建表后填写 config 的字段映射使用。

安全约束（docs/team-portal-migration-plan.md §8.1 规则 5/6）：
  - 只读：本工具只调用 GET 类接口与 tenant_access_token 获取，绝不写入飞书；
  - 脱敏：令牌/密钥/人员姓名/记录字段值一律不原样输出；
  - 密钥不落盘：App Secret 只从环境变量读取（配置文件里用 "env:VAR" 引用）。

用法（仓库根目录）：
  .venv/bin/python scripts/feishu_verify.py                     # 用环境变量配置
  .venv/bin/python scripts/feishu_verify.py --config config/feishu.json
  .venv/bin/python scripts/feishu_verify.py --mock              # 匿名 fixture 演练
  .venv/bin/python scripts/feishu_verify.py --scan tblXXXXXXXX   # 打印某表现有字段

退出码：0=全部通过；1=存在 FAIL；2=配置缺失（未执行网络检查）。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO, "tests", "fixtures", "feishu")

API_BASE = "https://open.feishu.cn"
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
TABLES_PATH = "/open-apis/bitable/v1/apps/{app_token}/tables"
FIELDS_PATH = "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
RECORDS_PATH = "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
CONTACT_USER_PATH = "/open-apis/contact/v3/users/{user_id}"

# 每张表每个语义键允许的 ui_type（与 config/feishu.example.json 的字段映射一一对应）
EXPECTED_UI = {
    "projects": {"name": {"Text"}, "description": {"Text"},
                 "owner_group": {"SingleSelect"}, "collab_groups": {"MultiSelect"},
                 "owners": {"User"}, "plan_start": {"DateTime"}, "plan_end": {"DateTime"},
                 "status": {"SingleSelect"}},
    "tasks": {"name": {"Text"}, "project": {"SingleLink", "DuplexLink"},
              "owner_group": {"SingleSelect"}, "collab_groups": {"MultiSelect"},
              "owners": {"User"}, "priority": {"SingleSelect"},
              "status": {"SingleSelect"}, "start_time": {"DateTime"},
              "due_time": {"DateTime"}, "due_precision": {"SingleSelect"},
              "done_time": {"DateTime"}, "description": {"Text"},
              "blocker_note": {"Text"}},
    "milestones": {"name": {"Text"}, "kind": {"SingleSelect"},
                   "target_time": {"DateTime"}, "time_precision": {"SingleSelect"},
                   "project": {"SingleLink", "DuplexLink"},
                   "visibility": {"SingleSelect"}, "description": {"Text"}},
    "schedule": {"name": {"Text"}, "start_time": {"DateTime"},
                 "end_time": {"DateTime"}, "all_day": {"Checkbox"},
                 "kind": {"SingleSelect"}, "groups": {"MultiSelect"},
                 "participants": {"User"}, "location": {"Text"},
                 "project": {"SingleLink", "DuplexLink"}, "description": {"Text"},
                 "status": {"SingleSelect"}},
}
TABLE_ORDER = ("projects", "tasks", "milestones", "schedule")

_GROUPS = ["机械", "硬件", "电控", "算法", "运营"]
# 单选/多选字段必须包含的枚举值（来自计划 §4 字典；表里额外自定义值仅 WARN）
EXPECTED_ENUM = {
    "projects": {"owner_group": _GROUPS, "collab_groups": _GROUPS,
                 "status": ["筹备中", "进行中", "已结束", "已归档"]},
    "tasks": {"owner_group": _GROUPS, "collab_groups": _GROUPS,
              "priority": ["P0", "P1", "P2", "P3"],
              "status": ["未开始", "进行中", "受阻", "已完成", "已取消"],
              "due_precision": ["仅日期", "精确时刻"]},
    "milestones": {"kind": ["比赛", "关键检查"],
                   "time_precision": ["仅日期", "精确时刻"],
                   "visibility": ["展示", "隐藏", "已取消"]},
    "schedule": {"groups": _GROUPS,
                 "kind": ["例会", "联调", "测试", "其他"],
                 "status": ["正常", "已取消"]},
}
# 关联字段必须指向的表（阶段 1 复验：项目关联不得指向既有业务表）
EXPECTED_LINK_TARGET = {"projects"}

RESULTS = {"PASS": [], "FAIL": [], "SKIP": [], "WARN": []}


def report(level, check, detail=""):
    RESULTS[level].append(check)
    mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "−", "WARN": "!"}[level]
    print(f"  {mark} [{level}] {check}" + (f"  — {detail}" if detail else ""))


def mask(value, keep=4):
    """令牌/ID 脱敏：保留前 keep 后 keep 字符与长度。"""
    s = str(value or "")
    if len(s) <= keep * 2:
        return "***"
    return f"{s[:keep]}...{s[-keep:]}(len={len(s)})"


# ---------------- 配置加载 ----------------

def load_config(path):
    """返回 (cfg, problems)。cfg 为 None 表示配置不可用。"""
    problems, cfg = [], {}
    if path:
        if not os.path.isfile(path):
            return None, [f"配置文件不存在: {path}"]
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as e:
            return None, [f"配置文件解析失败: {e}"]
        cfg = raw
        app_id = cfg.get("app_id", "")
        secret_ref = cfg.get("app_secret_ref", "")
        app_token = (cfg.get("base") or {}).get("app_token", "")
        if not app_id or str(app_id).startswith("<"):
            problems.append("app_id 未填写（示例配置占位符无效）")
        secret = resolve_secret(secret_ref)
        if not secret:
            problems.append(
                f"App Secret 未获取（app_secret_ref={secret_ref!r}；"
                "应设置环境变量 FEISHU_APP_SECRET）")
        if not app_token or str(app_token).startswith("<"):
            problems.append("base.app_token 未填写（Base 链接中 feishu.cn/base/ 之后的部分）")
        tables = cfg.get("tables") or {}
        for key in TABLE_ORDER:
            tid = (tables.get(key) or {}).get("table_id", "")
            if not tid or str(tid).startswith("<"):
                problems.append(f"tables.{key}.table_id 未填写（目标表尚未建立时属预期，先建表）")
        return cfg, problems

    # 环境变量模式
    env_id = os.environ.get("FEISHU_APP_ID", "")
    env_secret = os.environ.get("FEISHU_APP_SECRET", "")
    env_token = os.environ.get("FEISHU_BASE_APP_TOKEN", "")
    if not (env_id and env_secret and env_token):
        missing = [n for n, v in (("FEISHU_APP_ID", env_id),
                                  ("FEISHU_APP_SECRET", env_secret),
                                  ("FEISHU_BASE_APP_TOKEN", env_token)) if not v]
        return None, ["环境变量未配置: " + ", ".join(missing) +
                      "；或用 --config 指向结构同 config/feishu.example.json 的配置文件"]
    return {"app_id": env_id, "app_secret_ref": "env:FEISHU_APP_SECRET",
            "base": {"app_token": env_token}, "tables": {}}, []


def resolve_secret(ref):
    """解析 secret 引用：'env:VAR' -> 环境变量；其余视为无效（禁止明文落盘）。"""
    ref = str(ref or "")
    if ref.startswith("env:"):
        return os.environ.get(ref[4:], "")
    return ""


# ---------------- API 层（真实 / mock 双实现） ----------------

class FeishuClient:
    def __init__(self, app_id=None, secret=None, mock=False):
        self.app_id, self.secret, self.mock = app_id, secret, mock
        self._token = None
        self._tables = None
        self._fields = None
        self._records = None

    def _http(self, method, url, body=None, headers=None, timeout=15):
        """单次请求；网络类异常（code=-1）由 _http_retry 退避重试。"""
        req = urllib.request.Request(
            url, data=json.dumps(body).encode() if body is not None else None,
            headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"code": -1, "msg": f"HTTP {e.code}"}
        except Exception as e:
            return {"code": -1, "msg": str(e)}

    def _http_retry(self, method, url, body=None, headers=None, timeout=15):
        """网络层故障（连接/TLS 失败）退避重试；API 业务错误（code>=1）不重试。"""
        delays = (1, 2, 4, 8, 16)
        resp = self._http(method, url, body, headers, timeout)
        for d in delays:
            if resp.get("code") != -1:
                return resp
            print(f"       （网络异常，{d}s 后重试：{str(resp.get('msg'))[:80]}）")
            time.sleep(d)
            resp = self._http(method, url, body, headers, timeout)
        return resp

    def tenant_token(self):
        if self.mock:
            return {"code": 0, "tenant_access_token": "t-fixture", "expire": 7200}
        return self._http_retry("POST", API_BASE + TOKEN_PATH,
                          {"app_id": self.app_id, "app_secret": self.secret},
                          {"Content-Type": "application/json"})

    def _bearer(self):
        if self._token is None:
            r = self.tenant_token()
            self._token = r.get("tenant_access_token")
        return {"Authorization": "Bearer " + (self._token or ""),
                "Content-Type": "application/json"}

    def _load_fixture(self, name):
        if getattr(self, f"_{name}") is None:
            with open(os.path.join(FIXTURES, f"{name}.json"), encoding="utf-8") as fh:
                setattr(self, f"_{name}", json.load(fh))
        return getattr(self, f"_{name}")

    def list_tables(self, app_token):
        if self.mock:
            return self._load_fixture("tables")
        return self._http_retry("GET", API_BASE + TABLES_PATH.format(app_token=app_token),
                          headers=self._bearer())

    def list_fields(self, app_token, table_id):
        if self.mock:
            fields = self._load_fixture("fields")
            key = _fixture_key(table_id)
            return fields.get(key, {"code": 1254041, "msg": "TableIdNotFound"})
        return self._http_retry("GET", API_BASE + FIELDS_PATH.format(
            app_token=app_token, table_id=table_id), headers=self._bearer())

    def iter_records(self, app_token, table_id, page_size=2, max_pages=500):
        """生成 (resp, fetched_count)。真实模式翻页；mock 模式回放 fixture 两页。"""
        if self.mock:
            pages = self._load_fixture("records")[_fixture_key(table_id)]
            fetched = 0
            for p in pages:
                fetched += len((p["resp"].get("data") or {}).get("items") or [])
                yield p["resp"], fetched
            return
        token, fetched, pages = None, 0, 0
        while pages < max_pages:
            q = f"?page_size={page_size}&user_id_type=open_id"
            if token:
                q += f"&page_token={token}"
            resp = self._http_retry("GET", API_BASE + RECORDS_PATH.format(
                app_token=app_token, table_id=table_id) + q, headers=self._bearer())
            data = resp.get("data") or {}
            fetched += len(data.get("items") or [])
            pages += 1
            yield resp, fetched
            if resp.get("code") != 0 or not data.get("has_more"):
                return
            token = data.get("page_token")

    def contact_user(self, user_id):
        if self.mock:
            return {"code": 99991672, "msg": "fixture: 模拟未开通通讯录权限"}
        return self._http_retry("GET", API_BASE + CONTACT_USER_PATH.format(user_id=user_id)
                                + "?user_id_type=open_id", headers=self._bearer())

    def scopes_first_user(self):
        """从官方通讯录授权范围接口取第一个用户 open_id（C7 人员来源的最后手段；
        不读取任何业务数据表）。返回 open_id 或 None。"""
        if self.mock:
            return None
        resp = self._http_retry("GET", API_BASE + "/open-apis/contact/v3/scopes"
                                "?page_size=100&user_id_type=open_id", headers=self._bearer())
        if resp.get("code") != 0:
            return None
        us = ((resp.get("data") or {}).get("user_ids")) or []
        return us[0] if us else None

    def scopes_summary(self):
        """通讯录授权范围摘要（脱敏计数）。"""
        if self.mock:
            return None
        resp = self._http_retry("GET", API_BASE + "/open-apis/contact/v3/scopes"
                                "?page_size=100&user_id_type=open_id", headers=self._bearer())
        if resp.get("code") != 0:
            return {"code": resp.get("code"), "msg": str(resp.get("msg"))[:120]}
        d = resp.get("data") or {}
        return {"code": 0,
                "users": len(d.get("user_ids") or []),
                "departments": len(d.get("department_ids") or []),
                "groups": len(d.get("group_ids") or [])}


def _fixture_key(table_id):
    for key, prefix in (("projects", "tblFixProjects"), ("tasks", "tblFixTasks"),
                        ("milestones", "tblFixMilestn"), ("schedule", "tblFixSchedl")):
        if str(table_id).startswith(prefix):
            return key
    return ""


# ---------------- 检查序列 ----------------

def verify(cfg, client, scan_table=None, test_user=None):
    app_token = (cfg.get("base") or {}).get("app_token", "")
    tables_cfg = cfg.get("tables") or {}

    print("\n== C2 应用凭据（tenant_access_token，只读获取）==")
    tok = client.tenant_token()
    if tok.get("code") == 0 and tok.get("tenant_access_token"):
        report("PASS", "凭据有效，令牌获取成功",
               f"token={mask(tok['tenant_access_token'])} expire={tok.get('expire')}s")
    elif tok.get("code") == -1:
        report("FAIL", "网络不可达（非凭据/权限问题）",
               f"{str(tok.get('msg'))[:140]}；排查出网/DNS/防火墙后重试")
        return
    else:
        report("FAIL", "凭据被拒绝（应用侧拒绝，非网络错误）",
               f"code={tok.get('code')} msg={str(tok.get('msg'))[:140]}")
        return

    print("\n== C3 Base 资源授权（列出数据表）==")
    tdoc = client.list_tables(app_token)
    if tdoc.get("code") != 0:
        report("FAIL", "Base 访问失败", f"code={tdoc.get('code')} msg={tdoc.get('msg')}")
        return
    items = ((tdoc.get("data") or {}).get("items")) or []
    report("PASS", f"Base 授权有效，现有 {len(items)} 张数据表")
    for t in items:
        print(f"       · {t.get('table_id')}  {t.get('name')!r}")
    by_id = {t.get("table_id"): t for t in items}

    if scan_table:
        print(f"\n== --scan {scan_table} 字段清单 ==")
        fd = client.list_fields(app_token, scan_table)
        if fd.get("code") != 0:
            report("FAIL", "字段读取失败", f"code={fd.get('code')} msg={fd.get('msg')}")
        else:
            report("PASS", "字段清单如下（复制 field_id 填入配置映射）")
            for f in (fd.get("data") or {}).get("items") or []:
                prop = f.get("property") or {}
                extra = ""
                if f.get("type") in (3, 4) and prop.get("options"):
                    extra = " opts=" + "|".join(o.get("name", "") for o in prop["options"])
                if prop.get("table_id"):
                    extra += f" ->table={prop['table_id']}"
                print(f"       · {f.get('field_id')}  {f.get('field_name')!r} "
                      f"type={f.get('type')} ui={f.get('ui_type')}{extra}")
        return

    for key in TABLE_ORDER:
        tcfg = tables_cfg.get(key) or {}
        tid = tcfg.get("table_id", "")
        print(f"\n== C4/C5/C6 表 {key}（{tcfg.get('display_name', '')}）==")
        if not tid or str(tid).startswith("<"):
            report("SKIP", f"{key}.table_id 未配置（表尚未建立属预期）",
                   "建表后用 --scan 获取 field_id 并回填配置")
            continue
        if tid not in by_id:
            report("FAIL", f"table_id {mask(tid)} 不在当前 Base 中", "核对 Base 链接与表 ID")
            continue
        report("PASS", f"数据表存在: {by_id[tid].get('name')!r}")

        fd = client.list_fields(app_token, tid)
        if fd.get("code") != 0:
            report("FAIL", "字段读取失败", f"code={fd.get('code')} msg={fd.get('msg')}")
            continue
        fmap = {f.get("field_id"): f for f in (fd.get("data") or {}).get("items") or []}
        fields_cfg = tcfg.get("fields") or {}
        projects_tid = ((tables_cfg.get("projects") or {}).get("table_id") or "")
        enum_cfg = EXPECTED_ENUM.get(key, {})
        missing_keys = [k for k, v in fields_cfg.items()
                        if not v or str(v).startswith("<")]
        if missing_keys:
            report("FAIL", f"必需字段映射未填齐: {missing_keys}",
                   "回填真实 field_id 后重跑")
        for fkey, fid in fields_cfg.items():
            if not fid or str(fid).startswith("<"):
                continue
            expect = EXPECTED_UI.get(key, {}).get(fkey)
            f = fmap.get(fid)
            if not f:
                report("FAIL", f"字段映射 {key}.{fkey} -> {mask(fid)} 不存在")
                continue
            if expect and f.get("ui_type") not in expect:
                report("FAIL", f"字段 {key}.{fkey} 类型不符",
                       f"实际 ui={f.get('ui_type')} 期望 {sorted(expect)}")
                continue
            detail = f"'{f.get('field_name')}' ({f.get('ui_type')})"
            # 枚举齐全性（单选/多选）
            need_enum = enum_cfg.get(fkey)
            if need_enum is not None:
                opts = {o.get("name", "") for o in
                        ((f.get("property") or {}).get("options") or [])}
                lacked = [v for v in need_enum if v not in opts]
                extra = [v for v in opts if v not in need_enum]
                if lacked:
                    report("FAIL", f"字段 {key}.{fkey} 枚举缺失",
                           f"缺少 {lacked}；现有 {sorted(opts)}")
                    continue
                if extra:
                    report("WARN", f"字段 {key}.{fkey} 含字典外自定义枚举",
                           f"额外值 {sorted(extra)}（数据侧按未知枚举入质量报告）")
                detail += f" 枚举{len(need_enum)}项齐全"
            # 关联指向（project 类字段必须指向 projects 目标表）
            if fkey == "project":
                link_tid = (f.get("property") or {}).get("table_id", "")
                if link_tid and projects_tid and link_tid != projects_tid:
                    report("FAIL", f"字段 {key}.{fkey} 关联指向错误",
                           f"指向 {mask(link_tid)} 而非目标项目表 {mask(projects_tid)}")
                    continue
                if link_tid and projects_tid and link_tid == projects_tid:
                    detail += " ->指向目标项目表"
            report("PASS", f"字段映射 {key}.{fkey} -> {detail}")
        unmapped = set(fmap) - set(fields_cfg.values())
        if unmapped:
            report("WARN", f"表中另有 {len(unmapped)} 个字段未纳入映射（不影响只读同步）")

        # 分页完整性
        server_total, fetched, pages, err = None, 0, 0, None
        for resp, cnt in client.iter_records(app_token, tid, page_size=2):
            if resp.get("code") != 0:
                err = f"code={resp.get('code')} msg={resp.get('msg')}"
                break
            fetched, pages = cnt, pages + 1
            server_total = (resp.get("data") or {}).get("total", server_total)
        if err:
            report("FAIL", "记录读取失败", err)
        elif server_total is not None and fetched != server_total:
            report("FAIL", "分页不完整", f"拉取 {fetched} != 服务端 total {server_total}")
        else:
            report("PASS", "分页读取完整",
                   f"records={fetched} pages={pages} server_total={server_total}")

    print("\n== C7 通讯录权限、授权范围与成员复核 ==")
    # C7a 授权范围（官方 scopes 接口，脱敏计数；同时验证 contact 接口权限）
    sc = client.scopes_summary()
    if sc is None:
        report("SKIP", "scopes 摘要仅真实模式可用")
    elif sc.get("code") != 0:
        report("FAIL", "通讯录授权范围不可读（contact 接口权限缺失或范围未配置）",
               f"code={sc.get('code')} {sc.get('msg', '')}")
    elif sc.get("users") == 0 and sc.get("departments") == 0:
        report("FAIL", "通讯录授权范围为空（无用户/部门）",
               "在开发者后台为应用配置通讯录权限范围")
    else:
        report("PASS", "通讯录授权范围非空",
               f"用户={sc['users']} 部门={sc['departments']} 用户组={sc['groups']}"
               "（与战队成员名单的对应由维护者确认）")

    # C7b 成员 status 字段复核。人员来源优先级：
    # --test-user 显式指定 > 四张目标表记录内引用 > 官方 scopes 范围内第一个用户。
    # 不扫描其他既有业务表（复验规则：避免无关业务数据访问）。
    sample_uid = test_user or _first_user_ref(client, app_token, tables_cfg)
    src = "指定" if (test_user and sample_uid == test_user) else \
        ("目标表" if sample_uid else "")
    if not sample_uid:
        sample_uid = client.scopes_first_user()
        src = "scopes 范围" if sample_uid else ""
    if not sample_uid:
        report("SKIP", "无可用人员引用（未指定 --test-user、目标表未建/为空、范围为空）",
               "指定 --test-user ou_xxx 或在目标表录入人员后重跑")
    else:
        print(f"       探测对象: {mask(sample_uid, 6)}（来源: {src}）")
        cu = client.contact_user(sample_uid)
        if cu.get("code") == 0:
            st = (((cu.get("data") or {}).get("user")) or {}).get("status") or {}
            need = ("is_resigned", "is_exited")
            if all(k in st for k in need):
                report("PASS", "成员复核可用：status 含撤权判定字段",
                       f"键={sorted(st)} is_resigned={st.get('is_resigned')} "
                       f"is_exited={st.get('is_exited')}")
            else:
                report("FAIL", "接口 code=0 但 status 不可用（缺字段级权限）",
                       f"status={st or '(空)'}；需开通 contact:user.employee:readonly 等"
                       " status 字段级权限，见 docs/feishu-setup.md §3")
        elif cu.get("code") == 99991672:
            report("FAIL", "成员状态不可读（应用缺通讯录接口权限）",
                   "需 contact:contact.base:readonly 等接口级 scope，"
                   "见 docs/feishu-setup.md §3")
        else:
            report("FAIL", "成员状态不可读",
                   f"code={cu.get('code')} msg={str(cu.get('msg'))[:120]}")

    print("\n== C8 OAuth 回调前置检查（不发起授权）==")
    oauth = cfg.get("oauth") or {}
    redirect = oauth.get("redirect_url", "")
    if not redirect or "<" in redirect:
        report("SKIP", "redirect_url 未配置",
               "须为 http(s)://host[:port]/path 形式，且先加入飞书应用 安全设置>重定向 URL 列表")
    else:
        ok_shape = redirect.startswith(("http://", "https://")) and \
            "/" in redirect.split("//", 1)[1]
        report("PASS" if ok_shape else "FAIL", "redirect_url 形状检查", redirect)
        report("WARN", "回调真实跳转需维护者将 URL 加入应用安全设置后浏览器实测",
               "列表外回调返回 code=2000 redirect_uri unmatch")


def _first_user_ref(client, app_token, tables_cfg):
    """从四张目标表记录中取第一个人员引用（不输出、不扫其他业务表）。"""
    ids = [((tables_cfg.get(k) or {}).get("table_id") or "")
           for k in TABLE_ORDER]
    if client.mock:
        recs = client._load_fixture("records")
        for key in TABLE_ORDER:
            for p in recs.get(key, []):
                for it in ((p["resp"].get("data") or {}).get("items") or []):
                    for v in (it.get("fields") or {}).values():
                        if isinstance(v, list) and v and isinstance(v[0], dict) \
                                and str(v[0].get("id", "")).startswith("ou_"):
                            return v[0]["id"]
        return None
    for tid in ids:
        if not tid or str(tid).startswith("<"):
            continue
        for resp, _ in client.iter_records(app_token, tid, page_size=2, max_pages=1):
            for it in ((resp.get("data") or {}).get("items") or []):
                for v in (it.get("fields") or {}).values():
                    if isinstance(v, list) and v and isinstance(v[0], dict) \
                            and str(v[0].get("id", "")).startswith("ou_"):
                        return v[0]["id"]
    return None


# ---------------- mock 配置构造（对齐 fixture）----------------

MOCK_CFG = {
    "app_id": "cli_fixture0000000000",
    "app_secret_ref": "env:FEISHU_APP_SECRET",
    "base": {"app_token": "FixtureBaseToken0000000000000"},
    "tables": {
        "projects": {"table_id": "tblFixProjects01", "display_name": "项目",
                     "fields": {"name": "fldFixPrjName01", "description": "fldFixPrjDesc01",
                                "owner_group": "fldFixPrjOwnG01", "collab_groups": "fldFixPrjColG01",
                                "owners": "fldFixPrjOwner1", "plan_start": "fldFixPrjStart1",
                                "plan_end": "fldFixPrjEnd011", "status": "fldFixPrjStat01"}},
        "tasks": {"table_id": "tblFixTasks0001", "display_name": "事项",
                  "fields": {"name": "fldFixTskName01", "project": "fldFixTskProj01",
                             "owner_group": "fldFixTskOwnG01", "collab_groups": "fldFixTskColG01",
                             "owners": "fldFixTskOwner1", "priority": "fldFixTskPri001",
                             "status": "fldFixTskStat01", "start_time": "fldFixTskStart1",
                             "due_time": "fldFixTskDue001", "due_precision": "fldFixTskDueP01",
                             "done_time": "fldFixTskDone01", "description": "fldFixTskDesc01",
                             "blocker_note": "fldFixTskBlk001"}},
        "milestones": {"table_id": "tblFixMilestn01", "display_name": "重要节点",
                       "fields": {"name": "fldFixMilName01", "kind": "fldFixMilKind01",
                                  "target_time": "fldFixMilTime01", "time_precision": "fldFixMilPrec01",
                                  "project": "fldFixMilProj01", "visibility": "fldFixMilVis001",
                                  "description": "fldFixMilDesc01"}},
        "schedule": {"table_id": "tblFixSchedl01", "display_name": "公共日程",
                     "fields": {"name": "fldFixSchName01", "start_time": "fldFixSchStart1",
                                "end_time": "fldFixSchEnd001", "all_day": "fldFixSchAllD001",
                                "kind": "fldFixSchKind01", "groups": "fldFixSchGrp001",
                                "participants": "fldFixSchPart001", "location": "fldFixSchLoc001",
                                "project": "fldFixSchProj01", "description": "fldFixSchDesc01",
                                "status": "fldFixSchStat01"}},
    },
    "oauth": {"redirect_url": "http://portal.lan.example:8000/auth/feishu/callback"},
}


def main():
    ap = argparse.ArgumentParser(description="飞书门户只读配置校验（脱敏输出）")
    ap.add_argument("--config", help="配置文件路径（结构见 config/feishu.example.json）")
    ap.add_argument("--mock", action="store_true", help="匿名 fixture 演练（无网络无凭据）")
    ap.add_argument("--scan", metavar="TABLE_ID", help="仅打印指定数据表的字段清单后退出")
    ap.add_argument("--test-user", metavar="OPEN_ID",
                    help="C7 指定测试成员 open_id（优先于目标表内人员引用）")
    args = ap.parse_args()

    print("飞书门户只读配置校验 — 输出已脱敏（令牌/姓名/记录值不显示）")
    if args.mock:
        print("模式: mock（tests/fixtures/feishu 匿名数据）")
        verify(MOCK_CFG, FeishuClient(mock=True), test_user=args.test_user)
    else:
        cfg, problems = load_config(args.config)
        print(f"模式: 真实（config={'文件' if args.config else '环境变量'}）")
        print("\n== C1 配置完整性 ==")
        if cfg is None:
            for p in problems:
                report("FAIL", "配置缺失", p)
            print("\n提示：参照 docs/feishu-setup.md 与 config/feishu.example.json 配置后重跑；"
                  "或先用 --mock 演练检查逻辑。")
            sys.exit(2)
        for p in problems:
            is_table_gap = "table_id" in p
            report("WARN" if is_table_gap else "FAIL", "配置不完整（可继续部分检查）", p)
        client = FeishuClient(cfg.get("app_id"), resolve_secret(cfg.get("app_secret_ref")))
        verify(cfg, client, scan_table=args.scan, test_user=args.test_user)

    print("\n==== 汇总 ====")
    for k in ("PASS", "FAIL", "SKIP", "WARN"):
        print(f"  {k}: {len(RESULTS[k])}")
    if RESULTS["SKIP"] or RESULTS["WARN"]:
        print("  注意：存在 SKIP/WARN 项——退出码 0 不等于全部验收完成，"
              "SKIP=未验证，WARN=需人工确认。")
    sys.exit(1 if RESULTS["FAIL"] else 0)


if __name__ == "__main__":
    main()
