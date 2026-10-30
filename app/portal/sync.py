"""飞书四表只读同步：分页拉取 + 超时/重试/429 退避 + 调度器。

- 应用身份（tenant token，缓存复用、过期重取一次）；
- 完整分页（page_token 循环，total 对账）；
- 有限重试：网络/超时/429/5xx 退避重试（配置次数）；4xx 业务错误立即失败；
- 单一同步锁：定时 60s 与手动请求合并（手动最小间隔限频，前端人数
  不放大飞书请求）；
- 候选快照四表全部成功 → 原子发布；任一失败 → 丢弃候选保留旧版；
- 全程运行在受控工作线程（urllib 同步 IO 不进事件循环，不阻塞调试 WS）。
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config
from ..auth import feishu as auth_feishu
from . import cache_store
from . import store as portal_store

log = logging.getLogger(__name__)

RETRYABLE_HTTP = {429, 500, 502, 503, 504}
# members（成员数据）由 scripts/feishu_member_sync.py 写入，门户侧只读同步；
# procurement（物资采购）全员可读写（组长批复列除外，见 api.py）
# duty（值日表）由维护者导入飞书，门户只读；未映射时快照置空、考勤页提示未接入
TABLE_ORDER = ("projects", "tasks", "milestones", "schedule", "members", "roster",
               "procurement", "duty")
# 页面上不展示单条记录链接的表：跳过 batch_get，少打飞书接口
NO_LINK_TABLES = ("duty",)
MAX_PAGES = 200          # 分页上界：防 page_token 缺失/重复导致死循环持锁
# 字段元数据缓存：{table_id: {"at": 拉取时间戳, "map": {飞书列名: field_id}}}
# 列名→field_id 对照几乎不变，缓存后每轮不再为 8 张表各打一次 fields 请求；
# 缓存过期或配置字段对不上时强制重拉，改字段名不会被缓存挡住
FIELDS_CACHE_META = "fields_cache"


class SyncError(Exception):
    """同步失败（分类：network / rate_limited / api / config / parse）。

    code：飞书业务错误码（api 类才有，如 1254043 记录不存在），供调用方细分处理。
    """

    def __init__(self, klass, detail, code=None):
        super().__init__(detail)
        self.klass = klass
        self.code = code


def _load_sync_cfg():
    """同步配置：**配置文件为底，env 覆盖单个字段**（避免只给 secret 就丢表映射）。

    阶段 5 部署实测缺陷：env 只注入 FEISHU_APP_ID/SECRET/TOKEN 时，原实现
    直接跳过 feishu.json，导致 tables 为空 → 同步恒失败（config 类错误）。
    """
    import os
    cfg = {"api_base": os.environ.get("FEISHU_API_BASE", "https://open.feishu.cn"),
           "app_id": "", "app_secret": "", "tables": {}, "app_token": ""}
    try:
        raw = json.loads(auth_feishu.config.FEISHU_CONFIG_FILE
                         .read_text(encoding="utf-8"))
        sec = raw.get("app_secret_ref", "")
        cfg["app_id"] = raw.get("app_id", "")
        cfg["app_secret"] = (os.environ.get(sec[4:], "")
                             if sec.startswith("env:") else "")
        cfg["tables"] = raw.get("tables") or {}
        cfg["app_token"] = (raw.get("base") or {}).get("app_token", "")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("同步配置读取失败（继续用 env）: %s", e)
    # env 逐字段覆盖
    cfg["app_id"] = os.environ.get("FEISHU_APP_ID") or cfg["app_id"]
    cfg["app_secret"] = os.environ.get("FEISHU_APP_SECRET") or cfg["app_secret"]
    cfg["app_token"] = (os.environ.get("FEISHU_BASE_APP_TOKEN")
                        or cfg["app_token"])
    # 测试模式：env 显式给的表映射优先
    tjson = os.environ.get("FEISHU_TABLES_JSON")
    if tjson:
        try:
            cfg["tables"] = json.loads(tjson)
        except Exception as e:
            log.warning("FEISHU_TABLES_JSON 解析失败: %s", e)
    return cfg


# 令牌缓存（阶段 8）：进程级共享，按响应里的 expire 复用。
# 修复前每轮同步都新建 FeishuReader → 每 60s 一次 token 请求（阶段 4 遗留 P1）。
_TOKEN_CACHE = {}            # app_id -> (token, expires_at)
TOKEN_SAFETY_SEC = 300       # 到期前 5 分钟视为过期，避免边界上 401


def _cached_token(app_id):
    item = _TOKEN_CACHE.get(app_id)
    if item and item[1] > time.time():
        return item[0]
    return None


def _store_token(app_id, token, expire):
    ttl = float(expire or 7200)
    _TOKEN_CACHE[app_id] = (token, time.time() + max(60.0, ttl - TOKEN_SAFETY_SEC))


def invalidate_token(app_id=None):
    """令牌失效（上游报 token 过期/被撤销）时清缓存，强制下次重取。"""
    if app_id is None:
        _TOKEN_CACHE.clear()
    else:
        _TOKEN_CACHE.pop(app_id, None)


class FeishuReader:
    """bitable 只读客户端（records 全量分页；无任何写调用）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._token = None
        self._token_at = 0.0
        # 本轮发出的真实 HTTP 请求数（日志与验收用：请求数是同步速度的主指标）
        self.request_count = 0

    def _tenant_token(self, force=False):
        app_id = self.cfg["app_id"]
        if not force:
            cached = _cached_token(app_id)
            if cached:
                return cached
        else:
            invalidate_token(app_id)
        r = self._req_json("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                           body={"app_id": app_id,
                                 "app_secret": self.cfg["app_secret"]},
                           auth=False)
        if r.get("code") != 0 or not r.get("tenant_access_token"):
            raise SyncError("config", f"tenant_token 失败 code={r.get('code')}")
        _store_token(app_id, r["tenant_access_token"], r.get("expire"))
        return r["tenant_access_token"]

    def _req_json(self, method, path, body=None, auth=True, timeout=None):
        url = self.cfg["api_base"] + path
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        if auth:
            hdrs["Authorization"] = "Bearer " + self._tenant_token()
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        timeout = timeout or config.PORTAL_SYNC_TIMEOUT_SEC
        self.request_count += 1

        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP:
                raise SyncError("rate_limited" if e.code == 429 else "network",
                                f"HTTP {e.code}")
            try:
                return json.loads(e.read().decode())
            except Exception:
                raise SyncError("api", f"HTTP {e.code}")
        except Exception as e:
            raise SyncError("network", str(e)[:100])

    def _get(self, path, params=None):
        """GET with 有限重试（网络/429/5xx 退避；token 过期重取一次）。"""
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        last = None
        for attempt in range(config.PORTAL_SYNC_RETRIES + 1):
            if attempt:
                time.sleep(config.PORTAL_SYNC_BACKOFF_SEC * attempt)
            try:
                r = self._req_json("GET", path + q)
            except SyncError as e:
                last = e
                if e.klass in ("network", "rate_limited"):
                    continue
                raise
            code = r.get("code")
            if code == 0:
                return r
            if code in (99991663, 99991661, 99991668):    # token 失效类
                invalidate_token(self.cfg["app_id"])
                self._tenant_token(force=True)
                continue
            raise SyncError("api", f"code={code} {str(r.get('msg'))[:80]}")
        raise last or SyncError("network", "重试耗尽")

    def fields(self, table_id):
        """字段元数据：{field_name: field_id}（分页；records 的键是字段名，
        须经此对照翻译到语义键——字段名可改，field_id 稳定）。"""
        name2id, token, pages = {}, None, 0
        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise SyncError("parse", f"字段分页超过上限 {MAX_PAGES}")
            params = {"page_size": config.PORTAL_SYNC_PAGE_SIZE}
            if token:
                params["page_token"] = token
            r = self._get(
                f"/open-apis/bitable/v1/apps/{self.cfg['app_token']}"
                f"/tables/{table_id}/fields", params)
            d = r.get("data") or {}
            for f in d.get("items") or []:
                name2id[f.get("field_name")] = f.get("field_id")
            if not d.get("has_more"):
                break
            nxt = d.get("page_token")
            if not nxt or nxt == token:
                raise SyncError("parse", "字段分页 token 缺失或重复")
            token = nxt
        return name2id

    @staticmethod
    def translate_keys(items, name2id, fmap):
        """records.fields 的中文键 → 语义键（经 field_id 中转）。

        未映射的键保留原名（normalize 忽略未知键；重命名字段在
        fields 元数据层对不上时按缺值处理并可能计入质量报告）。
        """
        id2sem = {fid: sem for sem, fid in fmap.items()}
        out = []
        for it in items:
            newf = {}
            for fname, val in (it.get("fields") or {}).items():
                fid = name2id.get(fname, fname)
                newf[id2sem.get(fid, fname)] = val
            out.append({"record_id": it.get("record_id"), "fields": newf})
        return out

    def records(self, table_id):
        """全量分页拉取 + total 对账（带页数上界与 token 防重）。"""
        items, token, total, pages = [], None, None, 0
        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise SyncError("parse", f"记录分页超过上限 {MAX_PAGES}")
            # automatic_fields：顺带取回 last_modified_time 等系统字段（在 fields
            # 之外，不影响规范化），供本地缓存库判断飞书侧改动（2026-10-21 实测可用）
            params = {"page_size": config.PORTAL_SYNC_PAGE_SIZE,
                      "user_id_type": "open_id", "automatic_fields": "true"}
            if token:
                params["page_token"] = token
            r = self._get(
                f"/open-apis/bitable/v1/apps/{self.cfg['app_token']}"
                f"/tables/{table_id}/records", params)
            d = r.get("data") or {}
            items += d.get("items") or []
            total = d.get("total", total)
            if not d.get("has_more"):
                break
            nxt = d.get("page_token")
            if not nxt or nxt == token:
                raise SyncError("parse", "记录分页 token 缺失或重复")
            token = nxt
        if total is None:
            # 上游没给 total 时对账等于关闭：至少留一条日志，别让缺页静默通过
            log.warning("表 %s 未返回 total，本轮不做总数对账（%d 条）", table_id, len(items))
        elif abs(len(items) - total) > 1:
            # 容差 1 条：翻页期间别人新增/删除一条会让计数差一，不能因此整轮同步作废
            raise SyncError("parse", f"分页不完整 {len(items)} != total {total}")
        return items


    def record_links(self, table_id, record_ids, known=None):
        """记录链接：官方 batch_get(with_shared_url=true) → {record_id: shared_url}。

        阶段 1 实测：search/列表接口**不返回**链接，且 record_url 的 token 与
        record_id 不同（不能构造），只有 batch_get/get 能拿到官方链接。
        链接属展示增强：失败不影响数据快照（调用方兜底）。

        known：上一轮已取到的链接。只对**还缺链接**的 record_id 发请求（差集补取）：
        全量重取会让每张表每轮白白多出 ceil(N/100) 次 batch_get，而链接几乎不变。
        """
        out = {}
        known = known or {}
        ids = [r for r in (record_ids or []) if r and r not in known]
        if not ids:
            return out
        step = max(1, min(config.PORTAL_LINK_BATCH, 100))
        for i in range(0, len(ids), step):
            chunk = ids[i:i + step]
            r = self._req_json(
                "POST",
                f"/open-apis/bitable/v1/apps/{self.cfg['app_token']}"
                f"/tables/{table_id}/records/batch_get",
                body={"record_ids": chunk, "with_shared_url": True,
                      "user_id_type": "open_id"})
            if r.get("code") != 0:
                raise SyncError("api", f"记录链接获取失败 code={r.get('code')}")
            for rec in (r.get("data") or {}).get("records") or []:
                url = rec.get("shared_url")
                if rec.get("record_id") and url:
                    out[rec["record_id"]] = url
        return out


def _load_fields_cache():
    """读字段元数据缓存（meta 表里的 JSON；坏了就当空）。"""
    try:
        data = json.loads(portal_store.get_meta(FIELDS_CACHE_META) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_fields_cache(cache):
    """写字段元数据缓存；写失败不影响本轮同步（下轮重新拉取即可）。"""
    try:
        portal_store.set_meta(
            FIELDS_CACHE_META,
            json.dumps(cache, ensure_ascii=False, sort_keys=True))
    except Exception as e:
        log.warning("字段元数据缓存写失败: %s", e)


def _cached_fields(reader, table_id, force=False):
    """字段元数据（飞书列名 → field_id），带 TTL 缓存。

    TTL 内直接复用，省掉每轮每表一次 fields 请求；force=True 或过期才真正拉取。
    每轮同步最后都会把**实际用到**的对照存进 meta（view.save_sync_meta），
    所以缓存只影响请求数，不影响快照内容。
    """
    cache = _load_fields_cache()
    entry = cache.get(table_id) or {}
    ttl = config.PORTAL_FIELDS_CACHE_TTL_SEC
    fresh = (not force
             and isinstance(entry.get("map"), dict)
             and time.time() - float(entry.get("at") or 0) < ttl)
    if fresh:
        return dict(entry["map"])
    name2id = reader.fields(table_id)
    cache[table_id] = {"at": time.time(), "map": name2id}
    _save_fields_cache(cache)
    return name2id


def run_sync():
    """执行一次完整同步（调用方持有调度锁）。返回 counts / 抛 SyncError。

    流程（缓存库第 4 步起）：拉取飞书 → 保存列对照与记录链接 → 刷新飞书镜像 →
    由 view.rebuild 用「镜像 ⊕ 未落地操作」规范化并发布快照（内容没变不发布）。

    错误类别：config（未配置/字段映射问题）、network（上游不可达/超时）、
    rate_limited、api（飞书返回错误）、parse（数据形状/分页校验）、
    schema（**快照库版本不匹配，读写被拒**——阶段 7 条款）。
    """
    from . import view
    cfg = _load_sync_cfg()
    if not (cfg.get("app_id") and cfg.get("app_secret") and cfg.get("app_token")
            and cfg.get("tables")):
        raise SyncError("config", "飞书同步未配置（app_id/secret/base/tables）")
    try:
        # 版本不匹配时在**第一步**就失败：不建候选、不读上游、不写任何行
        portal_store.next_version()
    except portal_store.SchemaVersionError as e:
        raise SyncError("schema", str(e)[:160])
    started = time.time()
    reader = FeishuReader(cfg)
    raw_items = {}        # 原始记录（飞书列名为键）：写镜像，快照由镜像生成
    field_ids = {}        # 飞书列名 → field_id：本地重建时按它把列名翻译成语义键
    try:
        for t in TABLE_ORDER:
            tcfg = cfg["tables"].get(t) or {}
            tid = tcfg.get("table_id", "")
            if not tid:
                # 该语义表未映射（如新数据源没有"项目/重要节点"表）：
                # 置空表继续，页面显示空态——不再视为配置错误中断整轮同步
                log.info("语义表 %s 未映射（table_id 为空），本次快照置空", t)
                raw_items[t] = []
                continue
            # 字段名 → field_id 对照（TTL 缓存命中时省一次请求）
            name2id = _cached_fields(reader, tid)
            # §6.4.6 结构性校验：配置的字段在源表必须真实存在（字段被删/改
            # 名 → 本次快照失败并保留旧版，而不是把整表读成"缺值"）
            known_ids = set(name2id.values())
            missing = [sem for sem, fid in (tcfg.get("fields") or {}).items()
                       if fid and fid not in known_ids]
            if missing:
                # 对不上不等于配置错：缓存可能滞后（字段刚改名/新增）。
                # 强制重拉一次再判定，避免把「缓存过期」误报成「字段不存在」
                name2id = _cached_fields(reader, tid, force=True)
                known_ids = set(name2id.values())
                missing = [sem for sem, fid in (tcfg.get("fields") or {}).items()
                           if fid and fid not in known_ids]
            if missing:
                raise SyncError("config",
                                f"表 {t} 的配置字段在源表不存在: {missing}")
            raw_items[t] = reader.records(tid)
            field_ids[t] = name2id
        # 记录链接（展示增强）：官方 batch_get 取 shared_url；只补还缺链接的记录，
        # 某表失败时沿用上一轮的链接（链接过期不影响快照）
        links = view._meta_json(view.LINKS_META)
        if config.PORTAL_LINKS_ENABLED:
            for t in TABLE_ORDER:
                if not (cfg["tables"].get(t) or {}).get("table_id") or t in NO_LINK_TABLES:
                    continue
                tids = [it.get("record_id") for it in raw_items[t]]
                try:
                    known = links.get(t) or {}
                    fresh = reader.record_links(
                        cfg["tables"][t]["table_id"], tids, known=known)
                    # 合并新一轮链接，并丢掉本轮已不存在的记录（防 meta 无界增长）
                    now_ids = {r for r in tids if r}
                    links[t] = {rid: url for rid, url in {**known, **fresh}.items()
                                if rid in now_ids}
                except Exception as e:
                    log.warning("表 %s 记录链接获取失败（沿用上一轮链接）: %s", t, e)
        view.save_sync_meta(field_ids, links)
        _refresh_mirror(raw_items, started)
        # 内容哈希门控在 rebuild 里：数据零变化时不发布新版本。否则每轮同步版本号
        # 都会递增，门户轮询把其当作"有更新"整页重渲染并恢复旧滚动快照——
        # 观感即"每次同步抖一下"，且快照库每分钟空写一轮。
        counts = view.rebuild(cfg=cfg, sync_round=True, duration=time.time() - started)
        if counts is None:
            log.info("同步完成（数据无变化，不发布新版本）请求数=%d 耗时=%.1fs",
                     reader.request_count, time.time() - started)
            return {"unchanged": True}
        log.info("同步完成 请求数=%d 耗时=%.1fs 条数=%s",
                 reader.request_count, time.time() - started,
                 {t: counts[t] for t in TABLE_ORDER if t in counts})
        return counts
    except SyncError:
        raise
    except portal_store.SchemaVersionError as e:
        raise SyncError("schema", str(e)[:160])
    except Exception as e:
        raise SyncError("parse", str(e)[:100])


def _refresh_mirror(raw_items, started):
    """把本轮原始记录写进飞书镜像（内容不变则不写库）。

    缓存库第 4 步起页面快照由镜像生成，镜像不可信就不能算同步成功：失败即整轮失败，
    保留上一版快照。
    """
    try:
        if cache_store.replace_remote_tables(raw_items, started):
            log.info("飞书镜像已刷新: %s", {t: len(v) for t, v in raw_items.items()})
        if portal_store.get_meta("remote_mirror_error"):
            portal_store.set_meta("remote_mirror_error", "")
    except portal_store.SchemaVersionError:
        raise
    except Exception as e:
        log.warning("飞书镜像刷新失败: %s", e)
        try:
            portal_store.set_meta("remote_mirror_error", str(e)[:120])
        except Exception:
            pass
        raise SyncError("parse", "飞书镜像刷新失败: " + str(e)[:80])


class Scheduler:
    """单实例后台调度：定时 + 手动合并（限频）+ 单锁互斥。"""

    def __init__(self):
        self._lock = threading.Lock()          # 同步执行互斥（单一任务）
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._last_start = 0.0
        self._last_manual_ok = 0.0
        self._last_manual_by_user = {}     # 按用户的限频窗口（阶段 8）
        self._running = False
        self._fail_streak = 0              # 连续失败轮数（轮级退避用，成功清零）
        self._next_run_at = 0.0            # 下一轮计划时间（/sync-status 展示）

    # ---- 生命周期 ----

    def start(self):
        if not config.PORTAL_SYNC_ENABLED:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="portal-sync")
        self._thread.start()

    def stop(self, timeout=5.0):
        """退出取消（§6.4.10）：置停止标志并 join，不再多触发一轮同步。"""
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)

    def _loop(self):
        # 启动即触发首轮（服务启动不等待飞书：本线程后台自跑）
        self._run_once()
        while not self._stop.is_set():
            self._wake.wait(self._wait_sec())
            self._wake.clear()
            if self._stop.is_set():
                return
            self._run_once()

    def _wait_sec(self):
        """下一轮等待秒数：连续失败按 2 的幂退避（封顶 PORTAL_SYNC_BACKOFF_MAX_SEC）。

        失败也固定间隔全量重跑，会在上游限流/宕机时放大请求；退避让失败期请求量降一个量级，
        恢复成功后 `_fail_streak` 清零，下一轮立刻回到正常间隔。
        """
        base = max(1, config.PORTAL_SYNC_INTERVAL_SEC)
        if not self._fail_streak:
            return base
        return max(base, min(base * (2 ** min(self._fail_streak, 4)),
                             config.PORTAL_SYNC_BACKOFF_MAX_SEC))

    # ---- 执行 ----

    def _run_once(self):
        if not self._lock.acquire(blocking=False):
            return                             # 已在同步中：手动请求自然合并
        self._running = True
        self._last_start = time.time()
        ok = False
        try:
            portal_store.mark_attempt("")
            counts = run_sync()
            self._last_manual_ok = time.time()
            ok = True
            log.info("门户同步完成: %s", counts)
        except SyncError as e:
            portal_store.mark_attempt(e.klass)   # 失败保留旧快照
            log.warning("门户同步失败[%s]: %s", e.klass, e)
        except Exception as e:
            portal_store.mark_attempt("internal:" + str(e)[:60])
            log.exception("门户同步内部异常")
        finally:
            self._running = False
            self._lock.release()
            if ok:
                self._fail_streak = 0
            else:
                self._fail_streak += 1
            self._next_run_at = time.time() + self._wait_sec()

    # ---- 手动刷新（§6.4：合并 + 每用户限频由全局最小间隔承担） ----

    def request_manual(self, user=None):
        """返回 (accepted, detail)。进行中→合并；限频→拒绝（按用户）。

        限频口径（阶段 8）：**每个用户**各自的 `PORTAL_MANUAL_MIN_GAP` 窗口，
        外加一个全局下限 `PORTAL_SYNC_GLOBAL_MIN_GAP`（默认取同一值），
        保证"前端人数不放大飞书请求"这条设计不变。
        """
        now = time.time()
        if not (self._thread and self._thread.is_alive()):
            return False, "同步线程未运行（PORTAL_SYNC_ENABLED=0 或已停止）"
        if self._running:
            return True, "同步进行中，本次请求已合并"
        gap = config.PORTAL_MANUAL_MIN_GAP_SEC
        if user:
            last = self._last_manual_by_user.get(user, 0.0)
            if now - last < gap:
                wait = int(gap - (now - last)) + 1
                return False, f"刷新过于频繁（约 {wait}s 后可再试）"
        ggap = max(config.PORTAL_SYNC_GLOBAL_MIN_GAP_SEC, 0)
        if ggap and now - self._last_start < ggap:
            wait = int(ggap - (now - self._last_start)) + 1
            return False, f"刚刚已刷新过（约 {wait}s 后可再试）"
        if user:
            self._last_manual_by_user[user] = now
            if len(self._last_manual_by_user) > 200:      # 防无界增长
                for k in sorted(self._last_manual_by_user,
                                key=self._last_manual_by_user.get)[:100]:
                    self._last_manual_by_user.pop(k, None)
        self._wake.set()
        return True, "已触发同步"

    def trigger_now(self):
        """测试/内部触发（不等间隔）。"""
        self._wake.set()

    def is_running(self):
        return self._running

    def next_run_in(self):
        """距离下一轮同步的秒数（正在跑返回 0）；供 /sync-status 展示。"""
        if self._running or not self._next_run_at:
            return 0
        return max(0, int(self._next_run_at - time.time()))

    def fail_streak(self):
        """连续失败轮数（0 表示上一轮成功）。"""
        return self._fail_streak

    def is_alive(self):
        """后台同步线程是否在跑（PORTAL_SYNC_ENABLED=0 或已停止时为 False）。"""
        return bool(self._thread and self._thread.is_alive())


_sched = Scheduler()


def scheduler():
    return _sched
