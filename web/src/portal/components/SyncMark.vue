<script setup lang="ts">
/* 记录的同步标记：待同步 / 同步失败 / 冲突（标记由后端在生成快照时算好）。
 * 列表里是小徽标（悬停看说明）；详情页用 block 模式显示整句说明，手机上也看得到。 */
import { RouterLink } from "@/shared/router";
import type { SyncMark } from "../types";

defineProps<{ mark?: SyncMark | null; block?: boolean }>();

const TONE: Record<string, string> = { pending: "neutral", failed: "danger", conflict: "warn" };
</script>

<template>
  <template v-if="mark">
    <p v-if="block" class="note sync-note" :class="{ 'is-error': mark.state === 'failed' }" :data-sync="mark.state">
      ※ {{ mark.label }}：{{ mark.text }}
      <RouterLink v-if="mark.state !== 'pending'" to="/quality">查看推送与冲突 →</RouterLink>
    </p>
    <span v-else class="badge sm sync-mark" :class="TONE[mark.state] || 'neutral'" :data-sync="mark.state" :title="mark.text">{{ mark.label }}</span>
  </template>
</template>

<style scoped>
.sync-mark { margin-left: 6px; vertical-align: middle; white-space: nowrap; }
</style>
