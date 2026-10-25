<script setup lang="ts">
/* 右侧通知框容器：每个应用的根组件挂一次（门户 App、登录类页面框架、调试台 App）。
 * 新通知追加在最下面、从右侧滑入；消失时先淡出右移、再收起占位，下面的通知平滑上移。 */
import { dismissToast, pauseToast, resumeToast, toasts, type ToastTone } from "../toast";
import { pad2 } from "../format";

const TAG: Record<ToastTone, string> = { info: "NOTICE", ok: "DONE", error: "ERROR" };

function clock(ms: number): string {
  const d = new Date(ms);
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
}
</script>

<template>
  <div id="toast" class="toast-stack" aria-live="polite">
    <TransitionGroup name="toast">
      <div v-for="t in toasts" :key="t.id" class="toast-slot">
        <div class="toast-wrap">
          <div
            class="toast"
            :class="'is-' + t.tone"
            :role="t.tone === 'error' ? 'alert' : 'status'"
            @mouseenter="pauseToast(t.id)"
            @mouseleave="resumeToast(t.id)"
            @focusin="pauseToast(t.id)"
            @focusout="resumeToast(t.id)"
          >
            <span class="toast-head">
              <span class="toast-tag">{{ TAG[t.tone] }}</span>
              <span class="toast-time">{{ clock(t.at) }}</span>
            </span>
            <p class="toast-text">{{ t.text }}</p>
            <button type="button" class="toast-close" aria-label="关闭通知" @click="dismissToast(t.id)">×</button>
            <i
              :key="t.round"
              class="toast-timer"
              :class="{ 'is-paused': t.paused }"
              :style="{ animationDuration: t.ttl + 'ms' }"
              aria-hidden="true"
            ></i>
          </div>
        </div>
      </div>
    </TransitionGroup>
  </div>
</template>
