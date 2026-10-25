<script setup lang="ts">
/* 项目列表：主责组筛选 + 「含归档」开关（默认隐藏已归档项目） */
import { computed } from "vue";
import PortalPage from "../components/PortalPage.vue";
import SyncMark from "../components/SyncMark.vue";
import { navNo } from "../nav";
import ChipFilter, { type ChipOption } from "../components/ChipFilter.vue";
import SrcLink from "../components/SrcLink.vue";
import { RouterLink } from "@/shared/router";
import { fetchProjects } from "../api";
import { groups, isLeader, openForm, store } from "../store";
import { usePageData } from "../usePageData";
import { fmtD } from "@/shared/format";
import type { Project, UserRef } from "../types";

const f = store.projectFilters;
const { body } = usePageData(() => fetchProjects(f), [() => ({ ...f })]);
const list = computed<Project[]>(() => (body.value && body.value.data) || []);
const groupOpts = computed<ChipOption[]>(() =>
  [{ id: "全部", label: "全部" }].concat(groups().map((g) => ({ id: g, label: g }))));

function names(l?: UserRef[]): string {
  return (l || []).map((u) => u.name || "…" + u.id.slice(-4)).join("、") || "待分配";
}
function progressText(p: Project): string {
  const pr = p.progress;
  return pr.percent == null ? "暂无事项" : pr.done + "/" + pr.total + " · " + pr.percent + "%";
}
</script>

<template>
  <PortalPage :no="navNo('/projects')" en="PROJECT" title="项目" desc="主责组 │ 计划周期 │ 完成率" watermark="PROJECT">
    <template v-if="isLeader()" #actions>
      <button type="button" class="btn" @click="openForm('project')">＋ 新建项目</button>
    </template>

    <div class="filters">
      <ChipFilter id="proj-group-filter" v-model="f.group" label="主责组" aria-label="按主责组筛选" :options="groupOpts" />
      <div class="chip-row-solo">
        <span class="chip-bar-label" aria-hidden="true">归档</span>
        <div id="proj-archived-filter" class="chip-bar compact" role="group" aria-label="归档项目">
          <button type="button" class="chip" :aria-pressed="f.archived ? 'true' : 'false'" @click="f.archived = !f.archived">{{ f.archived ? "含归档：是" : "含归档：否" }}</button>
        </div>
      </div>
    </div>

    <div id="project-list" class="grid-auto">
      <section v-for="p in list" :key="p.id" class="module project-card enter">
        <div class="module-head">
          <span class="badge ink">{{ p.owner_group || "—" }}</span>
          <h2><RouterLink :to="'/projects/' + encodeURIComponent(p.id)" class="lnk">{{ p.name }}</RouterLink><SyncMark :mark="p.sync" /></h2>
          <span v-if="p.invalid" class="badge danger sm">数据异常</span>
        </div>
        <div class="module-body">
          <p class="prose project-desc">{{ p.description || "（无说明）" }}</p>
          <div class="project-meta">
            <span class="badge neutral">{{ p.status || "—" }}</span>
            <span class="row-meta">负责人：{{ names(p.owners) }}</span>
          </div>
          <div class="project-meta">
            <span class="row-meta t-num">{{ fmtD(p.plan_start && p.plan_start.ts) }} → {{ fmtD(p.plan_end && p.plan_end.ts) }}</span>
            <span class="row-meta t-num">{{ progressText(p) }}</span>
            <SrcLink :url="p.url" />
          </div>
          <div class="prog" :aria-label="'完成率 ' + (p.progress.percent ?? 0) + '%'"><div class="prog-fill" :style="{ width: (p.progress.percent ?? 0) + '%' }"></div></div>
        </div>
      </section>
      <p v-if="body && !list.length" class="empty">暂无项目（源表为空或被筛选）</p>
    </div>
  </PortalPage>
</template>

<style scoped>
.chip-row-solo { display: flex; align-items: center; gap: 10px; }
.project-card { margin: 0; }
.project-card + .project-card { margin-top: 0; }
.project-desc { min-height: 2.4em; }
.project-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; margin-top: 10px; }
.prog { margin-top: 12px; }
</style>
