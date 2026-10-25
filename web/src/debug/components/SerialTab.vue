<script setup lang="ts">
/* 02 串口调试：串口 / 波特率（单STA 下隐藏，波特率由 Basic2 决定）+ 打开关闭 +
 * 预设指令按钮 + 收发日志 + 文本 / HEX 发送。 */
import { computed, onMounted, ref } from "vue";
import DdSelect from "./DdSelect.vue";
import { deviceCtx } from "../lib/device";
import { station } from "../lib/status";
import { presets } from "../lib/presets";
import { bindSerialLog, closeSerial, openSerial, sendData, sendInput } from "../lib/serial";

defineProps<{ active: boolean }>();

const log = ref<HTMLElement | null>(null);
onMounted(() => bindSerialLog(log.value));

const input = ref("");
const hex = ref(false);
const dev = computed(() => deviceCtx.current);
const hint = computed(() => deviceCtx.isTcp
  ? "TCP 串口的实际波特率由 Basic2 配置决定，网页不会修改硬件参数。请确认已启用 TCP-UART，并与 MCU 波特率一致。"
  : "选择服务器上的串口与波特率。");
const cmds = computed(() => (presets.doc && presets.doc.commands) || []);

function onSend() {
  if (!dev.value) return;               // 与旧版一致：发送按钮禁用时回车也不发
  if (sendInput(input.value, hex.value)) input.value = "";
}
</script>

<template>
  <section id="tab-serial" class="panel" :class="{ active }" aria-labelledby="tabbtn-serial">
    <div class="card">
      <h2 class="lbl"><span class="tag">SERIAL / 01</span>串口调试</h2>
      <p id="serial-connection-hint" class="hint">{{ hint }}</p>
      <div class="row wrap serial-ctl">
        <label class="ifield" :hidden="deviceCtx.isTcp"><span class="ifield-label">串口</span>
          <DdSelect id="port-select" v-model:value="station.port" wrap-class="sel-port" :options="station.ports" /></label>
        <label class="ifield" :hidden="deviceCtx.isTcp"><span class="ifield-label">波特率</span>
          <input id="baud-input" v-model="presets.baud" class="input baud" type="number"></label>
        <button id="serial-open" type="button" class="btn sm" :disabled="!dev" @click="openSerial(String(presets.baud))">打开串口</button>
        <button id="serial-close" type="button" class="btn ghost sm" :disabled="!dev" @click="closeSerial">关闭串口</button>
        <span id="serial-state" class="badge" :class="station.serialOpen ? 'ok' : 'neutral'">{{ station.serialText }}</span>
      </div>
      <div class="row wrap">
        <span class="hint inline">预设指令:</span>
        <span id="preset-buttons" class="preset-buttons">
          <button
            v-for="(c, i) in cmds" :key="i" type="button" class="preset-btn"
            :title="`data=${c.data}  encoding=${c.encoding}  newline=${c.newline}`"
            @click="sendData(c.data || '', c.newline || 'crlf')"
          >{{ c.name || c.id || "?" }}</button>
        </span>
      </div>
      <pre id="serial-log" ref="log" class="console"></pre>
      <div class="row send-row">
        <input id="serial-input" v-model="input" class="input" placeholder="输入要发送的内容，回车发送" @keydown.enter="onSend">
        <label class="inline chk"><input id="hex-mode" v-model="hex" type="checkbox"> HEX</label>
        <button id="serial-send" type="button" class="btn sm" :disabled="!dev" @click="onSend">发送</button>
      </div>
    </div>
  </section>
</template>
