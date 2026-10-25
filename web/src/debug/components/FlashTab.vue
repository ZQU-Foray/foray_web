<script setup lang="ts">
/* 01 固件烧录：设备状态 + 烧录参数（目标芯片 / 调试器 / 固件）+ 实时烧录日志。 */
import { computed, onMounted, ref } from "vue";
import DdSelect from "./DdSelect.vue";
import { deviceCtx } from "../lib/device";
import { flashBadge, station } from "../lib/status";
import { firmware } from "../lib/firmware";
import { bindFlashLog, clearFlashLog, startFlash } from "../lib/flash";

defineProps<{ active: boolean }>();

const log = ref<HTMLElement | null>(null);
onMounted(() => bindFlashLog(log.value));

const badge = computed(() => flashBadge());
const disabled = computed(() => !deviceCtx.current || station.flashState === "running");
</script>

<template>
  <section id="tab-flash" class="panel" :class="{ active }" aria-labelledby="tabbtn-flash">
    <div class="grid2">
      <div class="flash-side">
        <div class="card">
          <h2 class="lbl"><span class="tag">DEVICES / 01</span>设备状态</h2>
          <div id="device-list" class="devlist">
            <template v-if="station.devMode === 'init'">扫描中…</template>
            <template v-else-if="station.devMode === 'tcp'">{{ station.devText }}</template>
            <template v-else-if="station.devMode === 'none'">请选择设备后查看状态。</template>
            <template v-else-if="!station.devRows.length">未检测到 USB 调试器/串口</template>
            <template v-else>
              <div v-for="(row, i) in station.devRows" :key="i" class="item"><span class="kind">{{ row.label }}</span><br>{{ row.detail }}</div>
            </template>
          </div>
        </div>
        <div class="card">
          <h2 class="lbl"><span class="tag">PARAMS / 02</span>烧录参数</h2>
          <label class="prow"><span class="prow-label">目标芯片</span>
            <DdSelect id="target-select" v-model:value="station.target" :options="station.targets" /></label>
          <label class="prow" :hidden="deviceCtx.isTcp"><span class="prow-label">调试器</span>
            <DdSelect id="probe-select" v-model:value="station.probe" :options="station.probes" /></label>
          <label class="prow"><span class="prow-label">固件</span>
            <DdSelect id="firmware-select" v-model:value="firmware.selected" :options="firmware.options" /></label>
          <div class="row flash-row">
            <button id="flash-btn" type="button" class="btn" :disabled="disabled" @click="startFlash">开始烧录</button>
            <span id="flash-state" class="badge" :class="badge[0]">{{ badge[1] }}</span>
          </div>
        </div>
      </div>

      <div class="card log-card">
        <h2 class="lbl"><span class="tag">LOG / 03</span>烧录日志
          <button id="log-clear" type="button" class="btn ghost xs mini" @click="clearFlashLog">清屏</button></h2>
        <pre id="flash-log" ref="log" class="console"></pre>
      </div>
    </div>
  </section>
</template>
