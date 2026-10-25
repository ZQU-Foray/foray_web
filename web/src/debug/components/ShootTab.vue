<script setup lang="ts">
/* 03 发射调试：连接 / 断开 / 链路与 RTT 徽标 / 急停 + 左列三电机卡 + 右侧波形区。 */
import { computed } from "vue";
import MotorCard from "./MotorCard.vue";
import DetailPanel from "./DetailPanel.vue";
import { deviceCtx } from "../lib/device";
import { shootClose, shootEstop, shootOpen, shootUi } from "../lib/shoot";

defineProps<{ active: boolean }>();

const dev = computed(() => deviceCtx.current);
const hint = computed(() => {
  const d = dev.value;
  if (deviceCtx.isTcp && d) return `当前目标：${d.name || d.id} · TCP 串口 ${d.host}:${d.uart_port || 20010}。连接后等待 MCU 遥测。`;
  return d ? "当前目标使用服务器 USB 串口；连接后等待 MCU 遥测。" : "请先在上方添加并选择设备。";
});
</script>

<template>
  <section id="tab-shoot" class="panel" :class="{ active }" aria-labelledby="tabbtn-shoot">
    <div class="card">
      <h2 class="lbl"><span class="tag">SHOOT / 01</span>发射机构电机调试</h2>
      <p id="shoot-connection-hint" class="hint">{{ hint }}</p>
      <div class="shoot-top">
        <button id="shoot-open" type="button" class="btn" :disabled="shootUi.openBusy || !dev" @click="shootOpen">连接串口</button>
        <button id="shoot-close" type="button" class="btn ghost sm" :disabled="!dev" @click="shootClose">断开</button>
        <span id="link-badge" class="badge" :class="shootUi.link.cls" :title="shootUi.link.title">{{ shootUi.link.text }}</span>
        <span id="rtt-badge" class="badge t-num" :class="shootUi.rtt.cls">{{ shootUi.rtt.text }}</span>
        <span class="spacer"></span>
        <button id="shoot-estop" type="button" class="btn danger estop" :disabled="!dev" @click="shootEstop">■ 急停 (E-STOP)</button>
      </div>

      <div class="shoot-body">
        <!-- 左列：按 id 折叠收纳的电机参数 -->
        <div id="motor-panels" class="shoot-motors">
          <MotorCard v-for="s in [0, 1, 2]" :key="s" :s="s" />
        </div>
        <!-- 右侧：波形（按数据类型分图）与反馈数据 -->
        <DetailPanel />
      </div>

      <p class="hint">与“串口调试”页互斥：打开一方会自动断开另一方（仅影响当前设备）。
        型号/模式切换会自动失能；修改参数后需点 → 或 应用全部 提交生效。</p>
    </div>
  </section>
</template>
