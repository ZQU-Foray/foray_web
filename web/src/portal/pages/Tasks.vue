<script setup lang="ts">
/* 事项列表：主责组 / P 等级 / 状态 / 负责人 / 项目 五组筛选（状态在 store，跨页保留）。
 * 组长标记已完成的事项即归档，默认折叠在列表末尾，点「展开」才显示；状态筛选选「已完成」时直接列出。 */
import { computed, onMounted, ref } from "vue";
import PortalPage from "../components/PortalPage.vue";
import SyncMark from "../components/SyncMark.vue";
import { navNo } from "../nav";
import ChipFilter, { type ChipOption } from "../components/ChipFilter.vue";
import SrcLink from "../components/SrcLink.vue";
import DueTag from "../components/DueTag.vue";
import { RouterLink } from "@/shared/router";
import { fetchProjectsAll, fetchTasks, fetchTasksAll } from "../api";
import { groups, isLeader, openForm, store } from "../store";
import { usePageData } from "../usePageData";
import type { Task, UserRef } from "../types";
import { ACCEPTED, isAccepted, statusText } from "../taskStatus";

const f = store.taskFilters;
const { body } = usePageData(() => fetchTasks(f), [() => ({ ...f })]);
const rows = computed<Task[]>(() => (body.value && body.value.data) || []);
/* 筛选本身就是「已完成」时不折叠 */
const foldAccepted = computed(() => f.status !== ACCEPTED);
const activeRows = computed(() => (foldAccepted.value ? rows.value.filter((t) => !isAccepted(t)) : rows.value));
const acceptedRows = computed(() => (foldAccepted.value ? rows.value.filter(isAccepted) : []));
const showAccepted = ref(false);
const invalidCount = computed(() => rows.value.filter((t) => t.invalid).length);

const ALL: ChipOption = { id: "全部", label: "全部" };
const groupOpts = computed(() => [ALL].concat(groups().map((g) => ({ id: g, label: g }))));
const priOpts = ["全部", "P0", "P1", "P2", "P3"].map((v) => ({ id: v, label: v }));
const statusOpts = ["全部", "未开始", "进行中", "受阻", ACCEPTED, "已取消"].map((v) => ({ id: v, label: v }));
const ownerOpts = computed(() => [ALL].concat(store.ownerOptions || []));
const projectOpts = computed(() => [ALL].concat(store.projectOptions || []));

/* 负责人 / 项目筛选项：一次性取全量后缓存（含归档项目，便于按旧项目回查） */
async function ensureFilterOptions() {
  if (store.ownerOptions && store.projectOptions) return;
  try {
    const [ts, ps] = await Promise.all([fetchTasksAll(), fetchProjectsAll()]);
    const seen: Record<string, 1> = {};
    const owners: ChipOption[] = [];
    (ts.data || []).forEach((t) => (t.owners || []).forEach((u) => {
      if (u.id && !seen[u.id]) { seen[u.id] = 1; owners.push({ id: u.id, label: u.name || "…" + u.id.slice(-4) }); }
    }));
    owners.sort((a, b) => a.label.localeCompare(b.label, "zh"));
    store.ownerOptions = owners;
    store.projectOptions = (ps.data || []).map((p) => ({ id: p.id, label: p.name }));
  } catch {
    store.ownerOptions = store.ownerOptions || [];
    store.projectOptions = store.projectOptions || [];
  }
}
onMounted(ensureFilterOptions);

function names(list?: UserRef[]): string {
  return (list || []).map((u) => u.name || "…" + u.id.slice(-4)).join("、") || "待分配";
}

/* 「项目」列显示项目名（不显示原始 record_id）；残留关联不静默 */
function projectName(t: Task): string {
  if (t.project_name) return t.project_name;
  if (!t.project_id) return "—";
  const opts = store.projectOptions;
  if (!opts) return t.project_id;
  const hit = opts.find((o) => o.id === t.project_id);
  return hit ? hit.label : "项目已移除";
}

function statusTone(s?: string | null): string {
  if (s === "已完成") return "neutral";
  if (s === "受阻") return "warn";
  if (s === "进行中" || s === "待验收") return "ok";
  if (s === "已取消") return "neutral";
  return "neutral";
}
</script>

<template>
  <PortalPage :no="navNo('/tasks')" en="TASKS" title="事项" desc="五组事项 │ 截止与逾期 │ 点事项进入详情" watermark="TASKS">
    <template v-if="isLeader()" #actions>
      <button type="button" class="btn" @click="openForm('task')">＋ 新建事项</button>
    </template>

    <div class="filters">
      <ChipFilter id="group-filter" v-model="f.group" label="主责组" aria-label="按主责组筛选" :options="groupOpts" />
      <ChipFilter id="pri-filter" v-model="f.priority" label="P 等级" aria-label="按 P 等级筛选" :options="priOpts" />
      <ChipFilter id="status-filter" v-model="f.status" label="状态" aria-label="按状态筛选" :options="statusOpts" />
      <ChipFilter id="owner-filter" v-model="f.owner" label="负责人" aria-label="按负责人筛选" :options="ownerOpts" />
      <ChipFilter id="project-filter" v-model="f.project" label="项目" aria-label="按项目筛选" :options="projectOpts" />
    </div>

    <p v-if="invalidCount" id="tq-note" class="note">
      ※ （本筛选结果内计数）{{ invalidCount }} 条源数据存在质量异常（未知枚举/失效关联等），已排除出统计。
      <RouterLink to="/quality">查看异常明细 →</RouterLink>
    </p>

    <section id="task-list-card" class="module enter">
      <div class="tbl-wrap in-module">
        <table class="tbl cards-sm">
          <thead>
            <tr><th>P</th><th>事项</th><th>主责组</th><th>负责人</th><th>状态</th><th>项目</th><th>截止</th><th>源</th></tr>
          </thead>
          <tbody id="task-body">
            <tr v-for="t in activeRows" :key="t.id">
              <td data-label="优先级"><span class="prio" :data-p="t.priority || ''">{{ t.priority || "—" }}</span></td>
              <td data-label="事项"><span><RouterLink :to="'/tasks/' + encodeURIComponent(t.id)" class="lnk"><b>{{ t.name }}{{ t.invalid ? "（数据异常）" : "" }}</b></RouterLink><SyncMark :mark="t.sync" /></span></td>
              <td data-label="主责组"><span class="badge neutral">{{ t.owner_group || "—" }}</span></td>
              <td data-label="负责人">{{ names(t.owners) }}</td>
              <td data-label="状态"><span class="badge" :class="statusTone(t.status)">{{ statusText(t.status) }}</span></td>
              <td data-label="项目">{{ projectName(t) }}</td>
              <td data-label="截止"><DueTag :task="t" /></td>
              <td data-label="源记录"><SrcLink :url="t.url" /></td>
            </tr>
            <tr v-if="body && !activeRows.length">
              <td colspan="8" class="is-empty"><p class="empty">{{ acceptedRows.length ? "本筛选下没有未归档的事项" : "本筛选下暂无事项（源表可能为空）" }}</p></td>
            </tr>
            <tr v-if="acceptedRows.length" class="accepted-toggle">
              <td colspan="8">
                <button id="accepted-toggle" type="button" class="btn ghost sm" :aria-expanded="showAccepted ? 'true' : 'false'" @click="showAccepted = !showAccepted">
                  {{ showAccepted ? "收起" : "展开" }}已归档的事项（{{ acceptedRows.length }} 条）
                </button>
              </td>
            </tr>
            <template v-if="showAccepted">
              <tr v-for="t in acceptedRows" :key="t.id" class="is-accepted">
                <td data-label="优先级"><span class="prio" :data-p="t.priority || ''">{{ t.priority || "—" }}</span></td>
                <td data-label="事项"><span><RouterLink :to="'/tasks/' + encodeURIComponent(t.id)" class="lnk"><b>{{ t.name }}</b></RouterLink><SyncMark :mark="t.sync" /></span></td>
                <td data-label="主责组"><span class="badge neutral">{{ t.owner_group || "—" }}</span></td>
                <td data-label="负责人">{{ names(t.owners) }}</td>
                <td data-label="状态"><span class="badge neutral">已完成</span></td>
                <td data-label="项目">{{ projectName(t) }}</td>
                <td data-label="截止"><DueTag :task="t" /></td>
                <td data-label="源记录"><SrcLink :url="t.url" /></td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </section>
  </PortalPage>
</template>

<style scoped>
.accepted-toggle td { padding: 10px 14px; background: var(--c-surface); }
.is-accepted td { color: var(--c-text-3); }
</style>
