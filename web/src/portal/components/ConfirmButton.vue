<script setup lang="ts">
/* 两步确认按钮（与删除按钮同一手势）：第一次点击进入待确认态，3 秒内再点才触发 confirm。
 * cls 换按钮样式（默认小号描边 ghost xs）。 */
import { onBeforeUnmount, ref } from "vue";

const props = withDefaults(
  defineProps<{ label: string; armedLabel: string; busy?: boolean; danger?: boolean; cls?: string }>(),
  { cls: "ghost xs" },
);
const emit = defineEmits<{ confirm: [] }>();
const armed = ref(false);
let timer = 0;

function onClick() {
  if (props.busy) return;
  if (!armed.value) {
    armed.value = true;
    timer = window.setTimeout(() => { armed.value = false; }, 3000);
    return;
  }
  window.clearTimeout(timer);
  armed.value = false;
  emit("confirm");
}
onBeforeUnmount(() => window.clearTimeout(timer));
</script>

<template>
  <button
    type="button"
    class="btn"
    :class="[cls, { danger, 'is-armed': armed }]"
    :disabled="busy"
    @click="onClick"
  >{{ armed ? armedLabel : label }}</button>
</template>
