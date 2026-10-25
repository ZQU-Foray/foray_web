<script setup lang="ts">
/* 芯片筛选条：按钮组（aria-pressed 表示选中）。按钮必须是组内唯一的子元素，
   标签文字放在组外，保证「第 n 个按钮」与「第 n 个子元素」一致。 */
export interface ChipOption { id: string; label: string }

defineProps<{
  id: string;
  label: string;         // 可见标签，如「主责组」
  ariaLabel: string;     // 组的无障碍名称，如「按主责组筛选」
  options: ChipOption[];
}>();
const model = defineModel<string>({ required: true });
</script>

<template>
  <div class="chip-row">
    <span class="chip-bar-label" aria-hidden="true">{{ label }}</span>
    <div :id="id" class="chip-bar compact" role="group" :aria-label="ariaLabel">
      <button
        v-for="o in options"
        :key="o.id"
        type="button"
        class="chip"
        :aria-pressed="model === o.id ? 'true' : 'false'"
        @click="model = o.id"
      >{{ o.label }}</button>
    </div>
  </div>
</template>

<style scoped>
.chip-row { display: flex; align-items: center; gap: 10px; min-width: 0; }
.chip-row .chip-bar { flex: 1; min-width: 0; }
@media (max-width: 640px) {
  .chip-row { align-items: flex-start; flex-direction: column; gap: 4px; }
}
</style>
