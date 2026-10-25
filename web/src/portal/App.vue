<script setup lang="ts">
/* 门户外壳：侧栏导航 + 同步状态 + 身份 + 调试台入口；主区按路由渲染页面。 */
import { computed, ref, watch } from "vue";
import AppShell from "@/shared/components/AppShell.vue";
import TopBar from "@/shared/components/TopBar.vue";
import { RouterLink, RouterView, useRoute } from "@/shared/router";
import { onHandoffClick } from "@/shared/handoff";
import { logout, requestSync } from "./api";
import { closeForm, queueCounts, requestRefresh, store, syncLine } from "./store";
import { NAV } from "./routes";
import WriteForm from "./components/WriteForm.vue";
import ToastStack from "@/shared/components/ToastStack.vue";

const route = useRoute();
const line = computed(syncLine);
/* 推送失败或有冲突待确认时，侧栏给出去「数据质量」页处理的入口 */
const needsAttention = computed(() => { const q = queueCounts(); return q.failed + q.conflicts > 0; });
const dotTone = computed(() => {
  const c = line.value.cls;
  if (c.includes("is-failed")) return "danger";
  return c.includes("is-stale") || c.includes("is-empty") || c.includes("is-warn") ? "warn" : "ok";
});

/* 当前导航项：列表页与其下的详情页（/tasks/xx）都算 */
function isCurrent(href: string): boolean {
  const p = route.value.path;
  if (href === "/") return p === "/";
  return p === href || p.indexOf(href + "/") === 0;
}

const crumb = computed(() => {
  const m = (route.value.def?.meta || {}) as { crumb?: string };
  return m.crumb || "门户";
});

watch(() => route.value.def?.meta, (m) => {
  const t = (m as { title?: string } | undefined)?.title;
  document.title = "Foray 信息面板" + (t ? " · " + t : "");
}, { immediate: true });

/* 切页时关掉写入弹窗（通知框自己定时消失，切页不清） */
watch(() => route.value.path, () => { closeForm(); });

const who = computed(() => {
  const src = store.me || store.bootWho;
  if (!src) return store.meLoaded ? "未登录" : "";
  return src.display + "（" + (src.role === "member" ? "成员" : "应急·仅调试") + "）";
});
const avatar = computed(() => {
  const src = store.me || store.bootWho;
  return src && src.display ? src.display.slice(0, 1) : "?";
});

/* 手动刷新：合并定时任务 + 限频；接受后稍等再局部刷新 */
const refreshText = ref("刷新");
const refreshing = ref(false);
async function onRefresh() {
  refreshing.value = true;
  refreshText.value = "刷新中…";
  try {
    const res = await requestSync();
    refreshText.value = res.body.accepted ? "已触发同步" : (res.body.detail || "已限频");
    if (res.body.accepted) window.setTimeout(requestRefresh, 1500);
    window.setTimeout(() => { refreshing.value = false; refreshText.value = "刷新"; }, 2000);
  } catch {
    refreshText.value = "刷新失败（网络或未认证）";
    refreshing.value = false;
  }
}

async function onLogout() {
  try { await logout(); } catch { /* 无论成败都回登录页 */ }
  location.href = "/login";
}

</script>

<template>
  <AppShell title="信息面板" subtitle="INFO PANEL" code="PORTAL" nav-label="主导航">
    <template #nav>
      <template v-for="sec in NAV" :key="sec.label">
        <span class="side-nav-label">{{ sec.label }}</span>
        <RouterLink
          v-for="it in sec.items"
          :key="it.href"
          :to="it.href"
          class="side-nav-item"
          :aria-current="isCurrent(it.href) ? 'page' : undefined"
        ><span class="nav-no">{{ it.no }}</span><span class="nav-name"><b>{{ it.name }}</b><small>{{ it.en }}</small></span></RouterLink>
      </template>
    </template>

    <template #foot>
      <div class="side-mod">
        <div class="side-mod-head"><span>SYNC</span><b>FS</b></div>
        <div class="side-mod-row">
          <span class="dot" :class="dotTone" aria-hidden="true"></span>
          <span id="sync-line" :class="line.cls">{{ line.text }}</span>
        </div>
        <div v-if="needsAttention" class="side-mod-row" style="margin-top: 4px">
          <RouterLink id="sync-queue-link" to="/quality" class="lnk t-micro">查看推送失败与冲突 →</RouterLink>
        </div>
        <div class="side-mod-row" style="margin-top: 6px">
          <button id="sync-refresh" type="button" class="btn ghost xs" :disabled="refreshing" @click="onRefresh">{{ refreshText }}</button>
        </div>
      </div>
      <div class="side-mod side-user">
        <div class="side-avatar" aria-hidden="true">{{ avatar }}</div>
        <div style="min-width: 0">
          <span id="who">{{ who }}</span>
          <small>OPERATOR</small>
        </div>
        <button v-if="store.me || store.bootWho" id="logout-btn" type="button" class="btn ghost xs" @click="onLogout">退出</button>
      </div>
      <a class="side-portal-link" href="/debug/" @click="onHandoffClick($event, '/debug/')"><span>调试台</span><small>DEBUG →</small></a>
    </template>

    <TopBar :crumb="crumb" switch-href="/debug/" switch-text="调试台 →">
      <span class="topbar-chip is-optional">
        <span class="dot" :class="line.cls.includes('is-failed') ? 'danger' : store.sync.has_snapshot ? 'ok' : 'warn'" aria-hidden="true"></span>飞书同步
      </span>
      <span class="topbar-chip is-optional t-micro">局域网</span>
    </TopBar>

    <main id="p-main" class="app-content">
      <RouterView />
    </main>
    <WriteForm
      v-if="store.form"
      :key="store.form.kind + (store.form.recordId || '')"
      :kind="store.form.kind"
      :preset="store.form.preset"
      :record-id="store.form.recordId"
    />
  </AppShell>
  <ToastStack />
</template>
