<script setup lang="ts">
/* 子页头：白色上半 + 黄色色块（表示「你现在在这一区」），右上描边巨字水印。 */
defineProps<{
  no: string;            // 编号，如「01」
  en: string;            // 英文眉标，如「HOME │ OVERVIEW」
  title: string;         // 中文大标题
  desc?: string;         // 一行说明
  watermark?: string;    // 描边巨字
  plate?: string;        // 右下角铭牌
}>();
</script>

<template>
  <div class="page-head">
    <div class="page-head-inner">
      <div v-if="watermark" class="page-watermark" aria-hidden="true">{{ watermark }}</div>
      <div class="page-eyebrow">
        <span class="page-no t-num">— {{ no }}</span>
        <span class="page-en">{{ en }}</span>
      </div>
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="desc" class="page-desc">{{ desc }}</p>
      <div v-if="$slots.actions" class="page-actions"><slot name="actions" /></div>
      <div v-else-if="plate" class="page-plate" aria-hidden="true">{{ plate }}</div>
    </div>
  </div>
</template>
