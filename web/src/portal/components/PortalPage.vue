<script setup lang="ts">
/* 门户页面骨架：子页头 + 内容区；内容区顶部放「加载失败」与「尚无同步数据」两条状态说明
 * （一次性提示走右侧通知框，常驻说明留在这里，避免用户把过期内容当成最新）。 */
import PageHead from "@/shared/components/PageHead.vue";
import { store } from "../store";

defineProps<{
  no: string;
  en: string;
  title: string;
  desc?: string;
  watermark?: string;
  plate?: string;
}>();
</script>

<template>
  <PageHead :no="no" :en="en" :title="title" :desc="desc" :watermark="watermark" :plate="plate">
    <template v-if="$slots.actions" #actions><slot name="actions" /></template>
  </PageHead>
  <div class="page-body">
    <p v-if="store.loadError" id="load-err-note" class="note note-err" role="status">
      ※ {{ store.loadError }}
    </p>
    <p v-if="store.noSnapshot" id="no-snap-note" class="note">
      ※ 尚无可用同步数据 —— 首次同步未完成或飞书未配置（与『源表为空』不同）。
    </p>
    <slot />
  </div>
</template>
