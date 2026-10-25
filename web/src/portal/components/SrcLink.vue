<script setup lang="ts">
/* 记录级源链接：后端 url 来自飞书官方 batch_get(with_shared_url)，逐条直达；没有就不渲染 */
withDefaults(defineProps<{ url?: string | null; label?: string }>(), { url: null, label: "飞书 ↗" });
</script>

<template>
  <a v-if="url" class="src-link" :href="url" target="_blank" rel="noopener">{{ label }}</a>
</template>
