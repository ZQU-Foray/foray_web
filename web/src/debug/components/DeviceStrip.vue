<script setup lang="ts">
/* 设备条：连接方式 / 当前设备（含在线徽标）/ 扫描 STA / 手动添加 STA + 状态提示。
 * id 契约与旧版一致：#connection-mode #device-select #device-health #dev-scan #sta-add #sta-host #device-hint。 */
import { computed, ref } from "vue";
import DdSelect from "./DdSelect.vue";
import { addSta, deviceCtx, deviceOptions, deviceUi, scanStaDevices, setConnectionMode } from "../lib/device";
import type { DdOption } from "../lib/dropdown";

const MODE_OPTS: DdOption[] = [{ value: "sta", label: "单STA" }, { value: "usb", label: "USB / AP" }];
const devOpts = computed(() => deviceOptions());
const host = ref("");
const sta = computed(() => deviceCtx.mode === "sta");

function onDevice(v: string) {
  deviceCtx.set(v || null);
}
</script>

<template>
  <div class="dev-strip">
    <div id="dev-zone" class="dev-zone">
      <label class="dev-field"><span class="dev-label">连接方式</span>
        <DdSelect id="connection-mode" wrap-class="sel-conn" :options="MODE_OPTS" :value="deviceCtx.mode" @change="setConnectionMode" /></label>
      <label class="dev-field"><span class="dev-label">当前设备</span>
        <DdSelect id="device-select" wrap-class="sel-device" :options="devOpts" :value="deviceCtx.id || ''" @change="onDevice" />
        <span v-show="deviceUi.health.show" id="device-health" class="badge sm device-health" :class="deviceUi.health.cls" :title="deviceUi.health.title">{{ deviceUi.health.text }}</span></label>
      <button id="dev-scan" type="button" class="btn ghost sm" :hidden="!sta" :disabled="deviceUi.scanning" @click="scanStaDevices">{{ deviceUi.scanning ? "扫描中…" : "扫描 STA" }}</button>
      <form id="sta-add" class="sta-add" :hidden="!sta" @submit.prevent="addSta(host)">
        <input id="sta-host" v-model="host" class="input" aria-label="STA IPv4 地址" placeholder="STA 地址，如 192.168.1.20" required>
        <button type="submit" class="btn sm">添加</button>
      </form>
    </div>
  </div>
</template>
