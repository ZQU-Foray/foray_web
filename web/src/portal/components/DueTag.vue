<script setup lang="ts">
/* 截止标注：逾期 / 临期用文字写明（不只靠颜色），未设置单独标注 */
import { computed } from "vue";
import { fmtD, fmtDT } from "@/shared/format";
import type { Task } from "../types";

const props = defineProps<{ task: Task }>();
const text = computed(() => {
  const t = props.task;
  const base = t.due ? (t.due_precision === "date" ? fmtD(t.due.ts) : fmtDT(t.due.ts)) : "未设置";
  if (t.overdue) return base + " │ 逾期";
  if (t.due_soon) return base + " │ 临期";
  return base;
});
</script>

<template>
  <span class="due-tag" :class="{ 'is-overdue': task.overdue, 'is-soon': !task.overdue && task.due_soon }">{{ text }}</span>
</template>
