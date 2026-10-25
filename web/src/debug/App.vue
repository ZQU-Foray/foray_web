<script setup lang="ts">
/* 调试台外壳：侧栏（四个页签导航 / 服务连接指示 / 身份与退出 / 回门户）+ 顶栏 + 页头 +
 * 设备条 + 页签条 + 四个页签。四个页签始终挂载、按 .panel.active 显隐
 * （日志 WebSocket、遥测与波形录制在切页时不中断，与旧版一致）。 */
import { computed, ref } from "vue";
import AppShell from "@/shared/components/AppShell.vue";
import TopBar from "@/shared/components/TopBar.vue";
import PageHead from "@/shared/components/PageHead.vue";
import { onHandoffClick } from "@/shared/handoff";
import DeviceStrip from "./components/DeviceStrip.vue";
import FlashTab from "./components/FlashTab.vue";
import SerialTab from "./components/SerialTab.vue";
import ShootTab from "./components/ShootTab.vue";
import SettingsTab from "./components/SettingsTab.vue";
import { station } from "./lib/status";
import { deviceCtx, deviceUi } from "./lib/device";
import { shootUi } from "./lib/shoot";
import { avatarText, identity, logout, whoText } from "./lib/identity";
import ToastStack from "@/shared/components/ToastStack.vue";

type TabKey = "flash" | "serial" | "shoot" | "settings";
const TABS: { key: TabKey; no: string; name: string; en: string }[] = [
  { key: "flash", no: "01", name: "固件烧录", en: "FLASH" },
  { key: "serial", no: "02", name: "串口调试", en: "SERIAL" },
  { key: "shoot", no: "03", name: "发射调试", en: "SHOOT" },
  { key: "settings", no: "04", name: "设置", en: "SYSTEM" },
];

const tab = ref<TabKey>("flash");
const cur = computed(() => TABS.find((t) => t.key === tab.value) || TABS[0]);

/* 服务连接指示：null = 连接中（尚无结果） */
const connCls = computed(() => station.online === null ? "dot" : station.online ? "dot ok" : "dot danger");
const connText = computed(() => station.online === null ? "连接中…" : station.online ? "服务在线" : "服务离线");

const who = computed(whoText);
const avatar = computed(avatarText);

/* 页头铭牌：当前设备 + 在线徽标（TCP 设备才探测） */
const devChip = computed(() => {
  const d = deviceCtx.current;
  if (!d) return "未选择设备";
  return d.transport === "vllink_tcp" ? `STA · ${d.host ? d.host.split(".").pop() : d.id}` : (d.name || d.id);
});
const devDot = computed(() => {
  const h = deviceUi.health;
  if (!deviceCtx.current || !h.show) return "dot";
  return h.cls === "ok" ? "dot ok" : h.cls === "danger" ? "dot danger" : "dot warn";
});
</script>

<template>
  <AppShell title="调试台" subtitle="LAN DEBUG TERMINAL" code="DEBUG" nav-label="功能导航">
    <template #nav>
      <span class="side-nav-label">OPERATIONS</span>
      <button
        v-for="t in TABS"
        :key="t.key"
        type="button"
        class="side-nav-item tab"
        :class="{ active: tab === t.key }"
        :data-tab="t.key"
        :aria-current="tab === t.key ? 'page' : undefined"
        @click="tab = t.key"
      ><span class="nav-no">{{ t.no }}</span><span class="nav-name"><b>{{ t.name }}</b><small>{{ t.en }}</small></span></button>
    </template>

    <template #foot>
      <div class="side-mod">
        <div class="side-mod-head"><span>LINK STATUS</span><b>SRV</b></div>
        <div class="side-mod-row conn" :class="{ online: station.online === true }">
          <span id="conn-dot" :class="connCls" aria-hidden="true"></span>
          <span id="conn-text">{{ connText }}</span>
        </div>
      </div>
      <div class="side-mod side-user">
        <div class="side-avatar" aria-hidden="true">{{ avatar }}</div>
        <div style="min-width: 0">
          <span id="who">{{ who }}</span>
          <small>OPERATOR</small>
        </div>
        <button v-if="identity.who" id="logout-btn" type="button" class="btn ghost xs" @click="logout">退出</button>
      </div>
      <a class="side-portal-link" href="/" @click="onHandoffClick($event, '/')"><span>← 管理门户</span><small>PORTAL</small></a>
    </template>

    <TopBar :crumb="cur.name" switch-href="/" switch-text="← 门户">
      <span class="topbar-chip is-optional"><span :class="connCls" aria-hidden="true"></span>{{ connText }}</span>
      <span class="topbar-chip is-optional t-micro">局域网</span>
    </TopBar>

    <main class="app-content dbg-main">
      <PageHead :no="cur.no" :en="'DEBUG CONSOLE │ ' + cur.en" title="调试台" watermark="DEBUG">
        <template #actions>
          <span class="topbar-chip"><span :class="devDot" aria-hidden="true"></span>{{ devChip }}</span>
          <span class="topbar-chip t-num">{{ shootUi.rtt.text }}</span>
        </template>
      </PageHead>

      <DeviceStrip />

      <nav class="tab-bar" aria-label="页签">
        <button
          v-for="t in TABS"
          :id="'tabbtn-' + t.key"
          :key="t.key"
          type="button"
          class="tab-btn"
          :class="{ active: tab === t.key }"
          :data-tab="t.key"
          :aria-current="tab === t.key ? 'page' : undefined"
          @click="tab = t.key"
        ><span class="tab-no">{{ t.no }}</span>{{ t.name }}</button>
      </nav>

      <div class="main-area">
        <FlashTab :active="tab === 'flash'" />
        <SerialTab :active="tab === 'serial'" />
        <ShootTab :active="tab === 'shoot'" />
        <SettingsTab :active="tab === 'settings'" />
      </div>
    </main>
  </AppShell>

  <ToastStack />
</template>
