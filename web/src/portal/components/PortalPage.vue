<script setup lang="ts">
/* 门户页面骨架：子页头 + 内容区；内容区顶部放「尚无同步数据」的状态说明（通知改由右侧通知框弹出）。 */
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
    <p v-if="store.noSnapshot" id="no-snap-note" class="note">
      ※ 尚无可用同步数据 —— 首次同步未完成或飞书未配置（与『源表为空』不同）。
    </p>
    <slot />
  </div>
</template>
