"""网页写入的统一出口：按开关决定直写飞书，还是先进本地队列由推送线程推送（缓存库第 3 步）。

- PORTAL_WRITE_MODE=direct（默认）：直写飞书，行为与之前一致；写成功后顺手更新镜像，
  让以后排队的编辑拿到新鲜的基准。例外：该记录还有未落地的排队操作时也必须排队，
  否则会插到前面的操作之前写进飞书；
- PORTAL_WRITE_MODE=cache 且表在 PORTAL_CACHE_TABLES 里：入队后立即返回，
  新建返回本地临时 ID（loc_ 开头）；
- 冲突规则与队列状态见 cache_rules / cache_store，设计见 docs/portal-cache-design.md。
"""
import time

from .. import config
from . import cache_rules as rules
from . import cache_store
from . import writer as portal_writer

# 门户可写的列（删除前要核对的列）：与 api.py 里各表的列构造函数同源。
# 附件列不在内：附件始终直写飞书并写回镜像，不经队列。
WATCH_COLS = {
    "tasks": ("事项名称", "具体事项", "组别", "优先级", "负责人", "计划开始", "计划截止",
              "提交验收时间", "验收时间", "执行状态", "验收结论", "所属项目"),
    "projects": ("项目名称", "项目说明", "主责组", "协作组", "负责人", "计划开始", "计划结束",
                 "项目状态"),
    "schedule": ("日程名称", "日程类型", "开始时间", "结束时间", "涉及组别", "人员", "地点",
                 "日程状态", "安排详情"),
    "procurement": ("采购物资名称", "申请组别", "购物链接", "用途说明", "赛季", "一级分类",
                    "二级分类", "采购编号", "采购日期", "单价（元）", "数量", "采购人",
                    "组长批复", "已采购", "发票已开"),
    "members": ("最近签到", "最近签退", "有效时长（小时）"),
}

# 直接覆盖、不做冲突判断的列（规则 6）：成员同步脚本每 5 分钟也会写这两列
FORCE_COLS = {"members": ("同步时间", "同步说明")}


def queued(table):
    """该表的写入是否走队列。"""
    return config.PORTAL_WRITE_MODE == "cache" and table in config.PORTAL_CACHE_TABLES


def wake():
    # 延迟导入：pusher 依赖本模块的列配置
    from . import pusher
    pusher.pusher().wake()


def changed(queued):
    """本地数据变了：重建快照让页面立即看到（缓存库第 4 步）；排队的唤醒推送线程。"""
    from . import view
    view.refresh()
    if queued:
        wake()


def _must_queue(table, record_id):
    return queued(table) or cache_store.has_open_ops(table, record_id)


def create(table, cols, by=None):
    """新建：返回 (record_id, 是否排队)。排队时 record_id 是本地临时 ID。"""
    if queued(table):
        lid = cache_store.enqueue_create(table, cols, by=by)
        changed(True)
        return lid, True
    rid = portal_writer.FeishuWriter().create(table, cols)
    _quietly(cache_store.put_mirror, table, rid, cols)
    changed(False)
    return rid, False


def update(table, record_id, cols, by=None):
    """修改：返回是否排队。记录不在本地（排队时）抛 cache_store.RecordUnknown。"""
    if _must_queue(table, record_id):
        cache_store.enqueue_update(table, record_id, cols, by=by)
        changed(True)
        return True
    portal_writer.FeishuWriter().update(table, record_id, cols)
    _quietly(cache_store.patch_mirror, table, record_id, cols)
    changed(False)
    return False


def delete(table, record_id, by=None):
    """删除：返回是否排队（新建还没推出去就删掉的，本地直接作废，也算排队处理）。"""
    if _must_queue(table, record_id):
        cache_store.enqueue_delete(table, record_id, WATCH_COLS.get(table, ()), by=by)
        changed(True)
        return True
    portal_writer.FeishuWriter().delete(table, record_id)
    _quietly(cache_store.drop_mirror, table, record_id)
    changed(False)
    return False


def resolve_remote(record_id, timeout=0.0):
    """临时 ID → 飞书 record_id；还没推出去时最多等 timeout 秒，仍没有返回 None。

    采购新建后前端紧接着上传附件：附件必须挂在飞书记录上，所以等推送线程推完这条新建。
    """
    if not rules.is_local_id(record_id):
        return record_id
    deadline = time.time() + timeout
    while True:
        rid = cache_store.resolve_id(record_id)
        if not rules.is_local_id(rid):
            return rid
        if time.time() >= deadline:
            return None
        wake()
        time.sleep(0.2)


def _quietly(fn, *args):
    """镜像写回是锦上添花：飞书已经写成功了，镜像失败不能把这次写入报成失败。"""
    try:
        fn(*args)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("直写后更新镜像失败（下一轮同步会补上）", exc_info=True)
