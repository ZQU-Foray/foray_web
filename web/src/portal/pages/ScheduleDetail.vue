<script setup lang="ts">
/* 日程详情：时间 / 状态 / 相关组 / 地点 / 参与人 / 说明；组长可删除 */
import { computed } from "vue";
import PortalPage from "../components/PortalPage.vue";
import SyncMark from "../components/SyncMark.vue";
import { navNo } from "../nav";
import SrcLink from "../components/SrcLink.vue";
import DeleteButton from "../components/DeleteButton.vue";
import { RouterLink, useRoute } from "@/shared/router";
import { fetchScheduleItem } from "../api";
import { isLeader, store } from "../store";
import { usePageData } from "../usePageData";
import { fmtD, fmtDT } from "@/shared/format";
import type { ScheduleItem, UserRef } from "../types";

const route = useRoute();
const id = route.value.params.id;
const { body, notFound } = usePageData(() => fetchScheduleItem(id));
const s = computed<ScheduleItem | null>(() => (body.value && body.value.data) || null);
const tableLink = computed(() => (store.meta && store.meta.source_links && store.meta.source_links.schedule) || null);

function names(l?: UserRef[]): string {
  return (l || []).map((u) => u.name || "…" + u.id.slice(-4)).join("、") || "待分配";
}
const timeText = computed(() => {
  const x = s.value;
  if (!x) return "";
  if (x.all_day) return "全天 " + fmtD(x.start && x.start.ts) + (x.end ? " – " + fmtD(x.end.ts) : "");
  return fmtDT(x.start && x.start.ts) + (x.end ? " → " + fmtDT(x.end.ts) : "");
});
</script>

<template>
  <div id="schedule-detail">
    <PortalPage :no="navNo('/schedule')" en="SCHEDULE │ DETAIL" :title="s ? s.name : (notFound ? '记录不存在' : '日程详情')" watermark="SCHEDULE">
      <template #actions>
        <RouterLink to="/schedule" class="btn ghost sm">← 返回日程</RouterLink>
      </template>
      <template v-if="s">
        <SyncMark :mark="s.sync" block />
        <p v-if="s.invalid" class="note">※ 该日程源数据存在质量异常。</p>
        <section class="module enter">
          <div class="module-head">
            <span class="sched-type">{{ s.kind || "—" }}</span>
            <h2>{{ s.name }}</h2>
            <span v-if="s.status === '已取消'" class="badge danger sm">已取消</span>
            <SrcLink v-if="s.url" :url="s.url" label="在飞书中打开该记录 ↗" />
            <SrcLink v-else :url="tableLink" label="在飞书中打开该表 ↗" />
          </div>
          <dl class="kv">
            <dt>时间</dt><dd class="t-num">{{ timeText }}</dd>
            <dt>状态</dt><dd>{{ s.status || "—" }}</dd>
            <dt>相关组</dt><dd>{{ (s.groups || []).join("、") || "—" }}</dd>
            <dt>地点</dt><dd>{{ s.location || "—" }}</dd>
            <dt>参与人</dt><dd>{{ names(s.participants) }}</dd>
            <template v-if="s.description"><dt>说明</dt><dd class="prose">{{ s.description }}</dd></template>
          </dl>
          <div v-if="isLeader()" class="module-body" style="padding-top: 0">
            <div class="edit-actions"><span class="spacer"></span><DeleteButton :id="s.id" table="schedule" label="日程" /></div>
          </div>
        </section>
      </template>
    </PortalPage>
  </div>
</template>
