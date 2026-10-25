<script setup lang="ts">
/* 附件格（图片补充 / 发票文件）：有临时链接就点得开第一份，悬停看全部文件名；否则退化为文件名提示 */
import { computed } from "vue";
import type { Attachment } from "../types";

const props = defineProps<{ list?: Attachment[]; empty?: string }>();
const items = computed(() => props.list || []);
const text = computed(() => {
  const first = items.value[0];
  return items.value.length + " 个" + (first && first.name ? "·" + first.name : "");
});
const title = computed(() => {
  const t = items.value.map((x) => x.name).join("\n");
  return items.value[0] && items.value[0].url ? t : t + "（临时链接未随快照下发，请在飞书中查看）";
});
</script>

<template>
  <template v-if="!items.length">{{ empty || "—" }}</template>
  <a v-else-if="items[0].url" class="lnk clip" :href="items[0].url" target="_blank" rel="noopener" :title="title">{{ text }}</a>
  <span v-else class="clip" :title="title">{{ text }}</span>
</template>
