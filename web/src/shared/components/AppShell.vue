<script setup lang="ts">
/* 应用外壳（门户与调试台共用）：侧栏导轨 + 主区。
 * 交互契约移植自旧版 static/ui/sidebar.js，行为保持不变：
 * - 桌面：收纳开关，偏好记在 localStorage「foray.nav.collapsed」，状态写到 html[data-nav]；
 * - 窄屏（≤900px）：抽屉导航，独立于桌面偏好；打开时 html[data-mobile-nav]、遮罩可点关闭、
 *   Esc 关闭并把焦点还给开关、Tab 在抽屉内循环、点导航项后自动关闭；
 * - 抽屉关闭时侧栏 inert，打开时主区 inert（读屏与键盘不会落到被遮住的部分）。 */
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import mark from "../assets/foray-mark-r1.svg";

defineProps<{
  title: string;          // 品牌区主标题，如「信息面板」
  subtitle: string;       // 英文副标题，如「INFO PANEL」
  code: string;           // 铭牌，如「PORTAL」
  navLabel: string;       // 导航的无障碍名称
}>();

const KEY = "foray.nav.collapsed";
const mq = window.matchMedia("(max-width: 900px)");
const reduced = (typeof window.matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches);

const sidebar = ref<HTMLElement | null>(null);
const main = ref<HTMLElement | null>(null);
const toggle = ref<HTMLButtonElement | null>(null);

const mobile = ref(mq.matches);
const mobileOpen = ref(false);
const collapsed = ref(false);
const animating = ref(false);

let stored: string | null = null;
try { stored = localStorage.getItem(KEY); } catch { stored = null; }

function applyCollapsed(v: boolean) {
  collapsed.value = v;
  document.documentElement.setAttribute("data-nav", v ? "collapsed" : "expanded");
  /* 收纳后标签隐藏 → 用原生 title 保留可读性 */
  sidebar.value?.querySelectorAll<HTMLElement>(".side-nav-item").forEach((it) => {
    if (v) {
      const label = it.querySelector(".nav-name b");
      if (label && !it.title) it.title = (label.textContent || "").trim();
    } else if (it.title) {
      it.removeAttribute("title");
    }
  });
}

function setMobileOpen(open: boolean, restoreFocus: boolean) {
  mobileOpen.value = mobile.value && open;
  document.documentElement.toggleAttribute("data-mobile-nav", mobileOpen.value);
  if (mobileOpen.value) {
    nextTick(() => sidebar.value?.querySelector<HTMLElement>(".side-nav-item")?.focus());
  } else if (restoreFocus) {
    toggle.value?.focus();
  }
}

function syncViewport() {
  mobile.value = mq.matches;
  const hadFocus = !!sidebar.value && sidebar.value.contains(document.activeElement);
  setMobileOpen(false, hadFocus && mobile.value);
  applyCollapsed(!mobile.value && stored === "1");
}

let animTimer = 0;
function onToggle() {
  if (mobile.value) {
    setMobileOpen(!mobileOpen.value, true);
    return;
  }
  const next = !collapsed.value;
  applyCollapsed(next);
  stored = next ? "1" : "0";
  try { localStorage.setItem(KEY, stored); } catch { /* 隐私模式：本次会话内有效 */ }
  if (!reduced) {
    animating.value = true;
    window.clearTimeout(animTimer);
    animTimer = window.setTimeout(() => { animating.value = false; }, 500);
  }
}

function onSidebarClick(ev: MouseEvent) {
  const t = ev.target as HTMLElement | null;
  if (mobile.value && t && t.closest("a, .side-nav-item")) setMobileOpen(false, true);
}

function onKeydown(ev: KeyboardEvent) {
  if (!mobileOpen.value || !sidebar.value) return;
  if (ev.key === "Escape") {
    ev.preventDefault();
    setMobileOpen(false, true);
  } else if (ev.key === "Tab") {
    const focusable = Array.from(sidebar.value.querySelectorAll<HTMLElement>(
      "a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex='0']"))
      .filter((el) => el.getClientRects().length && getComputedStyle(el).visibility !== "hidden");
    if (toggle.value) focusable.push(toggle.value);
    if (!focusable.length) return;
    const i = focusable.indexOf(document.activeElement as HTMLElement);
    ev.preventDefault();
    focusable[(i + (ev.shiftKey ? -1 : 1) + focusable.length) % focusable.length].focus();
  }
}

onMounted(() => {
  syncViewport();
  mq.addEventListener("change", syncViewport);
  document.addEventListener("keydown", onKeydown);
});
onBeforeUnmount(() => {
  mq.removeEventListener("change", syncViewport);
  document.removeEventListener("keydown", onKeydown);
  window.clearTimeout(animTimer);
});

/* 供子组件在需要时关闭抽屉（如软导航后） */
defineExpose({ closeMobile: () => setMobileOpen(false, false) });
</script>

<template>
  <div class="app-shell" :class="{ 'is-collapsed': collapsed, 'is-nav-animating': animating }">
    <aside
      id="app-sidebar"
      ref="sidebar"
      class="app-sidebar"
      :inert="mobile && !mobileOpen"
      @click="onSidebarClick"
    >
      <div class="side-brand">
        <img :src="mark" alt="FORAY">
        <div class="side-brand-text">
          <b>{{ title }}</b>
          <small>{{ subtitle }}</small>
          <div class="side-code" aria-hidden="true">
            <span class="side-code-tag">{{ code }}</span><i></i>
          </div>
        </div>
      </div>
      <nav class="side-nav" :aria-label="navLabel">
        <slot name="nav" />
      </nav>
      <div class="side-foot">
        <slot name="foot" />
      </div>
    </aside>

    <div ref="main" class="app-main" :inert="mobileOpen">
      <div class="signal-bar" aria-hidden="true"></div>
      <slot />
    </div>

    <button
      ref="toggle"
      type="button"
      class="side-toggle"
      aria-controls="app-sidebar"
      :aria-expanded="mobile ? String(mobileOpen) : String(!collapsed)"
      :aria-label="mobile ? (mobileOpen ? '关闭导航菜单' : '打开导航菜单') : '收起 / 展开侧边栏'"
      :title="mobile ? (mobileOpen ? '关闭导航菜单' : '打开导航菜单') : '收起 / 展开侧边栏'"
      @click="onToggle"
    ></button>
    <button
      type="button"
      class="side-backdrop"
      aria-label="关闭导航菜单"
      tabindex="-1"
      :hidden="!mobileOpen"
      @click="setMobileOpen(false, true)"
    ></button>
  </div>
</template>
