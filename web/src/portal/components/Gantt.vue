<script setup lang="ts">
/* 只读甘特：日期轴 + 事项条；缺开始或截止 → 进「未排期」列表（不伪造排期）；无任何拖拽交互 */
import { computed } from "vue";
import { RouterLink } from "@/shared/router";
import { fmtD } from "@/shared/format";
import type { Task } from "../types";
import { statusText } from "../taskStatus";

const props = defineProps<{ tasks: Task[] }>();

const dated = computed(() => props.tasks.filter((t) => t.start && t.due && t.due.ts >= t.start.ts));
const unscheduled = computed(() => props.tasks.filter((t) => !(t.start && t.due && t.due.ts >= t.start.ts)));

const axis = computed(() => {
  if (!dated.value.length) return null;
  let min = Infinity;
  let max = -Infinity;
  dated.value.forEach((t) => {
    min = Math.min(min, (t.start as { ts: number }).ts);
    max = Math.max(max, (t.due as { ts: number }).ts);
  });
  const span = Math.max(max - min, 86400 * 7);
  return { min, span, ticks: [0, 1, 2, 3, 4].map((i) => fmtD(min + (span * i) / 4)) };
});

function barStyle(t: Task): Record<string, string> {
  const a = axis.value;
  if (!a || !t.start || !t.due) return {};
  const l = ((t.start.ts - a.min) / a.span) * 100;
  const w = Math.max(((t.due.ts - t.start.ts) / a.span) * 100, 1.2);
  return { left: l.toFixed(2) + "%", width: w.toFixed(2) + "%" };
}
function barCls(t: Task): string {
  return "gantt-bar st-" + (t.status === "已完成" ? "done" : t.status === "受阻" ? "blocked"
    : t.status === "已取消" ? "cancel" : "open");
}
</script>

<template>
  <p v-if="!tasks.length" class="empty">项目暂无事项</p>
  <template v-else>
    <div v-if="axis" class="gantt">
      <div class="gantt-inner">
        <div class="gantt-head">
          <span class="gantt-name">事项</span>
          <div class="gantt-axis"><span v-for="(d, i) in axis.ticks" :key="i">{{ d }}</span></div>
        </div>
        <div v-for="t in dated" :key="t.id" class="gantt-row">
          <RouterLink :to="'/tasks/' + encodeURIComponent(t.id)" class="gantt-name">
            <span class="prio" :data-p="t.priority || ''">{{ t.priority || "—" }}</span>
            <span>{{ t.name }}</span>
          </RouterLink>
          <div class="gantt-track">
            <div
              :class="barCls(t)"
              :style="barStyle(t)"
              :title="t.name + ' · ' + fmtD(t.start && t.start.ts) + ' → ' + fmtD(t.due && t.due.ts) + ' · ' + statusText(t.status)"
            ></div>
          </div>
        </div>
      </div>
    </div>
    <div v-if="unscheduled.length" class="unsched">
      <p class="note">未排期（缺开始或截止日期，不伪造排期）：{{ unscheduled.length }} 条</p>
      <div v-for="t in unscheduled" :key="t.id" class="list-row">
        <span class="prio" :data-p="t.priority || ''">{{ t.priority || "—" }}</span>
        <RouterLink :to="'/tasks/' + encodeURIComponent(t.id)" class="row-name lnk">{{ t.name }}</RouterLink>
        <span class="badge neutral">{{ statusText(t.status) }}</span>
      </div>
    </div>
  </template>
</template>
