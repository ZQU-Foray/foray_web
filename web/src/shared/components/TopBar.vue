<script setup lang="ts">
/* 顶栏：面包屑 + 状态芯片（插槽）+ 时钟 + 窄屏下的跨应用入口 */
import { useClock } from "../useClock";
import { onHandoffClick } from "../handoff";

defineProps<{
  crumb: string;
  switchHref?: string;   // 窄屏显示的跨应用入口（门户 ↔ 调试台）
  switchText?: string;
}>();
const clock = useClock();
</script>

<template>
  <header class="topbar">
    <nav class="crumb" aria-label="当前位置">
      <span class="t-wide" style="font-size: 9px">FORAY</span>
      <span class="sep" aria-hidden="true">/</span>
      <b>{{ crumb }}</b>
    </nav>
    <div class="topbar-gap"></div>
    <slot />
    <span class="topbar-clock" aria-label="当前时间">{{ clock }}</span>
    <a
      v-if="switchHref"
      class="topbar-switch"
      :href="switchHref"
      @click="onHandoffClick($event, switchHref)"
    >{{ switchText }}</a>
  </header>
</template>
