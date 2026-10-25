<script setup lang="ts">
/* 项目详情：基本信息 + 只读甘特 + 事项列表 + 相关节点 */
import { computed, ref } from "vue";
import PortalPage from "../components/PortalPage.vue";
import SyncMark from "../components/SyncMark.vue";
import { navNo } from "../nav";
import SrcLink from "../components/SrcLink.vue";
import DueTag from "../components/DueTag.vue";
import Gantt from "../components/Gantt.vue";
import DeleteButton from "../components/DeleteButton.vue";
import { RouterLink, useRoute } from "@/shared/router";
import { fetchProject } from "../api";
import { isLeader, store } from "../store";
import { usePageData } from "../usePageData";
import { fmtD, fmtDT } from "@/shared/format";
import type { Project, UserRef } from "../types";
import { isAccepted, statusText } from "../taskStatus";

const route = useRoute();
const id = route.value.params.id;
const { body, notFound } = usePageData(() => fetchProject(id));
const p = computed<Project | null>(() => (body.value && body.value.data) || null);
/* 已完成（已归档）的事项默认折叠，点「展开」才显示（与事项页一致） */
const openTasks = computed(() => ((p.value && p.value.tasks) || []).filter((t) => !isAccepted(t)));
const acceptedTasks = computed(() => ((p.value && p.value.tasks) || []).filter(isAccepted));
const showAccepted = ref(false);
const tableLink = computed(() => (store.meta && store.meta.source_links && store.meta.source_links.projects) || null);

function names(l?: UserRef[]): string {
  return (l || []).map((u) => u.name || "…" + u.id.slice(-4)).join("、") || "待分配";
}
const progress = computed(() => {
  const pr = p.value && p.value.progress;
  if (!pr) return "—";
  return pr.percent == null ? "暂无事项 / —" : "完成率 " + pr.percent + "%（" + pr.done + "/" + pr.total + "）";
});
</script>

<template>
  <div id="project-detail">
    <PortalPage :no="navNo('/projects')" en="PROJECT │ DETAIL" :title="p ? p.name : (notFound ? '记录不存在' : '项目详情')" watermark="PROJECT">
      <template #actions>
        <RouterLink to="/projects" class="btn ghost sm">← 返回项目</RouterLink>
      </template>
      <template v-if="p">
        <SyncMark :mark="p.sync" block />
        <p v-if="p.invalid" class="note">※ 该项目源数据存在质量异常，统计已做排除处理。</p>
        <div class="stack">
          <section class="module enter">
            <div class="module-head">
              <span class="badge ink">{{ p.owner_group || "—" }}</span>
              <h2>{{ p.name }}</h2>
              <SrcLink v-if="p.url" :url="p.url" label="在飞书中打开该记录 ↗" />
              <SrcLink v-else :url="tableLink" label="在飞书中打开该表 ↗" />
            </div>
            <dl class="kv">
              <dt>状态</dt><dd>{{ p.status || "—" }}</dd>
              <dt>负责人</dt><dd>{{ names(p.owners) }}</dd>
              <dt>协作组</dt><dd>{{ (p.collab_groups || []).join("、") || "—" }}</dd>
              <dt>计划周期</dt><dd class="t-num">{{ fmtD(p.plan_start && p.plan_start.ts) }} → {{ fmtD(p.plan_end && p.plan_end.ts) }}</dd>
              <dt>进度</dt><dd>{{ progress }}</dd>
              <template v-if="p.description"><dt>说明</dt><dd class="prose">{{ p.description }}</dd></template>
            </dl>
            <div v-if="isLeader()" class="module-body" style="padding-top: 0">
              <div class="edit-actions"><span class="spacer"></span><DeleteButton :id="p.id" table="projects" label="项目" /></div>
            </div>
          </section>

          <section class="module enter">
            <div class="module-head"><span class="module-tag">GANTT / 02</span><h2>事项甘特</h2><span class="module-sub">只读 · 禁止拖拽写入</span></div>
            <div class="module-body"><Gantt :tasks="p.tasks || []" /></div>
          </section>

          <section class="module enter">
            <div class="module-head"><span class="module-tag">TASKS / 03</span><h2>事项列表</h2></div>
            <div class="module-body">
              <div v-for="t in openTasks" :key="t.id" class="list-row">
                <span class="prio" :data-p="t.priority || ''">{{ t.priority || "—" }}</span>
                <RouterLink :to="'/tasks/' + encodeURIComponent(t.id)" class="row-name lnk">{{ t.name }}{{ t.blocker_note ? "（受阻：" + t.blocker_note + "）" : "" }}</RouterLink>
                <span class="row-side"><SyncMark :mark="t.sync" /><span class="badge neutral">{{ statusText(t.status) }}</span><DueTag :task="t" /></span>
              </div>
              <p v-if="!(p.tasks || []).length" class="empty">暂无事项</p>
              <p v-else-if="!openTasks.length" class="empty">没有未归档的事项</p>
              <button v-if="acceptedTasks.length" type="button" class="btn ghost sm accepted-toggle" :aria-expanded="showAccepted ? 'true' : 'false'" @click="showAccepted = !showAccepted">
                {{ showAccepted ? "收起" : "展开" }}已归档的事项（{{ acceptedTasks.length }} 条）
              </button>
              <template v-if="showAccepted">
                <div v-for="t in acceptedTasks" :key="t.id" class="list-row is-accepted">
                  <span class="prio" :data-p="t.priority || ''">{{ t.priority || "—" }}</span>
                  <RouterLink :to="'/tasks/' + encodeURIComponent(t.id)" class="row-name lnk">{{ t.name }}</RouterLink>
                  <span class="row-side"><span class="badge neutral">已完成</span><DueTag :task="t" /></span>
                </div>
              </template>
            </div>
          </section>

          <section v-if="(p.milestones || []).length" class="module enter">
            <div class="module-head"><span class="module-tag">MILESTONES / 04</span><h2>相关节点</h2></div>
            <div class="module-body">
              <div v-for="m in p.milestones" :key="m.id" class="list-row">
                <span class="sched-type">{{ m.kind || "—" }}</span>
                <span class="row-name">{{ m.name }}</span>
                <span class="row-meta t-num">{{ fmtDT(m.target && m.target.ts) }}</span>
              </div>
            </div>
          </section>
        </div>
      </template>
    </PortalPage>
  </div>
</template>

<style scoped>
.accepted-toggle { margin-top: 12px; }
.is-accepted .row-name { color: var(--c-text-3); }
</style>
