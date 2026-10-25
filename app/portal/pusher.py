"""推送线程：把待推送队列里的网页改动按序推到飞书（缓存库第 3 步）。

每条操作的流程：读飞书上该记录的当前值 → 按 cache_rules 算出该推什么 → 写飞书 →
重新读回（拿到公式列等飞书算出来的值）→ 写回镜像、更新队列状态。

失败处理：
- 网络 / 限流 / 飞书临时性错误码 → 退避重试（2、4、8…秒，上限 PORTAL_PUSH_MAX_BACKOFF）；
- 配置类错误（表未映射、权限不足）→ 按退避上限慢慢重试，等运维修好；
- 飞书明确拒绝（字段不存在、类型不符等）→ 置为 failed，不再重试，等人处理；
- 程序意外异常 → 重试几次后置为 failed；
- 冲突一律飞书优先，记进冲突表（规则见 cache_rules）。

单进程单实例（与同步调度器一样）：服务以单 worker 运行，见 deploy.py 的校验。
"""
import logging
import threading
import time

from .. import config
from . import cache_rules as rules, cache_store, directives, store
from . import writer as portal_writer
from . import writes
from .sync import SyncError

log = logging.getLogger(__name__)

# 飞书侧的临时性业务错误码：限流、同表并发写冲突、数据未就绪、处理超时
RETRYABLE_CODES = {1254290, 1254291, 1254607, 1255040}
# 程序意外异常最多试几次就置为失败，免得一个缺陷让队列无限空转
MAX_UNEXPECTED_ATTEMPTS = 5


def backoff(attempts):
    """第 n 次失败后的等待秒数：2^n，封顶 PORTAL_PUSH_MAX_BACKOFF。"""
    return min(config.PORTAL_PUSH_MAX_BACKOFF_SEC, 2.0 ** min(max(int(attempts), 1), 12))


class Pusher:
    """后台推送线程；client_factory 默认是飞书写客户端（测试可注入假客户端）。"""

    def __init__(self, client_factory=None):
        self._client_factory = client_factory or portal_writer.FeishuWriter
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._need_sync = False
        self.pushed = 0

    # ---- 生命周期 ----

    def start(self):
        if not config.PORTAL_PUSH_ENABLED:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="portal-push")
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)

    def wake(self):
        self._wake.set()

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def _loop(self):
        try:
            n = cache_store.requeue_stuck(older_than=0)    # 单进程：启动时的 pushing 都是上次中断的
            if n:
                log.warning("推送线程启动：%d 条上次中断的操作已重新排队", n)
        except store.SchemaVersionError as e:
            log.error("快照库版本不匹配，推送线程不工作: %s", e)
            return
        except Exception:
            log.exception("推送线程启动检查失败（继续运行）")
        while not self._stop.is_set():
            try:
                self.drain()
            except Exception:
                log.exception("推送循环异常（线程继续存活）")
            self._maybe_sync()
            self._wake.wait(self._idle_wait())
            self._wake.clear()

    def _idle_wait(self):
        idle = config.PORTAL_PUSH_IDLE_SEC
        if self._need_sync:
            idle = min(idle, 2.0)
        try:
            due = cache_store.next_due()
        except Exception:
            return idle
        if due is None:
            return idle
        return max(0.05, min(idle, due - time.time()))

    # ---- 推送 ----

    def drain(self, limit=100):
        """把当前可推的操作推完（单轮最多 limit 条），返回处理条数。"""
        n = 0
        while n < limit and not self._stop.is_set():
            op = cache_store.claim_next()
            if op is None:
                break
            self.process(op)
            # 每条推完（含冲突回退为飞书值、失败）立即本地重建，页面不必等同步
            from . import view
            view.refresh()
            n += 1
        return n

    def process(self, op):
        try:
            if not self._resolve_links(op):
                return
            client = self._client_factory()
            handler = {"create": self._create, "update": self._update,
                       "delete": self._delete}[op["op"]]
            if handler(client, op):
                self._need_sync = True
                self.pushed += 1
        except SyncError as e:
            self._on_sync_error(op, e)
        except Exception as e:
            log.exception("推送操作 #%s 内部异常", op["id"])
            msg = "内部错误: " + str(e)[:150]
            if op["attempts"] >= MAX_UNEXPECTED_ATTEMPTS:
                cache_store.mark_failed(op["id"], msg)
            else:
                cache_store.mark_retry(op["id"], msg, backoff(op["attempts"]))

    def _resolve_links(self, op):
        """关联列里的本地临时 ID 换成飞书 ID（关联到还没推出去的新建记录时）。

        对方还在队列里 → 2 秒后再试；对方的新建已失败 → 本条置为失败。
        返回 True 表示可以继续推送（op 的字段与基准已就地替换）。
        """
        pending, failed = [], []

        def swap(v):
            if isinstance(v, str) and rules.is_local_id(v):
                st = cache_store.local_state(v)
                if st == "mapped":
                    return cache_store.resolve_id(v)
                (pending if st == "pending" else failed).append(v)
                return v
            if isinstance(v, list):
                return [swap(x) for x in v]
            if isinstance(v, dict) and isinstance(v.get("record_ids"), list):
                return dict(v, record_ids=[swap(x) for x in v["record_ids"]])
            return v

        for key in ("fields", "base"):
            op[key] = {k: swap(v) for k, v in (op.get(key) or {}).items()}
        if failed:
            cache_store.mark_failed(op["id"], "关联的新建记录没有推送成功：%s" % failed[0])
            return False
        if pending:
            cache_store.mark_retry(op["id"], "等待关联的新建记录先推到飞书", 2.0)
            return False
        return True

    def _on_sync_error(self, op, e):
        tag = "%s/%s#%s" % (op["table_name"], op["op"], op["id"])
        if e.klass in ("network", "rate_limited") or e.code in RETRYABLE_CODES:
            delay = backoff(op["attempts"])
            cache_store.mark_retry(op["id"], "%s: %s" % (e.klass, e), delay)
            log.warning("推送 %s 暂时失败，%.0f 秒后重试: %s", tag, delay, e)
        elif e.klass == "config":
            cache_store.mark_retry(op["id"], "config: %s" % e, config.PORTAL_PUSH_MAX_BACKOFF_SEC)
            log.warning("推送 %s 配置类错误，稍后重试: %s", tag, e)
        else:
            cache_store.mark_failed(op["id"], "%s: %s" % (e.klass, e))
            log.error("推送 %s 被飞书拒绝，已置为失败: %s", tag, e)

    @staticmethod
    def _refetch(client, table, rid, fallback):
        """写完重新读回（补公式列与修改时间）；读失败不影响结果，退回本地拼出的字段。"""
        try:
            rec = client.get(table, rid)
        except SyncError as e:
            log.info("推送后读回 %s/%s 失败（下一轮同步会补上）: %s", table, rid, e)
            return fallback, None
        if rec is None:
            return fallback, None
        return rec.get("fields") or {}, rec.get("last_modified_time")

    def _create(self, client, op):
        table = op["table_name"]
        fields, dirs = rules.split_directives(op["fields"])
        fields.update(directives.evaluate(dirs, {}))
        rid = client.create(table, fields, client_token=op["client_token"])
        if not rid:
            raise SyncError("api", "新建成功但飞书未返回 record_id")
        remote, mod = self._refetch(client, table, rid, fields)
        cache_store.complete_create(op["id"], rid, remote, mod)
        log.info("推送新建 %s %s → %s", table, op["record_id"], rid)
        return True

    def _update(self, client, op):
        table, rid = op["table_name"], op["remote_id"]
        if not rid:
            cache_store.mark_failed(op["id"], "依赖的新建没有推送成功，这条修改无法推送")
            return False
        rec = client.get(table, rid)
        remote = (rec.get("fields") or {}) if rec else None
        plain, dirs = rules.split_directives(op["fields"])
        plan = rules.plan_update(op["base"], plain, remote, op["increments"],
                                 force=writes.FORCE_COLS.get(table, ()))
        if plan.remote_missing:
            cache_store.reject(op["id"], plan.conflicts,
                               note="飞书侧记录已删除，本地修改作废", remote_gone=True)
            log.warning("推送修改 %s/%s：飞书侧已删除，本地修改作废", table, rid)
            return False
        push = dict(plan.push)
        push.update(directives.evaluate(dirs, remote))
        if push:
            client.update(table, rid, push)
            fields, mod = self._refetch(client, table, rid, dict(remote, **push))
        else:
            fields, mod = remote, rec.get("last_modified_time")
        cache_store.complete_update(op["id"], push, plan.conflicts,
                                    remote_fields=fields, modified_at=mod)
        if plan.conflicts:
            log.warning("推送修改 %s/%s：%d 列冲突，已按飞书为准", table, rid, len(plan.conflicts))
        return bool(push)

    def _delete(self, client, op):
        table, rid = op["table_name"], op["remote_id"]
        if not rid:
            cache_store.mark_failed(op["id"], "依赖的新建没有推送成功，这条删除无法推送")
            return False
        rec = client.get(table, rid)
        plan = rules.plan_delete(op["base"], (rec.get("fields") or {}) if rec else None)
        if plan.action == "blocked":
            cache_store.reject(op["id"], plan.conflicts, note="飞书侧已修改，删除未执行")
            log.warning("推送删除 %s/%s：飞书侧已修改，删除未执行", table, rid)
            return False
        if plan.action == "delete":
            client.delete(table, rid)
        cache_store.complete_delete(op["id"])
        return plan.action == "delete"

    # ---- 推完触发同步 ----

    def _maybe_sync(self):
        """推送成功后请求一轮同步，让快照尽快反映飞书上的新数据。

        同步正在跑时不并入（那一轮可能在推送前就读完了），等它结束再请求；
        被全局限频拒绝也保留标记，下一圈再试。
        """
        if not self._need_sync:
            return
        from . import sync
        sched = sync.scheduler()
        if not sched.is_alive():
            self._need_sync = False
            return
        if sched.is_running():
            return
        accepted, _ = sched.request_manual(None)
        if accepted:
            self._need_sync = False


_pusher = Pusher()


def pusher():
    return _pusher
