"""本地视图快照：飞书镜像 ⊕ 未落地操作 → 规范化 → 发布（缓存库第 4 步）。

页面读到的快照一律由这里生成：
- 每轮同步：拉取飞书 → 刷新镜像 → 在这里重建并发布（publish，带同步统计）；
- 网页写入、推送线程推完一批、零点任务入队、组长处理队列与冲突之后：不访问飞书，
  在这里本地重建（publish_local，不动同步统计）——写完立即可见，冲突回退也立即反映。
每条记录还带上同步标记 sync（待同步 / 同步失败 / 冲突），标记变了快照也随之换版本，
门户轮询据此刷新。
内容没变（哈希相同）不发布新版本，门户轮询不会无谓刷新。

本地重建需要两样只有同步时才拿得到的东西，每轮同步时存进 meta：
飞书列名 → field_id 对照（规范化按 field_id 认列）、记录的飞书链接。
"""
import hashlib
import json
import logging
import threading

from . import cache_store, store
from . import normalize as portal_normalize

log = logging.getLogger(__name__)

TABLE_ORDER = store.TABLE_ORDER
FIELD_IDS_META = "field_ids"       # {表: {飞书列名: field_id}}
LINKS_META = "record_links"        # {表: {record_id: 飞书记录链接}}

# 同步与本地重建共用：候选版本号的分配、写入与发布必须串行
_build_lock = threading.Lock()


def _meta_json(key):
    try:
        return json.loads(store.get_meta(key) or "{}")
    except ValueError:
        return {}


def save_sync_meta(field_ids, links):
    """保存本轮同步拿到的列对照与记录链接（内容不变不写库）。"""
    for key, val in ((FIELD_IDS_META, field_ids), (LINKS_META, links)):
        text = json.dumps(val, ensure_ascii=False, sort_keys=True)
        if store.get_meta(key) != text:
            store.set_meta(key, text)


def content_digest(recs, quality):
    """快照内容指纹（与发布门控同一口径：记录按 id 排序 + 质量报告）。"""
    payload = json.dumps({
        t: sorted(recs[t], key=lambda r: str(r.get("id", "")))
        for t in TABLE_ORDER
    } | {"quality": {t: sorted(quality[t]) for t in TABLE_ORDER}},
        sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _people_names(views):
    """镜像里出现过的人员 open_id → 姓名（排队中的人员列只有 id，用它补姓名）。"""
    names = {}
    for rows in views.values():
        for _, f in rows:
            for v in f.values():
                if isinstance(v, list):
                    for u in v:
                        if isinstance(u, dict) and u.get("id") and u.get("name"):
                            names.setdefault(u["id"], u["name"])
    return names


def _fill_names(fields, names):
    out = {}
    for k, v in fields.items():
        if (isinstance(v, list) and v
                and all(isinstance(u, dict) and u.get("id") for u in v)
                and any(not u.get("name") for u in v)):
            v = [dict(u, name=u.get("name") or names.get(u["id"], "")) for u in v]
        out[k] = v
    return out


_OP_TEXT = {"create": "新建", "update": "修改", "delete": "删除"}


def _short(text, n=60):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def sync_mark(m):
    """把 cache_store.record_marks 的一条原料变成页面显示的标记（缓存库第 5 步）。

    优先级：同步失败 > 冲突 > 待同步。返回 {"state", "label", "text"}；没有标记返回 None。
    失败与冲突要组长处理，文案里指明去哪里处理。
    """
    if m["failed"]:
        f = m["failed"][-1]
        return {"state": "failed", "label": "同步失败",
                "text": "这条记录的%s没能推到飞书（%s），页面显示的是飞书上的值；"
                        "组长可在「数据质量」页重试或放弃这次改动。"
                        % (_OP_TEXT.get(f["op"], "改动"), _short(f["error"]))}
    if m["conflicts"]:
        cols = [c["field"] for c in m["conflicts"] if c.get("field")]
        if cols:
            what = "「%s」在飞书上也被改过" % "、".join(dict.fromkeys(cols))
        elif any(c["kind"] == "delete_blocked" for c in m["conflicts"]):
            what = "飞书上这条记录被改过，网页上的删除没有执行"
        else:
            what = "飞书上这条记录也被改过"
        return {"state": "conflict", "label": "冲突",
                "text": what + "，已按飞书为准；组长可在「数据质量」页查看并确认。"}
    if m["pending"]:
        text = "改动已保存在门户，正在推送到飞书。"
        if m["retry_error"]:
            text += "上次推送没成功（%s），会自动重试。" % _short(m["retry_error"])
        return {"state": "pending", "label": "待同步", "text": text}
    return None


def rebuild(cfg=None, sync_round=False, duration=0.0):
    """重建并发布快照。返回 counts（发布了新版本）或 None（内容没变 / 还不能本地重建）。

    sync_round=True 由同步调用：内容没变时记一次同步成功；发布时带同步统计。
    本地重建要求每张已映射的表都同步过（有列对照），否则什么也不做，等首轮同步。
    """
    from . import sync
    cfg = cfg or sync._load_sync_cfg()
    tables_cfg = cfg.get("tables") or {}
    mapped = [t for t in TABLE_ORDER if (tables_cfg.get(t) or {}).get("table_id")]
    field_ids = _meta_json(FIELD_IDS_META)
    if any(t not in field_ids for t in mapped):
        if sync_round:
            raise RuntimeError("列对照缺失：%s" % [t for t in mapped if t not in field_ids])
        return None
    links = _meta_json(LINKS_META)
    with _build_lock:
        views = cache_store.table_views(TABLE_ORDER)
        marks = cache_store.record_marks(TABLE_ORDER)
        names = _people_names(views)
        raw = {}
        for t in TABLE_ORDER:
            if t not in mapped:
                raw[t] = []
                continue
            items = [{"record_id": rid, "fields": _fill_names(f, names)}
                     for rid, f in views[t]]
            raw[t] = sync.FeishuReader.translate_keys(
                items, field_ids.get(t) or {}, tables_cfg[t].get("fields") or {})
        recs, quality = portal_normalize.Normalizer(tables_cfg).normalize_all(raw)
        for t in TABLE_ORDER:
            lk = links.get(t) or {}
            mk = marks.get(t) or {}
            for r in recs[t]:
                if lk.get(r["id"]):
                    r["url"] = lk[r["id"]]
                mark = sync_mark(mk[r["id"]]) if r["id"] in mk else None
                if mark:
                    r["sync"] = mark
        digest = content_digest(recs, quality)
        if store.get_meta("content_hash") == digest:
            if sync_round:
                store.mark_success(duration)
            return None
        version = store.next_version()
        try:
            for t in TABLE_ORDER:
                store.write_candidate(version, t, recs[t], quality[t])
        except Exception:
            store.discard_candidate(version)
            raise
        counts = {t: len(recs[t]) for t in TABLE_ORDER}
        counts["quality_issues"] = sum(len(v) for v in quality.values())
        if sync_round:
            store.publish(version, counts, duration)
        else:
            store.publish_local(version, counts)
        store.set_meta("content_hash", digest)
        return counts


def refresh():
    """本地重建（写入之后调用）：失败只记日志——写入本身已经成功，
    页面最迟在下一轮同步后也会更新。"""
    try:
        return rebuild()
    except Exception:
        log.warning("本地视图重建失败（下一轮同步会补上）", exc_info=True)
        return None
