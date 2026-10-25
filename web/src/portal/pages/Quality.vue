<script setup lang="ts">
/* 数据质量：被排除出统计与紧急列表的异常记录（未知枚举 / 失效关联等），
 * 以及网页改动推到飞书的情况——推送失败、排队中、冲突（缓存库第 5 步）。
 * 推送与冲突全员可看；重试 / 放弃 / 确认冲突只有组长看得到按钮，权限以服务端为准。 */
import { computed, onMounted, ref, shallowRef, watch } from "vue";
import PortalPage from "../components/PortalPage.vue";
import ConfirmButton from "../components/ConfirmButton.vue";
import { navNo } from "../nav";
import { ackConflict, discardQueueOp, fetchQuality, fetchSyncQueue, retryQueueOp } from "../api";
import { usePageData } from "../usePageData";
import { requestRefresh, store } from "../store";
import { RouterLink } from "@/shared/router";
import { errorDetail, type SendResult } from "@/shared/http";
import { fmtDT } from "@/shared/format";
import type { QualityIssue, QueueOp, SyncQueue } from "../types";
import { toast } from "@/shared/toast";

const TABLE_LABEL: Record<string, string> = {
  projects: "项目", tasks: "事项", milestones: "重要节点", schedule: "公共日程", procurement: "物资采购",
  members: "成员数据", duty: "值日表",
};
/* 有详情页的表：记录名可点进去看 */
const DETAIL_PATH: Record<string, string> = {
  projects: "/projects/", tasks: "/tasks/", schedule: "/schedule/", procurement: "/procurement/",
};

const { body } = usePageData(fetchQuality);
const rows = computed<QualityIssue[]>(() => (body.value && body.value.data) || []);
const summary = computed(() => {
  const b = body.value;
  if (!b) return "";
  const parts = Object.keys(b.by_table || {}).map((k) => (TABLE_LABEL[k] || k) + " " + (b.by_table || {})[k]);
  return "共 " + (b.total || 0) + " 条异常记录（" + (parts.join(" · ") || "无") + "）。这些记录已排除出统计与紧急列表。";
});

/* ---- 推送队列与冲突：与页面数据同一刷新节奏（轮询发现变化 / 写入后 / 手动刷新） ---- */
const queue = shallowRef<SyncQueue | null>(null);
let queueFailed = false;          // 上次读取是否失败：连续失败只弹一次通知
let gen = 0;
async function loadQueue(): Promise<void> {
  const my = ++gen;
  try {
    const res = await fetchSyncQueue();
    if (my !== gen) return;
    queue.value = res.data;
    queueFailed = false;
  } catch (e) {
    if (my !== gen) return;
    if (!queueFailed) toast("推送队列读取失败（" + (e instanceof Error ? e.message : "未知错误") + "）", "error");
    queueFailed = true;
  }
}
onMounted(loadQueue);
watch(() => store.refreshSeq, loadQueue);

const canManage = computed(() => !!(queue.value && queue.value.can_manage));
const queueSummary = computed(() => {
  const q = queue.value;
  if (!q) return "";
  const c = q.cache;
  const mode = c.write_mode === "cache"
    ? "先存本地再推送到飞书（" + (c.cache_tables.map((t) => TABLE_LABEL[t] || t).join("、") || "暂无表") + "）"
    : "直接写入飞书";
  const bits = ["写入方式：" + mode];
  bits.push("推送线程" + (c.pusher_alive ? "运行中" : "未运行"));
  bits.push("待推送 " + ((c.pending || 0) + (c.pushing || 0)) + " · 失败 " + (c.failed || 0) +
    " · 冲突待确认 " + (c.open_conflicts || 0));
  return bits.join("。") + "。";
});

function tableLabel(t: string): string {
  return TABLE_LABEL[t] || t;
}
function detailLink(table: string, id: string): string | null {
  return DETAIL_PATH[table] && id ? DETAIL_PATH[table] + encodeURIComponent(id) : null;
}
function recordName(x: { name: string; record_id: string }): string {
  return x.name || "…" + x.record_id.slice(-6);
}
function opDetail(o: QueueOp): string {
  return o.columns.length ? o.columns.join("、") : "—";
}

/* ---- 组长操作 ---- */
const busy = ref("");
async function act(key: string, call: () => Promise<SendResult<{ count?: number; detail?: string }>>,
                   done: (n: number) => string) {
  busy.value = key;
  try {
    const res = await call();
    if (res.ok) {
      toast(done(res.body.count || 1), "ok");
      requestRefresh();
    } else {
      toast("操作失败：" + errorDetail(res), "error");
      void loadQueue();
    }
  } catch {
    toast("网络错误，操作未执行", "error");
  } finally {
    busy.value = "";
  }
}
const retry = (o: QueueOp) => act("r" + o.id, () => retryQueueOp(o.id),
  (n) => "已重新排队 " + n + " 条，正在推送到飞书");
const discard = (o: QueueOp) => act("d" + o.id, () => discardQueueOp(o.id),
  (n) => "已放弃 " + n + " 条改动，页面已回到飞书上的值");
const ack = (id: number) => act("a" + id, () => ackConflict(id), () => "已确认这条冲突");
</script>

<template>
  <PortalPage :no="navNo('/quality')" en="DIAGNOSTICS │ QUALITY" title="数据质量" desc="被排除的记录 │ 推送失败与冲突 │ 去飞书修正后自动恢复" watermark="QUALITY">
    <section class="module enter">
      <div class="module-head"><span class="module-tag">EXCLUDED / 01</span><h2>被排除的记录</h2></div>
      <div class="module-body">
        <p id="quality-summary" class="note">{{ summary }}</p>
        <div id="quality-list">
          <p v-if="body && !rows.length" class="empty">没有质量异常记录</p>
          <div v-else-if="rows.length" class="tbl-wrap">
            <table class="tbl">
              <thead><tr><th>数据表</th><th>记录 ID</th><th>原因</th></tr></thead>
              <tbody>
                <tr v-for="(q, i) in rows" :key="(q.table || '') + (q.record_id || '') + i">
                  <td>{{ TABLE_LABEL[q.table || ""] || q.table || "—" }}</td>
                  <td class="t-mono">{{ q.record_id || "—" }}</td>
                  <td>{{ q.issue || "—" }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>

    <section id="push-queue" class="module enter">
      <div class="module-head">
        <span class="module-tag">PUSH / 02</span><h2>网页改动推送</h2>
        <span class="module-sub">推送失败的改动不会显示在页面上，需要组长重试或放弃</span>
      </div>
      <div class="module-body">
        <p v-if="queue" id="queue-summary" class="note">{{ queueSummary }}</p>
        <template v-if="queue">
          <p v-if="!queue.failed.length && !queue.pending.length" class="empty">没有待推送或推送失败的改动</p>
          <div v-if="queue.failed.length" class="tbl-wrap">
            <table id="queue-failed" class="tbl cards-sm">
              <thead><tr><th>状态</th><th>数据表</th><th>记录</th><th>操作</th><th>改动的列</th><th>原因</th><th>提交人</th><th>提交时间</th><th v-if="canManage">处理</th></tr></thead>
              <tbody>
                <tr v-for="o in queue.failed" :key="o.id" :data-op="o.id">
                  <td data-label="状态"><span class="badge danger sm">推送失败</span></td>
                  <td data-label="数据表">{{ tableLabel(o.table) }}</td>
                  <td data-label="记录">
                    <RouterLink v-if="o.op !== 'create' && detailLink(o.table, o.record_id)" :to="detailLink(o.table, o.record_id) || ''" class="lnk">{{ recordName(o) }}</RouterLink>
                    <template v-else>{{ recordName(o) }}</template>
                  </td>
                  <td data-label="操作">{{ o.op_text }}</td>
                  <td data-label="改动的列">{{ opDetail(o) }}</td>
                  <td data-label="原因" class="q-reason">{{ o.error || "—" }}</td>
                  <td data-label="提交人">{{ o.created_by }}</td>
                  <td data-label="提交时间" class="t-num">{{ fmtDT(o.created_at, "—") }}</td>
                  <td v-if="canManage" data-label="处理" class="q-actions">
                    <div class="q-btns">
                      <button type="button" class="btn ghost xs" :disabled="busy === 'r' + o.id" @click="retry(o)">重试</button>
                      <ConfirmButton label="放弃" armed-label="再点一次放弃" danger :busy="busy === 'd' + o.id" @confirm="discard(o)" />
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="queue.pending.length" class="tbl-wrap">
            <table id="queue-pending" class="tbl cards-sm">
              <thead><tr><th>状态</th><th>数据表</th><th>记录</th><th>操作</th><th>改动的列</th><th>上次推送</th><th>提交人</th><th>提交时间</th><th v-if="canManage">处理</th></tr></thead>
              <tbody>
                <tr v-for="o in queue.pending" :key="o.id" :data-op="o.id">
                  <td data-label="状态"><span class="badge neutral sm">{{ o.state === "pushing" ? "推送中" : "待推送" }}</span></td>
                  <td data-label="数据表">{{ tableLabel(o.table) }}</td>
                  <td data-label="记录">
                    <RouterLink v-if="detailLink(o.table, o.record_id)" :to="detailLink(o.table, o.record_id) || ''" class="lnk">{{ recordName(o) }}</RouterLink>
                    <template v-else>{{ recordName(o) }}</template>
                  </td>
                  <td data-label="操作">{{ o.op_text }}</td>
                  <td data-label="改动的列">{{ opDetail(o) }}</td>
                  <td data-label="上次推送" class="q-reason">{{ o.error ? "没成功，会自动重试：" + o.error : "—" }}</td>
                  <td data-label="提交人">{{ o.created_by }}</td>
                  <td data-label="提交时间" class="t-num">{{ fmtDT(o.created_at, "—") }}</td>
                  <td v-if="canManage" data-label="处理" class="q-actions">
                    <ConfirmButton v-if="o.state !== 'pushing'" label="放弃" armed-label="再点一次放弃" danger :busy="busy === 'd' + o.id" @confirm="discard(o)" />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>
    </section>

    <section id="conflicts" class="module enter">
      <div class="module-head">
        <span class="module-tag">CONFLICT / 03</span><h2>冲突</h2>
        <span class="module-sub">网页和飞书改了同一处，一律以飞书为准；确认只是表示已知悉</span>
      </div>
      <div class="module-body">
        <template v-if="queue">
          <p v-if="!queue.conflicts.length" class="empty">没有待确认的冲突</p>
          <div v-else class="tbl-wrap">
            <table id="conflict-list" class="tbl cards-sm">
              <thead><tr><th>数据表</th><th>记录</th><th>情况</th><th>列</th><th>网页上的改动（未生效）</th><th>飞书上的值（现行）</th><th>发现时间</th><th v-if="canManage">处理</th></tr></thead>
              <tbody>
                <tr v-for="c in queue.conflicts" :key="c.id" :data-conflict="c.id">
                  <td data-label="数据表">{{ tableLabel(c.table) }}</td>
                  <td data-label="记录">
                    <RouterLink v-if="c.kind !== 'remote_deleted' && detailLink(c.table, c.record_id)" :to="detailLink(c.table, c.record_id) || ''" class="lnk">{{ recordName(c) }}</RouterLink>
                    <template v-else>{{ recordName(c) }}</template>
                  </td>
                  <td data-label="情况">{{ c.kind_text }}</td>
                  <td data-label="列">{{ c.field || "—" }}</td>
                  <td data-label="网页上的改动" class="q-reason">{{ c.local }}</td>
                  <td data-label="飞书上的值" class="q-reason">{{ c.remote }}</td>
                  <td data-label="发现时间" class="t-num">{{ fmtDT(c.detected_at, "—") }}</td>
                  <td v-if="canManage" data-label="处理" class="q-actions">
                    <button type="button" class="btn ghost xs" :disabled="busy === 'a' + c.id" @click="ack(c.id)">确认</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>
    </section>
  </PortalPage>
</template>

<style scoped>
.q-reason { max-width: 280px; overflow-wrap: anywhere; }
.q-actions { white-space: nowrap; }
.q-btns { display: flex; flex-wrap: wrap; gap: 6px; }
#push-queue .tbl-wrap + .tbl-wrap { margin-top: 12px; }
</style>
