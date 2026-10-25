<script setup lang="ts">
/* 打卡工时排行（累计有效时长）：服务端已按时长降序排好，这里只按序渲染。
 * 总览与考勤页共用；没有考勤数据的成员排在末尾、不给名次。
 * 总览只显示前 limit 名，当前用户不在其中时在末尾补一行「我」（名次照实写）。 */
import { computed } from "vue";
import SrcLink from "./SrcLink.vue";
import { fmtDT } from "@/shared/format";
import type { MemberRank } from "../types";

const props = defineProps<{ members: MemberRank[] | null; boxId: string; limit?: number; meId?: string | null }>();

interface Row { m: MemberRank; i: number; me: boolean }
const rows = computed<Row[]>(() => {
  const all = (props.members || []).map((m, i) => ({ m, i, me: !!props.meId && m.member?.id === props.meId }));
  if (!props.limit || all.length <= props.limit) return all;
  const top = all.slice(0, props.limit);
  const mine = all.find((r) => r.me && r.i >= props.limit!);
  return mine ? top.concat(mine) : top;
});
const hidden = computed(() => {
  const n = (props.members || []).length;
  return props.limit && n > props.limit ? n - rows.value.length : 0;
});
</script>

<template>
  <div :id="boxId" class="scroll-list">
    <template v-for="r in rows" :key="(r.m.member && r.m.member.id) || r.i">
      <p v-if="limit && r.i >= limit" class="rank-gap" aria-hidden="true">⋯</p>
      <div class="rank-row" :class="{ 'is-me': r.me }">
        <span v-if="r.m.work_hours != null" class="rank-no" :class="{ top: r.i === 0 }">#{{ r.i + 1 }}</span>
        <span v-else class="rank-no none" aria-label="未入榜">—</span>
        <div class="row-main">
          <span class="row-name">{{ (r.m.member && r.m.member.name) || "(未命名)" }}<small v-if="r.me" class="rank-me">我</small></span>
          <span v-if="r.m.last_check_in" class="row-meta">最近 {{ fmtDT(r.m.last_check_in.ts) }}</span>
        </div>
        <div class="rank-hours">
          <template v-if="r.m.work_hours != null"><b>{{ r.m.work_hours.toFixed(2) }}</b> h</template>
          <span v-else class="row-meta">未统计打卡</span>
        </div>
        <SrcLink :url="r.m.url" />
      </div>
    </template>
    <p v-if="hidden" class="rank-more row-meta">另有 {{ hidden }} 人，完整排行见考勤页</p>
    <p v-if="members && !members.length" class="empty">暂无成员考勤数据</p>
  </div>
</template>

<style scoped>
.rank-row.is-me { background: var(--c-surface); }
.rank-me { margin-left: 6px; padding: 1px 5px; font: 700 10px/1.4 var(--f-cn); background: var(--c-ink); color: var(--c-paper); vertical-align: 2px; }
.rank-gap { margin: 0; padding: 2px 0 2px 14px; color: var(--c-text-3); line-height: 1; }
.rank-more { margin: 0; padding: 10px 0 2px 14px; }
</style>
